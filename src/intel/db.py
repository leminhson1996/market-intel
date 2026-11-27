"""SQLite storage: relational tables + FTS5 full-text + sqlite-vec vectors + graph tables."""
from __future__ import annotations

import json
import sqlite3

import numpy as np
import sqlite_vec

from .config import DB_PATH, ensure_dirs, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,
    url TEXT,
    weight REAL DEFAULT 1.0,
    category TEXT,
    last_fetched_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id),
    url TEXT NOT NULL,
    url_hash TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    summary TEXT,
    body TEXT,
    author TEXT,
    published_at TEXT,
    fetched_at TEXT DEFAULT (datetime('now')),
    engagement INTEGER DEFAULT 0,      -- HN points, reddit score, GitHub stars
    extra TEXT,                         -- JSON
    cluster_id INTEGER REFERENCES clusters(id),
    embedded INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_published ON items(published_at);
CREATE INDEX IF NOT EXISTS idx_items_cluster ON items(cluster_id);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    title, summary, content='items', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
    INSERT INTO items_fts(rowid, title, summary) VALUES (new.id, new.title, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
    INSERT INTO items_fts(items_fts, rowid, title, summary) VALUES ('delete', old.id, old.title, old.summary);
END;

-- A cluster = one story / emerging trend made of related items.
CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY,
    label TEXT,
    centroid BLOB,
    item_count INTEGER DEFAULT 0,
    source_count INTEGER DEFAULT 0,
    first_seen TEXT,
    last_seen TEXT,
    score REAL DEFAULT 0,
    analyzed_item_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',       -- active | dormant
    triage TEXT,                        -- relevant | skip (LLM pre-filter)
    triaged_item_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    norm_name TEXT UNIQUE NOT NULL,
    type TEXT,                          -- company | technology | person | product | regulation | market | other
    description TEXT,
    first_seen TEXT DEFAULT (datetime('now')),
    last_seen TEXT DEFAULT (datetime('now')),
    mention_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS relations (
    id INTEGER PRIMARY KEY,
    src_id INTEGER REFERENCES entities(id),
    dst_id INTEGER REFERENCES entities(id),
    rel TEXT NOT NULL,                  -- launches | acquires | invests_in | partners_with | competes_with | enables | regulates | uses | other
    insight_id INTEGER REFERENCES insights(id),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(src_id, dst_id, rel, insight_id)
);

CREATE TABLE IF NOT EXISTS insight_entities (
    insight_id INTEGER REFERENCES insights(id),
    entity_id INTEGER REFERENCES entities(id),
    PRIMARY KEY (insight_id, entity_id)
);

-- CEO strategic analysis of a cluster.
CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY,
    cluster_id INTEGER REFERENCES clusters(id),
    created_at TEXT DEFAULT (datetime('now')),
    headline TEXT,
    category TEXT,
    summary TEXT,
    why_it_matters TEXT,
    impact INTEGER,                     -- 1-10
    confidence REAL,                    -- 0-1
    time_horizon TEXT,                  -- now | 3-12 months | 1-3 years | 3+ years
    data TEXT,                          -- full JSON analysis
    feedback INTEGER DEFAULT 0,         -- +1 / -1 from user
    note_path TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS insights_fts USING fts5(
    headline, summary, why_it_matters, content='insights', content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS insights_ai AFTER INSERT ON insights BEGIN
    INSERT INTO insights_fts(rowid, headline, summary, why_it_matters)
    VALUES (new.id, new.headline, new.summary, new.why_it_matters);
END;

CREATE TABLE IF NOT EXISTS trend_snapshots (
    cluster_id INTEGER REFERENCES clusters(id),
    snapshot_date TEXT,
    item_count INTEGER,
    source_count INTEGER,
    score REAL,
    PRIMARY KEY (cluster_id, snapshot_date)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    stage TEXT,
    stats TEXT
);
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class EmbeddingMismatch(RuntimeError):
    pass


def init_db() -> None:
    emb = settings()["embedding"]
    dim = emb["dim"]
    with connect() as conn:
        conn.executescript(SCHEMA)
        stored = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('embed_model','embed_dim')").fetchall())
        has_vec = conn.execute("SELECT 1 FROM sqlite_master WHERE name='item_vec'").fetchone()
        if not stored and has_vec:
            stored = {"embed_model": "nomic-embed-text", "embed_dim": "768"}  # databases created before meta existed
        if stored and (stored.get("embed_model") != emb["model"] or int(stored.get("embed_dim", 0)) != dim):
            raise EmbeddingMismatch(
                f"Database vectors use {stored.get('embed_model')} ({stored.get('embed_dim')}d) but settings say "
                f"{emb['model']} ({dim}d). Run: uv run intel reembed")
        if not stored:
            set_embed_meta(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(clusters)")}
        if "triage" not in cols:
            conn.execute("ALTER TABLE clusters ADD COLUMN triage TEXT")
            conn.execute("ALTER TABLE clusters ADD COLUMN triaged_item_count INTEGER DEFAULT 0")
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS item_vec USING vec0(item_id INTEGER PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)"
        )
        conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS insight_vec USING vec0(insight_id INTEGER PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)"
        )


def set_embed_meta(conn) -> None:
    emb = settings()["embedding"]
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (emb["model"],))
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_dim', ?)", (str(emb["dim"]),))


def reembed(progress=print) -> dict:
    """Rebuild all vectors with the configured embedding model; recompute cluster centroids."""
    from .embed import embed_texts

    dim = settings()["embedding"]["dim"]
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.execute("DROP TABLE IF EXISTS item_vec")
        conn.execute("DROP TABLE IF EXISTS insight_vec")
        conn.execute(f"CREATE VIRTUAL TABLE item_vec USING vec0(item_id INTEGER PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)")
        conn.execute(f"CREATE VIRTUAL TABLE insight_vec USING vec0(insight_id INTEGER PRIMARY KEY, embedding float[{dim}] distance_metric=cosine)")
        items = conn.execute("SELECT id, title, summary FROM items WHERE embedded=1 OR cluster_id IS NOT NULL").fetchall()
        progress(f"embedding {len(items)} items…")
        vecs = embed_texts([f"{r['title']}. {r['summary'] or ''}" for r in items])
        for r, v in zip(items, vecs):
            conn.execute("INSERT INTO item_vec(item_id, embedding) VALUES (?,?)", (r["id"], to_blob(v)))
        ins = conn.execute("SELECT id, headline, summary, why_it_matters FROM insights").fetchall()
        progress(f"embedding {len(ins)} insights…")
        ivecs = embed_texts([f"{r['headline']}. {r['summary']} {r['why_it_matters']}" for r in ins]) if ins else []
        for r, v in zip(ins, ivecs):
            conn.execute("INSERT INTO insight_vec(insight_id, embedding) VALUES (?,?)", (r["id"], to_blob(v)))
        by_id = {r["id"]: v for r, v in zip(items, vecs)}
        n_clusters = 0
        for c in conn.execute("SELECT id FROM clusters").fetchall():
            members = [by_id[i[0]] for i in conn.execute("SELECT id FROM items WHERE cluster_id=?", (c["id"],)) if i[0] in by_id]
            if members:
                m = np.mean(members, axis=0)
                conn.execute("UPDATE clusters SET centroid=? WHERE id=?", (to_blob(m / np.linalg.norm(m)), c["id"]))
                n_clusters += 1
        set_embed_meta(conn)
    return {"items": len(items), "insights": len(ins), "clusters": n_clusters}


def to_blob(vec) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
