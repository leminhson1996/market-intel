"""Memory layer: knowledge graph ops + hybrid (full-text + vector) retrieval."""
from __future__ import annotations

import json
import re

import networkx as nx

from . import db
from .embed import embed_one

# ---------------------------------------------------------------- graph

def norm_name(name: str) -> str:
    n = re.sub(r"[^\w\s.+-]", "", name.lower()).strip()
    n = re.sub(r"\s+(inc|corp|corporation|ltd|llc|co)\.?$", "", n)
    return re.sub(r"\s+", " ", n)


def upsert_entity(conn, name: str, etype: str | None = None, description: str | None = None) -> int:
    nn = norm_name(name)
    row = conn.execute("SELECT id FROM entities WHERE norm_name=?", (nn,)).fetchone()
    if row:
        conn.execute(
            """UPDATE entities SET last_seen=datetime('now'), mention_count=mention_count+1,
                 type=COALESCE(type, ?), description=COALESCE(description, ?) WHERE id=?""",
            (etype, description, row["id"]),
        )
        return row["id"]
    return conn.execute(
        "INSERT INTO entities(name, norm_name, type, description, mention_count) VALUES (?,?,?,?,1)",
        (name.strip(), nn, etype, description),
    ).lastrowid


def add_relation(conn, src: str, rel: str, dst: str, insight_id: int) -> None:
    s, d = upsert_entity(conn, src), upsert_entity(conn, dst)
    if s != d:
        conn.execute(
            "INSERT OR IGNORE INTO relations(src_id, dst_id, rel, insight_id) VALUES (?,?,?,?)",
            (s, d, rel.lower().replace(" ", "_"), insight_id),
        )


def load_graph(conn) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    for e in conn.execute("SELECT id, name, type, mention_count FROM entities"):
        g.add_node(e["id"], name=e["name"], type=e["type"], mentions=e["mention_count"])
    for r in conn.execute("SELECT src_id, dst_id, rel, insight_id, created_at FROM relations"):
        g.add_edge(r["src_id"], r["dst_id"], rel=r["rel"], insight_id=r["insight_id"], at=r["created_at"])
    return g


def entity_profile(conn, name: str, depth: int = 1) -> dict | None:
    row = conn.execute("SELECT * FROM entities WHERE norm_name=?", (norm_name(name),)).fetchone()
    if not row:
        row = conn.execute(
            "SELECT * FROM entities WHERE norm_name LIKE ? ORDER BY mention_count DESC LIMIT 1",
            (f"%{norm_name(name)}%",),
        ).fetchone()
    if not row:
        return None
    eid = row["id"]
    rels = conn.execute(
        """SELECT s.name src, r.rel, d.name dst, r.created_at, i.headline
           FROM relations r JOIN entities s ON s.id=r.src_id JOIN entities d ON d.id=r.dst_id
           LEFT JOIN insights i ON i.id=r.insight_id
           WHERE r.src_id=? OR r.dst_id=? ORDER BY r.created_at DESC LIMIT 50""",
        (eid, eid),
    ).fetchall()
    insights = conn.execute(
        """SELECT i.id, i.headline, i.created_at, i.impact FROM insights i
           JOIN insight_entities ie ON ie.insight_id=i.id WHERE ie.entity_id=?
           ORDER BY i.created_at DESC LIMIT 20""",
        (eid,),
    ).fetchall()
    out = {**dict(row), "relations": [dict(r) for r in rels], "insights": [dict(i) for i in insights]}
    if depth > 1:
        g = load_graph(conn).to_undirected()
        if eid in g:
            near = nx.single_source_shortest_path_length(g, eid, cutoff=depth)
            out["neighborhood"] = sorted(
                (g.nodes[n]["name"] for n, d in near.items() if 0 < d <= depth), key=str.lower
            )[:100]
    return out


def top_entities(conn, days: int = 7, limit: int = 25) -> list[dict]:
    rows = conn.execute(
        """SELECT e.name, e.type, COUNT(*) n FROM insight_entities ie
           JOIN entities e ON e.id=ie.entity_id JOIN insights i ON i.id=ie.insight_id
           WHERE i.created_at >= datetime('now', ?) GROUP BY e.id ORDER BY n DESC LIMIT ?""",
        (f"-{days} days", limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- retrieval

def _fts_query(q: str) -> str:
    words = re.findall(r"\w{2,}", q.lower())
    return " OR ".join(f'"{w}"' for w in words) or '""'


def related_insights_by_vec(conn, vec, k: int = 5, exclude_cluster: int | None = None) -> list[dict]:
    rows = conn.execute(
        """SELECT i.id, i.headline, i.summary, i.created_at, i.cluster_id, v.distance
           FROM insight_vec v JOIN insights i ON i.id = v.insight_id
           WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance""",
        (db.to_blob(vec), k + 5),
    ).fetchall()
    return [dict(r) for r in rows if r["cluster_id"] != exclude_cluster and r["distance"] < 0.5][:k]


def search(conn, query: str, k: int = 10, kind: str = "all") -> list[dict]:
    """Hybrid search with reciprocal rank fusion over FTS5 + vector results.
    kind: 'insights' | 'items' | 'all'"""
    qvec = embed_one(query, kind="query")
    results: dict[tuple, dict] = {}

    def fuse(key, payload, rank):
        r = results.setdefault(key, {**payload, "score": 0.0})
        r["score"] += 1.0 / (60 + rank)

    fq = _fts_query(query)
    if kind in ("insights", "all"):
        for rank, r in enumerate(conn.execute(
            """SELECT i.id, i.headline title, i.summary, i.created_at date FROM insights_fts f
               JOIN insights i ON i.id=f.rowid WHERE insights_fts MATCH ? ORDER BY rank LIMIT 30""", (fq,))):
            fuse(("insight", r["id"]), {"type": "insight", **dict(r)}, rank)
        for rank, r in enumerate(conn.execute(
            """SELECT i.id, i.headline title, i.summary, i.created_at date FROM insight_vec v
               JOIN insights i ON i.id=v.insight_id WHERE v.embedding MATCH ? AND k=30 ORDER BY v.distance""",
            (db.to_blob(qvec),))):
            fuse(("insight", r["id"]), {"type": "insight", **dict(r)}, rank)
    if kind in ("items", "all"):
        for rank, r in enumerate(conn.execute(
            """SELECT i.id, i.title, i.summary, i.published_at date, i.url FROM items_fts f
               JOIN items i ON i.id=f.rowid WHERE items_fts MATCH ? ORDER BY rank LIMIT 30""", (fq,))):
            fuse(("item", r["id"]), {"type": "item", **dict(r)}, rank)
        for rank, r in enumerate(conn.execute(
            """SELECT i.id, i.title, i.summary, i.published_at date, i.url FROM item_vec v
               JOIN items i ON i.id=v.item_id WHERE v.embedding MATCH ? AND k=30 ORDER BY v.distance""",
            (db.to_blob(qvec),))):
            fuse(("item", r["id"]), {"type": "item", **dict(r)}, rank)
    ranked = sorted(results.values(), key=lambda x: -x["score"])[:k]
    for r in ranked:
        r["summary"] = (r.get("summary") or "")[:400]
    return ranked


def get_insight(conn, insight_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM insights WHERE id=?", (insight_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["data"] = json.loads(d["data"] or "{}")
    d["sources"] = [dict(r) for r in conn.execute(
        """SELECT i.title, i.url, s.name source FROM items i JOIN sources s ON s.id=i.source_id
           WHERE i.cluster_id=? ORDER BY i.engagement DESC LIMIT 15""", (row["cluster_id"],))]
    return d
