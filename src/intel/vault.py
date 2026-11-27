"""Markdown vault (Obsidian-compatible): human-readable, linkable long-term memory."""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml

from .config import VAULT_DIR


def slug(text: str, n: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower()).strip()
    return re.sub(r"[\s_-]+", "-", s)[:n].strip("-") or "untitled"


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|#^\[\]]', "", name).strip()[:80] or "unnamed"


def elink(name: str) -> str:
    return f"[[Entities/{safe_name(name)}|{name}]]"


def _bullets(xs) -> str:
    return "\n".join(f"- {x}" for x in xs) if xs else "- _none_"


def _frontmatter(d: dict) -> str:
    return "---\n" + yaml.safe_dump(d, sort_keys=False, allow_unicode=True) + "---\n\n"


def write_insight(iid: int, cluster: dict, a: dict, items: list) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    path = VAULT_DIR / "Insights" / f"{today}-{iid}-{slug(a['headline'])}.md"
    fm = {
        "id": iid, "date": today, "cluster": cluster["id"], "category": a["category"],
        "signal_type": a["signal_type"], "impact": a["impact"], "confidence": a["confidence"],
        "time_horizon": a["time_horizon"], "tags": [slug(t, 30) for t in a.get("tags", [])],
    }
    opps = "\n".join(
        f"- **{o['title']}** ({o['effort']} effort) — {o['description']} _Model: {o['business_model']}_"
        for o in a.get("opportunities", [])
    ) or "- _none_"
    moves = a.get("ceo_moves", {})
    vc = a.get("value_chain", {})
    rels = "\n".join(f"- {elink(r['source'])} **{r['relation']}** {elink(r['target'])}" for r in a.get("relations", []))
    ents = ", ".join(elink(e["name"]) for e in a.get("entities", []))
    srcs = "\n".join(f"- [{it['title']}]({it['url']}) — {it['src']}" for it in items)
    body = f"""# {a['headline']}

**Impact {a['impact']}/10 · Confidence {a['confidence']:.0%} · Horizon: {a['time_horizon']} · {a['category']} / {a['signal_type']}**

## What happened
{a['summary']}

## Why it matters
{a['why_it_matters']}

## Hype check
{a['hype_check']}

## Value chain
**Winners:** {', '.join(vc.get('winners', [])) or '—'}
**Losers:** {', '.join(vc.get('losers', [])) or '—'}

## Opportunities
{opps}

## Threats
{_bullets(a.get('threats'))}

## CEO moves
**Now**
{_bullets(moves.get('now'))}

**Next 90 days**
{_bullets(moves.get('next_90_days'))}

**Next 12 months**
{_bullets(moves.get('next_12_months'))}

## Leading indicators to watch
{_bullets(a.get('leading_indicators'))}

## Connection to past signals
{a.get('connection_to_past') or '_No strong link to earlier insights._'}

## Entities
{ents or '_none_'}

## Relations
{rels or '_none_'}

## Sources
{srcs}
"""
    path.write_text(_frontmatter(fm) + body)
    return path


def write_entities(conn, min_mentions: int = 1) -> int:
    """Regenerate entity notes (graph nodes) with their relations and insight backlinks."""
    n = 0
    for e in conn.execute("SELECT * FROM entities WHERE mention_count >= ?", (min_mentions,)).fetchall():
        rels = conn.execute(
            """SELECT s.name src, r.rel, d.name dst FROM relations r
               JOIN entities s ON s.id=r.src_id JOIN entities d ON d.id=r.dst_id
               WHERE r.src_id=? OR r.dst_id=? GROUP BY s.id, r.rel, d.id ORDER BY MAX(r.created_at) DESC LIMIT 40""",
            (e["id"], e["id"]),
        ).fetchall()
        ins = conn.execute(
            """SELECT i.headline, i.created_at, i.note_path FROM insights i
               JOIN insight_entities ie ON ie.insight_id=i.id WHERE ie.entity_id=?
               ORDER BY i.created_at DESC LIMIT 40""",
            (e["id"],),
        ).fetchall()
        ins_links = "\n".join(
            f"- {r['created_at'][:10]} [[Insights/{Path(r['note_path']).stem}|{r['headline']}]]"
            for r in ins if r["note_path"]
        )
        rel_lines = "\n".join(f"- {elink(r['src'])} **{r['rel']}** {elink(r['dst'])}" for r in rels)
        fm = {"type": e["type"], "first_seen": e["first_seen"][:10], "last_seen": e["last_seen"][:10],
              "mentions": e["mention_count"]}
        text = _frontmatter(fm) + f"# {e['name']}\n\n{e['description'] or ''}\n\n## Relations\n{rel_lines or '_none_'}\n\n## Insights\n{ins_links or '_none_'}\n"
        (VAULT_DIR / "Entities" / f"{safe_name(e['name'])}.md").write_text(text)
        n += 1
    return n


def write_doc(folder: str, name: str, text: str, meta: dict | None = None) -> Path:
    path = VAULT_DIR / folder / f"{name}.md"
    path.write_text((_frontmatter(meta) if meta else "") + text)
    return path
