"""MCP server exposing the intelligence memory to Claude Code (or any MCP client)."""
from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import db
from .config import VAULT_DIR
from .memory import entity_profile, get_insight, search, top_entities
from .reports import rising_trends

server = MCPServer(
    "market-intel",
    instructions=(
        "Local market-intelligence memory: news/tech signals collected daily, clustered into trends, "
        "analyzed from a CEO strategy lens, plus a knowledge graph of companies/technologies. "
        "Use search_memory first, then get_insight for full analysis."
    ),
)


@server.tool()
def search_memory(query: str, k: int = 10, kind: str = "all") -> list[dict]:
    """Hybrid keyword+semantic search. kind: 'insights' (strategic analyses), 'items' (raw articles) or 'all'."""
    with db.connect() as conn:
        return search(conn, query, k, kind)


@server.tool()
def get_insight_detail(insight_id: int) -> dict:
    """Full CEO analysis for an insight: opportunities, threats, CEO moves, entities, sources."""
    with db.connect() as conn:
        return get_insight(conn, insight_id) or {"error": "not found"}


@server.tool()
def recent_insights(days: int = 3, limit: int = 20, category: str | None = None) -> list[dict]:
    """Most impactful recent insights, optionally filtered by category (e.g. 'AI')."""
    sql = """SELECT id, created_at, category, impact, confidence, time_horizon, headline, why_it_matters
             FROM insights WHERE created_at >= datetime('now', ?)"""
    args: list = [f"-{days} days"]
    if category:
        sql += " AND category LIKE ?"
        args.append(f"%{category}%")
    sql += " ORDER BY impact*confidence DESC LIMIT ?"
    args.append(limit)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


@server.tool()
def get_trends(days: int = 7, limit: int = 15) -> list[dict]:
    """Fastest-growing story clusters over the last N days."""
    with db.connect() as conn:
        return rising_trends(conn, days, limit)


@server.tool()
def get_entity(name: str, depth: int = 1) -> dict:
    """Knowledge-graph view of a company/technology/person: relations, insights, neighborhood (depth>1)."""
    with db.connect() as conn:
        return entity_profile(conn, name, depth) or {"error": "not found"}


@server.tool()
def hot_entities(days: int = 7, limit: int = 25) -> list[dict]:
    """Entities mentioned most often in recent insights."""
    with db.connect() as conn:
        return top_entities(conn, days, limit)


@server.tool()
def latest_report(kind: str = "brief") -> str:
    """Latest daily brief ('brief') or weekly memo ('memo') as markdown."""
    folder = VAULT_DIR / ("Memos" if kind == "memo" else "Briefs")
    files = sorted(folder.glob("*.md"))
    return files[-1].read_text() if files else "No report yet."


def main() -> None:
    server.run("stdio")
