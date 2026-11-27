"""Generic RSS/Atom collector."""
from __future__ import annotations

from datetime import datetime, timezone

import feedparser

from . import clean_text, http_client, iso


def _entry_time(e) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        t = e.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def collect(src: dict) -> list[dict]:
    with http_client() as c:
        resp = c.get(src["url"])
        resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries[: src.get("limit", 50)]:
        if not e.get("link") or not e.get("title"):
            continue
        summary = e.get("summary") or ""
        if not summary and e.get("content"):
            summary = e["content"][0].get("value", "")
        items.append({
            "url": e["link"],
            "title": clean_text(e["title"], 300),
            "summary": clean_text(summary),
            "author": e.get("author"),
            "published_at": iso(_entry_time(e)),
            "engagement": 0,
            "extra": {"tags": [t.get("term") for t in e.get("tags", []) if t.get("term")][:10]},
        })
    return items
