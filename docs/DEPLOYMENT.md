# Deployment Guide — Genie Code Memory (MCP + Lakebase)

Deploy this persistent-memory framework to **your own** Databricks workspace. It's a single
Databricks App (an MCP server) backed by a Lakebase Postgres + `pgvector` database. Genie
Code connects to the app and gains memory that persists across sessions.

**The bundle does almost everything:** `databricks bundle deploy` creates the Lakebase
project *and* the app, and the app **creates its own schema on first boot** — so there's no
manual schema step and **no GRANT**. A fresh install is essentially: configure → deploy →
register in Genie Code.

> New here? Skim the [README](README.md). After deploying, verify with [TEST_PLAN.md](TEST_PLAN.md).
> Everything below uses placeholders/shell variables — nothing is specific to one workspace.

---

## Prerequisites

A stock workspace can't necessarily host this — confirm all of the below. Quick checks use
`$PROFILE` from the next section.

### A. Workspace capabilities

| # | Requirement | Why / how to verify |
|---|---|---|
| A1 | **Serverless (FE-VM) workspace** | Lakebase and Foundation Model serving need a serverless-enabled workspace; classic-only won't work. |
| A2 | **Lakebase — Autoscaling tier + `pgvector`** | This framework uses Autoscaling (`databricks postgres`), not legacy Provisioned (`databricks database`). Verify: `databricks postgres list-projects -p "$PROFILE"` returns without "unknown command"/404. [Lakebase docs](https://docs.databricks.com/aws/en/oltp/) |
| A3 | **Databricks Apps enabled, with a free slot** | Apps are capped per workspace; deploy fails at the cap. Check count: `databricks apps list -p "$PROFILE" -o json \| python3 -c "import json,sys;print(len(json.load(sys.stdin)))"`. [Apps docs](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/) |
| A4 | **An embedding serving endpoint** | e.g. `databricks-gte-large-en` (1024-dim) or `databricks-bge-large-en`. Verify: `databricks serving-endpoints list -p "$PROFILE" -o json \| grep -i embed`. [FM API docs](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/) |
| A5 | **Genie Code enabled + custom MCP servers allowed** | You'll register the app under Settings → MCP Servers (admin-controlled). [Genie Code MCP](https://docs.databricks.com/aws/en/genie-code/mcp) |
| A6 | *(Optional)* **Unity Catalog** | Only for the optional Delta analytical mirror. The core stores everything in Lakebase Postgres — UC not required. |

### B. Permissions you need
- **Create a Lakebase project** and **deploy a Databricks App** (the bundle does both, as you).
- That's it for the default flow — the app's service principal self-creates and **owns** its
  schema, so no manual `GRANT` is required. (The `postgres` resource binding gives the SP the
  database-level `CREATE` it needs.)

### C. Local tooling
- **Databricks CLI ≥ 0.298.0** — `databricks --version` (upgrade: `brew upgrade databricks`).
  [CLI auth](https://docs.databricks.com/aws/en/dev-tools/cli/authentication)
- **Python 3.10+**. `psql` and **Docker** are optional (only for the local sandbox in Step 0).

---

## Set your values once

Authenticate, then export the two values used by the CLI commands below:

```bash
# Log in (opens a browser). Pick any profile name.
databricks auth login --host https://<your-workspace-host> --profile <your-profile>

export PROFILE=<your-profile>          # the profile you just created
export APP_NAME=mcp-genie-memory       # the app name — MUST start with "mcp-"
```

> **`APP_NAME` must start with `mcp-`** — Databricks only lists `mcp-*` apps in the Genie
> Code MCP picker. (The Lakebase project id is set in `databricks.yml`, next step.)

---

## Step 0 — (Optional) Try it locally first

Proves the server + hybrid search work with no Databricks cost.

```bash
cd genie_memory_mcp
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
docker compose up -d                                   # local Postgres + pgvector
export DATABASE_URL=postgresql://memory:memory@localhost:5432/memory PG_SCHEMA=public
python scripts/init_db.py                              # create tables + indexes
python -m src.run                                      # serves MCP at http://localhost:8000/mcp
```

Without Databricks credentials it runs **keyword-only** (embeddings degrade gracefully) —
that's expected locally. `Ctrl-C` to stop; `docker compose down` when done.

---

## Step 1 — Point the bundle at your workspace

Edit **`databricks.yml`** — set the workspace host and the Lakebase project id the bundle
should create, in the `dev` target:

```yaml
targets:
  dev:
    mode: development
    default: true
    workspace:
      host: https://<your-workspace-host>
    variables:
      pg_project_id: genie-memory     # the bundle CREATES this Lakebase project
      # pg_schema: genie              # default; app-owned schema (no GRANT). Leave as-is.
```

`app_name` defaults to `mcp-genie-memory`, `embed_endpoint` to `databricks-gte-large-en`,
`pg_schema` to `genie` (the app-owned schema). Override any via `variables:` if needed.

> **Already have a Lakebase project** (created outside this bundle)? `deploy` will conflict on
> the `postgres_projects` resource. Either point `pg_project_id` at a **new** id, or adopt the
> existing one first:
> `databricks bundle deployment bind postgres_projects.genie <existing-project-id> -t dev`
> (and set `pg_schema: public` if its tables already live in `public` with a GRANT).

Bundle reference: [Asset Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/) ·
[Apps + Lakebase](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/lakebase).

---

## Step 2 — Deploy (creates the project, the app, and the schema)

```bash
databricks bundle validate -t dev -p "$PROFILE"
databricks bundle deploy   -t dev -p "$PROFILE"          # creates the Lakebase project + app
databricks bundle run genie_memory -t dev -p "$PROFILE"  # starts the app; it self-creates its schema
databricks apps get "$APP_NAME" -p "$PROFILE" -o json \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['app_status']['state'], d['url'])"
```

The MCP endpoint is `<app-url>/mcp`. On boot the app runs `init_schema()` to create its
extensions/tables/indexes (idempotent), so it **owns** its tables — no GRANT. Confirm:

```bash
databricks apps logs "$APP_NAME" -p "$PROFILE" | grep startup
#  -> [startup] schema ready (N statements applied)
```

> **First-deploy branch race (expected on a brand-new project):** the very first `deploy` can
> fail with `Postgres branch projects/<id>/branches/production does not exist` — the project's
> `production` branch finishes provisioning a few seconds *after* the project is created. Just
> **re-run `databricks bundle deploy`**; it succeeds once the branch is READY. Then `run`.
>
> Hit `maximum limit of ... apps`? Delete an app you own (`databricks apps delete <name> -p "$PROFILE"`) and re-run deploy.

---

## Step 3 — Register the MCP server in Genie Code

In the workspace, open Genie Code (a notebook, SQL editor, pipeline editor, dashboard, or
MLflow) → the **gear** icon → **MCP Servers** → **Add Server** → **Custom MCP server** →
select your app (`$APP_NAME`) → save. Use Genie Code in **Agent mode** (MCP only works there).

Reference: [Connect Genie Code to MCP servers](https://docs.databricks.com/aws/en/genie-code/mcp).

---

## Step 4 — Install the behavior protocol (`AGENTS.md`)

Step 3 gives Genie the *tools*; `AGENTS.md` tells it *when* to use them (boot at start,
capture as you work, recall on demand). Genie Code auto-discovers it — pick one scope:

- **Global (every session):** upload `AGENTS.md` as your user-home instructions file
  `/Workspace/Users/<you>/.assistant_instructions.md`.
- **Project-scoped:** place `AGENTS.md` at the root of your project folder (Genie Code walks
  *up* the directory tree to find `AGENTS.md` / `CLAUDE.md`).

```bash
databricks workspace import "/Users/<you>/.assistant_instructions.md" \
  --file AGENTS.md --format RAW --overwrite -p "$PROFILE"
```

Reference: [Add custom instructions to Genie Code](https://docs.databricks.com/aws/en/genie-code/instructions).

---

## Step 5 — Verify

In Agent mode: **"Boot my memory."** → state a decision and ask it to save → open a **new**
chat and ask it to recall that decision. Cross-session recall = success. Full edge-case
matrix in **[TEST_PLAN.md](TEST_PLAN.md)**.

---

## Configuration (environment variables)

Set in `databricks.yml` under the app's `config.env`. See `src/config.py`.

| Var | Default | Purpose |
|---|---|---|
| `PG_SCHEMA` | `genie` | Schema the app creates/owns its tables in. Keep the default so the app self-inits with **no GRANT**. Use `public` only for deployments whose tables already live in `public` (with a GRANT to the app SP). |
| `EMBED_ENDPOINT` | `databricks-gte-large-en` | Foundation Model embedding endpoint |
| `EMBED_DIM` | `1024` | Must match the endpoint **and** the `vector(N)` columns |
| `ENDPOINT_NAME` | *(set by bundle)* | Lakebase endpoint path used to mint OAuth tokens |
| `VECTOR_MIN_SCORE` | `0.5` | Min cosine similarity for semantic hits. `0` = off (return nearest neighbours). `gte-large-en` scores high even for unrelated text, so ≈`0.5` filters off-topic while keeping relevant. Tune per model. |
| `RRF_K`, `SEARCH_POOL` | `60`, `50` | Hybrid-search fusion tuning |
| `PGHOST`/`PGUSER`/`PGPORT`/`PGDATABASE`/`PGSSLMODE` | *(auto-injected)* | Set automatically by the `postgres` app resource |
| `DATABASE_URL` | — | Local-dev only: connect directly, skip token minting |

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `unknown command "postgres"` | CLI < 0.285.0 — upgrade (`brew upgrade databricks`). |
| First `deploy` fails: `Postgres branch ... does not exist` | The new project's branch is still provisioning — **re-run `databricks bundle deploy`** once it's READY (a few seconds). |
| App logs: `[startup] FATAL: schema init failed: permission denied for schema public` | The app is set to `PG_SCHEMA=public` but its SP can't create tables there (PG15+). Use the default `PG_SCHEMA=genie` (app-owned), **or** pre-create the tables and GRANT `SELECT,INSERT,UPDATE` to the app SP. |
| `postgres_projects` create conflict (`already exists`) | The Lakebase project already exists outside the bundle — use a new `pg_project_id`, or `databricks bundle deployment bind postgres_projects.genie <id> -t <target>`. |
| **"MCP server could not be added"** in Genie Code | FastMCP DNS-rebinding protection rejects the workspace `Origin`. Already handled in `src/server.py` (protection disabled; the app is still OAuth-gated). Ensure you deployed the current code. |
| App not listed in the MCP picker | App name must start with `mcp-`. |
| `maximum limit of ... apps` | Delete an unused app you own, then redeploy. |
| Search returns results but `semantic: false` | Embedding endpoint unreachable or the app SP lacks `CAN_QUERY`. Still works keyword-only. |
| Browser shows `-32600 ... must accept text/event-stream` | Expected — you can't browse an MCP endpoint; only MCP clients can talk to it. The server is healthy. |
| Dimension mismatch on insert | `EMBED_DIM` and the `vector(N)` columns don't match the endpoint's output size. |

---

## Updating later

```bash
cd genie_memory_mcp
databricks bundle deploy -t dev -p "$PROFILE"
databricks bundle run genie_memory -t dev -p "$PROFILE"
```

## Reference docs

- [Lakebase (OLTP)](https://docs.databricks.com/aws/en/oltp/) ·
  [Databricks Apps](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/) ·
  [Apps + Lakebase](https://docs.databricks.com/aws/en/dev-tools/databricks-apps/lakebase)
- [Genie Code](https://docs.databricks.com/aws/en/genie-code/) ·
  [Genie Code + MCP](https://docs.databricks.com/aws/en/genie-code/mcp) ·
  [Custom instructions](https://docs.databricks.com/aws/en/genie-code/instructions)
- [Asset Bundles](https://docs.databricks.com/aws/en/dev-tools/bundles/) ·
  [CLI auth](https://docs.databricks.com/aws/en/dev-tools/cli/authentication)

## Optional — analytical mirror

Use **Lakebase synced tables** to mirror these tables into Unity Catalog Delta for
dashboards, while Lakebase stays the hot OLTP + vector path.
