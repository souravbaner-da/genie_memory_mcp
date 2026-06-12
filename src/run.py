"""Entrypoint: initialize the schema, then serve the MCP ASGI app.

Databricks Apps inject DATABRICKS_APP_PORT; locally we fall back to PORT or 8000.
Run with:  python -m src.run
"""
import os

import uvicorn

from . import memory
from .server import app


def main() -> None:
    # Self-initialize the schema so the app owns its tables (idempotent — all DDL
    # uses IF NOT EXISTS). Fail fast: if this can't run, the app can't serve.
    try:
        n = memory.init_schema()
        print(f"[startup] schema ready ({n} statements applied)", flush=True)
    except Exception as exc:  # noqa: BLE001 - surface a clear cause then re-raise
        print(
            f"[startup] FATAL: schema init failed: {exc}\n"
            "  If this is a CREATE EXTENSION / permission error, create the 'vector' "
            "extension once as a privileged role, then redeploy (see DEPLOYMENT.md).",
            flush=True,
        )
        raise

    port = int(os.getenv("DATABRICKS_APP_PORT", os.getenv("PORT", "8000")))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
