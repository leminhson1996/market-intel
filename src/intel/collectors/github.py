"""Fast-rising GitHub repositories (created in the last 7 days, sorted by stars)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from . import clean_text, http_client, iso


def collect(src: dict) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    params = {"q": f"created:>{since} stars:>50", "sort": "stars", "order": "desc", "per_page": src.get("limit", 30)}
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    with http_client() as c:
        resp = c.get(src["url"], params=params, headers=headers)
        resp.raise_for_status()
        repos = resp.json().get("items", [])
    items = []
    for r in repos:
        desc = clean_text(r.get("description"), 500)
        topics = r.get("topics", [])
        items.append({
            "url": r["html_url"],
            "title": f"GitHub: {r['full_name']} - {desc[:150]}" if desc else f"GitHub: {r['full_name']}",
            "summary": f"{desc} Language: {r.get('language')}. Topics: {', '.join(topics)}. Stars: {r.get('stargazers_count')}.",
            "author": r.get("owner", {}).get("login"),
            "published_at": iso(datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))),
            "engagement": r.get("stargazers_count", 0),
            "extra": {"language": r.get("language"), "topics": topics, "forks": r.get("forks_count")},
        })
    return items
