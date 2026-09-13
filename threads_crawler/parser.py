"""Extract posts out of Threads payloads.

Threads server-renders its pages and ships the data as JSON inside
``<script type="application/json">`` blocks, and serves further pages from a
GraphQL endpoint.  Both carry the same post shape, so everything here works on
"some decoded JSON" rather than on a fixed document layout: we walk the tree
and pick up anything that looks like a post.  That survives the frequent
reshuffles of the surrounding wrapper structure.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterator

from .models import Author, Media, Post, epoch_to_datetime

BASE_URL = "https://www.threads.com"

_SCRIPT_RE = re.compile(
    r'<script[^>]+type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
_LSD_RE = re.compile(r'"LSD"\s*,\s*\[\s*\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"')
_LSD_FALLBACK_RE = re.compile(r'"lsd"\s*:\s*\{?\s*"?token"?\s*:?\s*"([^"]+)"')
_USER_ID_RE = re.compile(r'"user_id":"(\d+)"|"props":\{"user_id":"(\d+)"')


def iter_embedded_json(html: str) -> Iterator[Any]:
    """Yield every decoded ``application/json`` script block of a page."""
    for match in _SCRIPT_RE.finditer(html):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            yield json.loads(raw)
        except json.JSONDecodeError:
            continue


def extract_lsd_token(html: str) -> str:
    """The CSRF-ish token the GraphQL endpoint requires on every request."""
    match = _LSD_RE.search(html) or _LSD_FALLBACK_RE.search(html)
    return match.group(1) if match else ""


def extract_user_id(html: str, username: str = "") -> str:
    """Numeric profile id, needed as the GraphQL pagination key.

    Read it off a post by that author first — that survives key reordering in
    the payload — then fall back to scanning the raw HTML.
    """
    username = username.strip().lstrip("@")
    if username:
        for post in extract_posts(html):
            if post.author.username.lower() == username.lower() and post.author.user_id:
                return post.author.user_id
        # Proximity search: the id sits next to the username, either side of it.
        for match in re.finditer(r'"username"\s*:\s*"%s"' % re.escape(username), html):
            window = html[max(0, match.start() - 400) : match.end() + 400]
            found = re.search(r'"(?:pk|pk_id|id)"\s*:\s*"(\d+)"', window)
            if found:
                return found.group(1)
    match = re.search(r'"user_id"\s*:\s*"(\d+)"', html)
    if match:
        return match.group(1)
    match = _USER_ID_RE.search(html)
    if match:
        return match.group(1) or match.group(2)
    return ""


def _looks_like_post(node: dict) -> bool:
    if not isinstance(node.get("user"), dict):
        return False
    if not (node.get("pk") or node.get("id")):
        return False
    return any(
        key in node for key in ("caption", "text_post_app_info", "code", "taken_at")
    )


def walk_posts(node: Any, _depth: int = 0) -> Iterator[dict]:
    """Depth-first walk yielding every post-shaped dict inside ``node``."""
    if _depth > 200:
        return
    if isinstance(node, dict):
        if _looks_like_post(node):
            yield node
        for value in node.values():
            yield from walk_posts(value, _depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from walk_posts(value, _depth + 1)
    elif isinstance(node, str):
        # Some payloads nest a JSON document as a plain string value.
        stripped = node.strip()
        if (
            len(stripped) > 40
            and stripped.startswith(("{", "["))
            and ('"thread_items"' in stripped or '"text_post_app_info"' in stripped)
        ):
            try:
                decoded = json.loads(stripped)
            except json.JSONDecodeError:
                return
            yield from walk_posts(decoded, _depth + 1)


def _parse_author(node: dict) -> Author:
    user = node.get("user") or {}
    return Author(
        username=user.get("username") or "",
        user_id=str(user.get("pk") or user.get("id") or ""),
        full_name=user.get("full_name") or "",
        profile_pic_url=user.get("profile_pic_url") or "",
        is_verified=bool(user.get("is_verified")),
    )


def _parse_media(node: dict) -> list[Media]:
    media: list[Media] = []

    def add_image(item: dict) -> None:
        candidates = (item.get("image_versions2") or {}).get("candidates") or []
        if not candidates:
            return
        best = max(candidates, key=lambda c: (c.get("width") or 0) * (c.get("height") or 0))
        if best.get("url"):
            media.append(
                Media(
                    kind="image",
                    url=best["url"],
                    width=best.get("width") or 0,
                    height=best.get("height") or 0,
                )
            )

    def add_video(item: dict) -> None:
        for version in item.get("video_versions") or []:
            if version.get("url"):
                media.append(
                    Media(
                        kind="video",
                        url=version["url"],
                        width=version.get("width") or 0,
                        height=version.get("height") or 0,
                    )
                )
                break

    items = node.get("carousel_media") or [node]
    for item in items:
        if not isinstance(item, dict):
            continue
        add_video(item)
        add_image(item)
    return media


def parse_post(node: dict, keep_raw: bool = False) -> Post:
    """Turn one raw post dict into a :class:`Post`."""
    app_info = node.get("text_post_app_info") or {}
    caption = node.get("caption") or {}
    author = _parse_author(node)
    code = node.get("code") or ""

    link_preview = app_info.get("link_preview_attachment") or {}
    reply_to = ""
    replied = app_info.get("reply_to_author") or {}
    if isinstance(replied, dict):
        reply_to = replied.get("username") or ""

    return Post(
        id=str(node.get("pk") or node.get("id") or ""),
        code=code,
        url=f"{BASE_URL}/@{author.username}/post/{code}" if code and author.username else "",
        author=author,
        text=(caption.get("text") if isinstance(caption, dict) else "") or "",
        published_at=epoch_to_datetime(node.get("taken_at")),
        like_count=int(node.get("like_count") or 0),
        reply_count=int(app_info.get("direct_reply_count") or 0),
        repost_count=int(app_info.get("repost_count") or node.get("repost_count") or 0),
        quote_count=int(app_info.get("quote_count") or 0),
        media=_parse_media(node),
        link_url=link_preview.get("url") or "",
        is_reply=bool(app_info.get("is_reply") or reply_to),
        reply_to=reply_to,
        raw=node if keep_raw else None,
    )


def extract_posts(payload: Any, keep_raw: bool = False) -> list[Post]:
    """Collect every post in a page's HTML or in a decoded GraphQL response.

    Results keep document order and are de-duplicated by post id.
    """
    sources: list[Any]
    if isinstance(payload, str):
        sources = list(iter_embedded_json(payload))
    else:
        sources = [payload]

    posts: list[Post] = []
    seen: set[str] = set()
    for source in sources:
        for node in walk_posts(source):
            post = parse_post(node, keep_raw=keep_raw)
            if not post.id or post.id in seen:
                continue
            seen.add(post.id)
            posts.append(post)
    return posts


def extract_page_info(payload: Any) -> tuple[str, bool]:
    """Return ``(end_cursor, has_next_page)`` from a GraphQL response."""
    found: list[tuple[str, bool]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 200 or found:
            return
        if isinstance(node, dict):
            if "end_cursor" in node or "has_next_page" in node:
                found.append(
                    (node.get("end_cursor") or "", bool(node.get("has_next_page")))
                )
                return
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, list):
            for value in node:
                walk(value, depth + 1)

    walk(payload)
    return found[0] if found else ("", False)
