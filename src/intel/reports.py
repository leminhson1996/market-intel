"""Stage 4: daily brief + weekly strategic memo, written to the vault."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from . import db, vault
from .llm import LLMError, ask
from .memory import top_entities

log = logging.getLogger(__name__)


def _insights_since(conn, since: str) -> list[dict]:
    rows = conn.execute(
        """SELECT id, headline, category, summary, why_it_matters, impact, confidence, time_horizon,
                  data, note_path, created_at FROM insights WHERE created_at >= datetime('now', ?)
           ORDER BY impact * confidence DESC""",
        (since,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["data"] = json.loads(d["data"] or "{}")
        out.append(d)
    return out


def rising_trends(conn, days: int = 7, limit: int = 10) -> list[dict]:
    """Clusters whose item count grew most over the window."""
    rows = conn.execute(
        """SELECT c.id, c.label, c.item_count, c.source_count, c.score,
                  c.item_count - COALESCE((SELECT item_count FROM trend_snapshots t
                     WHERE t.cluster_id=c.id AND t.snapshot_date <= date('now', ?)
                     ORDER BY snapshot_date DESC LIMIT 1), 0) AS growth
           FROM clusters c WHERE c.status='active' AND c.item_count >= 2
           ORDER BY growth DESC, c.score DESC LIMIT ?""",
        (f"-{days} days", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _link(i: dict) -> str:
    if i.get("note_path"):
        return f"[[Insights/{Path(i['note_path']).stem}|{i['headline']}]]"
    return i["headline"]


def daily_brief(use_llm: bool = True) -> Path | None:
    today = datetime.now().strftime("%Y-%m-%d")
    with db.connect() as conn:
        ins = _insights_since(conn, "-1 day")
        trends = rising_trends(conn, days=1)
        ents = top_entities(conn, days=7, limit=15)
    if not ins:
        log.info("no new insights for brief")
        return None

    exec_summary = ""
    if use_llm:
        digest = "\n".join(
            f"- [impact {i['impact']}] {i['headline']}: {i['why_it_matters']} | moves now: {'; '.join(i['data'].get('ceo_moves', {}).get('now', []))}"
            for i in ins[:20]
        )
        try:
            exec_summary = ask(
                "Here are today's analyzed market signals:\n" + digest +
                "\n\nWrite a CEO morning briefing in markdown (max 250 words): "
                "1) 'Top line' — 2-3 sentences on what matters most today; "
                "2) 'Big themes' — 2-4 bullets connecting signals into themes; "
                "3) 'Decide/act this week' — 3 concrete bullets. No preamble, no title.",
                system="You are a chief strategy officer writing a crisp daily briefing for a CEO.",
            )
        except LLMError as e:
            log.warning("brief summary failed: %s", e)

    lines = [f"# Daily Brief — {today}\n"]
    if exec_summary:
        lines.append(exec_summary.strip() + "\n")
    lines.append("## Top signals\n")
    lines.append("| # | Signal | Category | Impact | Conf. | Horizon |\n|---|---|---|---|---|---|")
    for n, i in enumerate(ins[:15], 1):
        lines.append(f"| {n} | {_link(i)} | {i['category']} | {i['impact']} | {i['confidence']:.0%} | {i['time_horizon']} |")
    lines.append("\n## Opportunities spotted\n")
    for i in ins[:10]:
        for o in i["data"].get("opportunities", [])[:2]:
            lines.append(f"- **{o['title']}** ({o['effort']}) — {o['description']} ← {_link(i)}")
    if trends:
        lines.append("\n## Fast-growing stories (24h)\n")
        for t in trends:
            if t["growth"] > 0:
                lines.append(f"- {t['label']} — +{t['growth']} items, {t['source_count']} sources")
    if ents:
        lines.append("\n## Most-mentioned entities (7d)\n")
        lines.append(", ".join(f"{vault.elink(e['name'])} ({e['n']})" for e in ents))
    return vault.write_doc("Briefs", today, "\n".join(lines) + "\n", {"date": today, "type": "daily-brief", "insights": len(ins)})


def weekly_memo() -> Path | None:
    today = datetime.now()
    name = f"{today:%G-W%V}"
    with db.connect() as conn:
        ins = _insights_since(conn, "-7 days")
        trends = rising_trends(conn, days=7, limit=15)
        ents = top_entities(conn, days=7, limit=25)
        prev = conn.execute(
            "SELECT headline FROM insights WHERE created_at BETWEEN datetime('now','-35 days') AND datetime('now','-7 days') ORDER BY impact DESC LIMIT 30"
        ).fetchall()
    if not ins:
        return None
    digest = "\n".join(
        f"- ({i['created_at'][:10]}, impact {i['impact']}, {i['category']}, {i['time_horizon']}) {i['headline']}: {i['why_it_matters']}"
        for i in ins[:60]
    )
    trend_txt = "\n".join(f"- {t['label']} (+{t['growth']} items, {t['source_count']} sources)" for t in trends)
    ent_txt = ", ".join(f"{e['name']} ({e['n']})" for e in ents)
    prev_txt = "\n".join(f"- {p['headline']}" for p in prev)
    memo = ask(
        f"""This week's analyzed signals:\n{digest}\n\nFastest-growing stories:\n{trend_txt}\n\n"""
        f"""Most-mentioned entities: {ent_txt}\n\nHigh-impact signals from the previous month (for continuity):\n{prev_txt}\n\n"""
        """Write a weekly strategic memo for the CEO in markdown with these sections:
## Executive summary (5 sentences max)
## Macro themes of the week (3-5 themes; for each: evidence, trajectory, strategic implication)
## Top 3 strategic bets (for each: thesis, why now, first step, cost of inaction, kill criteria)
## Threat radar (bullets)
## Trends gaining vs. fading (compare with previous month)
## Questions for the leadership team (3-5)
No title line.""",
        system="You are a chief strategy officer writing a weekly strategy memo for a CEO. Be specific and decisive.",
        timeout=400,
    )
    return vault.write_doc("Memos", name, f"# Weekly Strategic Memo — {name}\n\n{memo.strip()}\n", {"week": name, "type": "weekly-memo", "insights": len(ins)})
