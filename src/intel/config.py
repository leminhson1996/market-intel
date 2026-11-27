"""Paths and YAML config loading."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(os.environ.get("INTEL_HOME", Path(__file__).resolve().parents[2]))
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "intel.db"
VAULT_DIR = ROOT / "vault"
LOG_DIR = ROOT / "logs"


def _load(name: str) -> dict:
    with open(CONFIG_DIR / name) as f:
        return yaml.safe_load(f) or {}


@lru_cache
def settings() -> dict:
    return _load("settings.yaml")


@lru_cache
def sources() -> list[dict]:
    return _load("sources.yaml").get("sources", [])


@lru_cache
def company_profile() -> dict:
    return _load("company_profile.yaml")


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, LOG_DIR, VAULT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    for sub in ("Briefs", "Insights", "Entities", "Trends", "Memos", "Predictions"):
        (VAULT_DIR / sub).mkdir(parents=True, exist_ok=True)
