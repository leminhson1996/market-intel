"""Latest arXiv papers via the Atom API."""
from __future__ import annotations

from datetime import datetime, timezone

import feedparser

from . import clean_text, http_client, iso


def collect(src: dict) -> list[dict]:
    with http_client() as c:
        resp = c.get(src["url"])
        resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    items = []
    for e in feed.entries:
        t = e.get("published_parsed")
        items.append({
            "url": e.get("link", "").replace("http://", "https://"),
            "title": "Paper: " + clean_text(e.get("title"), 300),
            "summary": clean_text(e.get("summary"), 1200),
            "author": ", ".join(a.get("name", "") for a in e.get("authors", [])[:5]),
            "published_at": iso(datetime(*t[:6], tzinfo=timezone.utc) if t else None),
            "engagement": 0,
            "extra": {"categories": [x.get("term") for x in e.get("tags", [])]},
        })
    return items
