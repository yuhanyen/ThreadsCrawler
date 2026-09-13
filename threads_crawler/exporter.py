"""Serialise scraped posts to JSON, JSON Lines or CSV."""

from __future__ import annotations

import csv
import json
import sys
from typing import Iterable, TextIO

from .models import Post

CSV_COLUMNS = [
    "id",
    "code",
    "url",
    "username",
    "full_name",
    "published_at",
    "text",
    "like_count",
    "reply_count",
    "repost_count",
    "quote_count",
    "media",
    "link_url",
    "is_reply",
    "reply_to",
]


def _open(path: str | None) -> tuple[TextIO, bool]:
    if not path or path == "-":
        return sys.stdout, False
    return open(path, "w", encoding="utf-8", newline=""), True


def write(posts: Iterable[Post], fmt: str, path: str | None = None, include_raw: bool = False) -> int:
    handle, should_close = _open(path)
    count = 0
    try:
        if fmt == "jsonl":
            for post in posts:
                handle.write(json.dumps(post.to_dict(include_raw), ensure_ascii=False) + "\n")
                count += 1
        elif fmt == "csv":
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for post in posts:
                row = post.to_dict(include_raw=False)
                row["media"] = " ".join(m["url"] for m in row["media"])
                writer.writerow(row)
                count += 1
        else:  # json
            items = []
            for post in posts:
                items.append(post.to_dict(include_raw))
                count += 1
            json.dump(items, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    finally:
        if should_close:
            handle.close()
    return count
