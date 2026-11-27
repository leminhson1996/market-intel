"""Stage 3: CEO-lens strategic analysis of top clusters, with memory of past insights."""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import yaml

from . import db, vault
from .embed import embed_one
from .llm import LLMError, ask
from .memory import add_relation, related_insights_by_vec, upsert_entity
from .config import company_profile, settings

log = logging.getLogger(__name__)

SYSTEM = """You are the chief strategy officer and market-intelligence analyst for a CEO.
You turn raw news and signals into sharp, actionable business strategy.
Be concrete and specific (name companies, products, numbers when present). Separate fact from
speculation. Call out hype honestly. Never invent facts that are not supported by the sources;
when you infer, say so. Write in concise, executive-ready English."""

_STR_LIST = {"type": "array", "items": {"type": "string"}}

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "description": "One-line executive headline (max ~15 words)"},
        "category": {"type": "string", "description": "Primary domain, e.g. AI, Semiconductors, Fintech, Energy, Biotech, Consumer Tech, Cloud, Security, Policy, Macro"},
        "signal_type": {"type": "string", "enum": ["new_technology", "product_launch", "funding_or_mna", "regulation_policy", "market_shift", "competitive_move", "research_breakthrough", "macro_geopolitics", "other"]},
        "summary": {"type": "string", "description": "What happened, 2-4 sentences, facts only"},
        "why_it_matters": {"type": "string", "description": "The 'so what' for business leaders, 2-4 sentences"},
        "impact": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Business impact potential"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "time_horizon": {"type": "string", "enum": ["now", "3-12 months", "1-3 years", "3+ years"]},
        "hype_check": {"type": "string", "description": "Honest assessment of substance vs hype"},
        "value_chain": {"type": "object", "properties": {"winners": _STR_LIST, "losers": _STR_LIST}, "required": ["winners", "losers"]},
        "opportunities": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "description": {"type": "string"},
            "business_model": {"type": "string"}, "effort": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["title", "description", "business_model", "effort"]}},
        "threats": _STR_LIST,
        "ceo_moves": {"type": "object", "properties": {"now": _STR_LIST, "next_90_days": _STR_LIST, "next_12_months": _STR_LIST},
                      "required": ["now", "next_90_days", "next_12_months"]},
        "leading_indicators": {**_STR_LIST, "description": "What to watch to confirm/refute this trend"},
        "connection_to_past": {"type": "string", "description": "How this relates to the past insights provided (empty if none relevant)"},
        "entities": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"},
            "type": {"type": "string", "enum": ["company", "technology", "product", "person", "regulation", "market", "organization", "other"]},
            "description": {"type": "string"}}, "required": ["name", "type"]}},
        "relations": {"type": "array", "items": {"type": "object", "properties": {
            "source": {"type": "string"},
            "relation": {"type": "string", "enum": ["launches", "acquires", "invests_in", "partners_with", "competes_with", "enables", "regulates", "uses", "sues", "hires", "other"]},
            "target": {"type": "string"}}, "required": ["source", "relation", "target"]}},
        "tags": _STR_LIST,
    },
    "required": ["headline", "category", "signal_type", "summary", "why_it_matters", "impact", "confidence",
                 "time_horizon", "hype_check", "value_chain", "opportunities", "threats", "ceo_moves",
                 "leading_indicators", "connection_to_past", "entities", "relations", "tags"],
}


TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"relevant_ids": {"type": "array", "items": {"type": "integer"},
                                    "description": "IDs of clusters with real strategic value for a CEO"}},
    "required": ["relevant_ids"],
}


def triage(conn, clusters: list[dict]) -> None:
    """One cheap LLM call: drop gossip, deals, lifestyle, trivia before deep analysis."""
    if not clusters:
        return
    lines = []
    for c in clusters:
        titles = [r[0] for r in conn.execute(
            "SELECT title FROM items WHERE cluster_id=? ORDER BY engagement DESC LIMIT 3", (c["id"],))]
        lines.append(f"{c['id']}: " + " | ".join(titles))
    prompt = (
        "Company lens:\n" + yaml.safe_dump(company_profile(), sort_keys=False) +
        "\nBelow are news/signal clusters (id: sample titles). Select the ones with genuine strategic value "
        "for a CEO: new technologies, market shifts, competitive moves, funding/M&A, regulation, "
        "macro/geopolitics affecting business, notable research, important security incidents. "
        "Exclude: product reviews, deals, celebrity/gossip, lifestyle, sports, personal blog trivia, "
        "minor bug reports, how-to tutorials.\n\n" + "\n".join(lines)
    )
    try:
        keep = set(ask(prompt, schema=TRIAGE_SCHEMA, timeout=180)["relevant_ids"])
    except (LLMError, KeyError) as e:
        log.warning("triage failed, analyzing without it: %s", e)
        keep = {c["id"] for c in clusters}
    for c in clusters:
        conn.execute(
            "UPDATE clusters SET triage=?, triaged_item_count=item_count WHERE id=?",
            ("relevant" if c["id"] in keep else "skip", c["id"]),
        )


def candidates(conn, top_n: int) -> list[dict]:
    growth = settings()["pipeline"]["reanalyze_growth"]
    needs_analysis = "(analyzed_item_count = 0 OR item_count - analyzed_item_count >= :g)"
    pool = conn.execute(
        f"""SELECT * FROM clusters WHERE status='active' AND {needs_analysis}
              AND (triage IS NULL OR item_count - triaged_item_count >= :g)
            ORDER BY score DESC LIMIT :n""",
        {"g": growth, "n": top_n * 3},
    ).fetchall()
    triage(conn, [dict(r) for r in pool])
    rows = conn.execute(
        f"""SELECT * FROM clusters WHERE status='active' AND triage='relevant' AND {needs_analysis}
            ORDER BY score DESC LIMIT :n""",
        {"g": growth, "n": top_n},
    ).fetchall()
    return [dict(r) for r in rows]


def _build_prompt(conn, cluster: dict) -> tuple[str, list]:
    items = conn.execute(
        """SELECT i.title, i.summary, i.body, i.url, i.published_at, i.engagement, s.name src
           FROM items i JOIN sources s ON s.id=i.source_id WHERE i.cluster_id=?
           ORDER BY s.weight * (1 + log(1 + i.engagement)) DESC LIMIT 12""",
        (cluster["id"],),
    ).fetchall()
    past = related_insights_by_vec(conn, db.from_blob(cluster["centroid"]), k=5, exclude_cluster=cluster["id"])
    prev_self = conn.execute(
        "SELECT headline, summary, created_at FROM insights WHERE cluster_id=? ORDER BY created_at DESC LIMIT 1",
        (cluster["id"],),
    ).fetchone()

    lines = [
        "## Company lens",
        yaml.safe_dump(company_profile(), sort_keys=False, allow_unicode=True),
        f"## Signal cluster (#{cluster['id']}): {cluster['item_count']} items from {cluster['source_count']} sources",
    ]
    for n, it in enumerate(items, 1):
        text = (it["body"] or it["summary"] or "")[:1200]
        lines.append(f"[{n}] ({it['src']}, {it['published_at'][:10]}, engagement {it['engagement']}) {it['title']}\n{text}\nURL: {it['url']}")
    if prev_self:
        lines.append(f"\n## Our previous analysis of this same story ({prev_self['created_at'][:10]})\n{prev_self['headline']}: {prev_self['summary']}\nUpdate it with what is new.")
    if past:
        lines.append("\n## Related past insights from memory")
        for p in past:
            lines.append(f"- ({p['created_at'][:10]}) {p['headline']}: {(p['summary'] or '')[:300]}")
    lines.append("\nAnalyze this signal for the CEO. Return the structured analysis.")
    return "\n\n".join(lines), [dict(i) for i in items]


def _store(conn, cluster: dict, a: dict, items: list) -> int:
    iid = conn.execute(
        """INSERT INTO insights(cluster_id, headline, category, summary, why_it_matters, impact,
             confidence, time_horizon, data) VALUES (?,?,?,?,?,?,?,?,?)""",
        (cluster["id"], a["headline"], a["category"], a["summary"], a["why_it_matters"],
         int(a["impact"]), float(a["confidence"]), a["time_horizon"], db.dumps(a)),
    ).lastrowid
    vec = embed_one(f"{a['headline']}. {a['summary']} {a['why_it_matters']}")
    conn.execute("INSERT INTO insight_vec(insight_id, embedding) VALUES (?,?)", (iid, db.to_blob(vec)))
    for e in a.get("entities", []):
        if e.get("name"):
            eid = upsert_entity(conn, e["name"], e.get("type"), e.get("description"))
            conn.execute("INSERT OR IGNORE INTO insight_entities VALUES (?,?)", (iid, eid))
    for r in a.get("relations", []):
        if r.get("source") and r.get("target"):
            add_relation(conn, r["source"], r["relation"], r["target"], iid)
    conn.execute(
        "UPDATE clusters SET analyzed_item_count=item_count, label=? WHERE id=?", (a["headline"], cluster["id"])
    )
    path = vault.write_insight(iid, cluster, a, items)
    conn.execute("UPDATE insights SET note_path=? WHERE id=?", (str(path), iid))
    return iid


def analyze(top_n: int | None = None) -> dict:
    cfg = settings()
    top_n = top_n or cfg["pipeline"]["analyze_top_n"]
    with db.connect() as conn:
        todo = candidates(conn, top_n)
        prompts = [(c, *_build_prompt(conn, c)) for c in todo]

    done, failed = [], []

    def run(job):
        cluster, prompt, items = job
        return cluster, items, ask(prompt, schema=SCHEMA, system=SYSTEM)

    with ThreadPoolExecutor(cfg["llm"].get("parallel", 3)) as ex:
        futs = [ex.submit(run, j) for j in prompts]
        for f in as_completed(futs):
            try:
                cluster, items, result = f.result()
            except LLMError as e:
                log.error("analysis failed: %s", e)
                failed.append(str(e)[:200])
                continue
            with db.connect() as conn:
                done.append(_store(conn, cluster, result, items))
            log.info("analyzed cluster %s -> insight %s", cluster["id"], done[-1])
    if done:
        with db.connect() as conn:
            vault.write_entities(conn)
    return {"analyzed": len(done), "failed": len(failed), "insight_ids": done, "errors": failed[:3]}
