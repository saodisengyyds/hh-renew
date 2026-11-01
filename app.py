#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HH 自动续期

登录：手机号 + 邮箱验证码
续期：到期前 2 天自动执行

环境变量
--------
必填
  HH_SITE               站点地址，如 https://example.com
  HH_PHONE              手机号
  IMAP_HOST             邮箱 IMAP，如 imap.gmail.com
  IMAP_PORT             默认 993
  EMAIL_USER            收件邮箱
  EMAIL_APP_PASSWORD    邮箱应用密码

可选
  MAIL_SUBJECT          验证码邮件的主题关键词，默认 "verification code"
  NODE_LINK             代理节点
  IS_PROXY / PROXY_SERVER
  TG_BOT_TOKEN / TG_CHAT_ID
  DUMP_PAGE=1           打印页面结构
  FORCE_RENEW=1         忽略到期日，强制续期
"""

import io
import os
import re
import sys
import json
import time
import base64
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone

import requests
from seleniumbase import SB

# ==================== 配置 ====================

SITE = (os.environ.get("HH_SITE") or "").strip().rstrip("/")
PHONE = (os.environ.get("HH_PHONE") or "").strip()

IMAP_HOST = os.environ.get("IMAP_HOST") or ""
IMAP_PORT = int(os.environ.get("IMAP_PORT") or "993")
EMAIL_USER = os.environ.get("EMAIL_USER") or ""
EMAIL_APP_PASSWORD = os.environ.get("EMAIL_APP_PASSWORD") or ""

MAIL_SUBJECT = os.environ.get("MAIL_SUBJECT") or "verification code"

TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""

DUMP_PAGE = os.environ.get("DUMP_PAGE", "0").lower() in ("1", "true", "yes")
FORCE_RENEW = os.environ.get("FORCE_RENEW", "0").lower() in ("1", "true", "yes")

LOGIN_PATH = "/login"
RENEW_PATH = "/vps-renew/"
SITE_HOST = re.sub(r"^https?://", "", SITE).split("/")[0] if SITE else ""

STATE_FILE = "hh_state.json"      # 记录到期日
RENEW_AHEAD_DAYS = 2              # 提前几天续期

CST = timezone(timedelta(hours=8))


def now_cst():
    return datetime.now(CST)


def login_url():
    return SITE + LOGIN_PATH


def renew_url():
    return SITE + RENEW_PATH


# ==================== Telegram ====================

def send_tg(status_icon: str, status_text: str, detail: str = ""):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        print("ℹ️  未配置 TG，跳过推送")
        return
    text = (
        f"🟣 HH 续期通知\n\n"
        f"{status_icon} {status_text}\n"
        f"⏱️ {now_cst().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    if detail:
        text += f"\n\n📄 {detail[:400]}"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text},
            timeout=15,
        )
        print("📩 TG 已发送" if r.status_code == 200
              else f"⚠️  TG 失败: {r.status_code}")
    except Exception as e:
        print(f"⚠️  TG 异常: {e}")


# ==================== 状态（到期日） ====================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    try:
        json.dump(state, open(STATE_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"💾 已保存状态: {state}")
    except Exception as e:
        print(f"⚠️  保存状态失败: {e}")


def parse_expiry(text: str):
    """从页面文本里解析到期日，返回 date 或 None"""
    if not text:
        return None

    # 优先匹配英文月份格式：November 4, 2026 或 04 Nov 2026
    months = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
        'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
        'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
        'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12
    }
    m_en = re.search(r'([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})', text)
    if m_en:
        mon_str, day_str, year_str = m_en.group(1).lower(), m_en.group(2), m_en.group(3)
        if mon_str in months:
            try:
                return datetime(int(year_str), months[mon_str], int(day_str)).date()
            except Exception:
                pass

    pats = [
        r"(?:expir\w*|renew\w*|到期|过期)[^\d]{0,30}(\d{4})[-/](\d{1,2})[-/](\d{1,2})",
        r"(?:expir\w*|renew\w*|到期|过期)[^\d]{0,30}(\d{1,2})[-/](\d{1,2})[-/](\d{4})",
        r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})",
    ]

    for i, pat in enumerate(pats):
        m = re.search(pat, text, re.IGNORECASE)
        if not m:
            continue
        try:
            if i == 1:
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            else:
                y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dt = datetime(y, mo, d).date()
            today = now_cst().date()
            if today <= dt <= today + timedelta(days=730):
                return dt
        except Exception:
            continue
    return None


# ==================== 邮箱验证码 ====================

def _decode_mime(s):
    if not s:
        return ""
    out = []
    for txt, enc in decode_header(s):
        if isinstance(txt, bytes):
            try:
                out.append(txt.decode(enc or "utf-8", errors="ignore"))
            except Exception:
                out.append(txt.decode("utf-8", errors="ignore"))
        else:
            out.append(txt)
    return "".join(out)


# 验证码是一长串 base64（约 60 字符），解码后形如 手机号:::nonce
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _extract_token(text: str):
    """从邮件正文里提取验证令牌"""
    if not text:
        return None

    # 去掉 HTML 标签和常见包裹符号
    clean = re.sub(r"<[^>]+>", " ", text)

    cands = _TOKEN_RE.findall(clean)
    best = None
    for c in cands:
        # 排除纯数字、纯字母的偶然长串
        if not re.search(r"[0-9]", c) or not re.search(r"[A-Za-z]", c):
            continue
        # 尝试解码，成功的更可信
        try:
            dec = base64.b64decode(c + "=" * (-len(c) % 4)).decode("utf-8")
            if re.search(r"\d{6,}", dec):
                return c
            if best is None:
                best = c
        except Exception:
            if best is None:
                best = c
    return best


def fetch_code(max_wait: int = 180, interval: int = 8):
    """轮询邮箱，取最新的验证码"""
    if not (IMAP_HOST and EMAIL_USER and EMAIL_APP_PASSWORD):
        print("❌ 未配置邮箱，无法取验证码")
        return None

    print(f"📬 轮询邮箱 {IMAP_HOST}，等待主题含「{MAIL_SUBJECT}」的邮件…")
    deadline = time.time() + max_wait
    seen = set()

    while time.time() < deadline:
        try:
            M = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            M.login(EMAIL_USER, EMAIL_APP_PASSWORD)
            M.select("INBOX")

            typ, data = M.search(None, "ALL")
            if typ != "OK":
                M.logout()
                time.sleep(interval)
                continue

            for uid in reversed(data[0].split()[-25:]):
                if uid in seen:
                    continue
                seen.add(uid)

                typ, msg_data = M.fetch(uid, "(RFC822)")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                subject = _decode_mime(msg.get("Subject", ""))

                # 按主题匹配
                if MAIL_SUBJECT and MAIL_SUBJECT.lower() not in subject.lower():
                    continue

                # 只认 15 分钟内的
                try:
                    dh = msg.get("Date")
                    if dh:
                        dt = parsedate_to_datetime(dh)
                        if dt.tzinfo:
                            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                        if datetime.utcnow() - dt > timedelta(minutes=15):
                            continue
                except Exception:
                    pass

                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() in ("text/plain", "text/html"):
                            try:
                                p = part.get_payload(decode=True)
                                if p:
                                    body += p.decode(
                                        part.get_content_charset() or "utf-8",
                                        errors="ignore")
                            except Exception:
                                pass
                else:
                    try:
                        p = msg.get_payload(decode=True)
                        if p:
                            body = p.decode(
                                msg.get_content_charset() or "utf-8",
                                errors="ignore")
                    except Exception:
                        pass

                token = _extract_token(body)
                if token:
                    print(f"✅ 取到验证码: {token[:16]}…（{len(token)} 字符）")
                    print(f"   主题: {subject}")
                    M.logout()
                    return token

            M.logout()
        except Exception as e:
            print(f"⚠️  邮箱异常: {e}")

        print(f"⏳ 暂无邮件，{interval}s 后重试…")
        time.sleep(interval)

    print("❌ 等待验证码超时")
    return None


# ==================== Cookie 存取 ====================

COOKIE_FILE = "hh_cookies.json"


def load_cookies():
    if not os.path.exists(COOKIE_FILE):
        return []
    try:
        data = json.load(open(COOKIE_FILE, encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️  cookie 文件读取失败: {e}")
        return []


def save_cookies(sb):
    """把当前浏览器的 cookie 存下来"""
    try:
        raw = sb.get_cookies()
    except Exception as e:
        print(f"⚠️  取 cookie 失败: {e}")
        return False

    host = SITE_HOST.lower()
    out = []
    for c in raw:
        dom = (c.get("domain") or "").lower()
        if host and host not in dom and dom not in ("", host):
            continue
        item = {
            "name": c.get("name"),
            "value": c.get("value", ""),
            "domain": c.get("domain") or host,
            "path": c.get("path") or "/",
        }
        if c.get("expiry"):
            item["expires"] = int(c["expiry"])
        out.append(item)

    if not out:
        print("⚠️  没有可保存的 cookie")
        return False

    try:
        json.dump(out, open(COOKIE_FILE, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"🍪 已保存 {len(out)} 个 cookie → {COOKIE_FILE}")
        return True
    except Exception as e:
        print(f"⚠️  保存 cookie 失败: {e}")
        return False


def apply_cookies(sb, cookies) -> int:
    """把 cookie 注入浏览器，返回成功数量"""
    if not cookies:
        return 0
    ok = 0
    for c in cookies:
        try:
            sb.add_cookie({
                "name": c["name"],
                "value": c.get("value", ""),
                "domain": c.get("domain") or SITE_HOST,
                "path": c.get("path") or "/",
            })
            ok += 1
        except Exception:
            pass
    print(f"🍪 注入 {ok}/{len(cookies)} 个 cookie")
    return ok


def cookies_look_valid(cookies) -> bool:
    """粗判：有没有会话类 cookie"""
    if not cookies:
        return False
    names = " ".join((c.get("name") or "").lower() for c in cookies)
    keys = ["session", "sess", "auth", "token", "login", "remember", "sid"]
    return any(k in names for k in keys)


# ==================== 页面工具 ====================

def dump_page(sb, tag):
    if not DUMP_PAGE:
        return
    print(f"\n===== DUMP [{tag}] =====")

    # 完整 HTML 落盘，供 artifact 下载后分析
    try:
        html = sb.get_page_source() or ""
        fn = f"dump-{tag}.html"
        with io.open(fn, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"  📄 已保存完整 HTML: {fn}（{len(html)} 字符）")
        # 顺便统计关键信息
        low = html.lower()
        print(f"  <input> 出现: {low.count('<input')} 次")
        print(f"  <form>  出现: {low.count('<form')} 次")
        print(f"  <iframe>出现: {low.count('<iframe')} 次")
        print(f"  'phone' 出现: {low.count('phone')} 次")
        print(f"  'turnstile' 出现: {low.count('turnstile')} 次")
        print(f"  'short ad' 出现: {low.count('short ad')} 次")
    except Exception as e:
        print(f"  ⚠️ 保存 HTML 失败: {e}")
    try:
        print(f"URL   : {sb.get_current_url()}")
        print(f"TITLE : {sb.get_title()}")
        info = sb.execute_script(r"""
        (function(){
          function d(el){return {tag:el.tagName.toLowerCase(),id:el.id||'',
            name:el.getAttribute('name')||'',type:el.getAttribute('type')||'',
            ph:el.getAttribute('placeholder')||'',
            text:(el.innerText||el.value||'').trim().slice(0,40)};}
          var o={inputs:[],buttons:[],links:[],forms:0,bodyLen:0,bodyText:''};
          document.querySelectorAll('input,textarea,select').forEach(function(e){
            if(o.inputs.length<25)o.inputs.push(d(e));});
          document.querySelectorAll('button,input[type=submit],a').forEach(function(e){
            if(o.buttons.length<25)o.buttons.push(d(e));});
          document.querySelectorAll('a[href]').forEach(function(e){
            if(o.links.length<20)o.links.push({t:(e.innerText||'').trim().slice(0,30),h:e.getAttribute('href')});});
          o.forms=document.querySelectorAll('form').length;
          var b=document.body;
          o.bodyLen=b?(b.innerText||'').length:0;
          o.bodyText=b?(b.innerText||'').replace(/[\s\u00a0]+/g,' ').slice(0,800):'';
          return o;})()
        """)

        if not isinstance(info, dict):
            print("  ⚠️  execute_script 返回", type(info).__name__, "，改用 Selenium 取值")
            try:
                print("  BODY  :", (sb.get_text("body") or "").replace("\n", " | ")[:1200])
                print("  SOURCE:", (sb.get_page_source() or "")[:2000])
            except Exception as e2:
                print("  取值也失败:", e2)
            print("===== END =====\n")
            return

        # 兜底：即使 info 拿到了，正文太短也打印源码
        if info.get("bodyLen", 0) < 60:
            print("  ⚠️  正文过短，打印源码片段：")
            try:
                print("  SOURCE:", (sb.get_page_source() or "")[:2000])
            except Exception:
                pass

        # 无论哪种情况，都单独抓一次表单区域
        try:
            forminfo = sb.execute_script(r"""
            (function(){
              var out={forms:[],inputs:[],iframes:[],scripts:[],bodyTail:''};
              document.querySelectorAll('form').forEach(function(f){
                out.forms.push({action:f.getAttribute('action')||'',
                                method:f.getAttribute('method')||'',
                                id:f.id||'',cls:f.className||'',
                                html:f.outerHTML.slice(0,600)});
              });
              document.querySelectorAll('input,textarea,select,button').forEach(function(e){
                out.inputs.push({tag:e.tagName.toLowerCase(),type:e.getAttribute('type')||'',
                  name:e.getAttribute('name')||'',id:e.id||'',
                  ph:e.getAttribute('placeholder')||'',
                  visible: e.offsetParent!==null});
              });
              document.querySelectorAll('iframe').forEach(function(f){
                out.iframes.push(f.src||'(no src)');
              });
              document.querySelectorAll('script[src]').forEach(function(sc){
                var s=sc.getAttribute('src')||'';
                if(/login|auth|main|app|index/i.test(s)) out.scripts.push(s);
              });
              var b=document.body;
              if(b) out.bodyTail=(b.innerText||'').slice(-600);
              return out;})()
            """)
            if isinstance(forminfo, dict):
                print("  FORMS 数:", len(forminfo.get("forms", [])))
                for f in forminfo.get("forms", []):
                    print("   FORM:", f)
                print("  INPUTS 数:", len(forminfo.get("inputs", [])))
                for i in forminfo.get("inputs", [])[:15]:
                    print("   IN  :", i)
                print("  IFRAMES:", forminfo.get("iframes", []))
                print("  SCRIPTS:", forminfo.get("scripts", []))
                print("  TAIL  :", forminfo.get("bodyTail", "")[:400])
        except Exception as e:
            print("  表单抓取失败:", e)

        print("FORMS :", info.get("forms"), "| bodyLen:", info.get("bodyLen"))
        for i in info.get("inputs", []):
            print("  IN ", i)
        for b in info.get("buttons", []):
            print("  BTN", b)
        for l in info.get("links", []):
            print("  A  ", l)
        print("TEXT  :", info.get("bodyText", "")[:700])
    except Exception as e:
        print("dump 失败:", e)
    print("===== END =====\n")


def _wait_ready(sb, timeout=45):
    """等到页面不是 Cloudflare 挑战页，且正文有内容"""
    print("⏳ 等待页面…")
    for i in range(timeout):
        try:
            title = (sb.get_title() or "").lower()
            body = (sb.get_text("body") or "").strip()
            blocked = ("just a moment" in title
                       or "checking your browser" in title
                       or "attention required" in title
                       or "请稍候" in title)
            if not blocked and len(body) > 60:
                print(f"✅ 已加载（{i+1}s）")
                return True
        except Exception:
            pass
        time.sleep(1)
    print("⚠️  可能未完全加载")
    return False


AD_WALL_KWS = [
    "view a short ad", "short ad", "site-wide access",
    "watch ad", "sponsored", "continue to site",
    "看广告", "广告", "访问权限",
]


def _body_text(sb):
    try:
        return (sb.get_text("body") or "")
    except Exception:
        return ""


def _has_login_form(sb):
    """页面上有没有可用的输入框（优先 WebDriver，JS 在这个站不可靠）"""
    # 1) WebDriver 直接找
    try:
        for sel in ["input", "textarea"]:
            for el in sb.find_elements(sel):
                try:
                    if not el.is_displayed():
                        continue
                    t = (el.get_attribute("type") or "").lower()
                    if t in ("hidden",):
                        continue
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    # 2) JS 兜底
    try:
        r = sb.execute_script("""
        (function(){
          var els=document.querySelectorAll('input,textarea');
          for(var i=0;i<els.length;i++){
            if(els[i].offsetParent!==null) return true;
          }
          return false;})()
        """)
        return bool(r)
    except Exception:
        pass

    return False


def _count_inputs(sb) -> int:
    """统计页面上 input 数量（诊断用）"""
    try:
        return len(sb.find_elements("input"))
    except Exception:
        return -1


def _click_el(sb, el) -> bool:
    """安全点击 WebElement（避免 SeleniumBase 的 JSON 序列化问题）"""
    # 1) 原生 click
    try:
        el.click()
        return True
    except Exception:
        pass
    # 2) 用 driver 的 JS 点击（原生 WebElement，不经过 SeleniumBase 包装）
    try:
        sb.driver.execute_script(
            "arguments[0].scrollIntoView({block:'center'});", el)
        sb.driver.execute_script("arguments[0].click();", el)
        return True
    except Exception:
        pass
    # 3) 坐标点击
    try:
        loc = el.location
        size = el.size
        x = int(loc["x"] + size["width"] / 2)
        y = int(loc["y"] + size["height"] / 2)
        sb.driver.execute_script(f"window.scrollTo(0, {max(0, y - 300)});")
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(sb.driver).move_by_offset(x, y).click().perform()
        return True
    except Exception:
        return False


def _pass_ad_wall(sb, rounds: int = 3) -> bool:
    """
    广告墙是 Google Funding Choices（fc-dialog 结构）：
      div.fc-dialog-overlay
        button.fc-rewarded-ad-button  ← "View a short ad"
    """
    any_clicked = False

    for r in range(rounds):
        # 判断有没有广告墙
        has_wall = False
        try:
            has_wall = bool(sb.find_elements("css selector", ".fc-dialog-overlay, .fc-message-root"))
        except Exception:
            pass

        if not has_wall:
            try:
                txt = (_body_text(sb) or "").lower()
                has_wall = "short ad" in txt or "site-wide access" in txt
            except Exception:
                pass

        if not has_wall:
            return any_clicked

        print(f"📺 广告墙（第 {r+1} 轮），尝试关闭…")
        clicked = False

        # 1) 优先点 Dismiss（fc-cta-close）—— 不用看广告
        try:
            closes = sb.find_elements(
                "css selector",
                ".fc-cta-button.fc-cta-close, button[aria-label='Dismiss'], .fc-dialog-overlay .fc-cta-close")
            if closes:
                if _click_el(sb, closes[0]):
                    clicked = True
                    print("  🖱️  点击 Dismiss")
        except Exception as e:
            print(f"  ⚠️  Dismiss 失败: {str(e)[:70]}")

        # 2) Dismiss 不成就点「View a short ad」
        if not clicked:
            try:
                btns = sb.find_elements("css selector", ".fc-rewarded-ad-button")
                if _click_el(sb, btns[0]):
                    clicked = True
                    print("  🖱️  点击 View a short ad")
            except Exception as e:
                print(f"  ⚠️  广告按钮失败: {str(e)[:70]}")

        # 3) JS 兜底
        if not clicked:
            try:
                clicked = bool(sb.execute_script(r"""
                (function(){
                  var c=document.querySelector('.fc-cta-close,button[aria-label="Dismiss"]');
                  if(c){ c.click(); return true; }
                  var b=document.querySelector('.fc-rewarded-ad-button');
                  if(b){ b.click(); return true; }
                  return false;})()
                """))
                if clicked:
                    print("  🖱️  JS 点击")
            except Exception:
                pass

        if clicked:
            any_clicked = True
            print("  ⏳ 等待广告流程…")
            # 广告可能开新标签
            try:
                handles = sb.driver.window_handles
                if len(handles) > 1:
                    main = handles[0]
                    sb.driver.switch_to.window(main)
                    for h in handles[1:]:
                        try:
                            sb.driver.switch_to.window(h)
                            sb.driver.close()
                        except Exception:
                            pass
                    sb.driver.switch_to.window(main)
                    print("  🔀 已关闭广告标签页")
            except Exception:
                pass

            time.sleep(12)
            # 等广告墙消失
            for _ in range(25):
                still = False
                try:
                    still = bool(sb.find_elements(
                        "css selector", ".fc-dialog-overlay, .fc-message-root"))
                except Exception:
                    pass
                if not still:
                    print("  ✅ 广告墙已消失")
                    return True
                time.sleep(1)
        else:
            print("  ⚠️  没找到可点元素")
            time.sleep(3)

    return any_clicked


def _fill(sb, selectors, value, label):
    # 1) 按选择器
    for sel in selectors:
        try:
            el = sb.find_element(sel, timeout=3)
            if not el:
                continue
            if not el.is_displayed():
                continue
            try:
                el.clear()
            except Exception:
                pass
            el.click()
            time.sleep(0.2)
            el.send_keys(value)
            print(f"✏️  {label} ({sel})")
            return True
        except Exception:
            continue

    # 2) 兜底：遍历所有可见输入框
    print(f"  ⚠️  选择器都没命中，尝试遍历输入框填 {label}")
    try:
        for sel in ["input", "textarea"]:
            for el in sb.find_elements(sel):
                try:
                    if not (el.is_displayed() and el.is_enabled()):
                        continue
                    t = (el.get_attribute("type") or "").lower()
                    if t in ("hidden", "submit", "button", "checkbox", "radio"):
                        continue
                    el.clear()
                    el.click()
                    time.sleep(0.2)
                    el.send_keys(value)
                    print(f"✏️  {label} (兜底: {sel}, name={el.get_attribute('name')})")
                    return True
                except Exception:
                    continue
    except Exception:
        pass

    print(f"❌ 找不到 {label}")
    return False


def _is_logged_in(sb):
    try:
        url = (sb.get_current_url() or "").lower()
        if "/login" in url:
            return False
        body = (sb.get_text("body") or "").lower()
        if re.search(r"logout|退出|sign out", body):
            return True
        # 注意：续期表单对游客也会渲染，不能用表单存在判断登录态
        # （2026-11-01 实测：cookie 已死时该判断误报有效，提交后被踢回 /login）
        return False
    except Exception:
        return False


# ==================== 登录 ====================

def login(sb, tg_wait: int = 300) -> bool:
    """
    hax 的登录是 Telegram OAuth Widget（不是手机号表单）。

    流程：
      1. 打开 /login，点掉广告墙
      2. 找到 telegram-login iframe，点进去
      3. 弹出 Telegram 授权页 —— 需要手机 App 确认
      4. 推 TG 通知提醒你点确认
      5. 轮询等待回跳到 hax.co.id 且已登录
      6. 成功后保存 cookie
    """
    print(f"\n🌐 打开登录页: {login_url()}")
    try:
        sb.uc_open_with_reconnect(login_url(), reconnect_time=6)
    except Exception:
        sb.open(login_url())
    time.sleep(5)

    _wait_ready(sb, 40)
    _step(sb, "01-open-login")

    _pass_ad_wall(sb, rounds=3)
    _step(sb, "02-after-adwall")

    if _is_logged_in(sb):
        print("✅ 已有登录态")
        save_cookies(sb)
        return True

    # ---- 情况 A：有手机号表单 ----
    n_inputs = _count_inputs(sb)
    print(f"🔎 页面 input 数量: {n_inputs}")
    _step(sb, f"03-inputs-{n_inputs}")

    if n_inputs > 0:
        print("→ 走手机号表单流程")
        return _login_by_phone(sb, tg_wait)

    # ---- 情况 B：没有 input，先尝试点 Login 链接 ----
    print("→ 没有输入框，点击 Login 打开弹窗…")
    _step(sb, "04-before-click-login")
    if _click_login_entry(sb):
        _step(sb, "05-after-click-login")

        # 等弹窗里的表单出现
        print("  ⏳ 等弹窗加载…")
        for i in range(30):
            time.sleep(1)
            n = _count_inputs(sb)
            if n > 0:
                print(f"  ✅ 弹窗表单已出现（{i+1}s，{n} 个输入框）")
                _step(sb, f"05b-modal-{n}-inputs")
                return _login_by_phone(sb, tg_wait)
            if i % 5 == 0:
                _pass_ad_wall(sb, rounds=1)

        print("  ⚠️  点了 Login 仍无表单，检查弹窗结构")
        _step(sb, "05c-modal-no-input")
        dump_page(sb, "modal-no-input")

    # ---- 情况 C：Telegram widget ----
    print("→ 尝试 Telegram widget 流程")
    _step(sb, "06-telegram-fallback")
    return _login_by_telegram(sb, tg_wait)


def _click_login_entry(sb) -> bool:
    """点页面上的 Login 入口（导航栏链接或按钮）"""
    before_url = ""
    try:
        before_url = sb.get_current_url() or ""
    except Exception:
        pass
    print(f"  📍 点击前 URL: {before_url}")

    # 1) 找 href 含 login 的链接
    try:
        for el in sb.find_elements("css selector", "a[href*='login'], a[href*='Login']"):
            try:
                if el.is_displayed():
                    href = el.get_attribute("href") or ""
                    print(f"  🖱️  点击链接: {href}")
                    el.click()
                    time.sleep(4)
                    after = ""
                    try:
                        after = sb.get_current_url() or ""
                    except Exception:
                        pass
                    print(f"  📍 点击后 URL: {after}")
                    if after != before_url:
                        print("  ✅ 已跳转")
                    return True
            except Exception:
                continue
    except Exception:
        pass

    # 2) 按文本找
    try:
        for el in sb.find_elements("css selector", "a, button"):
            try:
                t = (el.text or "").strip().lower()
                if t in ("login", "log in", "sign in", "登录"):
                    if el.is_displayed():
                        el.click()
                        print(f"  🖱️  点击「{el.text.strip()}」")
                        time.sleep(4)
                        return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def _step(sb, name: str):
    """记录步骤：打印 + 落盘 + 截图，便于卡住时定位"""
    print(f"\n===== STEP: {name} =====")
    try:
        with io.open("steps.log", "a", encoding="utf-8") as f:
            f.write(f"{now_cst().strftime('%H:%M:%S')} {name} "
                    f"url={sb.get_current_url()}\n")
    except Exception:
        pass
    try:
        sb.save_screenshot(f"step-{name.replace('/', '_')[:40]}.png")
    except Exception:
        pass
    try:
        with io.open(f"step-{name.replace('/', '_')[:40]}.html", "w",
                     encoding="utf-8") as f:
            f.write(sb.get_page_source() or "")
    except Exception:
        pass


def _click_submit(sb):
    """点提交按钮：选择器 → 文本 → 回车"""
    try:
        for sel in ['button[name="submit_button"]', 'button[type="submit"]',
                    'input[type="submit"]']:
            try:
                btn = sb.find_element(sel, timeout=2)
                if btn and btn.is_displayed():
                    btn.click()
                    print(f"  🖱️  {sel}")
                    return True
            except Exception:
                continue

        ok = sb.execute_script(r"""
        (function(){
          var bs=document.querySelectorAll('button,input[type=submit],a.btn');
          for(var i=0;i<bs.length;i++){
            var t=(bs[i].innerText||bs[i].value||'').toLowerCase();
            if(/send|submit|verify|login|continue|next|confirm|验证码|发送|下一步|登录|确认/.test(t)){
              bs[i].click(); return true;
            }
          }
          return false;})()
        """)
        if ok:
            print("  🖱️  按文本点击")
            return True
    except Exception:
        pass

    try:
        for sel in ['input[type="text"]', 'input[type="tel"]',
                    'input[type="password"]', 'input[type="email"]']:
            try:
                el = sb.find_element(sel, timeout=1)
                if el and el.is_displayed():
                    el.send_keys("\n")
                    print("  ⌨️  回车提交")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _wait_logged_in(sb, timeout: int, label: str = "登录") -> bool:
    """轮询等待登录完成，成功后保存 cookie"""
    print(f"\n⏳ 等待{label}完成（最多 {timeout}s）…")
    send_tg("📲", "请在手机上确认登录",
            f"hax 登录需要 Telegram 确认，请在 {timeout} 秒内点击")

    deadline = time.time() + timeout
    last_report = 0

    while time.time() < deadline:
        time.sleep(3)

        # 多窗口情况（TG 授权可能开新标签）
        try:
            handles = sb.driver.window_handles
            if len(handles) > 1:
                cur = sb.driver.current_window_handle
                for h in handles:
                    if h != cur:
                        sb.driver.switch_to.window(h)
                        break
        except Exception:
            pass

        if _is_logged_in(sb):
            print(f"✅ {label}成功！")
            try:
                for h in sb.driver.window_handles:
                    sb.driver.switch_to.window(h)
                    if "hax.co.id" in (sb.get_current_url() or ""):
                        break
            except Exception:
                pass
            save_cookies(sb)
            return True

        remain = int(deadline - time.time())
        if remain // 30 != last_report:
            last_report = remain // 30
            cur = ""
            try:
                cur = sb.get_current_url() or ""
            except Exception:
                pass
            print(f"   等待中… 剩余 {remain}s  URL={cur[:70]}")

    print(f"❌ {label}超时")
    dump_page(sb, "login-timeout")
    send_tg("⏰", f"{label}超时", "没有检测到登录成功")
    return False


def _login_by_phone(sb, tg_wait: int) -> bool:
    """手机号 + 邮箱验证码登录"""
    print("\n📱 手机号登录流程")

    # 弹窗内的手机号输入框：先精确找 modal 里的，再全局找
    phone_sels = [
        # Bootstrap modal 常见容器
        '.modal.show input[name="phone"]',
        '.modal.show input[type="tel"]',
        '.modal.show input[type="text"]',
        '.modal-content input[type="tel"]',
        '.modal-content input[type="text"]',
        '.modal-dialog input[type="tel"]',
        '#loginModal input[type="tel"]',
        # 通用
        'input[name="phone"]', 'input[id="phone"]',
        'input[type="tel"]', 'input[name="mobile"]',
        'input[placeholder*="phone"]', 'input[placeholder*="手机"]',
        'input[name="username"]', 'input[id="text"]',
        'input[type="text"]', 'input[type="email"]',
    ]
    if not _fill(sb, phone_sels, PHONE, "手机号"):
        dump_page(sb, "no-phone-input")
        return False
    time.sleep(1)

    print("🖱️  提交手机号…")
    _click_submit(sb)
    time.sleep(6)
    _wait_ready(sb, 20)
    _pass_ad_wall(sb, rounds=2)

    # 验证码
    token = fetch_code(max_wait=180, interval=8)
    if not token:
        send_tg("❌", "取不到验证码", "检查 IMAP 配置或邮件主题关键词")
        return False

    code_sels = [
        'input[name="code"]', 'input[id="code"]',
        'input[name="token"]', 'input[id="token"]',
        'input[name*="verify"]', 'input[id*="verify"]',
        'input[name*="otp"]', 'input[id*="otp"]',
        'input[type="text"]',
    ]
    if not _fill(sb, code_sels, token, "验证码"):
        dump_page(sb, "no-code-input")
        return False
    time.sleep(1)

    print("🖱️  提交验证码…")
    _click_submit(sb)
    time.sleep(5)

    return _wait_logged_in(sb, tg_wait, "登录")


def _login_by_telegram(sb, tg_wait: int) -> bool:
    """
    Telegram OAuth Widget 登录。

    实际流程（用户确认）：
      1. 点 widget → 弹出小窗口
      2. 窗口里要求输入手机号
      3. 填手机号 → Telegram 发确认到你手机
      4. 手机点确认 → 回跳 → 登录完成
    """
    print("\n📲 Telegram Widget 登录流程")

    before_handles = set()
    try:
        before_handles = set(sb.driver.window_handles)
    except Exception:
        pass

    # ---- 找 widget ----
    print("🔎 查找 Telegram 登录按钮…")
    tg_frame = None
    try:
        for ifr in sb.find_elements("iframe"):
            try:
                src = ifr.get_attribute("src") or ""
                fid = ifr.get_attribute("id") or ""
                if "oauth.telegram.org" in src or "telegram-login" in fid:
                    tg_frame = ifr
                    print(f"  ✅ widget: id={fid}")
                    break
            except Exception:
                continue
    except Exception as e:
        print(f"  ⚠️  {e}")

    if not tg_frame:
        print("❌ 没找到 Telegram widget")
        dump_page(sb, "no-tg-widget")
        send_tg("❌", "找不到 Telegram 登录按钮", "页面结构可能变了")
        return False

    # ---- 直接打开 Telegram 官方授权页（跳过 iframe，防止浏览器崩溃） ----
    print("🌐 直接导航至 Telegram 授权页…")
    # Telegram OAuth 的 bot_id 必须是纯数字 ID (LoginHaxBot 的数字 ID 是 5938743585)
    auth_url = f"https://oauth.telegram.org/auth?bot_id=5938743585&origin={SITE}&return_to={SITE}/login"

    print(f"  🔗 目标 URL: {auth_url}")
    try:
        sb.open(auth_url)
    except Exception as e:
        print(f"⚠️  打开授权页异常: {e}")

    time.sleep(4)
    _step(sb, "tg-oauth-page")

    # ---- 找手机号输入并用 JS 直接赋值触发 ----
    print("🔎 处理手机号输入与提交…")
    phone_filled = False
    clean_phone = re.sub(r'[^0-9+]', '', PHONE)

    # 解析成 (国家代码, 本地号码)
    # 规则：
    #   +8613800138000   → 86 / 13800138000
    #   8613800138000    → 86 / 13800138000
    #   13800138000      → 86 / 13800138000   （11 位、1 开头）
    #   其他             → 原样，不猜
    country_code = "86"
    number = clean_phone

    if clean_phone.startswith("+"):
        rest = clean_phone[1:]
        if rest.startswith("86") and len(rest) >= 13:
            country_code, number = "86", rest[2:]
        elif rest.startswith("1") and len(rest) == 11:
            country_code, number = "1", rest[1:]
        else:
            # 其他国家的 +xx，交给页面自己处理
            country_code, number = "", rest
    elif clean_phone.startswith("86") and len(clean_phone) == 13:
        country_code, number = "86", clean_phone[2:]
    elif re.fullmatch(r'1[3-9]\d{9}', clean_phone):
        # 11 位且符合国内号段（1[3-9] 开头）才是国内手机号
        country_code, number = "86", clean_phone
    elif clean_phone.startswith("1") and len(clean_phone) == 11:
        # 同样是 11 位 1 开头、但不符合国内号段 → 北美号码（国家码 1）
        # （旧逻辑一律按 +86 解析，美号永远收不到 Telegram 确认）
        country_code, number = "1", clean_phone[1:]
    else:
        country_code, number = "86", clean_phone

    print(f"  📱 国家代码: +{country_code}, 号码: {number}")
    # 号码合理性检查
    if country_code == "86" and not re.fullmatch(r'1[3-9]\d{9}', number):
        print(f"  ⚠️  号码 {number} 不像有效的国内手机号（应为 1[3-9] 开头 11 位）")
        send_tg("⚠️", "手机号可能有问题",
                f"解析结果 +{country_code} {number}\n"
                f"请检查 HH_PHONE 是否填对")

    # 用 JS 强力填入并直接调用 Telegram 官方的 requestConfirmation(event)
    js_code = f"""
    (function(){{
      var codeEl = document.getElementById('login-phone-code');
      var phoneEl = document.getElementById('login-phone');
      if (codeEl && phoneEl) {{
        codeEl.value = '{country_code}';
        phoneEl.value = '{number}';
        codeEl.dispatchEvent(new Event('input', {{bubbles: true}}));
        codeEl.dispatchEvent(new Event('change', {{bubbles: true}}));
        phoneEl.dispatchEvent(new Event('input', {{bubbles: true}}));
        phoneEl.dispatchEvent(new Event('change', {{bubbles: true}}));
        
        // 直接触发官方提交函数
        if (typeof requestConfirmation === 'function') {{
          requestConfirmation(null);
          return 'submitted_by_fn';
        }}
        var form = document.getElementById('send-form');
        if (form) {{
          form.dispatchEvent(new Event('submit', {{bubbles: true, cancelable: true}}));
          return 'submitted_by_form';
        }}
        return 'filled';
      }}
      return false;
    }})();
    """

    # 先用原生 send_keys（能触发页面自己的 input 监听，比赋 value 更真）
    for attempt in range(8):
        try:
            code_el = sb.find_element('#login-phone-code', timeout=2)
            phone_el = sb.find_element('#login-phone', timeout=2)
            if code_el and phone_el and code_el.is_displayed():
                code_el.clear()
                code_el.send_keys(country_code)
                time.sleep(0.3)
                phone_el.clear()
                phone_el.send_keys(number)
                time.sleep(0.8)
                print(f"  ✅ 原生填入 +{country_code} {number}")

                # 提交
                clicked = False
                for sel in ['button#check-button', 'button.btn-primary',
                            'button[type="submit"]']:
                    try:
                        b = sb.find_element(sel, timeout=2)
                        if b and b.is_displayed():
                            _click_el(sb, b)
                            print(f"  🖱️  点击提交: {sel}")
                            clicked = True
                            break
                    except Exception:
                        continue
                if not clicked:
                    try:
                        phone_el.send_keys("\n")
                        print("  ⌨️  回车提交")
                    except Exception:
                        pass

                phone_filled = True
                break
        except Exception:
            pass
        time.sleep(1)

    # 原生不行再走 JS
    if not phone_filled:
        for attempt in range(10):
            try:
                ok = sb.execute_script(js_code)
                if ok:
                    print(f"  ✅ JS 填入并提交: {ok}")
                    phone_filled = True
                    break
            except Exception as e:
                print(f"  ⚠️  JS 尝试 {attempt+1}: {str(e)[:80]}")
            time.sleep(1)

    if not phone_filled:
        phone_sels = [
            '#login-phone',
            'input[name="phone"]', 'input[id="phone"]',
            'input[type="tel"]',
        ]
        if _fill(sb, phone_sels, number, "手机号"):
            phone_filled = True
            time.sleep(1)
            _click_submit(sb)

    time.sleep(4)
    _step(sb, "tg-phone-submitted")

    # ---- 已提交手机号，到此为止 ----
    print("\n" + "=" * 52)
    print("   手机号已提交，Telegram 消息已发送")
    print("=" * 52)
    send_tg(
        "📲",
        "已提交手机号",
        f"解析结果 +{country_code} {number}\n"
        f"Telegram 已发送确认消息，请在手机上点击确认",
    )

    _step(sb, "tg-phone-sent")
    print(f"⏳ 等待你在 Telegram 里点确认（最多 {tg_wait} 秒）…")
    deadline = time.time() + tg_wait
    confirmed = False
    while time.time() < deadline:
        time.sleep(5)
        try:
            cur = sb.get_current_url() or ""
        except Exception:
            cur = ""
        if SITE and SITE in cur:
            # 已回跳站点：会话可能刚种下，去续期页验登录态
            time.sleep(3)
            try:
                sb.open(renew_url())
                time.sleep(4)
            except Exception:
                pass
            if _is_logged_in(sb):
                confirmed = True
            break
        # 还在授权页：出现已确认提示时回登录页再验一次，验不到继续等
        try:
            t = (sb.get_text("body") or "").lower()
        except Exception:
            t = ""
        if "confirmed" in t or "已确认" in t:
            try:
                sb.open(login_url())
                time.sleep(4)
                if _is_logged_in(sb):
                    confirmed = True
                    break
            except Exception:
                pass

    if confirmed:
        print("✅ Telegram 已确认，登录完成")
        save_cookies(sb)
        return True

    print("❌ 等待 Telegram 确认超时")
    send_tg("❌", "登录超时", "未等到 Telegram 确认，请重试")
    return False


# ==================== 续期 ====================

def renew(sb):
    print("\n" + "#" * 26)
    print("   续期")
    print("#" * 26)

    print(f"\n🖥️  {renew_url()}")
    try:
        sb.uc_open_with_reconnect(renew_url(), reconnect_time=6)
    except Exception:
        sb.open(renew_url())
    time.sleep(5)

    _wait_ready(sb, 40)
    _pass_ad_wall(sb)

    if "/login" in (sb.get_current_url() or "").lower() or not _is_logged_in(sb):
        print("ℹ️  未登录（或会话已失效），先登录")
        if not login(sb):
            return False, None
        sb.open(renew_url())
        time.sleep(5)
        _wait_ready(sb, 40)

    dump_page(sb, "renew")

    body = sb.get_text("body") or ""
    low = body.lower()

    if "your vps has been renewed until" in low:
        print("🎉 页面显示已成功续期！")
        d = parse_expiry(body)
        send_tg("✅", f"VPS 当前已续期\n📅 到期日: {d}", body[:300])
        return True, d

    if re.search(r"can'?t renew|cannot renew|not (yet )?time|无法续期|未到|too early", low):
        print("⏳ 还没到续期时间")
        d = parse_expiry(body)
        send_tg("⏳", "未到续期时间", body[:300])
        return True, d

    # 表单存在性检查用原生（该页 execute_script 常返回 None）
    has_form = False
    try:
        el = sb.find_element('input[name="web_address"]', timeout=5)
        has_form = bool(el and el.is_displayed())
    except Exception:
        try:
            el = sb.find_element('#web_address', timeout=3)
            has_form = bool(el and el.is_displayed())
        except Exception:
            has_form = False

    if not has_form:
        print("❌ 找不到续期表单")
        _step(sb, "renew-no-form")
        send_tg("❌", "找不到续期表单", body[:300])
        return False, None
    print("  ✅ 续期表单已就位")

    # 网站地址（旧版先点输入框再 send_keys，点的时候常被广告遮罩拦截、
    # 整步静默失败；这里以 send_keys 为主，并用 get_property 验证真值）
    print("✏️  填网站地址…")
    addr_ok = False
    for attempt in range(3):
        try:
            el = sb.find_element('input[name="web_address"]', timeout=3)
            if el:
                try:
                    sb.driver.execute_script(
                        "arguments[0].scrollIntoView({block:'center'});", el)
                except Exception:
                    pass
                time.sleep(0.4)
                try:
                    el.clear()
                except Exception:
                    pass
                try:
                    el.send_keys(SITE_HOST)
                except Exception:
                    from selenium.webdriver.common.action_chains import ActionChains
                    ActionChains(sb.driver).move_to_element(el).click().send_keys(SITE_HOST).perform()
                time.sleep(0.3)
                cur = el.get_property("value") or ""
                if cur == SITE_HOST:
                    addr_ok = True
                    print(f"  ✅ 已填 {SITE_HOST}（第 {attempt+1} 次）")
                    break
                print(f"  ⚠️  第 {attempt+1} 次填入未生效，现值: {cur!r}")
        except Exception as e:
            print(f"  ⚠️  填地址异常: {str(e)[:60]}")
        _pass_ad_wall(sb)
    if not addr_ok:
        try:
            sb.driver.execute_script(
                "var e=document.querySelector('#web_address');"
                "if(e){e.focus();e.value='" + SITE_HOST + "';"
                "e.dispatchEvent(new Event('input',{bubbles:true}));"
                "e.dispatchEvent(new Event('change',{bubbles:true}));}")
            el = sb.find_element('input[name="web_address"]', timeout=3)
            addr_ok = bool(el and (el.get_property("value") or "") == SITE_HOST)
            print(f"  {'✅' if addr_ok else '⚠️'} JS 兜底后验证: {addr_ok}")
        except Exception:
            pass
    time.sleep(0.5)

    # 协议（点 label 或坐标点复选框，用 get_property('checked') 验证）
    print("☑️  勾协议…")
    checked = False
    for attempt in range(3):
        try:
            ag = sb.find_element('input[name="agreement"]', timeout=3)
            if ag and (ag.get_property("checked") or ag.is_selected()):
                checked = True
                break
            clicked = False
            try:
                lab = sb.find_element('label.form-check-label', timeout=2)
                if lab:
                    lab.click()
                    clicked = True
            except Exception:
                pass
            if not clicked and ag:
                from selenium.webdriver.common.action_chains import ActionChains
                ActionChains(sb.driver).move_to_element(ag).click().perform()
            time.sleep(0.4)
            ag = sb.find_element('input[name="agreement"]', timeout=3)
            checked = bool(ag and (ag.get_property("checked") or ag.is_selected()))
            if checked:
                break
        except Exception as e:
            print(f"  ⚠️  勾协议异常: {str(e)[:60]}")
        _pass_ad_wall(sb)
    print(f"  {'✅' if checked else '⚠️'} 协议勾选状态: {checked}")
    time.sleep(0.5)

    # 验证码（如果页面要求）—— 原生查找
    need_code = None
    for sel in ['input[name*="code"]', 'input[id*="code"]',
                'input[name*="otp"]', 'input[id*="otp"]',
                'input[name*="verify"]', 'input[id*="verify"]']:
        try:
            el = sb.find_element(sel, timeout=1)
            if el and el.is_displayed():
                need_code = sel
                break
        except Exception:
            continue

    if need_code:
        print(f"🔐 需要验证码: {need_code}")
        token = fetch_code(max_wait=180, interval=8)
        if token:
            _fill(sb, [need_code], token, "验证码")
            time.sleep(1)

    # 提交
    print("🖱️  提交续期…")
    submitted = False
    reason = ""

    def _ts_token():
        # 本站属性读取不稳定（get_attribute 常拿到空值）：先 get_property，
        # 再用 page_source 正则兜底（历次 dump 证明序列化 DOM 里 token 完整）
        try:
            for e in sb.find_elements('input[name="cf-turnstile-response"]'):
                v = e.get_property("value") or ""
                if len(v) > 20:
                    return v
        except Exception:
            pass
        try:
            m = re.search(r'name="cf-turnstile-response"[^>]*value="([^"]{20,})"',
                          sb.get_page_source() or "")
            if m:
                return m.group(1)
        except Exception:
            pass
        return ""

    # 1) 等 Turnstile token
    print("  ⏳ 等 Turnstile token…")
    token_ok = False
    for i in range(15):
        if _ts_token():
            print("  ✅ Turnstile 就绪")
            token_ok = True
            break
        time.sleep(1)
    if not token_ok:
        print("  🖱️  token 未就绪，GUI 点击 Turnstile 验证框…")
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"  ⚠️  GUI 点击异常: {str(e)[:60]}")
        for i in range(25):
            if _ts_token():
                print("  ✅ Turnstile 就绪")
                token_ok = True
                break
            time.sleep(1)
    if not token_ok:
        print("  ⚠️  Turnstile token 未就绪，仍尝试提交")

    # 2) 按钮分类：outerHTML 带 data-callback 的是 reCAPTCHA 触发按钮；
    #    读不到时用 page_source 的 DOM 顺序兜底（历次 dump 中均为第一个）
    btns = []
    try:
        btns = sb.find_elements('button[name="submit_button"]')
    except Exception:
        pass
    print(f"  找到 {len(btns)} 个 submit_button")
    recaptcha_btn, plain_btn = None, None
    for b in btns:
        try:
            oh = b.get_attribute("outerHTML") or ""
        except Exception:
            oh = ""
        if "data-callback" in oh:
            recaptcha_btn = b
        elif plain_btn is None:
            plain_btn = b
    if recaptcha_btn is None and len(btns) >= 2:
        try:
            src = sb.get_page_source() or ""
            pos = src.find('name="submit_button"')
            if pos != -1 and "data-callback" in src[pos:pos + 300]:
                recaptcha_btn = btns[0]
                if plain_btn is btns[0]:
                    plain_btn = btns[1]
        except Exception:
            pass
    print(f"  按钮分类: reCAPTCHA={'有' if recaptcha_btn else '无'}, 普通={'有' if plain_btn else '无'}")

    # 3) 点击：先点 reCAPTCHA 按钮；未见成功且 token 已就绪时改点普通按钮
    def _click_btn(b, tag):
        try:
            sb.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'});", b)
        except Exception:
            pass
        time.sleep(0.5)
        try:
            b.click()
        except Exception:
            from selenium.webdriver.common.action_chains import ActionChains
            ActionChains(sb.driver).move_to_element(b).click().perform()
        print(f"  ✅ 点击按钮（{tag}）")
        return True

    def _success_seen():
        try:
            t = (sb.get_text("body") or "").lower()
        except Exception:
            return False
        return ("has been renewed" in t or "renewed until" in t
                or "successfully renewed" in t or "续期成功" in t)

    first = recaptcha_btn or plain_btn
    if first is not None:
        try:
            if _click_btn(first, "reCAPTCHA" if first is recaptcha_btn else "普通"):
                submitted = True
                reason = "first_click"
        except Exception as e:
            print(f"  ⚠️  首次点击失败: {str(e)[:60]}")

    if submitted:
        print("⏳ 等待结果…")
        time.sleep(10)
        _pass_ad_wall(sb)
        if not _success_seen() and plain_btn is not None and plain_btn is not first:
            if _ts_token():
                print("  🔁 首击未见成功且 Turnstile token 已就绪，改点普通按钮重试…")
                try:
                    _click_btn(plain_btn, "普通")
                    reason = "plain_retry"
                    time.sleep(10)
                    _pass_ad_wall(sb)
                except Exception as e:
                    print(f"  ⚠️  重试点击失败: {str(e)[:60]}")
            else:
                print("  ℹ️  首击未见成功，token 仍未就绪，不重试")

    # 4) 兜底：提交表单
    if not submitted:
        try:
            f = sb.find_element('form#form-submit', timeout=3)
            if f:
                f.submit()
                submitted = True
                reason = "form_submit"
                print("  ✅ 直接提交表单")
        except Exception as e:
            print(f"  ⚠️  表单提交失败: {str(e)[:60]}")

    if not submitted:
        print(f"❌ 提交失败（{reason or '所有方式都失败'}）")
        _step(sb, "renew-submit-fail")
        send_tg("❌", "续期提交失败", f"原因: {reason}\n{body[:200]}")
        return False, None

    print(f"  ✅ 已提交（{reason}）")
    return _check_result(sb)


def _check_result(sb):
    print("\n📋 检查结果…")
    body = ""
    for _ in range(4):
        try:
            body = sb.get_text("body") or ""
        except Exception:
            body = ""
        if body:
            break
        time.sleep(2)

    low = body.lower()
    expiry = parse_expiry(body)

    # 准确判定：必须出现明确的成功语句（例如 Your VPS has been renewed until...）
    if "has been renewed" in low or "renewed until" in low or "successfully renewed" in low or "续期成功" in low:
        msg = "续期成功"
        if expiry:
            msg += f"\n📅 新到期日: {expiry}"
        print(f"✅ {msg}")
        send_tg("✅", msg, body[:300])
        return True, expiry

    if re.search(r"can'?t renew|cannot renew|not (yet )?time|未到|too early", low):
        print("⏳ 未到续期时间")
        send_tg("⏳", "未到续期时间", body[:300])
        return True, expiry

    if re.search(r"error|failed|失败|invalid|无效|expired|过期", low):
        print("❌ 续期失败")
        send_tg("❌", "续期失败", body[:300])
        return False, expiry

    print("❓ 结果不明确")
    dump_page(sb, "result")
    send_tg("❓", "结果不明确，请手动确认", body[:300])
    return False, expiry


# ==================== 入口 ====================

def main():
    print("#" * 26)
    print("   HH 自动续期")
    print("#" * 26)

    missing = []
    if not SITE:
        missing.append("HH_SITE")
    if not PHONE:
        missing.append("HH_PHONE")
    if missing:
        print("❌ 缺少配置: " + " / ".join(missing))
        sys.exit(2)

    print(f"🌐 站点: {SITE}")

    state = load_state()
    expiry_str = state.get("expires")
    today = now_cst().date()

    # ---- 判断是否该续期 ----
    if expiry_str and not FORCE_RENEW:
        try:
            exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            days_left = (exp - today).days
            print(f"📅 记录到期日: {exp}（还剩 {days_left} 天）")
            if days_left > RENEW_AHEAD_DAYS:
                print(f"⏭️  还没到提前 {RENEW_AHEAD_DAYS} 天的窗口，跳过")
                print("   （要强制续期：设 FORCE_RENEW=1）")
                sys.exit(0)
            print(f"✅ 进入续期窗口（≤{RENEW_AHEAD_DAYS} 天）")
        except Exception as e:
            print(f"⚠️  状态文件异常，继续尝试续期: {e}")
    elif FORCE_RENEW:
        print("⚡ FORCE_RENEW=1，忽略到期日")
    else:
        print("ℹ️  没有到期日记录，本次先续期并记录")

    is_proxy = os.environ.get("IS_PROXY", "false").lower() == "true"
    proxy_str = (os.environ.get("PROXY_SERVER") or "").strip() or "http://127.0.0.1:1081"
    kw = {"uc": True, "headless": False}
    if is_proxy:
        print(f"🔗 代理: {proxy_str}")
        kw["proxy"] = proxy_str
    else:
        print("🌐 直连")

    ok = False
    new_expiry = None

    # 先读上次保存的 cookie
    cookies = load_cookies()
    print(f"🍪 本地 cookie: {len(cookies)} 个")

    with SB(**kw) as sb:
        try:
            sb.open("https://api.ip.sb/ip")
            print(f"📍 出口 IP: {(sb.get_text('body') or '').strip()}")
        except Exception:
            pass

        try:
            # ---------- 1) 尝试用 cookie 直接续期 ----------
            used_cookie = False
            if cookies:
                print("\n🔑 尝试用已保存的 cookie 续期…")
                try:
                    sb.open(SITE)
                    time.sleep(2)
                except Exception:
                    pass

                injected = apply_cookies(sb, cookies)
                if injected:
                    # 直接开续期页看是否登录
                    try:
                        sb.uc_open_with_reconnect(renew_url(), reconnect_time=5)
                    except Exception:
                        sb.open(renew_url())
                    time.sleep(5)
                    _wait_ready(sb, 30)

                    if _is_logged_in(sb):
                        print("✅ cookie 有效，跳过登录")
                        used_cookie = True
                        ok, new_expiry = renew(sb)
                    else:
                        print("⚠️  cookie 已失效，需要重新登录")
                        try:
                            os.remove(COOKIE_FILE)
                            print("   已删除失效的 cookie 文件")
                        except Exception:
                            pass

            # ---------- 2) cookie 不可用 → 登录 ----------
            if not used_cookie:
                if not login(sb):
                    sys.exit(5)
                ok, new_expiry = renew(sb)

        except Exception as e:
            print(f"❌ 异常: {e}")
            import traceback
            traceback.print_exc()
            send_tg("❌", "运行异常", str(e)[:300])

    # ---- 记录到期日 ----
    if new_expiry:
        state["expires"] = new_expiry.strftime("%Y-%m-%d")
        state["updated"] = now_cst().strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
    elif ok and expiry_str:
        # 续期成功但没解析到新日期，按 +7 天估算
        try:
            exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
            state["expires"] = (exp + timedelta(days=7)).strftime("%Y-%m-%d")
            state["updated"] = now_cst().strftime("%Y-%m-%d %H:%M:%S")
            save_state(state)
            print(f"⚠️  未解析到新到期日，按 +7 天估算: {state['expires']}")
        except Exception:
            pass

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
