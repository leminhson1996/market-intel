"""Hacker News top stories via the official Firebase API."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import clean_text, http_client, iso


def collect(src: dict) -> list[dict]:
    base = src["url"]
    limit, min_score = src.get("limit", 60), src.get("min_score", 50)
    with http_client() as c:
        ids = c.get(f"{base}/topstories.json").json()[:limit]

        def fetch(i):
            try:
                return c.get(f"{base}/item/{i}.json").json()
            except Exception:
                return None

        with ThreadPoolExecutor(8) as ex:
            stories = list(ex.map(fetch, ids))
    items = []
    for s in stories:
        if not s or s.get("type") != "story" or s.get("score", 0) < min_score:
            continue
        hn_url = f"https://news.ycombinator.com/item?id={s['id']}"
        items.append({
            "url": s.get("url") or hn_url,
            "title": clean_text(s.get("title"), 300),
            "summary": clean_text(s.get("text")),
            "author": s.get("by"),
            "published_at": iso(datetime.fromtimestamp(s.get("time", 0), tz=timezone.utc)),
            "engagement": s.get("score", 0) + s.get("descendants", 0),
            "extra": {"hn_url": hn_url, "comments": s.get("descendants", 0)},
        })
    return items
