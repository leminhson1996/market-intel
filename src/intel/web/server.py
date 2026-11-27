"""Local web UI: JSON API over the memory store + a single-page dashboard."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .. import db
from ..config import LOG_DIR, ROOT, VAULT_DIR
from ..memory import entity_profile, get_insight, search, top_entities
from ..reports import rising_trends

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="Market Intel")


def rows(sql: str, args=()) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------- overview

@app.get("/api/overview")
def overview(days: int = 1):
    with db.connect() as conn:
        counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("items", "clusters", "insights", "entities", "relations")}
        counts["items_24h"] = conn.execute(
            "SELECT COUNT(*) FROM items WHERE fetched_at >= datetime('now','-1 day')").fetchone()[0]
        counts["insights_24h"] = conn.execute(
            "SELECT COUNT(*) FROM insights WHERE created_at >= datetime('now','-1 day')").fetchone()[0]
        last_run = conn.execute(
            "SELECT finished_at, stage FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        categories = [dict(r) for r in conn.execute(
            """SELECT category, COUNT(*) n, ROUND(AVG(impact),1) avg_impact FROM insights
               WHERE created_at >= datetime('now','-7 days') GROUP BY category ORDER BY n DESC""")]
        top = [dict(r) for r in conn.execute(
            """SELECT id, headline, category, impact, confidence, time_horizon, why_it_matters, created_at
               FROM insights WHERE created_at >= datetime('now', ?) ORDER BY impact*confidence DESC LIMIT 8""",
            (f"-{days} days",))]
        trends = rising_trends(conn, days=7, limit=8)
        ents = top_entities(conn, days=7, limit=20)
    briefs = sorted((VAULT_DIR / "Briefs").glob("*.md"))
    brief = _strip_fm(briefs[-1].read_text()) if briefs else ""
    return {"counts": counts, "last_run": dict(last_run) if last_run else None, "categories": categories,
            "top": top, "trends": trends, "entities": ents, "brief": brief,
            "brief_name": briefs[-1].stem if briefs else None}


def _strip_fm(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


# ---------------------------------------------------------------- insights

@app.get("/api/insights")
def list_insights(days: int = 30, category: str = "", min_impact: int = 0, horizon: str = "",
                  q: str = "", limit: int = 200):
    sql = """SELECT id, headline, category, impact, confidence, time_horizon, why_it_matters,
                    created_at, feedback, json_extract(data,'$.signal_type') signal_type
             FROM insights WHERE created_at >= datetime('now', ?) AND impact >= ?"""
    args: list = [f"-{days} days", min_impact]
    if category:
        sql += " AND category = ?"; args.append(category)
    if horizon:
        sql += " AND time_horizon = ?"; args.append(horizon)
    if q:
        sql += " AND (headline LIKE ? OR summary LIKE ? OR why_it_matters LIKE ?)"; args += [f"%{q}%"] * 3
    sql += " ORDER BY created_at DESC, impact*confidence DESC LIMIT ?"; args.append(limit)
    return {"insights": rows(sql, args),
            "categories": [r["category"] for r in rows("SELECT DISTINCT category FROM insights ORDER BY 1")]}


@app.get("/api/insights/{iid}")
def insight_detail(iid: int):
    with db.connect() as conn:
        d = get_insight(conn, iid)
        if not d:
            raise HTTPException(404)
        d["entities"] = [dict(r) for r in conn.execute(
            """SELECT e.name, e.type FROM insight_entities ie JOIN entities e ON e.id=ie.entity_id
               WHERE ie.insight_id=?""", (iid,))]
    return d


class Vote(BaseModel):
    vote: int


@app.post("/api/insights/{iid}/feedback")
def feedback(iid: int, v: Vote):
    with db.connect() as conn:
        conn.execute("UPDATE insights SET feedback=feedback+? WHERE id=?", (1 if v.vote > 0 else -1, iid))
        fb = conn.execute("SELECT feedback FROM insights WHERE id=?", (iid,)).fetchone()
    return {"feedback": fb[0] if fb else 0}


# ---------------------------------------------------------------- trends

@app.get("/api/trends")
def trends(days: int = 7, limit: int = 30):
    with db.connect() as conn:
        out = rising_trends(conn, days, limit)
        for t in out:
            t["history"] = [dict(r) for r in conn.execute(
                "SELECT snapshot_date d, item_count n, score FROM trend_snapshots WHERE cluster_id=? ORDER BY d",
                (t["id"],))]
            ins = conn.execute(
                "SELECT id FROM insights WHERE cluster_id=? ORDER BY created_at DESC LIMIT 1", (t["id"],)).fetchone()
            t["insight_id"] = ins[0] if ins else None
            t["items"] = [dict(r) for r in conn.execute(
                """SELECT i.title, i.url, s.name source FROM items i JOIN sources s ON s.id=i.source_id
                   WHERE cluster_id=? ORDER BY published_at DESC LIMIT 6""", (t["id"],))]
    return out


# ---------------------------------------------------------------- graph

@app.get("/api/graph")
def graph(days: int = 30, limit: int = 120):
    nodes = rows(
        """SELECT e.id, e.name, e.type, e.mention_count FROM entities e
           WHERE e.last_seen >= datetime('now', ?) ORDER BY e.mention_count DESC LIMIT ?""",
        (f"-{days} days", limit))
    ids = {n["id"] for n in nodes}
    edges = [e for e in rows(
        """SELECT src_id, dst_id, rel, COUNT(*) n FROM relations GROUP BY src_id, dst_id, rel""")
        if e["src_id"] in ids and e["dst_id"] in ids]
    return {"nodes": nodes, "edges": edges}


@app.get("/api/entity")
def entity(name: str):
    with db.connect() as conn:
        p = entity_profile(conn, name, depth=2)
    if not p:
        raise HTTPException(404)
    return p


# ---------------------------------------------------------------- search & ask

@app.get("/api/search")
def do_search(q: str, kind: str = "all", k: int = 20):
    with db.connect() as conn:
        return search(conn, q, k, kind)


class Question(BaseModel):
    question: str


@app.post("/api/ask")
def ask(body: Question):
    from ..llm import LLMError, ask as llm_ask
    with db.connect() as conn:
        res = search(conn, body.question, 15)
    ctx = "\n".join(f"[{r['type']}:{r['id']}] ({(r.get('date') or '')[:10]}) {r['title']}: {r['summary']}" for r in res)
    try:
        answer = llm_ask(
            f"Memory excerpts:\n{ctx}\n\nQuestion: {body.question}\n\n"
            "Answer as a strategy advisor to a CEO, using only the memory excerpts (say if they are insufficient). "
            "Reply in the same language as the question (e.g. Vietnamese question -> Vietnamese answer). "
            "Use markdown. Cite as [insight:ID] or [item:ID].",
            system="You answer from a market-intelligence memory store.",
        )
    except LLMError as e:
        raise HTTPException(502, str(e))
    return {"answer": answer, "sources": res}


@app.get("/api/items/{iid}")
def item(iid: int):
    r = rows("""SELECT i.*, s.name source FROM items i JOIN sources s ON s.id=i.source_id WHERE i.id=?""", (iid,))
    if not r:
        raise HTTPException(404)
    return r[0]


# ---------------------------------------------------------------- reports & sources

@app.get("/api/reports")
def reports():
    def ls(folder):
        return [p.stem for p in sorted((VAULT_DIR / folder).glob("*.md"), reverse=True)]
    return {"briefs": ls("Briefs"), "memos": ls("Memos")}


@app.get("/api/reports/{kind}/{name}")
def report(kind: str, name: str):
    folder = {"briefs": "Briefs", "memos": "Memos"}.get(kind)
    path = VAULT_DIR / (folder or "") / f"{name}.md"
    if not folder or not path.is_file() or path.parent != VAULT_DIR / folder:
        raise HTTPException(404)
    return {"name": name, "markdown": _strip_fm(path.read_text())}


@app.get("/api/sources")
def sources_health():
    return rows(
        """SELECT s.name, s.type, s.category, s.weight, s.last_fetched_at, s.last_error,
                  COUNT(i.id) items,
                  SUM(CASE WHEN i.fetched_at >= datetime('now','-1 day') THEN 1 ELSE 0 END) items_24h
           FROM sources s LEFT JOIN items i ON i.source_id=s.id GROUP BY s.id ORDER BY items DESC""")


# ---------------------------------------------------------------- predictions

class PredictReq(BaseModel):
    topic: str = ""
    horizon: str = "1 year"
    model: str = ""
    language: str = "en"


@app.get("/api/predict/options")
def predict_options():
    from .. import predict
    from ..config import settings
    return {"models": predict.models(), "horizons": predict.HORIZONS,
            "default_model": settings().get("predict", {}).get("default_model", "claude-sonnet-5-5")}


@app.get("/api/predictions")
def predictions_list():
    from .. import predict
    return predict.list_all()


@app.post("/api/predictions")
def predictions_create(req: PredictReq):
    from .. import predict
    if not req.horizon.strip():
        raise HTTPException(400, "horizon required")
    try:
        pid = predict.create(req.topic, req.horizon.strip(), req.model or predict_options()["default_model"],
                             "vi" if req.language == "vi" else "en")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return predict.get(pid)


@app.get("/api/predictions/{pid}")
def predictions_get(pid: int):
    from .. import predict
    p = predict.get(pid)
    if not p:
        raise HTTPException(404)
    return p


# ---------------------------------------------------------------- pipeline runner

_run = {"proc": None, "log": deque(maxlen=200), "started": None, "cmd": None, "code": None}


def _pump(proc):
    for line in proc.stdout:
        _run["log"].append(line.rstrip())
    _run["code"] = proc.wait()


class RunReq(BaseModel):
    stage: str = "run"


@app.post("/api/run")
def start_run(req: RunReq):
    if req.stage not in ("run", "collect", "enrich", "analyze", "brief", "weekly"):
        raise HTTPException(400, "bad stage")
    p = _run["proc"]
    if p and p.poll() is None:
        raise HTTPException(409, "already running")
    _run["log"].clear()
    _run.update(started=datetime.now().isoformat(timespec="seconds"), cmd=req.stage, code=None)
    proc = subprocess.Popen([sys.executable, "-m", "intel.cli", req.stage], cwd=ROOT,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    _run["proc"] = proc
    threading.Thread(target=_pump, args=(proc,), daemon=True).start()
    return run_status()


@app.get("/api/run")
def run_status():
    p = _run["proc"]
    return {"running": bool(p and p.poll() is None), "stage": _run["cmd"], "started": _run["started"],
            "exit_code": _run["code"], "log": list(_run["log"])[-60:]}


def serve(host: str = "127.0.0.1", port: int = 8787, open_browser: bool = True) -> None:
    import uvicorn
    from ..predict import mark_stale_running
    mark_stale_running()
    if open_browser:
        threading.Timer(1.2, lambda: subprocess.run(["open", f"http://{host}:{port}"], check=False)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
