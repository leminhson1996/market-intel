# market-intel

A Python (uv) app at `src/intel/`. See README.md for commands and the architecture.

## Setup / onboarding
If the user asks to set up, install or get started, or the app isn't working on this machine, use the `/setup` skill (`.claude/skills/setup/SKILL.md`). `uv run intel doctor` diagnoses every prerequisite and prints how to fix each one.

## Architecture notes
- LLM calls go through `src/intel/llm.py` (`claude -p --json-schema`), never the Anthropic API. This uses the user's Claude subscription.
- Embeddings come from local Ollama `bge-m3` (1024-d, multilingual: Vietnamese and English). After changing the model, run `intel reembed`. Vectors are stored in sqlite-vec tables in `data/intel.db`.
- Pipeline: `collect.py` → `enrich.py` (embed, cluster, score) → `analyze.py` (triage + CEO analysis; writes the graph and the vault) → `reports.py`. `predict.py` runs on demand only.
- Memory and query helpers are in `memory.py`. They're exposed to Claude via `mcp_server.py` (`.mcp.json`, server `market-intel`).
- Web UI: `src/intel/web/server.py` (FastAPI) plus `src/intel/web/static/index.html`.

## Conventions
- To answer questions about markets or trends, prefer the `market-intel` MCP tools or `uv run intel search/ask`.
- Per-machine settings go in `config/settings.local.yaml` (git-ignored), which is merged over `settings.yaml`.
- Never commit analysis output (`vault/`, `data/`); both are git-ignored. Commit code, config and docs only.
- Schema changes: add migrations in `db.init_db()`. Never drop `data/intel.db`, because it holds the accumulated long-term memory.
