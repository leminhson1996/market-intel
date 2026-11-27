"""Auto-commit and push the Markdown vault after pipeline runs."""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime

from .config import ROOT, settings

log = logging.getLogger(__name__)


def _git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0",
           # never hang on an SSH passphrase/host prompt when running unattended
           "GIT_SSH_COMMAND": os.environ.get("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=20")}
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, env=env, check=check, timeout=120)


def sync(reason: str) -> dict:
    """Commit only the configured paths (default: vault/) and push. Never raises."""
    cfg = settings().get("git", {})
    if not cfg.get("auto_commit", False):
        return {"skipped": "auto_commit disabled"}
    paths = cfg.get("paths", ["vault"])
    try:
        if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
            return {"skipped": "not a git repo"}
        _git("add", "--", *paths)
        changed = _git("diff", "--cached", "--name-only", "--", *paths).stdout.split()
        out: dict = {"files": len(changed)}
        if changed:
            msg = f"intel: {reason} — {datetime.now():%Y-%m-%d %H:%M} ({len(changed)} files)"
            # pathspec limits the commit to these paths even if other files are staged
            r = _git("commit", "-m", msg, "--", *paths)
            if r.returncode != 0:
                return {**out, "error": "commit failed: " + (r.stderr or r.stdout)[-300:]}
            out["commit"] = _git("rev-parse", "--short", "HEAD").stdout.strip()
        if cfg.get("auto_push", False) and _git("remote").stdout.strip():
            out["push"] = _push(cfg.get("remote", "origin"))
        return out
    except Exception as e:  # git trouble must never break the pipeline
        log.warning("git sync failed: %s", e)
        return {"error": str(e)}


def _push(remote: str) -> str:
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    ahead = _git("rev-list", "--count", f"{remote}/{branch}..HEAD")
    if ahead.returncode == 0 and ahead.stdout.strip() == "0":
        return "up to date"
    r = _git("push", remote, branch)
    if r.returncode == 0:
        return "pushed"
    # remote moved (e.g. pushed from another machine): rebase our vault commits on top, retry once
    if "rejected" in r.stderr or "fetch first" in r.stderr:
        pr = _git("pull", "--rebase", "--autostash", remote, branch)
        if pr.returncode != 0:
            _git("rebase", "--abort")
            log.warning("git pull --rebase failed; will retry next run: %s", pr.stderr[-300:])
            return "rebase failed (will retry next run)"
        r = _git("push", remote, branch)
        if r.returncode == 0:
            return "pushed after rebase"
    log.warning("git push failed; commits stay local and are pushed next run: %s", r.stderr[-300:])
    return "push failed (will retry next run): " + r.stderr.strip()[-200:]
