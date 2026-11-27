"""`intel` command line."""
from __future__ import annotations

import json
import logging
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table

from . import db
from .config import DB_PATH, LOG_DIR, RAW_DIR, ROOT, ensure_dirs, settings

app = typer.Typer(help="Market intelligence: scan, analyze, remember.", no_args_is_help=True)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    ensure_dirs()
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(LOG_DIR / "intel.log"), logging.StreamHandler(sys.stderr)],
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _record_run(stage: str, stats: dict, started: float) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO runs(started_at, finished_at, stage, stats) VALUES (datetime(?, 'unixepoch'), datetime('now'), ?, ?)",
            (int(started), stage, db.dumps(stats)),
        )


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")):
    _setup_logging(verbose)


@app.command()
def init():
    """Create database, vault folders, and check dependencies."""
    db.init_db()
    console.print(f"[green]✓[/] database at {DB_PATH}")
    try:
        from .embed import embed_one
        embed_one("test")
        console.print("[green]✓[/] Ollama embeddings working")
    except Exception as e:
        console.print(f"[red]✗[/] Ollama embeddings: {e}\n  run: ollama pull {settings()['embedding']['model']}")
    from .llm import ask
    try:
        ask("Reply with OK", timeout=90)
        console.print("[green]✓[/] claude -p working")
    except Exception as e:
        console.print(f"[red]✗[/] claude -p: {e}")


@app.command()
def collect(source: list[str] = typer.Option(None, "--source", "-s", help="Only these source names")):
    """Fetch all sources."""
    from .collect import collect_all
    db.init_db()
    t = time.time()
    stats = collect_all(source)
    _record_run("collect", stats, t)
    console.print(stats)


@app.command()
def enrich():
    """Embed, cluster and score new items."""
    from .enrich import enrich as run
    t = time.time()
    stats = run()
    _record_run("enrich", stats, t)
    console.print(stats)


@app.command()
def analyze(top: int = typer.Option(None, help="Number of clusters to analyze")):
    """CEO-lens analysis of top clusters with Claude."""
    from .analyze import analyze as run
    t = time.time()
    stats = run(top)
    _record_run("analyze", stats, t)
    console.print(stats)


@app.command()
def brief(no_llm: bool = False):
    """Write today's daily brief."""
    from .reports import daily_brief
    p = daily_brief(use_llm=not no_llm)
    console.print(f"[green]brief:[/] {p}" if p else "[yellow]no new insights[/]")


@app.command()
def weekly():
    """Write the weekly strategic memo."""
    from .reports import weekly_memo
    p = weekly_memo()
    console.print(f"[green]memo:[/] {p}" if p else "[yellow]no insights this week[/]")


@app.command()
def run(top: int = typer.Option(None, help="Clusters to analyze"), skip_analyze: bool = False):
    """Full pipeline: collect → enrich → analyze → brief."""
    from .analyze import analyze as do_analyze
    from .collect import collect_all
    from .enrich import enrich as do_enrich
    from .reports import daily_brief

    db.init_db()
    log = logging.getLogger("intel.run")
    t = time.time()
    stats = {"collect": collect_all()}
    log.info("collect: %s", {k: v for k, v in stats["collect"].items() if k != "errors"})
    stats["enrich"] = do_enrich()
    log.info("enrich: %s", stats["enrich"])
    if not skip_analyze:
        stats["analyze"] = do_analyze(top)
        log.info("analyze: %s", stats["analyze"])
        p = daily_brief()
        stats["brief"] = str(p) if p else None
    prune()
    _record_run("run", stats, t)
    console.print(stats)
    _notify("Market intel", f"{stats.get('analyze', {}).get('analyzed', 0)} new insights. Brief ready.")


def _notify(title: str, msg: str) -> None:
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "{title}"'], check=False)


@app.command()
def search(query: str, k: int = typer.Option(10, "-k"), kind: str = typer.Option("all", help="insights | items | all")):
    """Hybrid (keyword + semantic) search over memory."""
    from .memory import search as do_search
    with db.connect() as conn:
        res = do_search(conn, query, k, kind)
    t = Table("type", "id", "date", "title", "score")
    for r in res:
        t.add_row(r["type"], str(r["id"]), (r.get("date") or "")[:10], r["title"][:90], f"{r['score']:.3f}")
    console.print(t)


@app.command()
def ask(question: str, k: int = 15):
    """Ask a question; Claude answers from stored memory with citations."""
    from .llm import ask as llm_ask
    from .memory import search as do_search
    with db.connect() as conn:
        res = do_search(conn, question, k)
    ctx = "\n".join(
        f"[{r['type']}:{r['id']}] ({(r.get('date') or '')[:10]}) {r['title']}: {r['summary']}" for r in res
    )
    answer = llm_ask(
        f"Memory excerpts:\n{ctx}\n\nQuestion: {question}\n\n"
        "Answer as a strategy advisor to a CEO, using only the memory excerpts (say if they are insufficient). "
        "Cite as [insight:ID] or [item:ID].",
        system="You answer from a market-intelligence memory store.",
    )
    console.print(Markdown(answer))


@app.command()
def entity(name: str, depth: int = 1):
    """Show an entity's graph neighborhood and insights."""
    from .memory import entity_profile
    with db.connect() as conn:
        p = entity_profile(conn, name, depth)
    if not p:
        console.print("[yellow]not found[/]")
        raise typer.Exit(1)
    console.print(f"[bold]{p['name']}[/] ({p['type']}) — mentions {p['mention_count']}\n{p['description'] or ''}")
    for r in p["relations"][:25]:
        console.print(escape(f"  {r['src']} —[{r['rel']}]→ {r['dst']}"))
    for i in p["insights"][:10]:
        console.print(escape(f"  • {i['created_at'][:10]} [{i['id']}] {i['headline']}"))
    if p.get("neighborhood"):
        console.print("Neighborhood: " + ", ".join(p["neighborhood"]))


@app.command()
def trends(days: int = 7, limit: int = 20):
    """Rising stories/trends."""
    from .reports import rising_trends
    with db.connect() as conn:
        rows = rising_trends(conn, days, limit)
    t = Table("cluster", "label", "items", "sources", "growth", "score")
    for r in rows:
        t.add_row(str(r["id"]), (r["label"] or "")[:80], str(r["item_count"]), str(r["source_count"]), f"+{r['growth']}", f"{r['score']:.2f}")
    console.print(t)


@app.command()
def insights(days: int = 1, limit: int = 20):
    """List recent insights."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, category, impact, confidence, headline FROM insights WHERE created_at >= datetime('now', ?) ORDER BY impact*confidence DESC LIMIT ?",
            (f"-{days} days", limit),
        ).fetchall()
    t = Table("id", "date", "category", "impact", "conf", "headline")
    for r in rows:
        t.add_row(str(r["id"]), r["created_at"][:10], r["category"], str(r["impact"]), f"{r['confidence']:.0%}", r["headline"])
    console.print(t)


@app.command()
def feedback(insight_id: int, vote: str = typer.Argument(..., help="up | down")):
    """Rate an insight; future scoring of that story adapts."""
    v = 1 if vote.lower() in ("up", "+", "+1", "good") else -1
    with db.connect() as conn:
        conn.execute("UPDATE insights SET feedback=feedback+? WHERE id=?", (v, insight_id))
    console.print("[green]recorded[/]")


@app.command()
def stats():
    """Database and source health."""
    with db.connect() as conn:
        for tbl in ("items", "clusters", "insights", "entities", "relations"):
            console.print(f"{tbl:10} {conn.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]}")
        t = Table("source", "items", "last fetched", "error")
        for r in conn.execute(
            """SELECT s.name, COUNT(i.id) n, s.last_fetched_at, s.last_error FROM sources s
               LEFT JOIN items i ON i.source_id=s.id GROUP BY s.id ORDER BY n DESC"""
        ):
            t.add_row(r["name"], str(r["n"]), r["last_fetched_at"] or "", (r["last_error"] or "")[:60])
        console.print(t)


@app.command()
def prune():
    """Drop raw bodies and archives older than retention.raw_days (summaries & vectors kept)."""
    days = settings().get("retention", {}).get("raw_days", 90)
    with db.connect() as conn:
        conn.execute("UPDATE items SET body=NULL WHERE fetched_at < datetime('now', ?)", (f"-{days} days",))
    cutoff = time.time() - days * 86400
    for f in RAW_DIR.glob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            f.unlink()


@app.command()
def mcp():
    """Run the MCP server (stdio) so Claude Code can query memory."""
    from .mcp_server import main as run_mcp
    run_mcp()


@app.command()
def ui(port: int = 8787, no_browser: bool = False):
    """Open the local web dashboard (http://127.0.0.1:8787)."""
    from .web.server import serve
    console.print(f"Market Intel UI → http://127.0.0.1:{port}  (Ctrl+C to stop)")
    serve(port=port, open_browser=not no_browser)


# ---------------------------------------------------------------- scheduling (launchd)

LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"
JOBS = {
    "com.intel.daily": {"args": ["run"], "times": [{"Hour": 7, "Minute": 0}, {"Hour": 13, "Minute": 0}, {"Hour": 19, "Minute": 0}]},
    "com.intel.weekly": {"args": ["weekly"], "times": [{"Weekday": 1, "Hour": 7, "Minute": 45}]},
}


@app.command()
def schedule(action: str = typer.Argument("install", help="install | uninstall | status")):
    """Install launchd jobs: pipeline at 07:00/13:00/19:00, weekly memo Monday 07:45."""
    uid = os.getuid()
    uv = subprocess.run(["which", "uv"], capture_output=True, text=True).stdout.strip() or "/opt/homebrew/bin/uv"
    for label, job in JOBS.items():
        plist_path = LAUNCH_AGENTS / f"{label}.plist"
        if action == "status":
            r = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True, text=True)
            console.print(f"{label}: {'loaded' if r.returncode == 0 else 'not loaded'}")
            continue
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        if action == "uninstall":
            plist_path.unlink(missing_ok=True)
            console.print(f"removed {label}")
            continue
        plist = {
            "Label": label,
            "ProgramArguments": [uv, "run", "--project", str(ROOT), "intel", *job["args"]],
            "WorkingDirectory": str(ROOT),
            "StartCalendarInterval": job["times"],
            "StandardOutPath": str(LOG_DIR / f"{label}.out.log"),
            "StandardErrorPath": str(LOG_DIR / f"{label}.err.log"),
            "EnvironmentVariables": {
                "PATH": f"{Path.home()}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                "HOME": str(Path.home()),
            },
        }
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        (ROOT / "launchd").mkdir(exist_ok=True)
        for p in (plist_path, ROOT / "launchd" / f"{label}.plist"):
            with open(p, "wb") as f:
                plistlib.dump(plist, f)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], capture_output=True, text=True)
        console.print(f"{label}: {'installed' if r.returncode == 0 else 'FAILED ' + r.stderr}")


if __name__ == "__main__":
    app()
