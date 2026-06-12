# Genie Code Memory — MCP server over Lakebase

A persistent-memory system for **Databricks Genie Code**, modeled feature-for-feature on
[`claude-mem`](https://github.com/thedotmack/claude-mem). It reproduces claude-mem's
shape — *an MCP server in front of an OLTP + vector store* — using Databricks-native
components.

```
Genie Code ──MCP──► Databricks App (this server) ──► Lakebase (Postgres)
   │                  boot / search / timeline /        pgvector (HNSW)  — semantic
   │                  get_details / capture tools       tsvector  (GIN)  — keyword
   └─ AGENTS.md tells Genie to boot + capture           sessions/observations/decisions/
                                                         context_items/knowledge
```

| claude-mem | Here |
|---|---|
| SQLite (OLTP) + FTS5 (keyword) + Chroma (vector) | **Lakebase**: Postgres + `tsvector` + `pgvector` — one store |
| Bun worker on `:37777` exposing MCP search tools | **FastMCP server hosted as a Databricks App** (`/mcp`) |
| `search` → `timeline` → `get_observations` (progressive disclosure) | `search` → `timeline` → `get_details` |
| Hooks for auto-capture (`PostToolUse`, `Stop`, …) | **Instruction-driven** via `AGENTS.md` + write-through tools (see Limitations) |

## Layout

```
genie_memory_mcp/
├── databricks.yml        # Asset Bundle: app + postgres + serving-endpoint resources
├── app.yaml              # Databricks App config (manual-deploy path)
├── DEPLOYMENT.md         # full step-by-step deploy guide
├── requirements.txt
├── docker-compose.yml    # local Postgres+pgvector for dev
├── .env.example
├── AGENTS.md             # Genie Code memory protocol (install as instructions)
├── sql/schema.sql        # Lakebase DDL (pgvector + tsvector + indexes)
├── scripts/init_db.py    # apply schema
└── src/
    ├── config.py         # env-driven config
    ├── db.py             # pooled Lakebase connections; per-connection OAuth token (Autoscaling)
    ├── embeddings.py     # FMAPI embeddings (graceful keyword-only fallback)
    ├── memory.py         # core ops: boot/search/timeline/get_details + captures
    ├── server.py         # FastMCP tools (+CORS)  ->  app = mcp.streamable_http_app()
    └── run.py            # uvicorn entrypoint (binds $DATABRICKS_APP_PORT)
```

## Local development

```bash
cd genie_memory_mcp
pip install -r requirements.txt
docker compose up -d
cp .env.example .env          # DATABASE_URL points at local Postgres
export $(grep -v '^#' .env | xargs)
python scripts/init_db.py     # create tables + indexes
python -m src.run             # MCP at http://localhost:8000/mcp
```

Without Databricks credentials the server runs **keyword-only** (embeddings degrade
gracefully). To test hybrid search locally, also export `DATABRICKS_HOST` /
`DATABRICKS_TOKEN` so the embedding endpoint is reachable.

## Deploy to Databricks

Full guide: **`DEPLOYMENT.md`**. One-command path (set host + `pg_project_id` in the
`databricks.yml` `dev` target first):

```bash
cd genie_memory_mcp
databricks bundle deploy -t dev -p <profile>          # creates the Lakebase project + app
databricks bundle run    genie_memory -t dev -p <profile>   # starts it; app self-creates its schema
```

Key facts baked into `databricks.yml`:
- **The bundle creates the Lakebase Autoscaling project** (`postgres_projects`) and the app.
  (On a brand-new project the first `deploy` may need a re-run while the project's branch
  finishes provisioning.)
- **The app self-creates its schema on boot** into an app-owned schema (`PG_SCHEMA`, default
  `genie`), so it **owns its tables — no GRANT**. (`public` isn't writable by the app SP on
  PG15+; set `PG_SCHEMA=public` only for deployments whose tables already live there + a GRANT.)
- A **`postgres`** binding injects `PGHOST`/`PGUSER`/`PGPORT`/`PGDATABASE`/`PGSSLMODE`; `db.py`
  mints a fresh OAuth token per connection (native pg login is OFF). A **serving-endpoint**
  binding grants the app `CAN_QUERY` on the embedding endpoint.
- **The app name MUST start with `mcp-`** — Databricks only lists `mcp-*` apps in the Genie
  Code custom-MCP picker.

Then **register** the MCP server in the workspace (Settings → MCP servers —
[docs](https://docs.databricks.com/aws/en/genie-code/mcp)) and **install the protocol**
(`AGENTS.md`): as user-home `.assistant_instructions.md` for a global default, or in a
project folder for project scope. See `DEPLOYMENT.md`.

## Configuration

All via environment (see `src/config.py`): `PG_SCHEMA` (app-owned schema, default `genie`),
`EMBED_ENDPOINT`, `EMBED_DIM`, `ENDPOINT_NAME` (Lakebase endpoint path used to mint OAuth
tokens), `DATABASE_URL` (local-dev override), the auto-injected
`PGHOST`/`PGPORT`/`PGDATABASE`/`PGUSER`/`PGSSLMODE`, `RRF_K`, `SEARCH_POOL`, and `VECTOR_MIN_SCORE`.

> **`VECTOR_MIN_SCORE`** (default `0` = off): minimum cosine similarity for the semantic
> ranker. By default semantic search returns nearest neighbours regardless of distance
> (the agent judges relevance). Set it to drop loosely-related hits so off-topic queries
> return nothing. **Tune per embedding model** — `gte-large-en` runs hot (measured: ~0.30
> for unrelated text, ~0.59–0.71 for on-topic), so **`0.5`** sits in the gap and is the
> deployed value (filters off-topic, keeps relevant/adjacent). Too high also drops genuine
> distant matches. It's set in `databricks.yml` `config.env`:
> ```yaml
>     - name: VECTOR_MIN_SCORE
>       value: "0.5"
> ```

> **Embedding dimension must match.** Default `databricks-gte-large-en` = 1024. If you
> change the endpoint, update `EMBED_DIM` **and** the `vector(N)` columns in
> `sql/schema.sql`.

## Limitations vs claude-mem (by design)

Genie Code has **no programmable lifecycle hooks** (no enforced `PostToolUse`/`Stop`), so
capture is *instruction-driven* via `AGENTS.md` rather than deterministic. Hardening
options:
- **Write-through is server-side** — once a capture tool is called, persistence is
  guaranteed; only the *decision to call* depends on the model.
- Add a scheduled **Databricks Job** that mines session artifacts and back-fills
  observations for belt-and-suspenders capture.

## Optional: analytical mirror

Use **Lakebase synced tables** to mirror these tables into Unity Catalog Delta for
dashboards/analytics, keeping Lakebase as the hot OLTP + vector path.
