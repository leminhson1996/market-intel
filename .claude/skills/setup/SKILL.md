---
name: setup
description: Install and configure market-intel on this Mac end to end (tools, Claude CLI check, embedding model, database, health check, schedule, first run). Use when the user asks to set up, install, get started, or fix a broken install.
---

# Set up market-intel

Goal: get the user from a fresh clone to a working dashboard with their first insights. Explain each step briefly as you go. The user may be non-technical, so keep explanations simple.

## 1. Run the setup script
Run `./scripts/setup.sh --yes --no-schedule` from the repo root. The scheduling question is handled in step 3.

The script is idempotent. It:
- installs uv and Ollama with Homebrew
- checks the Claude Code CLI
- runs `uv sync`
- starts Ollama and pulls the embedding model
- runs `uv run intel doctor`

If it stops, read the error and fix it:
- **Homebrew missing:** you can't install it non-interactively, because it needs sudo. Ask the user to run `! /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"` themselves, then re-run the script.
- **Ollama not responding:** run `brew services start ollama` (or `open -a Ollama`), wait a few seconds, then re-run.
- **`claude -p` failed / not logged in:** the user must log in interactively. Ask them to run `! claude`, complete login, then exit. Analysis needs a Claude Pro/Max plan or API access.
- **Embedding mismatch:** run `uv run intel reembed`.

Re-run `uv run intel doctor` until it prints "All good".

## 2. Ask about their business (optional but valuable)
Ask the user whether they want a general tech-CEO lens or one tailored to their company. If tailored, ask for:
- the industry
- products
- target markets (for example Vietnam / SEA)
- strengths
- goals
- topics to watch

Then write the answers into `config/company_profile.yaml`. Keep the YAML keys that are already there.

## 3. Ask about automation
- **Schedule:** ask whether it should run automatically at 07:00, 13:00 and 19:00, plus a weekly memo on Monday at 07:45. If yes, run `uv run intel schedule install`.
- Per-machine choices go in `config/settings.local.yaml` (copy from `config/settings.local.example.yaml`). Never edit `settings.yaml` for them.

## 4. First run
Run `uv run intel run`. It takes about 3–5 minutes: it collects about 450 items, analyzes the top 15 stories with Claude, and writes the daily brief. Then:
- summarize the brief (`vault/Briefs/<today>.md`) in 5 bullets
- start the dashboard with `uv run intel ui` (run it in the background) and give the user http://127.0.0.1:8787
- tell them about the Predict page and the Search & Ask page (which works in Vietnamese)

## Rules
- Don't edit source code to work around setup problems. Fix the environment instead.
- Don't commit or push unless the user asks. Never commit `vault/` or `data/`: analysis output stays local.
- If something fails twice in the same way, stop and show the user the exact error.
