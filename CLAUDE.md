# Market Intelligence System

Python (uv) app at `src/intel/`. See README.md for commands and the architecture.

- LLM calls go through `src/intel/llm.py` (`claude -p --json-schema`), never the Anthropic API. This uses the user's subscription.
- Embeddings: local Ollama `nomic-embed-text` (768-d). Vectors are stored in sqlite-vec tables in `data/intel.db`.
- Pipeline stages: `collect.py` → `enrich.py` (embed, cluster, score) → `analyze.py` (triage + CEO analysis, writes graph and vault) → `reports.py`.
- Memory/query helpers are in `memory.py`. They are exposed to Claude through `mcp_server.py` (`.mcp.json`, server `market-intel`).
- To answer questions about markets or trends in this folder, prefer the `market-intel` MCP tools or `uv run intel search/ask`.
- Schema changes: add migrations in `db.init_db()`. Never drop `data/intel.db`, because it holds the accumulated long-term memory.
