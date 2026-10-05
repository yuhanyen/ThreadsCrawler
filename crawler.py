#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Threads 貼文收集器（瀏覽器爬取核心 + CLI）

原理：
  用 Playwright 開啟持久化瀏覽器（保留登入 session），瀏覽 Threads 個人頁
  或搜尋頁時攔截內部 GraphQL API 的 JSON 回應，直接從結構化資料抽取
  貼文（比解析 DOM 穩定），過濾出時間窗內的貼文。

用法：
  python crawler.py login                 # 開瀏覽器手動登入 Threads（session 存 browser_profile/）
  python crawler.py crawl                 # 收集 config.json 裡所有目標「過去 24 小時」的貼文
  python crawler.py crawl --hours 48 --max-scrolls 40 --headless
"""

import argparse
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")  # 貼文時間一律用台北時間，不受電腦時區影響

# Windows 主控台預設 cp950，印 emoji/特殊字元會炸掉
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = Path(__file__).resolve().parent
PROFILE_DIR = BASE_DIR / "browser_profile"   # 登入 session（勿上傳/分享）

THREADS_HOME = "https://www.threads.com/"
THREADS_LOGIN = THREADS_HOME + "login"
LOGIN_COOKIES = ("sessionid", "ds_user_id")   # 登入後才會出現的 cookie
LOGIN_WAIT_SECONDS = 600                       # 登入視窗最多等 10 分鐘（含驗證碼、簡訊等）
LOGIN_FAILURES = {
    "closed": "登入視窗被關掉了，還沒偵測到登入，請再試一次",
    "timeout": "逾時仍未偵測到登入，請再試一次",
}


# ---------------------------------------------------------------------------
# GraphQL JSON 解析（不依賴 DOM，Threads 改版時較耐用）
# ---------------------------------------------------------------------------

def walk_dicts(obj):
    """深度走訪巢狀 dict/list，yield 所有 dict 節點。"""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _dict(value):
    return value if isinstance(value, dict) else {}


def find_image_urls(post):
    """抽出貼文附圖的 CDN 網址（影片則是縮圖）。

    單圖/影片在貼文本身的 image_versions2，多圖貼文在 carousel_media 各項底下；
    每張圖有多種尺寸，取最大的那個。
    注意：fbcdn 網址帶 oh/oe 簽名，通常幾天～幾週就失效，要用就要趁早下載。
    """
    urls = []
    for media in post.get("carousel_media") or [post]:
        candidates = [c for c in _dict(_dict(media).get("image_versions2")).get("candidates") or []
                      if isinstance(c, dict) and isinstance(c.get("url"), str)]
        if candidates:
            best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
            urls.append(best["url"])
    return urls


def parse_post(post):
    """從一個貼文節點抽出欄位；不像貼文（沒時間或沒作者）就放棄。"""
    post = _dict(post)
    taken_at = post.get("taken_at")
    username = _dict(post.get("user")).get("username")
    if not isinstance(taken_at, int) or not isinstance(username, str):
        return None
    info = _dict(post.get("text_post_app_info"))
    code = post.get("code")
    post_id = post.get("pk") or post.get("id")
    return {
        "post_id": str(post_id) if post_id else None,
        "time": datetime.fromtimestamp(taken_at, tz=TZ),
        "author": username,
        "text": _dict(post.get("caption")).get("text") or "",
        "likes": post.get("like_count"),
        "replies": info.get("direct_reply_count"),
        "reposts": info.get("repost_count"),
        "quotes": info.get("quote_count"),
        "shares": info.get("reshare_count"),
        "is_reply": bool(info.get("is_reply")),
        "url": f"{THREADS_HOME}@{username}/post/{code}" if code else None,
        "images": find_image_urls(post),
        # 置頂貼文排在個人頁最上面但可能很舊，不能拿來判斷「已經捲過時間窗」
        "pinned": bool(_dict(info.get("pinned_post_info")).get("is_pinned_to_profile")),
    }


def parse_thread(thread_items):
    """一組 thread_items → (貼文, 被併掉的貼文 ID)；抽不到貼文時貼文為 None。

    Threads 的長文是作者「自己回覆自己」串起來的（串文）。這裡把同一作者
    緊接著的回覆併回開頭那篇：內文與附圖接在後面、互動數以開頭那篇為準，
    一篇文章只佔一列。
    搜尋命中的若是一則回覆，前面會帶出被回覆的原文當上下文（作者不同、
    通常也不含關鍵字），所以出現多位作者時只取最後一位的那篇。
    """
    head, absorbed = None, []
    for item in thread_items:
        post = parse_post(_dict(item).get("post"))
        if post is None:
            continue
        if head and post["is_reply"] and post["author"] == head["author"]:
            if post["text"]:
                head["text"] = f"{head['text']}\n\n{post['text']}".strip()
            head["images"].extend(u for u in post["images"] if u not in head["images"])
            if post["post_id"]:
                absorbed.append(post["post_id"])
            continue
        head = post
    return head, absorbed


class PostCollector:
    """收集去重後的貼文，並統計時間窗命中數（供捲動停止條件用）。"""

    def __init__(self, start_dt: datetime, end_dt: datetime, author=None):
        self.start_dt = start_dt
        self.end_dt = end_dt
        self.author = author.lower() if author else None   # 只收這個帳號的貼文（個人頁用）
        self.posts = {}          # key -> post dict（含時間窗以外的，最後才過濾）
        self.absorbed = set()    # 已併入別篇串文的貼文 ID
        self.older_seen = 0      # 已看到幾篇比時間窗起點更舊的貼文

    def ingest(self, payload):
        for node in walk_dicts(payload):
            items = node.get("thread_items")
            if not isinstance(items, list):
                continue
            post, absorbed = parse_thread(items)
            self.absorbed.update(absorbed)
            if post is None:
                continue
            if self.author and post["author"].lower() != self.author:
                continue
            key = post["post_id"] or post["url"] or (post["text"][:80], post["time"].isoformat())
            old = self.posts.get(key)
            if old is not None:
                # 同一篇在不同回應分塊會重複出現，留串文接得比較完整的那份
                if len(post["text"]) > len(old["text"]):
                    self.posts[key] = post
                continue
            self.posts[key] = post
            if post["time"] < self.start_dt and not post["pinned"]:
                self.older_seen += 1

    def ingest_text(self, body: str):
        """GraphQL 回應可能是單一 JSON 或多行 JSON（串流分塊），逐行嘗試。"""
        for chunk in body.split("\n"):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                self.ingest(json.loads(chunk))
            except (json.JSONDecodeError, RecursionError):
                continue

    def target_posts(self):
        hits = [p for p in self.posts.values()
                if self.start_dt <= p["time"] < self.end_dt
                and p["post_id"] not in self.absorbed]
        return sorted(hits, key=lambda p: p["time"], reverse=True)


# ---------------------------------------------------------------------------
# 瀏覽器操作
# ---------------------------------------------------------------------------

def launch_context(p, headless: bool):
    # 優先用系統已安裝的 Chrome / Edge（不需另外下載 Chromium，
    # 且真實瀏覽器較不易被判定為自動化）
    last_err = None
    for channel in ("chrome", "msedge", None):
        try:
            return p.chromium.launch_persistent_context(
                str(PROFILE_DIR),
                channel=channel,
                headless=headless,
                locale="zh-TW",
                timezone_id="Asia/Taipei",
                viewport={"width": 1280, "height": 900},
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            last_err = e
    raise RuntimeError(f"找不到可用的瀏覽器（Chrome/Edge/Chromium）：{last_err}")


def is_logged_in(context) -> bool:
    return any(c["name"] in LOGIN_COOKIES for c in context.cookies(THREADS_HOME))


def cookie_names(context):
    return sorted({c["name"] for c in context.cookies(THREADS_HOME)})


def close_quietly(context):
    """關閉瀏覽器；使用者已經自己關掉視窗時 close() 會丟例外，吞掉即可。"""
    try:
        context.close()
    except Exception:
        pass


def open_login_page(context):
    page = context.pages[0] if context.pages else context.new_page()
    try:
        page.goto(THREADS_LOGIN)
    except Exception:
        pass  # 登入頁可能馬上被導去 Instagram，goto 被打斷不算失敗，視窗照樣留著


def wait_for_login(context, timeout=LOGIN_WAIT_SECONDS, settle=5):
    """等使用者在視窗裡完成登入，回傳 "ok" / "closed"（視窗被關掉）/ "timeout"。

    只看整個瀏覽器的 cookie，不綁定某個分頁：登入要跨站跳轉、過驗證碼，
    也可能開新分頁，原本那個分頁被換掉或關掉都不代表登入失敗，視窗要繼續等。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if is_logged_in(context):
                time.sleep(settle)   # 多留幾秒讓登入後的跳轉跑完、cookie 寫進磁碟再關視窗
                return "ok"
            if not context.pages:
                return "closed"
        except Exception:
            return "closed"          # 整個瀏覽器已經被關掉
        time.sleep(2)
    return "timeout"


def block_login_redirect(context):
    """未登入收集時，擋掉 Threads 自動跳去 Instagram 登入頁的導向。

    未登入的頁面載入幾秒後會把整頁導去 Instagram SSO，貼文就讀不到了。
    把那個導向回成 204（無內容），瀏覽器會留在原頁。
    只能在未登入收集時用——登入流程本身要走 SSO，不能擋。
    """
    context.route("**/threads/sso/**", lambda route: route.fulfill(status=204))


def cmd_login():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = launch_context(p, headless=False)
        open_login_page(context)
        print("請在開啟的瀏覽器視窗中登入 Threads（用 Instagram 帳號，建議使用測試用副帳號）。")
        print(f"視窗會等您 {LOGIN_WAIT_SECONDS // 60} 分鐘；登入成功後程式會自動偵測並關閉，"
              "session 會保存在 browser_profile/。")
        result = wait_for_login(context)
        close_quietly(context)
    if result != "ok":
        print(f"⚠️ {LOGIN_FAILURES[result]}（python crawler.py login）")
        sys.exit(1)
    print("✅ 已偵測到登入，session 已保存。之後可直接執行 crawl。")


def ingest_embedded_json(page, collector):
    """初始載入時，第一批貼文直接嵌在 HTML 的 <script type=application/json> 裡。"""
    blobs = page.eval_on_selector_all(
        'script[type="application/json"]',
        "nodes => nodes.map(n => n.textContent)",
    )
    for blob in blobs:
        if blob and "thread_items" in blob:
            try:
                collector.ingest(json.loads(blob))
            except (json.JSONDecodeError, RecursionError):
                continue


def crawl_page(page, page_url: str, start_dt: datetime, end_dt: datetime,
               max_scrolls: int, author=None):
    collector = PostCollector(start_dt, end_dt, author=author)

    def on_response(response):
        if "/graphql" not in response.url:
            return
        try:
            collector.ingest_text(response.text())
        except Exception:
            pass

    page.on("response", on_response)
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        ingest_embedded_json(page, collector)

        stale_rounds = idle_rounds = 0
        prev_hits, prev_total = len(collector.target_posts()), len(collector.posts)
        for _ in range(max_scrolls):
            page.mouse.wheel(0, random.randint(2500, 4000))
            page.wait_for_timeout(random.randint(2000, 3500))

            hits, total = len(collector.target_posts()), len(collector.posts)
            if hits == prev_hits and collector.older_seen >= 3:
                # 時間窗內沒有新貼文、且已捲到更舊的貼文 → 連續 3 輪就停
                stale_rounds += 1
                if stale_rounds >= 3:
                    break
            else:
                stale_rounds = 0
            # 完全沒有新貼文進來 → 已經到底，或未登入被登入牆擋住，連續 4 輪就停
            idle_rounds = idle_rounds + 1 if total == prev_total else 0
            if idle_rounds >= 4:
                break
            prev_hits, prev_total = hits, total
    finally:
        page.remove_listener("response", on_response)

    return collector.target_posts()


def cmd_crawl(args):
    import threads_crawl  # 延後匯入：threads_crawl 本身會 import 這個模組

    cfg = threads_crawl.read_config()
    try:
        posts, _start_dt, end_dt = threads_crawl.crawl_targets(
            cfg.get("targets", []), max_scrolls=args.max_scrolls,
            hours=args.hours, headless=args.headless)
    except RuntimeError as e:
        print(f"❌ {e}")
        sys.exit(1)
    csv_name, _json_name = threads_crawl.save_outputs(posts, end_dt)
    print(f"\n✅ 共 {len(posts)} 篇貼文，已輸出 output/{csv_name}")


def main():
    parser = argparse.ArgumentParser(description="Threads 貼文收集器")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="開啟瀏覽器手動登入 Threads")

    crawl = sub.add_parser("crawl", help="收集 config.json 中所有目標的近期貼文")
    crawl.add_argument("--hours", type=int, default=24,
                       help="往回抓幾小時（預設 24）")
    crawl.add_argument("--max-scrolls", type=int, default=30,
                       help="每個目標最多捲動次數（預設 30）")
    crawl.add_argument("--headless", action="store_true",
                       help="無頭模式執行（不開視窗；登入帳號時較容易被偵測，預設關閉）")

    args = parser.parse_args()
    if args.command == "login":
        cmd_login()
    elif args.command == "crawl":
        cmd_crawl(args)


if __name__ == "__main__":
    main()
