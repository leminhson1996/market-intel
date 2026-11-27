"""LLM access through headless Claude Code (`claude -p`), using your Claude subscription."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess

from .config import DATA_DIR, settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def _claude_bin() -> str:
    path = shutil.which("claude") or str(__import__("pathlib").Path.home() / ".local/bin/claude")
    return path


def ask(prompt: str, schema: dict | None = None, system: str | None = None, model: str | None = None,
        timeout: int | None = None):
    """Run one prompt. Returns parsed dict when `schema` is given, else text."""
    cfg = settings()["llm"]
    cmd = [
        _claude_bin(), "-p",
        "--model", model or cfg["model"],
        "--output-format", "json",
        "--tools", "",                 # pure reasoning, no tool use
        "--no-session-persistence",
        "--strict-mcp-config",         # don't start any MCP servers
    ]
    if system:
        cmd += ["--append-system-prompt", system]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            timeout=timeout or cfg.get("timeout_sec", 240), cwd=DATA_DIR,
        )
    except subprocess.TimeoutExpired as e:
        raise LLMError("claude -p timed out") from e
    if proc.returncode != 0:
        raise LLMError(f"claude -p exit {proc.returncode}: {proc.stderr[-800:] or proc.stdout[-800:]}")
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise LLMError(f"unparseable output: {proc.stdout[:500]}") from e
    if out.get("is_error"):
        raise LLMError(f"claude error: {out.get('result') or out.get('subtype')}")
    if schema:
        if out.get("structured_output") is not None:
            return out["structured_output"]
        return json.loads(out.get("result", "{}"))
    return out.get("result", "")
