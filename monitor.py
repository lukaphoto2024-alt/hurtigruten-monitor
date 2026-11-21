#!/usr/bin/env python3
"""Hurtigruten 港到港（havn til havn）舱位监控。

只查询、只发提醒，不会自动下单。
接口：POST https://www.hurtigruten.com/nellie-coastal-v2/api/availability
（官网 /nb-no/havn-til-havn 搜索页自己用的接口，2026-11-21 用真实数据测通）
"""
import datetime as dt
import json
import os
import smtplib
import ssl
import sys
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
STATE_FILE = HERE / "state.json"

API = "https://www.hurtigruten.com/nellie-coastal-v2/api/availability"
SEARCH_PAGE = "https://www.hurtigruten.com/nb-no/havn-til-havn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# 舱型代码 -> (中文说明, 优先级，越小越想要)
CATEGORY_INFO = {
    "PolarInside": ("内舱 Polar（无窗，最便宜，常见上下铺/两张单人床）", 1),
    "PolarOutside": ("外舱 Polar（有窗）", 2),
    "ArcticSuperior": ("高级外舱 Arctic Superior（有窗，较贵）", 3),
    "ExpeditionSuite": ("套房 Expedition Suite（很贵）", 5),
    "DECK": ("无舱位 / 大厅座位（deck space）", 4),
}

NORWAY_OFFSET = dt.timedelta(hours=1)  # 2 月是冬令时 UTC+1


def pax(n):
    return {"adults": n, "children": 0, "infants": 0, "companions": 0,
            "students": 0, "seniors": 0, "militaryServicePersons": 0,
            "wheelchairs": 0, "pets": 0}


def build_body(deck: bool):
    dates = sorted(dt.date.fromisoformat(d) for d in CONFIG["dates"])
    # 和官网一样：起止都是挪威本地午夜，换成 UTC；前后各多查一天保险
    start = dt.datetime.combine(dates[0] - dt.timedelta(days=1), dt.time()) - NORWAY_OFFSET
    end = dt.datetime.combine(dates[-1] + dt.timedelta(days=2), dt.time()) - NORWAY_OFFSET
    p = pax(CONFIG["adults"])
    return {
        "searchInterval": "day", "locale": "nb-no",
        "automaticPromotionCode": None, "promotionCode": None,
        "fromDate": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "toDate": end.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "isReturnTrip": False, "isViaKirkenes": False,
        "fromPort": CONFIG["from_port"], "toPort": CONFIG["to_port"],
        "deckspaces": p if deck else None,
        "cabins": [] if deck else [p],
        "vehicles": {"cars": 0, "motorcycles": 0},
        "requestType": "voyage",
    }


class Fetcher:
    """先用普通 HTTP 请求；被 Cloudflare 拦了就改用无头浏览器在官网页面里调用同一个接口。"""

    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA, "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.hurtigruten.com",
            "Referer": SEARCH_PAGE + "/bestilling",
            "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8",
        })
        self.browser_page = None
        self._pw = None

    def post(self, body):
        if self.browser_page is None:
            try:
                r = self.s.post(API, json=body, timeout=30)
                if r.status_code == 200:
                    return r.json()
                print(f"[http] status {r.status_code}, 改用浏览器", file=sys.stderr)
            except (requests.RequestException, ValueError) as e:
                print(f"[http] {e!r}, 改用浏览器", file=sys.stderr)
            self._open_browser()
        return self.browser_page.evaluate(
            """async (b) => {
                const r = await fetch('/nellie-coastal-v2/api/availability', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(b)});
                if (!r.ok) throw new Error('HTTP ' + r.status);
                return await r.json();
            }""", body)

    def _open_browser(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        browser = self._pw.chromium.launch()
        page = browser.new_page(user_agent=UA, locale="nb-NO")
        page.goto(SEARCH_PAGE, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        self.browser_page = page

    def close(self):
        if self._pw:
            self._pw.stop()


def local_date(iso):
    t = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (t + NORWAY_OFFSET).strftime("%Y-%m-%d"), (t + NORWAY_OFFSET).strftime("%H:%M")


def find_hits(fetcher):
    wanted_dates = set(CONFIG["dates"])
    wanted_cats = set(CONFIG["categories"])
    hits = []
    for deck in (False, True):
        if deck and "DECK" not in wanted_cats:
            continue
        for v in fetcher.post(build_body(deck)):
            day, hhmm = local_date(v["departureDateTime"])
            if day not in wanted_dates:
                continue
            base = {"date": day, "time": hhmm, "ship": v.get("shipName", ""),
                    "arrive": " ".join(local_date(v["arrivalDateTime"]))}
            if deck:
                hits.append({**base, "cat": "DECK", "price": v.get("price")})
            else:
                for c in v.get("categoryPrices", []):
                    if c.get("isAvailable") and c["cabinCategory"] in wanted_cats:
                        hits.append({**base, "cat": c["cabinCategory"], "price": c.get("price")})
    hits.sort(key=lambda h: (CATEGORY_INFO.get(h["cat"], ("", 9))[1], h["price"] or 0))
    return hits


def send_mail(subject, body):
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    pwd = os.environ["SMTP_PASS"]
    to = [a.strip() for a in os.environ.get("MAIL_TO", CONFIG["mail_to"]).split(",") if a.strip()]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = ", ".join(to)
    msg["Date"] = formatdate(localtime=True)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ssl.create_default_context(), timeout=30) as s:
            s.login(user, pwd)
            s.sendmail(user, to, msg.as_string())
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(user, pwd)
            s.sendmail(user, to, msg.as_string())


def describe(hits):
    lines = []
    for h in hits:
        name = CATEGORY_INFO.get(h["cat"], (h["cat"],))[0]
        lines.append(f"• {name}\n  {h['date']} {h['time']} 从 Tromsø 出发 → {h['arrive']} 到 Svolvær"
                     f"（{h['ship']}）\n  价格：{h['price']} NOK / 人")
    return "\n\n".join(lines)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    if mode == "test-mail":
        send_mail("【测试】挪威邮轮舱位监控已连上邮箱",
                  "这是一封测试邮件。收到说明发信配置没问题，有票时会发同样的邮件给你。")
        print("测试邮件已发送")
        return

    f = Fetcher()
    try:
        hits = find_hits(f)
    finally:
        f.close()

    print(json.dumps(hits, ensure_ascii=False, indent=1) if hits else "暂无符合条件的舱位")

    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    keys = {f"{h['date']}|{h['time']}|{h['cat']}": h["price"] for h in hits}
    prev = state.get("available", {})
    new = [h for h in hits
           if f"{h['date']}|{h['time']}|{h['cat']}" not in prev
           or (h["price"] or 0) < (prev[f"{h['date']}|{h['time']}|{h['cat']}"] or 0)]
    if new and os.environ.get("SMTP_HOST"):
        best = CATEGORY_INFO.get(new[0]["cat"], (new[0]["cat"],))[0].split("（")[0]
        body = ("有票啦！Tromsø → Svolvær 出现了可订舱位（2 人）：\n\n" + describe(new) +
                "\n\n赶紧去官网订（舱位可能很快被抢走）：\n" + SEARCH_PAGE +
                "\n\n提示：在官网选好航次后，下一步选舱时可以看床型（上下铺 / 双床）。"
                "\n\n—— 这封邮件由自动监控发送，只提醒不下单。")
        send_mail(f"【有票】挪威邮轮 Tromsø→Svolvær {new[0]['date']}：{best} {new[0]['price']} NOK", body)
        print(f"已发送提醒邮件（{len(new)} 项新舱位）")
    # 只记录日期：每天最多产生一次提交，既不刷屏，又能让 GitHub 不因"60 天无活动"停掉定时任务
    state = {"available": keys, "checked_on": dt.date.today().isoformat()}
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
