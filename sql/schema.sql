-- Lakebase (Postgres) schema for Genie Code persistent memory.
-- Mirrors claude-mem's SQLite + FTS5 + Chroma in a single OLTP store:
--   * pgvector (HNSW)  -> semantic search   (claude-mem's Chroma)
--   * tsvector  (GIN)  -> keyword search     (claude-mem's FTS5)
-- Embedding dimension assumes databricks-gte-large-en (1024). Change vector(N)
-- here and EMBED_DIM in config if you use a different endpoint.

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------------
-- sessions : one row per Genie Code session (claude-mem sessions/summaries)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    session_id      text PRIMARY KEY,
    project         text NOT NULL,
    started_at      timestamptz NOT NULL DEFAULT now(),
    ended_at        timestamptz,
    summary         text,
    next_steps      text,
    open_items      jsonb NOT NULL DEFAULT '[]'::jsonb,
    decisions_made  int   NOT NULL DEFAULT 0,
    status          text  NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions (project, started_at DESC);

-- ---------------------------------------------------------------------------
-- observations : granular capture per tool use (claude-mem observations)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS observations (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id  text REFERENCES sessions (session_id),
    project     text NOT NULL,
    ts          timestamptz NOT NULL DEFAULT now(),
    tool        text,
    kind        text,
    title       text,
    content     text,
    embedding   vector(1024),
    search_tsv  tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))
    ) STORED
);
CREATE INDEX IF NOT EXISTS idx_obs_project_ts ON observations (project, ts DESC);
CREATE INDEX IF NOT EXISTS idx_obs_session    ON observations (session_id, ts);
CREATE INDEX IF NOT EXISTS idx_obs_tsv        ON observations USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS idx_obs_emb        ON observations USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- decisions : design choices with rationale (most valuable memory type)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decisions (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project       text NOT NULL,
    decision      text NOT NULL,
    rationale     text,
    alternatives  text,
    tags          text[] NOT NULL DEFAULT '{}',
    session_id    text,
    status        text NOT NULL DEFAULT 'active',   -- active | superseded
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    embedding     vector(1024),
    search_tsv    tsvector GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(decision, '') || ' ' || coalesce(rationale, '') || ' ' || coalesce(alternatives, ''))
    ) STORED
);
CREATE INDEX IF NOT EXISTS idx_dec_project ON decisions (project, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dec_tsv     ON decisions USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS idx_dec_tags    ON decisions USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_dec_emb     ON decisions USING hnsw (embedding vector_cosine_ops);

-- ---------------------------------------------------------------------------
-- context_items : structured project facts (schema/naming/env/team/pattern)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS context_items (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project     text NOT NULL,
    category    text NOT NULL,
    key         text NOT NULL,
    value       text,
    source      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project, category, key)
);
CREATE INDEX IF NOT EXISTS idx_ctx_project ON context_items (project, category);

-- ---------------------------------------------------------------------------
-- knowledge : reusable patterns/snippets/gotchas (project NULL = cross-project)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    project      text,                       -- NULL => applies to all projects
    title        text NOT NULL,
    content      text,
    category     text,                       -- pattern | snippet | gotcha | reference
    tags         text[] NOT NULL DEFAULT '{}',
    usage_count  int NOT NULL DEFAULT 0,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),
    embedding    vector(1024),
    search_tsv   tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(content, ''))
    ) STORED
);
CREATE INDEX IF NOT EXISTS idx_kn_project ON knowledge (project);
CREATE INDEX IF NOT EXISTS idx_kn_tsv     ON knowledge USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS idx_kn_tags    ON knowledge USING gin (tags);
CREATE INDEX IF NOT EXISTS idx_kn_emb     ON knowledge USING hnsw (embedding vector_cosine_ops);
