"""Predict: on-demand forecasting over a chosen horizon, grounded in the memory store."""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime, timedelta

import yaml

from . import db, vault
from .config import company_profile, settings
from .llm import LLMError, ask
from .memory import search, top_entities
from .reports import rising_trends

log = logging.getLogger(__name__)

DEFAULT_MODELS = [
    {"id": "claude-opus-5-5", "label": "Opus 5.5", "note": "deepest reasoning, slowest"},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "note": "balanced (default)"},
    {"id": "claude-fable-5-2", "label": "Fable 5.2", "note": "alternative perspective"},
    {"id": "claude-haiku-5-5", "label": "Haiku 5.5", "note": "fastest, cheapest"},
]
HORIZONS = ["1 month", "3 months", "6 months", "1 year", "2 years", "3 years", "5 years"]

PREDICTIONS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY,
    created_at TEXT DEFAULT (datetime('now')),
    finished_at TEXT,
    topic TEXT,
    horizon TEXT NOT NULL,
    target_date TEXT,
    model TEXT NOT NULL,
    language TEXT DEFAULT 'en',
    status TEXT DEFAULT 'running',     -- running | done | failed
    title TEXT,
    data TEXT,
    context_ids TEXT,                  -- insight ids given as evidence
    error TEXT,
    note_path TEXT
);
"""

_STR_LIST = {"type": "array", "items": {"type": "string"}}
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short title for this forecast"},
        "executive_outlook": {"type": "string", "description": "4-6 sentence outlook for the CEO over the horizon"},
        "baseline_assumptions": {**_STR_LIST, "description": "What must stay true for the base case"},
        "predictions": {"type": "array", "minItems": 5, "maxItems": 12, "items": {"type": "object", "properties": {
            "statement": {"type": "string", "description": "Specific, falsifiable prediction"},
            "probability": {"type": "number", "minimum": 0, "maximum": 1},
            "by_when": {"type": "string", "description": "Date or window within the horizon, e.g. 'by Q2 2027'"},
            "domain": {"type": "string"},
            "rationale": {"type": "string", "description": "Reasoning chain from evidence to prediction"},
            "evidence_insight_ids": {"type": "array", "items": {"type": "integer"}},
            "signposts": {**_STR_LIST, "description": "Observable signals that would confirm it"},
            "invalidators": {**_STR_LIST, "description": "Observable signals that would falsify it"},
            "business_implication": {"type": "string"}},
            "required": ["statement", "probability", "by_when", "domain", "rationale", "evidence_insight_ids",
                         "signposts", "invalidators", "business_implication"]}},
        "scenarios": {"type": "array", "minItems": 3, "maxItems": 4, "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "probability": {"type": "number", "minimum": 0, "maximum": 1},
            "narrative": {"type": "string"}, "triggers": _STR_LIST, "winners": _STR_LIST, "losers": _STR_LIST,
            "ceo_response": {"type": "string"}},
            "required": ["name", "probability", "narrative", "triggers", "winners", "losers", "ceo_response"]}},
        "wildcards": {"type": "array", "items": {"type": "object", "properties": {
            "event": {"type": "string"}, "probability": {"type": "number", "minimum": 0, "maximum": 1},
            "impact": {"type": "string"}}, "required": ["event", "probability", "impact"]}},
        "ceo_playbook": {"type": "object", "properties": {
            "no_regret_moves": _STR_LIST, "bets_to_place": _STR_LIST, "hedges": _STR_LIST,
            "decision_points": {**_STR_LIST, "description": "Dated checkpoints where the CEO should revisit decisions"}},
            "required": ["no_regret_moves", "bets_to_place", "hedges", "decision_points"]},
        "key_uncertainties": _STR_LIST,
        "overall_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence_gaps": {**_STR_LIST, "description": "What the memory store lacks to forecast better"},
    },
    "required": ["title", "executive_outlook", "baseline_assumptions", "predictions", "scenarios", "wildcards",
                 "ceo_playbook", "key_uncertainties", "overall_confidence", "evidence_gaps"],
}

SYSTEM = """You are a superforecaster and chief strategy officer advising a CEO.
Forecast the future using the evidence provided plus your general world knowledge.
Rules: make predictions specific and falsifiable with calibrated probabilities (avoid 0.5 hedging and
avoid overconfidence); reason from base rates and current momentum; separate evidence from inference;
cite evidence by insight id when it supports a prediction; scenario probabilities should sum to ~1.
Be decisive and concrete - name companies, technologies, markets, and dates."""


def models() -> list[dict]:
    return settings().get("predict", {}).get("models") or DEFAULT_MODELS


def ensure_table(conn) -> None:
    conn.executescript(PREDICTIONS_SCHEMA_SQL)


def target_date(horizon: str) -> str:
    m = re.match(r"(\d+)\s*(day|week|month|year)", horizon.lower())
    if not m:
        return ""
    n, unit = int(m.group(1)), m.group(2)
    days = {"day": 1, "week": 7, "month": 30.4, "year": 365.25}[unit] * n
    return (date.today() + timedelta(days=round(days))).isoformat()


def _context(conn, topic: str | None) -> tuple[str, list[int]]:
    if topic:
        hits = search(conn, topic, 30, "all")
        ids = [h["id"] for h in hits if h["type"] == "insight"][:20]
        item_lines = [f"- ({(h.get('date') or '')[:10]}) {h['title']}: {h['summary'][:250]}"
                      for h in hits if h["type"] == "item"][:12]
    else:
        ids = [r[0] for r in conn.execute(
            """SELECT id FROM insights WHERE created_at >= datetime('now','-30 days')
               ORDER BY impact*confidence DESC LIMIT 25""")]
        item_lines = []
    ins_lines = []
    for iid in ids:
        r = conn.execute("SELECT id, created_at, category, impact, confidence, time_horizon, headline, "
                         "why_it_matters, data FROM insights WHERE id=?", (iid,)).fetchone()
        if not r:
            continue
        d = json.loads(r["data"] or "{}")
        ins_lines.append(
            f"[insight:{r['id']}] ({r['created_at'][:10]}, {r['category']}, impact {r['impact']}, "
            f"conf {r['confidence']:.0%}, horizon {r['time_horizon']}) {r['headline']}\n"
            f"  Why: {r['why_it_matters']}\n  Indicators: {'; '.join(d.get('leading_indicators', [])[:4])}")
    trends = rising_trends(conn, days=14, limit=15)
    ents = top_entities(conn, days=30, limit=25)
    past = conn.execute(
        """SELECT created_at, horizon, title, json_extract(data,'$.executive_outlook') o FROM predictions
           WHERE status='done' ORDER BY id DESC LIMIT 3""").fetchall()
    parts = ["## Strategic insights from memory", "\n".join(ins_lines) or "(none)"]
    if item_lines:
        parts += ["## Related raw signals", "\n".join(item_lines)]
    parts += ["## Momentum: fastest-growing stories (14d)",
              "\n".join(f"- {t['label']} (+{t['growth']} items, {t['source_count']} sources)" for t in trends) or "(none)",
              "## Most-mentioned entities (30d)", ", ".join(f"{e['name']} ({e['n']})" for e in ents) or "(none)"]
    if past:
        parts += ["## Our previous forecasts (stay consistent or explain changes)",
                  "\n".join(f"- {p['created_at'][:10]} [{p['horizon']}] {p['title']}: {(p['o'] or '')[:300]}" for p in past)]
    return "\n\n".join(parts), ids


def run_prediction(pid: int) -> None:
    """Execute a queued prediction row (blocking)."""
    with db.connect() as conn:
        p = dict(conn.execute("SELECT * FROM predictions WHERE id=?", (pid,)).fetchone())
        ctx, ids = _context(conn, p["topic"])
        conn.execute("UPDATE predictions SET context_ids=? WHERE id=?", (json.dumps(ids), pid))
    lang = "Vietnamese" if p["language"] == "vi" else "English"
    prompt = (
        f"Today is {date.today().isoformat()}. Forecast horizon: {p['horizon']} (until ~{p['target_date']}).\n"
        f"Focus: {p['topic'] or 'the overall technology & business landscape most relevant to the company'}\n\n"
        f"## Company lens\n{yaml.safe_dump(company_profile(), sort_keys=False, allow_unicode=True)}\n{ctx}\n\n"
        f"Produce the forecast. Write all text fields in {lang}."
    )
    timeout = settings().get("predict", {}).get("timeout_sec", 900)
    try:
        result = ask(prompt, schema=SCHEMA, system=SYSTEM, model=p["model"], timeout=timeout)
    except LLMError as e:
        with db.connect() as conn:
            conn.execute("UPDATE predictions SET status='failed', error=?, finished_at=datetime('now') WHERE id=?",
                         (str(e)[:1000], pid))
        log.error("prediction %s failed: %s", pid, e)
        return
    with db.connect() as conn:
        conn.execute("UPDATE predictions SET status='done', title=?, data=?, finished_at=datetime('now') WHERE id=?",
                     (result["title"], db.dumps(result), pid))
        p = dict(conn.execute("SELECT * FROM predictions WHERE id=?", (pid,)).fetchone())
    path = write_note(p, result)
    with db.connect() as conn:
        conn.execute("UPDATE predictions SET note_path=? WHERE id=?", (str(path), pid))
    try:
        from .gitsync import sync
        sync(f"prediction #{pid} ({p['horizon']}, {p['model']})")
    except Exception as e:
        log.warning("git sync after prediction failed: %s", e)


def create(topic: str | None, horizon: str, model: str, language: str = "en", background: bool = True) -> int:
    allowed = {m["id"] for m in models()}
    if model not in allowed:
        raise ValueError(f"unknown model {model}; choose from {sorted(allowed)}")
    with db.connect() as conn:
        ensure_table(conn)
        pid = conn.execute(
            "INSERT INTO predictions(topic, horizon, target_date, model, language) VALUES (?,?,?,?,?)",
            ((topic or "").strip() or None, horizon, target_date(horizon), model, language),
        ).lastrowid
    if background:
        threading.Thread(target=run_prediction, args=(pid,), daemon=True).start()
    else:
        run_prediction(pid)
    return pid


def get(pid: int) -> dict | None:
    with db.connect() as conn:
        ensure_table(conn)
        r = conn.execute("SELECT * FROM predictions WHERE id=?", (pid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(d["data"]) if d["data"] else None
    d["context_ids"] = json.loads(d["context_ids"]) if d["context_ids"] else []
    return d


def list_all(limit: int = 50) -> list[dict]:
    with db.connect() as conn:
        ensure_table(conn)
        return [dict(r) for r in conn.execute(
            """SELECT id, created_at, finished_at, topic, horizon, target_date, model, language, status, title,
                      json_extract(data,'$.overall_confidence') confidence, error
               FROM predictions ORDER BY id DESC LIMIT ?""", (limit,))]


def mark_stale_running() -> None:
    """Predictions left 'running' by a stopped server can never finish."""
    with db.connect() as conn:
        ensure_table(conn)
        conn.execute("UPDATE predictions SET status='failed', error='interrupted (server stopped)' "
                     "WHERE status='running' AND created_at < datetime('now','-30 minutes')")


def write_note(p: dict, a: dict):
    def bl(xs):
        return "\n".join(f"- {x}" for x in xs) if xs else "- _none_"
    preds = "\n\n".join(
        f"### {n}. {x['statement']}\n**Probability {x['probability']:.0%} · {x['by_when']} · {x['domain']}**\n\n"
        f"{x['rationale']}\n\n_Business implication:_ {x['business_implication']}\n\n"
        f"**Signposts:** {'; '.join(x['signposts'])}\n\n**Invalidators:** {'; '.join(x['invalidators'])}"
        + (f"\n\n_Evidence:_ insights {', '.join(map(str, x['evidence_insight_ids']))}" if x['evidence_insight_ids'] else "")
        for n, x in enumerate(a["predictions"], 1))
    scen = "\n\n".join(
        f"### {s['name']} ({s['probability']:.0%})\n{s['narrative']}\n\n**Triggers:** {'; '.join(s['triggers'])}\n\n"
        f"**Winners:** {', '.join(s['winners'])} · **Losers:** {', '.join(s['losers'])}\n\n**CEO response:** {s['ceo_response']}"
        for s in a["scenarios"])
    pb = a["ceo_playbook"]
    text = f"""# {a['title']}

**Horizon {p['horizon']} (to ~{p['target_date']}) · Model {p['model']} · Overall confidence {a['overall_confidence']:.0%}**
{('Focus: ' + p['topic']) if p['topic'] else 'Focus: overall landscape'}

## Outlook
{a['executive_outlook']}

## Baseline assumptions
{bl(a['baseline_assumptions'])}

## Predictions
{preds}

## Scenarios
{scen}

## Wildcards
{bl([f"{w['event']} ({w['probability']:.0%}) — {w['impact']}" for w in a['wildcards']])}

## CEO playbook
**No-regret moves**
{bl(pb['no_regret_moves'])}

**Bets to place**
{bl(pb['bets_to_place'])}

**Hedges**
{bl(pb['hedges'])}

**Decision points**
{bl(pb['decision_points'])}

## Key uncertainties
{bl(a['key_uncertainties'])}

## Evidence gaps
{bl(a['evidence_gaps'])}
"""
    name = f"{p['created_at'][:10]}-{p['id']}-{vault.slug(a['title'])}"
    meta = {"id": p["id"], "type": "prediction", "created": p["created_at"], "horizon": p["horizon"],
            "target_date": p["target_date"], "model": p["model"], "topic": p["topic"],
            "confidence": a["overall_confidence"]}
    (vault.VAULT_DIR / "Predictions").mkdir(exist_ok=True)
    return vault.write_doc("Predictions", name, text, meta)
