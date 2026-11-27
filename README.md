# market-intel — Market Intelligence System

Scans tech, business and research sources several times a day. It clusters items into stories and trends, analyzes the most important ones from a CEO strategy point of view (using Claude), forecasts what comes next, and saves everything as structured memory on your Mac.

```
collect ─► enrich ─► triage ─► analyze ─► brief/memo ─► memory (SQLite + vectors + graph + Markdown) ─► UI · CLI · MCP
```

---

## Getting started

### What you need
| | Requirement | Why |
|---|---|---|
| 💻 | **macOS** (Apple Silicon or Intel) | Scheduling uses launchd. On Linux everything else works; use cron for scheduling. |
| 🍺 | **[Homebrew](https://brew.sh)** | Installs uv and Ollama |
| 🤖 | **[Claude Code](https://docs.claude.com/en/docs/claude-code/setup)**, logged in with a **Claude Pro/Max** plan (or API access) | All analysis runs through `claude -p`. No API key is needed. |
| 💾 | ~3 GB free disk | Python environment plus the local embedding model `bge-m3` (~1.2 GB) |

You don't need to install Python yourself: `uv` downloads the right version automatically.

### Option A: let Claude Code set it up (easiest)
```bash
git clone git@github.com:leminhson1996/market-intel.git
cd market-intel
claude
```
Then type:
```
/setup
```
Claude does the following:
- runs the installer and fixes anything missing
- optionally asks about your business, to tailor the strategy lens
- asks whether to run it automatically every day
- does the first run and opens the dashboard for you

You'll approve each command, and the `market-intel` MCP server, the first time.

### Option B: one command in the terminal
```bash
git clone git@github.com:leminhson1996/market-intel.git
cd market-intel
./scripts/setup.sh
```
The script is safe to re-run. It:
1. installs `uv` and `ollama` with Homebrew, and installs Claude Code if it's missing
2. installs the Python dependencies
3. starts Ollama and downloads the `bge-m3` embedding model
4. runs `intel doctor`, a health check of everything
5. offers to install the daily schedule

Non-interactive: `./scripts/setup.sh --yes` (accepts the defaults). Other flag: `--no-schedule`.

### First run
```bash
uv run intel run     # ≈3–5 min: collects ~450 items, analyzes the top 15 stories, writes today's brief
uv run intel ui      # opens the dashboard at http://127.0.0.1:8787
```

### Make it yours (optional)
- **Your business:** edit `config/company_profile.yaml` with your industry, markets, strengths, goals and topics to watch. Scoring and strategy advice adapt to it.
- **Your sources:** add RSS feeds to `config/sources.yaml`.
- **Per-machine settings:** copy `config/settings.local.example.yaml` to `config/settings.local.yaml`. This file is git-ignored. It's where to change the default model, how many stories are analyzed per run, and so on.

### Something not working?
```bash
uv run intel doctor
```
This checks macOS, Ollama, the embedding model, the database, Claude CLI login, the schedule and git sync, and prints the exact fix for each problem.

| Symptom | Fix |
|---|---|
| `claude -p failed` / not logged in | Run `claude` once in a terminal and log in |
| Ollama not running | `brew services start ollama` (or open the Ollama app) |
| `embedding model bge-m3 not pulled` | `ollama pull bge-m3` |
| `Database vectors use … Run: uv run intel reembed` | `uv run intel reembed` |
| A feed shows ✕ error in *Sources & runs* | The site changed or blocks bots. Update or remove it in `config/sources.yaml` |
| Scheduled runs don't happen | `uv run intel schedule status`. The Mac must be on: missed runs while asleep run on wake; runs while shut down are skipped |

---

## Using it

### Web UI: `uv run intel ui`
| Page | What it's for |
|---|---|
| **Dashboard** | KPIs, top signals, insights by category, rising stories, hot entities, today's brief |
| **Insights** | Filter and open the full CEO analysis: moves, opportunities, threats, hype check, sources. 👍/👎 tunes future scoring |
| **Trends** | Story clusters ranked by growth, with history |
| **Predict** | On-demand forecasts: pick a topic, horizon, **model** (Opus / Sonnet / Fable / Haiku) and language |
| **Knowledge graph** | Interactive network of companies, technologies, people and regulations |
| **Search & Ask** | Semantic search, or ask Claude questions answered from memory with citations. Works in **Vietnamese** |
| **Reports** | Daily briefs and weekly strategic memos |
| **Sources & runs** | Feed health and pipeline buttons with a live log |

### From Claude Code (MCP)
Open `claude` in this folder and ask things like *"What were the biggest AI signals this week?"* or *"Dự báo thị trường GPU 6 tháng tới?"*. Claude queries the memory through the `market-intel` MCP server, using these tools: `search_memory`, `recent_insights`, `get_trends`, `get_entity`, `latest_report`, `list_predictions` and others.

### CLI
| Command | What it does |
|---|---|
| `intel run` | Full pipeline |
| `intel collect` / `enrich` / `analyze --top N` / `brief` / `weekly` | Run one stage |
| `intel insights --days 3` | Recent insights, ranked by impact × confidence |
| `intel trends --days 7` | Fastest-growing story clusters |
| `intel search "query" -k 10` | Hybrid search: keyword (FTS5) + semantic (sqlite-vec) |
| `intel ask "question"` | Claude answers from memory with citations (in the question's language) |
| `intel predict "topic" -h "1 year" -m claude-opus-5-5 [--vi]` | On-demand forecast grounded in memory |
| `intel entity nvidia --depth 2` | Knowledge-graph view of an entity |
| `intel feedback <id> up\|down` | Rate an insight; scoring of that story adjusts |
| `intel doctor` | Health check with fixes |
| `intel stats` | Row counts and source health |
| `intel schedule install\|uninstall\|status` | Daily (07:00/13:00/19:00) and weekly (Mon 07:45) launchd jobs |
| `intel ui` | Local web dashboard |
| `intel reembed` | Rebuild all vectors after changing `embedding.model` |
| `intel mcp` | MCP server (stdio), registered in `.mcp.json` |

All commands are run as `uv run intel …`.

---

## How it works

### Where memory lives
| Store | Path | Contents |
|---|---|---|
| Relational + full-text | `data/intel.db` | sources, items, clusters, insights, entities, relations, predictions, trend_snapshots, runs |
| Vectors | `data/intel.db` (`item_vec`, `insight_vec`) | 1024-d `bge-m3` multilingual embeddings (sqlite-vec) |
| Knowledge graph | `entities` + `relations` tables; `vault/Entities/*.md` | Companies, technologies, people, regulations, and how they relate |
| Markdown vault | `vault/` (open it as an Obsidian vault) | `Insights/`, `Briefs/`, `Memos/`, `Predictions/`, `Entities/`, all wiki-linked |
| Raw archive | `data/raw/YYYY-MM-DD.jsonl` | Every new item as collected (pruned after `retention.raw_days`) |

`data/` and `vault/` are git-ignored: analysis output never goes into git, and each person builds their own memory. To back it up, copy those two folders (or use Time Machine).

### Scoring and analysis
Cluster score = (source breadth + volume + engagement + 24h velocity) × source weight × recency × relevance to your profile × your feedback.

After scoring, one triage call to Claude drops noise such as deals, gossip and trivia. Only the top N relevant clusters get the full analysis. A cluster is analyzed again once it has grown by `reanalyze_growth` items. Each analysis is given related past insights from vector memory, so it can link new events to earlier ones.

### Predict
Predict runs only on demand. Each forecast includes:
- falsifiable predictions with calibrated probabilities, plus the signs that would confirm or disprove them
- links to the evidence insights
- 3–4 scenarios
- wildcards
- a CEO playbook

Forecasts are stored in the `predictions` table and in `vault/Predictions/`. The model list lives in `predict.models` in `config/settings.yaml`.

### Languages
`bge-m3` puts Vietnamese and English in the same vector space, so Vietnamese questions find English articles. Ask and Predict can answer in Vietnamese.

### Usage and cost
Each pipeline run makes about 17 `claude -p` calls against your Claude plan (1 triage, about 15 analyses, 1 brief). With 3 runs a day, that's about 50 calls. Lower `pipeline.analyze_top_n` to use less. Embeddings run locally and cost nothing.

### Logs
`logs/intel.log`, `logs/com.intel.*.log`
