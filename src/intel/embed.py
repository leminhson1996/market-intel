"""Local embeddings via Ollama."""
from __future__ import annotations

import httpx
import numpy as np

from .config import settings


def embed_texts(texts: list[str], kind: str = "document") -> np.ndarray:
    """Return L2-normalized embeddings. Only nomic models use task prefixes."""
    cfg = settings()["embedding"]
    prefix = ("search_query: " if kind == "query" else "search_document: ") if "nomic" in cfg["model"] else ""
    out = []
    with httpx.Client(timeout=120) as c:
        for i in range(0, len(texts), 32):
            batch = [prefix + t[:2000] for t in texts[i : i + 32]]
            r = c.post(f"{cfg['ollama_url']}/api/embed", json={"model": cfg["model"], "input": batch})
            r.raise_for_status()
            out.extend(r.json()["embeddings"])
    arr = np.asarray(out, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return arr / np.where(norms == 0, 1, norms)


def embed_one(text: str, kind: str = "document") -> np.ndarray:
    return embed_texts([text], kind)[0]
