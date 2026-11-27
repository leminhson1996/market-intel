"""Source collectors. Each returns a list of raw item dicts:
{url, title, summary, author, published_at (ISO, UTC), engagement, extra}
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

USER_AGENT = "Mozilla/5.0 (Macintosh) intel-scanner/0.1"
TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid", "gclid", "mc_cid", "mc_eid"}


def http_client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=25, follow_redirects=True)


def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    query = urlencode([(k, v) for k, v in parse_qsl(p.query) if k.lower() not in TRACKING_PARAMS])
    netloc = p.netloc.lower().removeprefix("www.")
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower() or "https", netloc, path, "", query, ""))


def url_hash(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_text(text: str | None, limit: int = 1500) -> str:
    if not text:
        return ""
    text = html.unescape(_TAG_RE.sub(" ", text))
    return _WS_RE.sub(" ", text).strip()[:limit]


def iso(dt: datetime | None) -> str:
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_collector(kind: str):
    from . import arxiv, github, hackernews, reddit, rss

    return {
        "rss": rss.collect,
        "hackernews": hackernews.collect,
        "reddit": reddit.collect,
        "github": github.collect,
        "arxiv": arxiv.collect,
    }[kind]
