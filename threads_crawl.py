#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Threads 收集核心（共用模組）

app.py（網頁版）與 crawler.py 的 CLI 都呼叫這裡，
避免兩份爬取邏輯各自演化。
"""

import csv
import json
import random
import re
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

import crawler  # launch_context / is_logged_in / crawl_page / TZ

TZ = crawler.TZ
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
CONFIG_FILE = BASE_DIR / "config.json"


def read_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def upload_json(rows, log=print):
    """把貼文 JSON 上傳到服務平台各端點；失敗只記 log、不影響本地輸出檔。

    預設不上傳。要啟用就在 config.json 設 upload_urls（清單）與
    upload_api_key；"upload_enabled": false 可暫時關閉。
    """
    cfg = read_config()
    urls = [u for u in cfg.get("upload_urls") or [] if u]
    if not urls or not cfg.get("upload_enabled", True):
        return None
    body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    results = []
    for url in urls:
        try:
            resp = requests.post(url, data=body, timeout=90, headers={
                "Content-Type": "application/json; charset=utf-8",
                "x-api-key": cfg.get("upload_api_key") or "",
            })
            resp.raise_for_status()
            data = resp.json()
            log(f"☁️ 已上傳 {url}：id={data.get('id')}，postCount={data.get('postCount')}")
            results.append(data)
        except requests.RequestException as e:
            log(f"⚠️ 上傳 {url} 失敗（本地檔已保存，不影響）：{e}")
        except ValueError as e:
            log(f"⚠️ {url} 回應非 JSON：{e}")
    return results or None


def normalize_target(line):
    """一行設定 → (來源標籤, 網址, 帳號)；空行回 None。

    「@帳號」或個人頁網址 → 收集該帳號的貼文；其餘一律當關鍵字 → 搜尋的
    「最新」分頁（強制按時間排序，時間窗過濾才準確）。帳號只在個人頁時有值。
    """
    line = (line or "").strip()
    if line.startswith("http"):
        parsed = urlparse(line)
        m = re.match(r"/@([\w.]+)", parsed.path)
        if m:
            line = "@" + m.group(1)
        else:
            q = parse_qs(parsed.query).get("q")
            if not q:
                return line, line, None   # 其他 Threads 網址照原樣瀏覽
            line = q[0].strip()
    if line.startswith("@"):
        username = line[1:].strip().strip("/")
        if not username:
            return None
        return f"@{username}", f"{crawler.THREADS_HOME}@{username}", username
    keyword = line.lstrip("#").strip()
    if not keyword:
        return None
    query = urlencode({"q": keyword, "serp_type": "default", "filter": "recent"})
    return keyword, f"{crawler.THREADS_HOME}search?{query}", None


def crawl_targets(target_lines, max_scrolls=30, hours=24, headless=False, log=print):
    """收集所有目標過去 hours 小時的貼文，回傳 (posts, start_dt, end_dt)。

    posts 依時間新→舊排序，每篇多一個 "source" 欄位標示來源（@帳號 或關鍵字）；
    同一篇被多個目標撈到時只留一篇，來源以 | 串接。
    個別目標失敗會記 log 後跳過；全部失敗則丟 RuntimeError。
    """
    from playwright.sync_api import sync_playwright

    targets = [t for t in (normalize_target(line) for line in target_lines) if t]
    if not targets:
        raise RuntimeError("尚未設定任何收集目標，請先在網頁填入帳號或關鍵字並儲存")

    end_dt = datetime.now(TZ)
    start_dt = end_dt - timedelta(hours=hours)
    log(f"開始收集：{start_dt:%m/%d %H:%M} ～ {end_dt:%m/%d %H:%M}，共 {len(targets)} 個目標")

    by_id = {}
    failed = 0
    with sync_playwright() as p:
        context = crawler.launch_context(p, headless=headless)
        try:
            if not crawler.is_logged_in(context):
                log("⚠️ Threads 未登入：每個帳號只抓得到最近十幾串、關鍵字只有少量熱門結果")
                log("   要登入請按「主機登入 Threads」（或 python crawler.py login）；"
                    "這個收集視窗跑完就會自動關閉，請不要在裡面登入")
                crawler.block_login_redirect(context)
            page = context.pages[0] if context.pages else context.new_page()
            for idx, (label, url, username) in enumerate(targets, 1):
                log(f"[{idx}/{len(targets)}] 收集 {label} …")
                try:
                    posts = crawler.crawl_page(page, url, start_dt, end_dt,
                                               max_scrolls, author=username)
                except Exception as e:
                    failed += 1
                    log(f"  ⚠️ {label} 失敗：{e}")
                    continue
                for post in posts:
                    key = post["post_id"] or post["url"] or id(post)
                    if key in by_id:
                        by_id[key]["source"] += f" | {label}"
                    else:
                        post["source"] = label
                        by_id[key] = post
                log(f"  找到 {len(posts)} 篇")
                if idx < len(targets):
                    page.wait_for_timeout(random.randint(5000, 10000))  # 目標間隨機休息
        finally:
            context.close()

    if failed == len(targets):
        # 全滅通常是網路或 Threads 改版，別產出一個空檔讓人以為「今天沒貼文」
        raise RuntimeError("所有目標都收集失敗，請看上方紀錄")

    all_posts = sorted(by_id.values(), key=lambda p: p["time"], reverse=True)
    return all_posts, start_dt, end_dt


def save_outputs(posts, end_dt, log=print, upload=True):
    """輸出 output/threads_日期_時分.csv，回傳 (csv_name, json_name)。

    JSON 副檔預設也會寫；config.json 設 "save_json": false 就只留 CSV，
    此時回傳的 json_name 為 None。寫檔後（upload=True 且有設定端點時）
    把 JSON 上傳到服務平台。
    """
    OUTPUT_DIR.mkdir(exist_ok=True)
    stem = OUTPUT_DIR / f"threads_{end_dt:%Y-%m-%d_%H%M}"
    fields = ["source", "post_id", "time", "author", "text",
              "likes", "replies", "reposts", "quotes", "shares",
              "is_reply", "url", "images"]

    rows = []
    for p in posts:
        row = {k: p.get(k) for k in fields}
        row["time"] = p["time"].strftime("%Y-%m-%d %H:%M:%S")
        row["images"] = p.get("images") or []
        rows.append(row)

    save_json = read_config().get("save_json", True)
    if save_json:
        stem.with_suffix(".json").write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    # utf-8-sig：Excel 直接開不亂碼；images 清單以 | 分隔塞單一儲存格
    with open(stem.with_suffix(".csv"), "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            writer.writerow(dict(r, images=" | ".join(r["images"])))
    if upload:
        upload_json(rows, log=log)
    return (stem.with_suffix(".csv").name,
            stem.with_suffix(".json").name if save_json else None)
