"""Data model for scraped Threads content."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Author:
    username: str = ""
    user_id: str = ""
    full_name: str = ""
    profile_pic_url: str = ""
    is_verified: bool = False


@dataclass
class Media:
    kind: str  # "image" | "video"
    url: str
    width: int = 0
    height: int = 0


@dataclass
class Post:
    id: str
    code: str = ""
    url: str = ""
    author: Author = field(default_factory=Author)
    text: str = ""
    published_at: datetime | None = None
    like_count: int = 0
    reply_count: int = 0
    repost_count: int = 0
    quote_count: int = 0
    media: list[Media] = field(default_factory=list)
    link_url: str = ""
    is_reply: bool = False
    reply_to: str = ""
    raw: dict | None = None

    def to_dict(self, include_raw: bool = False) -> dict:
        data = {
            "id": self.id,
            "code": self.code,
            "url": self.url,
            "username": self.author.username,
            "user_id": self.author.user_id,
            "full_name": self.author.full_name,
            "is_verified": self.author.is_verified,
            "text": self.text,
            "published_at": self.published_at.isoformat() if self.published_at else "",
            "like_count": self.like_count,
            "reply_count": self.reply_count,
            "repost_count": self.repost_count,
            "quote_count": self.quote_count,
            "media": [dataclasses.asdict(m) for m in self.media],
            "link_url": self.link_url,
            "is_reply": self.is_reply,
            "reply_to": self.reply_to,
        }
        if include_raw:
            data["raw"] = self.raw
        return data


def epoch_to_datetime(value) -> datetime | None:
    """Threads timestamps are epoch seconds; some payloads use microseconds."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 1e14:  # microseconds
        ts /= 1_000_000
    elif ts > 1e11:  # milliseconds
        ts /= 1000
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
