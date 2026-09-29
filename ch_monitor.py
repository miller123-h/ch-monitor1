#!/usr/bin/env python3
"""
Chrome Hearts 官网上新监控
- 轮询官网页面(Salesforce Commerce Cloud),发现新出现的商品链接即推送上新
- 价格同时显示 USD / AUD / CNY(汇率自动更新)
- 推送渠道:邮件(SMTP)、飞书 webhook、Discord webhook(填了哪个用哪个)

用法:
  pip install requests
  python ch_monitor.py --test    # 发一条测试消息
  python ch_monitor.py           # 持续监控
"""
import argparse
import hashlib
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import os
import re
import time

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# ================= 配置 =================
SITE = "https://www.chromehearts.com"
# 要监控的页面:新品页 / 分类页 / 首页(把官网上"突发上新"会出现的页面填进来)
WATCH_URLS = [
    # 上新页面(已去掉 utm/fbclid 跟踪参数)
    SITE + "/on/demandware.store/Sites-ChromeHearts-Site/en_US/Default-Start",
]
# 页面内容变化也提醒(兜底:万一上新商品不是以 .html 链接形式出现)
# 官网底部的固定页面,不算商品,永远忽略
IGNORE_WORDS = ["disclosure", "general", "locations", "magazine", "privacy", "terms"]
PAGE_CHANGE_ALERT = False   # 默认关闭:只有出现新商品链接才发邮件;改成 True 则页面内容变化也提醒
# 从页面里提取商品链接的正则(官网是 Salesforce Commerce Cloud,商品页一般以 .html 结尾;
# 如果抓不到,把真实商品链接发给我再调整)
PRODUCT_LINK_RE = r'href="((?:https://www\.chromehearts\.com)?/[^"#?]*?\.html)'
POLL_SECONDS = 30                              # 轮询间隔,别太小以免被封
FEISHU_WEBHOOK = os.getenv("FEISHU_WEBHOOK", "")    # 飞书机器人 webhook
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")  # Discord webhook
# 邮件推送(Gmail 需要"应用专用密码",不是登录密码)
EMAIL_USER = os.getenv("EMAIL_USER", "")    # 发件邮箱,例如 you@gmail.com
EMAIL_PASS = os.getenv("EMAIL_PASS", "")    # 应用专用密码
EMAIL_TO = os.getenv("EMAIL_TO", "") or EMAIL_USER   # 收件邮箱,默认发给自己
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
# ---- 邮件推送(Gmail 示例:需要"应用专用密码",不是登录密码) ----
EMAIL_USER = os.getenv("EMAIL_USER", "")   # 发件邮箱,如 xxx@gmail.com
EMAIL_PASS = os.getenv("EMAIL_PASS", "")   # 应用专用密码(16位)
EMAIL_TO = os.getenv("EMAIL_TO", "") or EMAIL_USER   # 收件邮箱,默认发给自己
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
STATE_FILE = "ch_state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}
# =======================================

_rate_cache = {"t": 0, "rates": {"AUD": 1.5, "CNY": 7.2}}


def get_rates():
    """USD -> AUD / CNY,缓存 1 小时;失败则用上次的值。"""
    if time.time() - _rate_cache["t"] < 3600:
        return _rate_cache["rates"]
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10).json()
        _rate_cache["rates"] = {"AUD": r["rates"]["AUD"], "CNY": r["rates"]["CNY"]}
        _rate_cache["t"] = time.time()
    except Exception as e:
        print("汇率获取失败,沿用旧值:", e)
    return _rate_cache["rates"]


def fmt_price(usd):
    rt = get_rates()
    usd = float(usd)
    return f"${usd:,.2f} USD ｜ A${usd * rt['AUD']:,.0f} ｜ ¥{usd * rt['CNY']:,.0f}"


def _meta(html, prop):
    m = re.search(r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]+content=["\']([^"\']*)' % re.escape(prop), html, re.I)
    return m.group(1) if m else ""


def parse_product(url):
    """打开商品页,取标题/图片/价格/库存(优先 JSON-LD,其次 og meta)。"""
    html = requests.get(url, headers=HEADERS, timeout=15).text
    title, price, image, avail = _meta(html, "og:title"), None, _meta(html, "og:image"), True
    for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(blob)
        except Exception:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("@type") == "Product":
                title = d.get("name") or title
                offers = d.get("offers") or {}
                offers = offers[0] if isinstance(offers, list) and offers else offers
                if isinstance(offers, dict):
                    price = offers.get("price") or price
                    avail = "InStock" in str(offers.get("availability", "InStock"))
    if price is None:
        price = _meta(html, "product:price:amount") or "0"
    return {
        "title": title or url, "url": url, "image": image, "type": "",
        "price": float(str(price).replace(",", "")),
        "sizes": {"库存": avail},
    }


def fetch_all():
    """抓监控页面:返回 (商品链接集合, {页面: 内容指纹})。"""
    links, hashes = set(), {}
    for page_url in WATCH_URLS:
        r = requests.get(page_url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        for href in re.findall(PRODUCT_LINK_RE, r.text):
            full = href if href.startswith("http") else SITE + href
            if any(w in full.lower() for w in IGNORE_WORDS):
                continue
            links.add(full)
        # 指纹:去掉 script/style/标签和数字后再哈希,减少 token、时间戳带来的误报
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", r.text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\d+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        hashes[page_url] = hashlib.sha1(text.encode("utf-8")).hexdigest()
    return links, hashes


def stock_text(sizes):
    return "\n".join(f"{s}  {'✅ 有货' if ok else '❌ 无货'}" for s, ok in sizes.items())


def push_feishu(tag, p):
    if not FEISHU_WEBHOOK:
        return
    body = (
        f"**品名**\n{p['title']}\n\n"
        f"**价格**\n{fmt_price(p['price'])}\n\n"
        f"**尺码库存**\n{stock_text(p['sizes'])}"
    )
    card = {
        "header": {"template": "green", "title": {"tag": "plain_text", "content": f"(官网) Chrome Hearts {tag}"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            {"tag": "action", "actions": [{
                "tag": "button", "text": {"tag": "plain_text", "content": "查看商品"},
                "type": "primary", "url": p["url"]}]},
        ],
    }
    requests.post(FEISHU_WEBHOOK, json={"msg_type": "interactive", "card": card}, timeout=10)


def push_discord(tag, p):
    if not DISCORD_WEBHOOK:
        return
    embed = {
        "title": p["title"], "url": p["url"], "color": 0x57F287,
        "author": {"name": f"Chrome Hearts · {tag}"},
        "fields": [
            {"name": "Price", "value": fmt_price(p["price"]), "inline": False},
            {"name": "Stock", "value": stock_text(p["sizes"])[:1000], "inline": False},
        ],
    }
    if p["image"]:
        embed["thumbnail"] = {"url": p["image"]}
    requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)


def push_email(tag, p):
    if not (EMAIL_USER and EMAIL_PASS):
        return
    price = fmt_price(p["price"]) if p["price"] else "见商品页"
    stock = stock_text(p["sizes"]).replace("\n", "<br>") if p["sizes"] else ""
    img = f'<img src="{p["image"]}" width="300"><br>' if p.get("image") else ""
    html = f"""
    <h2>Chrome Hearts {tag}</h2>
    {img}
    <p><b>品名:</b><br>{p['title']}</p>
    <p><b>价格:</b><br>{price}</p>
    {'<p><b>库存:</b><br>' + stock + '</p>' if stock else ''}
    <p><a href="{p['url']}">👉 立即查看商品</a></p>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"⚠️上新⚠️Chrome Hearts | {p['title']} | {price.split('｜')[0].strip()}"
    msg["From"] = EMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
        srv.login(EMAIL_USER, EMAIL_PASS)
        srv.sendmail(EMAIL_USER, [a.strip() for a in EMAIL_TO.split(",")], msg.as_string())


def push_email(tag, p):
    if not (EMAIL_USER and EMAIL_PASS):
        return
    price_line = fmt_price(p["price"]) if p.get("price") else ""
    subject = f"⚠️上新⚠️ Chrome Hearts {tag} | {p['title']}" + (f" | ${p['price']:,.0f}" if p.get("price") else "")
    img = f'<p><img src="{p["image"]}" width="240"></p>' if p.get("image") else ""
    stock = "<br>".join(stock_text(p["sizes"]).split("\n")) if p.get("sizes") else ""
    html = (
        f"<h2>Chrome Hearts {tag}</h2>{img}"
        f"<p><b>品名</b><br><a href=\"{p['url']}\">{p['title']}</a></p>"
        + (f"<p><b>价格</b><br>{price_line}</p>" if price_line else "")
        + (f"<p><b>尺码库存</b><br>{stock}</p>" if stock else "")
        + f"<p><a href=\"{p['url']}\">👉 打开链接</a></p>"
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, EMAIL_USER, EMAIL_TO
    msg.attach(MIMEText(html, "html", "utf-8"))
    if SMTP_PORT == 465:   # SSL 直连(Gmail)
        srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20)
    else:                  # STARTTLS(Outlook 用 587)
        srv = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        srv.starttls()
    with srv:
        srv.login(EMAIL_USER, EMAIL_PASS)
        srv.sendmail(EMAIL_USER, [x.strip() for x in EMAIL_TO.split(",")], msg.as_string())


def notify(tag, p):
    ok = False
    for fn in (push_email, push_feishu, push_discord):
        try:
            fn(tag, p)
            # 只有配置了对应渠道才算发送成功
            if fn is push_email and EMAIL_USER and EMAIL_PASS:
                ok = True
            if fn is push_feishu and FEISHU_WEBHOOK:
                ok = True
            if fn is push_discord and DISCORD_WEBHOOK:
                ok = True
        except Exception as e:
            print("推送失败:", fn.__name__, e)
    return ok


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, list):  # 兼容旧格式
            d = {"links": d, "hashes": {}}
        return set(d["links"]), d.get("hashes", {})
    return None


def save_state(links, hashes):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"links": sorted(links), "hashes": hashes}, f, ensure_ascii=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="发送一条测试推送后退出")
    ap.add_argument("--links", action="store_true", help="只打印当前抓到的商品链接(用来调试正则)")
    ap.add_argument("--once", action="store_true", help="只检查一次就退出(给 GitHub Actions 用)")
    args = ap.parse_args()

    if args.links:
        links, _ = fetch_all()
        print(f"共抓到 {len(links)} 个商品链接")
        for l in sorted(links):
            print(l)
        return

    if args.test:
        ok = notify("🧪 测试", {
            "title": "TEST SCARF", "url": SITE, "image": "", "type": "Scarf",
            "price": 1100.0, "sizes": {"OS": True},
        })
        print("测试消息已发送,请查收邮箱" if ok else "测试失败:没有任何渠道发送成功,请看上面的报错")
        return

    state = load_state()
    if state is None:  # 首次运行:只记录现状,不推送
        seen, hashes = fetch_all()
        save_state(seen, hashes)
        print(f"首次运行,已记录 {len(seen)} 个商品链接")
        if args.once:
            return
    else:
        seen, hashes = state

    while True:
        try:
            seen, hashes = check(seen, hashes)
            save_state(seen, hashes)
        except Exception as e:
            print("轮询出错:", e)
            if args.once:
                raise SystemExit(1)
        if args.once:
            return
        time.sleep(POLL_SECONDS)


def check(seen, hashes):
    """检查一次,返回更新后的 (seen, hashes)。推送失败的链接不记入 seen,下次会重试。"""
    current, new_hashes = fetch_all()
    new_links = sorted(current - seen)
    failed = set()
    for url in new_links:
        try:
            p = parse_product(url)
        except Exception as e:
            p = {"title": url, "url": url, "image": "", "type": "", "price": 0.0, "sizes": {}}
            print("解析商品页失败:", e)
        print("🆕 上新", p["title"])
        if not notify("🆕 上新", p):
            failed.add(url)
    # 没有新商品链接、但页面内容变了 -> 发一条"页面有变化"提醒
    if PAGE_CHANGE_ALERT and not new_links:
        for u, h in new_hashes.items():
            if u in hashes and hashes[u] != h:
                print("⚠️ 页面内容变化", u)
                if not notify("⚠️ 页面有变化(可能上新)", {
                    "title": "上新页面内容发生变化,请立即查看", "url": u, "image": "",
                    "type": "", "price": 0.0, "sizes": {},
                }):
                    new_hashes[u] = hashes[u]  # 没发出去,保留旧指纹,下次重试
    return seen | (current - failed), new_hashes


if __name__ == "__main__":
    main()
