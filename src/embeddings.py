"""Embedding generation via a Databricks Foundation Model serving endpoint.

Degrades gracefully: if the endpoint is unreachable (e.g. local dev with no
Databricks credentials) embed() returns None and the search layer falls back to
keyword-only retrieval.
"""
from __future__ import annotations

from . import config

_client = None


def _ws():
    global _client
    if _client is None:
        from databricks.sdk import WorkspaceClient

        _client = WorkspaceClient()
    return _client


def embed(texts: list[str]) -> list[list[float]] | None:
    if not texts:
        return []
    try:
        resp = _ws().serving_endpoints.query(name=config.EMBED_ENDPOINT, input=list(texts))
        data = getattr(resp, "data", None) or []
        return [list(item.embedding) for item in data]
    except Exception as exc:  # noqa: BLE001 - intentional graceful degradation
        print(f"[embeddings] disabled ({exc}); falling back to keyword search")
        return None


def embed_one(text: str) -> list[float] | None:
    result = embed([text])
    return result[0] if result else None
