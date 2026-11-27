#!/usr/bin/env bash
# One-step setup for market-intel on macOS. Safe to re-run (idempotent).
#
#   ./scripts/setup.sh                 interactive
#   ./scripts/setup.sh --yes           accept all defaults (installs the schedule)
#   ./scripts/setup.sh --yes --no-schedule
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

YES=0; SCHEDULE=""
for a in "$@"; do
  case "$a" in
    --yes|-y) YES=1 ;;
    --schedule) SCHEDULE=yes ;;
    --no-schedule) SCHEDULE=no ;;
    -h|--help) sed -n 2,7p "$0"; exit 0 ;;
    *) echo "unknown option: $a"; exit 2 ;;
  esac
done

bold() { printf "\n\033[1m==> %s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[31m✗ %s\033[0m\n" "$*"; exit 1; }
ask()  { # ask "question" default(y/n) -> returns 0 for yes
  local q="$1" d="$2" r
  if [[ $YES == 1 ]]; then [[ $d == y ]]; return; fi
  read -r -p "  $q [$( [[ $d == y ]] && echo Y/n || echo y/N )] " r || true
  r="${r:-$d}"; [[ $r =~ ^[Yy] ]]
}

bold "Checking system"
[[ "$(uname)" == "Darwin" ]] && ok "macOS $(sw_vers -productVersion)" \
  || warn "Not macOS — setup continues, but scheduling (launchd) won't be available."

if ! command -v brew >/dev/null 2>&1; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [[ -x $b ]] && eval "$($b shellenv)"; done
fi
command -v brew >/dev/null 2>&1 && ok "Homebrew" || die "Homebrew is required. Install it from https://brew.sh then re-run this script."

bold "Installing tools"
if command -v uv >/dev/null 2>&1; then ok "uv $(uv --version | awk '{print $2}')"; else brew install uv && ok "uv installed"; fi
if command -v ollama >/dev/null 2>&1; then ok "ollama"; else brew install ollama && ok "ollama installed"; fi
export PATH="$HOME/.local/bin:$PATH"
if command -v claude >/dev/null 2>&1; then
  ok "Claude Code CLI $(claude --version 2>/dev/null | awk '{print $1}')"
elif ask "Claude Code CLI not found. Install it now?" y; then
  curl -fsSL https://claude.ai/install.sh | bash
  command -v claude >/dev/null 2>&1 && ok "Claude Code CLI installed" || die "Claude install failed — see https://docs.claude.com/en/docs/claude-code/setup"
else
  die "Claude Code CLI is required (the analysis runs through 'claude -p')."
fi

bold "Python environment"
uv sync -q && ok "dependencies installed (.venv)"

bold "Local embedding model"
if ! curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
  if [[ -d /Applications/Ollama.app ]]; then open -a Ollama; else brew services start ollama >/dev/null; fi
  for _ in $(seq 1 30); do curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1 && break; sleep 1; done
fi
curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1 && ok "Ollama server running" \
  || die "Ollama is not responding. Start it with: brew services start ollama"
MODEL="$(uv run python -c 'from intel.config import settings; print(settings()["embedding"]["model"])')"
if ollama list | awk 'NR>1{print $1}' | sed 's/:.*//' | grep -qx "$MODEL"; then ok "$MODEL already pulled"
else echo "  pulling $MODEL (~1.2 GB, one time)…"; ollama pull "$MODEL" >/dev/null && ok "$MODEL pulled"; fi

bold "Health check"
uv run intel doctor || die "Fix the items above, then re-run ./scripts/setup.sh"

if [[ "$(uname)" == "Darwin" ]]; then
  bold "Schedule"
  if [[ $SCHEDULE == yes ]] || { [[ -z $SCHEDULE ]] && ask "Run automatically every day at 07:00, 13:00, 19:00 (+ weekly memo Mon 07:45)?" y; }; then
    uv run intel schedule install
  else ok "skipped — enable later with: uv run intel schedule install"; fi
fi

bold "Done 🎉"
cat <<EOF
  First run (≈3–5 min, collects + analyzes):   uv run intel run
  Open the dashboard:                           uv run intel ui      → http://127.0.0.1:8787
  Tailor the strategy lens (optional):          edit config/company_profile.yaml
  Use it from Claude Code:                      open 'claude' in $ROOT and ask questions
EOF
