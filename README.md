# market-intel — Market Intelligence System

Scans tech, business and research sources several times a day. It clusters items into stories and trends, analyzes the most important ones from a CEO strategy point of view (using `claude -p`), and saves everything as local, structured memory.

```
collect ─► enrich ─► triage ─► analyze ─► brief/memo ─► memory (SQLite + vectors + graph + Markdown) ─► MCP
```

## Quick start
```bash
uv sync
ollama pull bge-m3
uv run intel init          # create the database and check that Ollama and claude -p work
uv run intel run           # full pipeline: collect, enrich, analyze, daily brief
uv run intel schedule install   # launchd: 07:00/13:00/19:00 daily, weekly memo Mon 07:45
```

## Web UI
```bash
uv run intel ui            # opens http://127.0.0.1:8787
```
Pages: Dashboard, Insights (filters, full analysis, 👍/👎), Trends, Predict, Knowledge graph, Search & Ask (Claude answers with citations), Reports (briefs and memos), Sources & runs (source health, run the pipeline from the UI). Link straight to an insight with `#/insights?open=<id>`.

## Commands
| Command | What it does |
|---|---|
| `intel run` | Full pipeline |
| `intel collect` / `enrich` / `analyze --top N` / `brief` / `weekly` | Run one stage |
| `intel insights --days 3` | Recent insights, ranked by impact × confidence |
| `intel trends --days 7` | Fastest-growing story clusters |
| `intel search "query" -k 10` | Hybrid search: keyword (FTS5) + semantic (sqlite-vec) |
| `intel ask "question"` | Claude answers from memory, with citations |
| `intel entity nvidia --depth 2` | Knowledge-graph view of an entity |
| `intel feedback <id> up\|down` | Rate an insight; scoring of that story adjusts |
| `intel stats` | Row counts and source health |
| `intel schedule install\|uninstall\|status` | Manage the launchd jobs |
| `intel ui` | Local web dashboard |
| `intel gitsync` | Commit and push `vault/` now (also runs automatically after `run`/`weekly`; see `git:` in settings.yaml) |
| `intel predict "topic" -h "1 year" -m claude-opus-5-5 [--vi]` | On-demand forecast grounded in memory |
| `intel reembed` | Rebuild all vectors after changing `embedding.model` in settings.yaml |
| `intel mcp` | MCP server (stdio). Registered for this folder in `.mcp.json` |

## Where memory lives
| Store | Path | Contents |
|---|---|---|
| Relational + full-text | `data/intel.db` | sources, items, clusters, insights, entities, relations, trend_snapshots, runs |
| Vectors | `data/intel.db` (`item_vec`, `insight_vec`) | 1024-d `bge-m3` multilingual embeddings (sqlite-vec) |
| Knowledge graph | `entities` + `relations` tables; `vault/Entities/*.md` | Companies, technologies, people, regulations, and how they relate |
| Markdown vault | `vault/` (open it as an Obsidian vault) | `Insights/`, `Briefs/`, `Memos/`, `Entities/`, all wiki-linked |
| Raw archive | `data/raw/YYYY-MM-DD.jsonl` | Every new item as collected (pruned after `retention.raw_days`) |

## Predict
On-demand forecasting that runs only when you click **Predict** in the UI or run `intel predict`. You choose:
- an optional focus topic
- a horizon (1 month to 5 years, or a custom one)
- the model (Opus 5.5, Sonnet 5.5, Fable 5.2 or Haiku 5.5; the list is in `predict.models` in settings.yaml)
- the language (English or Vietnamese)

The forecast draws on the most relevant insights, trend momentum, the most-mentioned entities and earlier forecasts. It contains:
- falsifiable predictions with calibrated probabilities, signposts, invalidators and links to the evidence insights
- 3–4 scenarios
- wildcards
- a CEO playbook

Forecasts are stored in the `predictions` table and in `vault/Predictions/`, and are auto-committed.

## Languages
Search and Ask work in Vietnamese as well as English. `bge-m3` puts both languages in the same vector space, so a Vietnamese question finds English articles. `ask` replies in the language of the question.

## Configuration (`config/`)
- `sources.yaml`: add RSS feeds and other sources (types: `rss`, `hackernews`, `reddit`, `github`, `arxiv`)
- `company_profile.yaml`: the strategy lens. It's generic now; fill in industry, markets, strengths and goals to tailor the analysis
- `settings.yaml`: model, parallel calls, cluster threshold, number of clusters to analyze, noise filters

## How scoring works
Cluster score = (source breadth + volume + engagement + 24h velocity) × source weight × recency × relevance to your profile × your feedback.
After scoring, one triage call to Claude drops noise such as deals, gossip and trivia. Only the top N relevant clusters get the full analysis. A cluster is analyzed again once it has grown by `reanalyze_growth` items. Each new analysis is given earlier related insights from vector memory, so it can link new events to past ones.

## Logs
`logs/intel.log`, `logs/com.intel.*.log`
