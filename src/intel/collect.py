"""Stage 1: fetch all sources, dedupe, store items + raw JSONL archive."""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import db
from .collectors import get_collector, url_hash
from .config import RAW_DIR, settings, sources

log = logging.getLogger(__name__)


def _upsert_source(conn, src: dict) -> int:
    conn.execute(
        """INSERT INTO sources(name, type, url, weight, category) VALUES (?,?,?,?,?)
           ON CONFLICT(name) DO UPDATE SET type=excluded.type, url=excluded.url,
             weight=excluded.weight, category=excluded.category""",
        (src["name"], src["type"], src.get("url"), src.get("weight", 1.0), src.get("category")),
    )
    return conn.execute("SELECT id FROM sources WHERE name=?", (src["name"],)).fetchone()[0]


def _run_one(src: dict) -> tuple[dict, list[dict], str | None]:
    try:
        return src, get_collector(src["type"])(src), None
    except Exception as e:  # one broken feed must not stop the run
        return src, [], f"{type(e).__name__}: {e}"[:500]


def _fetch_fulltext(url: str) -> str | None:
    import trafilatura

    try:
        downloaded = trafilatura.fetch_url(url)
        return trafilatura.extract(downloaded, include_comments=False)[:8000] if downloaded else None
    except Exception:
        return None


def collect_all(only: list[str] | None = None) -> dict:
    srcs = [s for s in sources() if not only or s["name"] in only]
    stats = {"sources": len(srcs), "fetched": 0, "new": 0, "errors": {}}
    fulltext = settings()["pipeline"].get("fetch_fulltext", False)
    now = datetime.now(timezone.utc)
    raw_path = RAW_DIR / f"{now:%Y-%m-%d}.jsonl"

    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(_run_one, srcs))
    noise = [re.compile(p, re.I) for p in settings().get("noise_patterns", [])]
    stats["filtered"] = 0

    with db.connect() as conn, open(raw_path, "a") as raw:
        for src, items, err in results:
            sid = _upsert_source(conn, src)
            conn.execute(
                "UPDATE sources SET last_fetched_at=datetime('now'), last_error=? WHERE id=?", (err, sid)
            )
            if err:
                stats["errors"][src["name"]] = err
                log.warning("source %s failed: %s", src["name"], err)
                continue
            stats["fetched"] += len(items)
            for it in items:
                if any(p.search(it["title"]) for p in noise):
                    stats["filtered"] += 1
                    continue
                h = url_hash(it["url"])
                cur = conn.execute(
                    """INSERT OR IGNORE INTO items(source_id, url, url_hash, title, summary, author,
                         published_at, engagement, extra) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (sid, it["url"], h, it["title"], it["summary"], it.get("author"),
                     it["published_at"], it.get("engagement", 0), db.dumps(it.get("extra", {}))),
                )
                if cur.rowcount:
                    stats["new"] += 1
                    raw.write(json.dumps({"source": src["name"], **it}, ensure_ascii=False) + "\n")
                    if fulltext:
                        if body := _fetch_fulltext(it["url"]):
                            conn.execute("UPDATE items SET body=? WHERE id=?", (body, cur.lastrowid))
                else:
                    # refresh engagement for already-known items (momentum signal)
                    conn.execute(
                        "UPDATE items SET engagement=MAX(engagement, ?) WHERE url_hash=?",
                        (it.get("engagement", 0), h),
                    )
    return stats
