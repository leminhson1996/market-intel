"""Reddit subreddit top posts via public JSON endpoints."""
from __future__ import annotations

from datetime import datetime, timezone

from . import clean_text, http_client, iso


def collect(src: dict) -> list[dict]:
    with http_client() as c:
        resp = c.get(src["url"])
        resp.raise_for_status()
        data = resp.json()
    items = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {})
        if p.get("stickied") or p.get("over_18"):
            continue
        permalink = f"https://www.reddit.com{p.get('permalink', '')}"
        url = p.get("url_overridden_by_dest") or permalink
        items.append({
            "url": url,
            "title": clean_text(p.get("title"), 300),
            "summary": clean_text(p.get("selftext")),
            "author": p.get("author"),
            "published_at": iso(datetime.fromtimestamp(p.get("created_utc", 0), tz=timezone.utc)),
            "engagement": p.get("score", 0) + p.get("num_comments", 0),
            "extra": {"subreddit": p.get("subreddit"), "permalink": permalink},
        })
    return items
