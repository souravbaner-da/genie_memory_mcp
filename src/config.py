"""Runtime configuration, all overridable via environment variables."""
import os

# --- Embeddings (Databricks Foundation Model serving endpoint) ---
EMBED_ENDPOINT = os.getenv("EMBED_ENDPOINT", "databricks-gte-large-en")
EMBED_DIM = int(os.getenv("EMBED_DIM", "1024"))

# --- Lakebase (Autoscaling) connection ---
# In a Databricks App, attaching the Lakebase project as a `postgres` resource
# auto-injects PGHOST / PGUSER / PGPORT / PGDATABASE / PGSSLMODE / PGAPPNAME.
# We additionally need ENDPOINT_NAME to mint OAuth tokens for the connection
# (native pg login is OFF, so auth is token-only and tokens expire hourly).
ENDPOINT_NAME = os.getenv("ENDPOINT_NAME", "")  # projects/<p>/branches/<b>/endpoints/<e>

PGHOST = os.getenv("PGHOST", "")
PGPORT = os.getenv("PGPORT", "5432")
PGDATABASE = os.getenv("PGDATABASE", "databricks_postgres")
PGUSER = os.getenv("PGUSER", "")
PGSSLMODE = os.getenv("PGSSLMODE", "require")

# Postgres schema the app uses for its tables. Default "genie" (app-owned) so the
# app can self-create the schema on boot and own its tables WITHOUT a GRANT — the
# `public` schema isn't writable by the app's service-principal role on PG15+.
# Set PG_SCHEMA=public for deployments whose tables already live in public (with a
# GRANT to the app SP). Must be a simple identifier.
PG_SCHEMA = os.getenv("PG_SCHEMA", "genie")

# Local dev override: when set, the pool connects with this URL directly and
# skips all OAuth token minting (e.g. a local Postgres, or a Lakebase URL with a
# fresh token already embedded). See docker-compose.yml / .env.example.
DATABASE_URL = os.getenv("DATABASE_URL", "")

# --- MCP transport security (DNS-rebinding protection) ---
# FastMCP rejects requests whose Origin/Host aren't allowlisted (localhost-only by
# default) with a 403 BEFORE CORS/the handler — which blocks Genie Code's browser
# (it sends the workspace Origin). By default we DISABLE rebinding protection: it
# guards unauthenticated localhost MCP servers, but this app is remote and gated by
# Databricks Apps OAuth (every request carries a platform-verified Bearer token), so
# rebinding is not a threat. For a strict allowlist instead, set ALLOWED_ORIGINS (and
# ALLOWED_HOSTS) to EXACT values — the SDK matches origins literally (only ":*" PORT
# wildcards are supported, NOT subdomain globs).
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
ALLOWED_HOSTS = [h.strip() for h in os.getenv("ALLOWED_HOSTS", "").split(",") if h.strip()]

# --- Hybrid search tuning ---
RRF_K = int(os.getenv("RRF_K", "60"))               # Reciprocal Rank Fusion constant
SEARCH_POOL = int(os.getenv("SEARCH_POOL", "50"))   # candidates pulled per ranker
# Minimum cosine similarity [0..1] for the semantic (vector) ranker.
# 0 = OFF (default): return nearest neighbours regardless of distance, so off-topic
# queries still surface the closest rows (the agent judges relevance).
# Set e.g. 0.3 to drop loosely-related semantic hits so off-topic queries return
# nothing. Tune per embedding model (gte-large-en: ~0.3-0.4 is a reasonable start).
VECTOR_MIN_SCORE = float(os.getenv("VECTOR_MIN_SCORE", "0"))
