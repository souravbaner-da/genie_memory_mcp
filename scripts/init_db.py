"""Apply sql/schema.sql to the configured database.

Local dev:   DATABASE_URL=postgresql://... python scripts/init_db.py
Databricks:  run with Lakebase env vars set (PGHOST/PGUSER/... + LAKEBASE_INSTANCE_NAME),
             or run once from a notebook/job against the Lakebase instance.
"""
import os
import pathlib
import sys

# Accept KEY=VALUE args and inject them into the environment BEFORE importing
# config/db (which read env at import time). Lets a bundle job pass connection
# config, e.g.:  init_db.py PGHOST=... PGUSER=... LAKEBASE_INSTANCE_NAME=...
for _arg in sys.argv[1:]:
    if "=" in _arg:
        _k, _v = _arg.split("=", 1)
        os.environ[_k] = _v

# Allow running as a standalone script (add project root to path).
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src import memory  # noqa: E402


def main() -> None:
    # Same idempotent bootstrap the app runs on startup (src/memory.py).
    n = memory.init_schema()
    print(f"Applied {n} statements to the database.")


if __name__ == "__main__":
    main()
