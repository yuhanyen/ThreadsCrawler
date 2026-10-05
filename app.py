#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Threads 貼文收集器（網頁版）

架構：
  - 這台「主機」負責實際爬取（瀏覽器 session 存 browser_profile/）
  - 用瀏覽器開 http://localhost:8014 → 設定帳號/關鍵字、按「立即收集」、下載 CSV
  - 可設定每天在固定時間自動收集「過去 N 小時」的貼文

啟動：python app.py
設定存 config.json；輸出存 output/threads_日期_時分.csv(+json)
"""

import csv
import json
import logging
import socket
import sys
import threading
import time as time_mod

# Windows 主控台預設 cp950，印 emoji/特殊字元會炸掉
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

import crawler
import threads_crawl
from threads_crawl import CONFIG_FILE, OUTPUT_DIR, TZ

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "service.log"
LOG_FILE_OLD = LOG_DIR / "service.log.1"   # 超過 5MB 輪替一次

DEFAULT_CONFIG = {
    "targets": [],                 # 每行一個：@帳號、個人頁網址，或搜尋關鍵字
    "schedule_times": ["09:00"],   # 每日自動收集的時間點（可多個）
    "schedule_enabled": False,
    "schedule_hours": 24,   # 每日自動收集的時間範圍（小時）
    "max_scrolls": 30,
    "save_json": True,      # false = 只輸出 CSV，不另外寫一份 JSON
    "headless": False,      # true = 收集時不開瀏覽器視窗
    "host": "127.0.0.1",    # 改成 "0.0.0.0" 才能讓內網同仁連線
    "port": 8014,
}


def sane_hours(value, default=24):
    """把設定/請求裡的時間範圍轉成 1～168 的整數，壞值一律退回預設。"""
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return default
    return hours if 1 <= hours <= 168 else default


def sane_times(value, default=("09:00",)):
    """整理每日時間點清單：格式化為 HH:MM、去重、排序；全壞則退回預設。"""
    if isinstance(value, str):
        value = [value]
    times = set()
    for t in value or []:
        try:
            times.add(datetime.strptime(str(t).strip(), "%H:%M").strftime("%H:%M"))
        except ValueError:
            continue
    return sorted(times) or list(default)

app = Flask(__name__)

# 收集狀態（login 與 crawl 共用 running 旗標：瀏覽器 profile 同時只能開一個）
state = {
    "running": False,
    "task": None,       # "crawl" | "login"
    "log": [],
    "result": None,     # {"count", "csv", "json", "finished_at"}
    "error": None,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(threads_crawl.read_config())
    cfg["schedule_times"] = sane_times(cfg["schedule_times"])
    return cfg


def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def _listen_addr():
    cfg = load_config()
    try:
        port = int(cfg.get("port") or DEFAULT_CONFIG["port"])
    except (TypeError, ValueError):
        port = DEFAULT_CONFIG["port"]
    return str(cfg.get("host") or DEFAULT_CONFIG["host"]), port


HOST, PORT = _listen_addr()


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_url():
    """同仁連線用的網址；只聽本機（預設）時回 None。"""
    if HOST in ("127.0.0.1", "localhost"):
        return None
    return f"http://{get_lan_ip()}:{PORT}"


def _log(msg):
    state["log"].append(f"{datetime.now(TZ):%H:%M:%S}  {msg}")
    del state["log"][:-200]
    print(msg, flush=True)
    # 同步寫入永久日誌檔；寫檔失敗不能影響收集作業
    try:
        LOG_DIR.mkdir(exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5 * 1024 * 1024:
            LOG_FILE.replace(LOG_FILE_OLD)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(TZ):%Y-%m-%d %H:%M:%S}  {msg}\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 收集工作（背景執行緒）
# ---------------------------------------------------------------------------

def start_task(task, trigger="手動", hours=24):
    if state["running"]:
        return False
    state.update(running=True, task=task, log=[], result=None, error=None)
    if task == "crawl":
        args = (trigger, hours)
        target = _crawl_job
    else:
        args = (trigger,)
        target = _login_job
    threading.Thread(target=target, args=args, daemon=True).start()
    return True


def _crawl_job(trigger, hours=24):
    try:
        cfg = load_config()
        _log(f"（{trigger}・過去 {hours} 小時）")
        # 時間窗拉長就要捲得更深，否則會被 max_scrolls 截斷、看起來像沒抓到
        scrolls = cfg.get("max_scrolls", 30)
        if hours > 24:
            scrolls = int(scrolls * hours / 24)
            _log(f"時間窗較長，捲動上限自動提高為 {scrolls}")
        posts, _start_dt, end_dt = threads_crawl.crawl_targets(
            cfg["targets"], max_scrolls=scrolls, hours=hours,
            headless=bool(cfg.get("headless")), log=_log)
        csv_name, json_name = threads_crawl.save_outputs(posts, end_dt, log=_log)
        state["result"] = {
            "count": len(posts), "csv": csv_name, "json": json_name,
            "finished_at": end_dt.strftime("%Y-%m-%d %H:%M"),
        }
        _log(f"✅ 完成，共 {len(posts)} 篇 → {csv_name}")
    except Exception as e:
        state["error"] = str(e)
        _log(f"❌ {e}")
    finally:
        state["running"] = False


def _login_job(trigger):
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            context = crawler.launch_context(p, headless=False)
            try:
                crawler.open_login_page(context)
                _log("已在主機開啟瀏覽器視窗，請在該視窗登入 Threads。"
                     f"視窗會等您 {crawler.LOGIN_WAIT_SECONDS // 60} 分鐘，"
                     "登入成功後自動關閉（請不要自己關掉它）…")
                result = crawler.wait_for_login(context)
                if result == "ok":
                    _log("✅ 登入成功，session 已保存，之後即可完整收集。")
                    return
                state["error"] = crawler.LOGIN_FAILURES[result]
                _log(f"⚠️ {state['error']}")
                if result == "timeout":
                    # 明明登入了卻沒偵測到時，靠這行判斷 Threads 是不是換了 cookie 名稱
                    _log("（偵錯用）目前 threads.com 的 cookie："
                         + ", ".join(crawler.cookie_names(context)))
            finally:
                crawler.close_quietly(context)
    except Exception as e:
        state["error"] = str(e)
        _log(f"❌ {e}")
    finally:
        state["running"] = False


# ---------------------------------------------------------------------------
# 每日排程（背景執行緒）
# ---------------------------------------------------------------------------

def scheduler_loop():
    last_fired = None   # (日期, "HH:MM")：同一個時間點當天只觸發一次
    while True:
        cfg = load_config()
        now = datetime.now(TZ)
        hhmm = now.strftime("%H:%M")
        if (cfg.get("schedule_enabled")
                and hhmm in cfg["schedule_times"]
                and last_fired != (now.date(), hhmm)):
            last_fired = (now.date(), hhmm)  # 無論是否成功啟動，這個時間點都不再重複觸發
            start_task("crawl", trigger=f"每日排程 {hhmm}",
                       hours=sane_hours(cfg.get("schedule_hours")))
        time_mod.sleep(20)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/status")
def api_status():
    cfg = load_config()
    return jsonify({
        "targets": cfg["targets"],
        "schedule_times": cfg["schedule_times"],
        "schedule_enabled": cfg["schedule_enabled"],
        "schedule_hours": sane_hours(cfg.get("schedule_hours")),
        "max_scrolls": cfg.get("max_scrolls", 30),
        "running": state["running"],
        "lan_url": lan_url(),
        "history": list_history(),
    })


@app.post("/api/config")
def api_config():
    body = request.get_json(silent=True) or {}
    cfg = load_config()
    if "targets" in body:
        if not isinstance(body["targets"], list):
            return jsonify(error="targets 需為清單"), 400
        cfg["targets"] = [t.strip() for t in body["targets"]
                          if isinstance(t, str) and t.strip()]
    if "schedule_times" in body:
        raw = body["schedule_times"]
        if not isinstance(raw, list):
            return jsonify(error="schedule_times 需為時間清單"), 400
        times = set()
        for t in raw:
            try:
                times.add(datetime.strptime(str(t).strip(), "%H:%M").strftime("%H:%M"))
            except ValueError:
                return jsonify(error=f"時間格式錯誤：{t}（請用 HH:MM）"), 400
        if not times:
            return jsonify(error="至少要設定一個自動收集時間"), 400
        cfg["schedule_times"] = sorted(times)
    if "schedule_enabled" in body:
        cfg["schedule_enabled"] = bool(body["schedule_enabled"])
    if "schedule_hours" in body:
        try:
            hours = int(body["schedule_hours"])
        except (TypeError, ValueError):
            return jsonify(error="每日收集範圍必須是數字"), 400
        if not 1 <= hours <= 168:
            return jsonify(error="每日收集範圍需介於 1～168 小時"), 400
        cfg["schedule_hours"] = hours
    save_config(cfg)
    return jsonify(ok=True)


@app.post("/api/crawl")
def api_crawl():
    body = request.get_json(silent=True) or {}
    try:
        hours = int(body.get("hours", 24))
    except (TypeError, ValueError):
        return jsonify(error="hours 必須是數字"), 400
    if not 1 <= hours <= 168:
        return jsonify(error="hours 需介於 1～168（最多 7 天）"), 400
    if not start_task("crawl", hours=hours):
        return jsonify(error="目前已有收集或登入作業進行中，請稍候"), 409
    return jsonify(ok=True, hours=hours)


@app.post("/api/login")
def api_login():
    if not start_task("login"):
        return jsonify(error="目前已有收集或登入作業進行中，請稍候"), 409
    return jsonify(ok=True)


@app.get("/api/logs")
def api_logs():
    lines = []
    for path in (LOG_FILE_OLD, LOG_FILE):
        if path.exists():
            try:
                lines.extend(path.read_text(encoding="utf-8").splitlines())
            except OSError:
                pass
    return jsonify(lines=lines[-300:])


@app.get("/api/crawl_status")
def api_crawl_status():
    return jsonify({
        "running": state["running"],
        "task": state["task"],
        "log": state["log"],
        "result": state["result"],
        "error": state["error"],
    })


def list_history():
    items = []
    for f in sorted(OUTPUT_DIR.glob("threads_*.csv"), reverse=True)[:30]:
        jf = f.with_suffix(".json")
        count = None
        if jf.exists():
            try:
                count = len(json.loads(jf.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        else:   # 只存 CSV 時，用資料列數（扣掉標題列）當篇數
            try:
                with open(f, encoding="utf-8-sig", newline="") as fh:
                    count = max(sum(1 for _ in csv.reader(fh)) - 1, 0)
            except OSError:
                pass
        items.append({
            "csv": f.name,
            "json": jf.name if jf.exists() else None,
            "count": count,
            "mtime": datetime.fromtimestamp(f.stat().st_mtime, TZ).strftime("%Y-%m-%d %H:%M"),
        })
    return items


@app.get("/download/<path:filename>")
def download(filename):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # 不記每筆 HTTP 請求
    threading.Thread(target=scheduler_loop, daemon=True).start()
    _log(f"=== 服務啟動：http://localhost:{PORT} ===")
    if lan_url():
        print(f"✅ 同仁請開 {lan_url()}（需同一個內部網路）")
    app.run(host=HOST, port=PORT, debug=False)
