"""Stage 2: embed new items, cluster them into stories/trends, score clusters."""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

import numpy as np

from . import db
from .config import company_profile, settings
from .embed import embed_texts

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else _utcnow()


def embed_new(conn) -> int:
    lookback = settings()["pipeline"]["lookback_days"]
    since = (_utcnow() - timedelta(days=lookback)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT id, title, summary FROM items WHERE embedded=0 AND published_at >= ?", (since,)
    ).fetchall()
    if not rows:
        return 0
    vecs = embed_texts([f"{r['title']}. {r['summary'] or ''}" for r in rows])
    for r, v in zip(rows, vecs):
        conn.execute("INSERT OR REPLACE INTO item_vec(item_id, embedding) VALUES (?, ?)", (r["id"], db.to_blob(v)))
        conn.execute("UPDATE items SET embedded=1 WHERE id=?", (r["id"],))
    return len(rows)


def cluster_new(conn) -> dict:
    p = settings()["pipeline"]
    threshold, active_days = p["cluster_threshold"], p["cluster_active_days"]
    cutoff = (_utcnow() - timedelta(days=active_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("UPDATE clusters SET status='dormant' WHERE status='active' AND last_seen < ?", (cutoff,))

    clusters = conn.execute("SELECT id, centroid, item_count FROM clusters WHERE status='active'").fetchall()
    ids = [c["id"] for c in clusters]
    counts = [c["item_count"] for c in clusters]
    cents = [db.from_blob(c["centroid"]).copy() for c in clusters]

    new_items = conn.execute(
        """SELECT i.id, v.embedding FROM items i JOIN item_vec v ON v.item_id = i.id
           WHERE i.cluster_id IS NULL ORDER BY i.published_at"""
    ).fetchall()
    touched, created = set(), 0
    for it in new_items:
        vec = db.from_blob(it["embedding"])
        best, best_sim = -1, -1.0
        if cents:
            sims = np.stack(cents) @ vec
            best = int(np.argmax(sims))
            best_sim = float(sims[best])
        if best_sim >= threshold:
            n = counts[best]
            c = cents[best] * n + vec
            cents[best] = c / np.linalg.norm(c)
            counts[best] = n + 1
            cid = ids[best]
        else:
            cid = conn.execute(
                "INSERT INTO clusters(centroid, first_seen, last_seen) VALUES (?, datetime('now'), datetime('now'))",
                (db.to_blob(vec),),
            ).lastrowid
            ids.append(cid); counts.append(1); cents.append(vec.copy())
            created += 1
        conn.execute("UPDATE items SET cluster_id=? WHERE id=?", (cid, it["id"]))
        touched.add(cid)

    for idx, cid in enumerate(ids):
        if cid in touched:
            conn.execute("UPDATE clusters SET centroid=? WHERE id=?", (db.to_blob(cents[idx]), cid))
    _refresh_cluster_stats(conn, touched)
    return {"assigned": len(new_items), "new_clusters": created, "touched": len(touched)}


def _refresh_cluster_stats(conn, cluster_ids) -> None:
    for cid in cluster_ids:
        s = conn.execute(
            """SELECT COUNT(*) n, COUNT(DISTINCT source_id) ns, MIN(published_at) f, MAX(published_at) l
               FROM items WHERE cluster_id=?""",
            (cid,),
        ).fetchone()
        label = conn.execute(
            """SELECT i.title FROM items i JOIN sources s ON s.id=i.source_id WHERE cluster_id=?
               ORDER BY s.weight * (1 + log(1 + i.engagement)) DESC LIMIT 1""",
            (cid,),
        ).fetchone()
        conn.execute(
            "UPDATE clusters SET item_count=?, source_count=?, first_seen=?, last_seen=?, label=? WHERE id=?",
            (s["n"], s["ns"], s["f"], s["l"], label["title"] if label else None, cid),
        )


def _relevance_terms() -> list[str]:
    prof = company_profile()
    terms = []
    for key in ("industries", "markets", "strengths", "goals", "watch_topics"):
        terms += [str(t).lower() for t in (prof.get(key) or [])]
    return terms


def score_clusters(conn) -> int:
    """score = (breadth + volume + engagement + velocity) * source_weight * recency * relevance * feedback"""
    terms = _relevance_terms()
    now = _utcnow()
    rows = conn.execute(
        """SELECT c.id, c.item_count, c.source_count, c.last_seen,
                  AVG(s.weight) w, SUM(i.engagement) eng,
                  SUM(CASE WHEN i.fetched_at >= datetime('now','-1 day') THEN 1 ELSE 0 END) recent,
                  GROUP_CONCAT(i.title, ' | ') titles,
                  (SELECT COALESCE(SUM(feedback),0) FROM insights WHERE cluster_id=c.id) fb
           FROM clusters c JOIN items i ON i.cluster_id=c.id JOIN sources s ON s.id=i.source_id
           WHERE c.status='active' GROUP BY c.id"""
    ).fetchall()
    today = now.strftime("%Y-%m-%d")
    for r in rows:
        breadth = 2.0 * math.log2(1 + r["source_count"])
        volume = math.log2(1 + r["item_count"])
        engagement = math.log10(1 + (r["eng"] or 0))
        velocity = math.log2(1 + (r["recent"] or 0))
        hours = max(0.0, (now - _parse(r["last_seen"])).total_seconds() / 3600)
        recency = math.exp(-hours / 72)
        titles = (r["titles"] or "").lower()
        relevance = 1.0 + 0.5 * min(3, sum(1 for t in terms if t in titles))
        feedback = max(0.3, 1.0 + 0.3 * (r["fb"] or 0))
        score = (breadth + volume + engagement + velocity) * (r["w"] or 1.0) * recency * relevance * feedback
        conn.execute("UPDATE clusters SET score=? WHERE id=?", (round(score, 3), r["id"]))
        conn.execute(
            "INSERT OR REPLACE INTO trend_snapshots VALUES (?,?,?,?,?)",
            (r["id"], today, r["item_count"], r["source_count"], round(score, 3)),
        )
    return len(rows)


def enrich() -> dict:
    with db.connect() as conn:
        embedded = embed_new(conn)
        cl = cluster_new(conn)
        scored = score_clusters(conn)
    return {"embedded": embedded, **cl, "scored": scored}
