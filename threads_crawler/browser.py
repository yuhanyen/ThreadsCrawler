"""Playwright-driven crawler — the fallback when plain HTTP gets walled.

Threads renders the feed client-side past the first page, so the sturdiest way
to go deep is to drive a real browser, scroll, and read the GraphQL responses
it makes on our behalf.  Slower than :mod:`threads_crawler.client`, but it does
not depend on a hardcoded persisted-query id.
"""

from __future__ import annotations

import json
import os
from http.cookies import SimpleCookie

from . import parser
from .models import Post

BASE_URL = "https://www.threads.com"


class BrowserUnavailable(RuntimeError):
    pass


def _cookie_list(cookie: str) -> list[dict]:
    if os.path.exists(cookie):
        with open(cookie, encoding="utf-8") as handle:
            cookie = handle.read()
    cookies: list[dict] = []
    if "\t" in cookie:
        for line in cookie.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            fields = line.split("\t")
            if len(fields) >= 7:
                cookies.append(
                    {
                        "name": fields[5],
                        "value": fields[6],
                        "domain": fields[0],
                        "path": fields[2] or "/",
                    }
                )
        return cookies
    jar = SimpleCookie()
    jar.load(cookie.strip())
    for key, morsel in jar.items():
        cookies.append(
            {"name": key, "value": morsel.value, "domain": ".threads.com", "path": "/"}
        )
    return cookies


def crawl_profile(
    username: str,
    limit: int = 50,
    cookie: str = "",
    headless: bool = True,
    scroll_pause: float = 1.5,
    max_scrolls: int = 60,
    keep_raw: bool = False,
    on_warning=None,
) -> list[Post]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise BrowserUnavailable(
            "Playwright is not installed. Run: pip install playwright "
            "&& playwright install chromium"
        ) from exc

    username = username.strip().lstrip("@")
    posts: list[Post] = []
    seen: set[str] = set()

    def collect(payload) -> int:
        added = 0
        for post in parser.extract_posts(payload, keep_raw=keep_raw):
            if not post.id or post.id in seen:
                continue
            seen.add(post.id)
            posts.append(post)
            added += 1
        return added

    with sync_playwright() as play:
        launch_kwargs: dict = {"headless": headless}
        executable = os.environ.get("CHROMIUM_PATH")
        if executable:
            launch_kwargs["executable_path"] = executable
        browser = play.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport={"width": 1280, "height": 1600})
        if cookie or os.environ.get("THREADS_COOKIE"):
            context.add_cookies(_cookie_list(cookie or os.environ["THREADS_COOKIE"]))
        page = context.new_page()

        def on_response(response) -> None:
            if "/graphql" not in response.url:
                return
            try:
                collect(json.loads(response.text()))
            except Exception:  # noqa: BLE001 - a body we cannot read is not fatal
                pass

        page.on("response", on_response)
        page.goto(f"{BASE_URL}/@{username}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(int(scroll_pause * 1000))
        collect(page.content())

        stale_rounds = 0
        for _ in range(max_scrolls):
            if len(posts) >= limit:
                break
            before = len(posts)
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(int(scroll_pause * 1000))
            collect(page.content())
            if len(posts) == before:
                stale_rounds += 1
                if stale_rounds >= 3:
                    break
            else:
                stale_rounds = 0

        if len(posts) < limit and on_warning:
            on_warning(
                f"stopped with {len(posts)} posts — the feed stopped growing "
                "(end of profile, or a login wall)"
            )
        context.close()
        browser.close()

    return posts[:limit]
