"""Core memory operations backed by Lakebase (Postgres + pgvector).

Retrieval follows claude-mem's progressive-disclosure pattern:
  1. search()      -> compact ranked index (id, type, title, score)   [cheap]
  2. timeline()    -> chronological neighbours for context
  3. get_details() -> full rows for a chosen set of ids                [on demand]

search() fuses keyword (tsvector) and semantic (pgvector) rankers with
Reciprocal Rank Fusion, so it works hybrid when embeddings are available and
degrades to keyword-only when they are not.
"""
from __future__ import annotations

import json
import pathlib
import uuid
from datetime import datetime, timezone

import psycopg

from . import config, db, embeddings

# Per-type projection so search can return a normalized (id, title, ts) shape.
TYPE_CFG = {
    "observation": {"table": "observations", "title": "title",    "ts": "ts",         "status": None,     "proj_nullable": False},
    "decision":    {"table": "decisions",    "title": "decision", "ts": "created_at", "status": "active", "proj_nullable": False},
    "knowledge":   {"table": "knowledge",    "title": "title",    "ts": "created_at", "status": None,     "proj_nullable": True},
}

_SCHEMA_SQL = pathlib.Path(__file__).resolve().parent.parent / "sql" / "schema.sql"


# --------------------------------------------------------------------------- #
# Schema bootstrap — run on app startup so the app owns its tables (no GRANT).
# --------------------------------------------------------------------------- #
def init_schema() -> int:
    """Create extensions/tables/indexes idempotently from sql/schema.sql.

    Every DDL statement uses IF NOT EXISTS, so this is safe to run on every boot.
    When the app's service principal runs this, it OWNS the tables, so no separate
    GRANT is needed. Returns the number of statements applied; raises on failure
    (callers fail fast so a misconfigured DB surfaces at startup, not mid-request).
    """
    statements = [s.strip() for s in _SCHEMA_SQL.read_text().split(";") if s.strip()]
    sch = config.PG_SCHEMA
    applied = 0
    with db.connection() as conn, conn.cursor() as cur:
        if sch and sch != "public":
            # The connection's search_path already points here (see db._configure);
            # creating the app-owned schema lets the app own its tables (no GRANT).
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{sch}"')
        for stmt in statements:
            try:
                cur.execute(stmt)            # autocommit: each statement is independent
                applied += 1
            except psycopg.errors.InsufficientPrivilege as exc:
                # Tolerated only for existing-object cases, e.g. PG_SCHEMA=public where
                # the SP lacks CREATE but the object already exists (owned by someone
                # else). Real errors (bad SQL, etc.) still raise. Other failures are
                # NOT swallowed.
                print(f"[init_schema] skipped (insufficient privilege): "
                      f"{str(exc).strip().splitlines()[0][:90]}", flush=True)
    return applied


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _q(sql: str, params: dict | None = None, fetch: str = "all"):
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(sql, params or {})
        if fetch == "all":
            return cur.fetchall()
        if fetch == "one":
            return cur.fetchone()
        return None


def _vec(emb: list[float] | None) -> str | None:
    """Render an embedding as a pgvector text literal: '[0.1,0.2,...]'."""
    if emb is None:
        return None
    return "[" + ",".join(format(float(x), ".7g") for x in emb) + "]"


def _clean(row: dict | None) -> dict | None:
    """Make a DB row JSON-serializable (timestamps -> iso, uuid -> str)."""
    if row is None:
        return None
    out = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            out[k] = v.isoformat()
        elif isinstance(v, uuid.UUID):
            out[k] = str(v)
        else:
            out[k] = v
    return out


def _proj_clause(cfg: dict, project: str | None) -> str:
    if project is None:
        return ""
    if cfg["proj_nullable"]:
        return "(project = %(project)s OR project IS NULL)"
    return "project = %(project)s"


# --------------------------------------------------------------------------- #
# Retrieval (progressive disclosure)
# --------------------------------------------------------------------------- #
def _kw_search(t: str, query: str, project: str | None) -> list[dict]:
    cfg = TYPE_CFG[t]
    where = ["search_tsv @@ plainto_tsquery('english', %(q)s)"]
    pc = _proj_clause(cfg, project)
    if pc:
        where.append(pc)
    if cfg["status"]:
        where.append(f"status = '{cfg['status']}'")
    sql = f"""
        SELECT id, {cfg['title']} AS title, {cfg['ts']} AS ts
        FROM {cfg['table']}
        WHERE {' AND '.join(where)}
        ORDER BY ts_rank_cd(search_tsv, plainto_tsquery('english', %(q)s)) DESC
        LIMIT %(pool)s
    """
    return _q(sql, {"q": query, "project": project, "pool": config.SEARCH_POOL})


def _vec_search(t: str, emb_literal: str, project: str | None) -> list[dict]:
    cfg = TYPE_CFG[t]
    where = ["embedding IS NOT NULL"]
    pc = _proj_clause(cfg, project)
    if pc:
        where.append(pc)
    if cfg["status"]:
        where.append(f"status = '{cfg['status']}'")
    params = {"qe": emb_literal, "project": project, "pool": config.SEARCH_POOL}
    # Optional relevance floor: drop semantic hits below a min cosine similarity.
    # `<=>` is cosine distance (0=identical); similarity = 1 - distance. Off when
    # VECTOR_MIN_SCORE == 0 (return nearest neighbours regardless — default).
    if config.VECTOR_MIN_SCORE > 0:
        where.append("(embedding <=> %(qe)s::vector) <= %(maxdist)s")
        params["maxdist"] = 1.0 - config.VECTOR_MIN_SCORE
    sql = f"""
        SELECT id, {cfg['title']} AS title, {cfg['ts']} AS ts
        FROM {cfg['table']}
        WHERE {' AND '.join(where)}
        ORDER BY embedding <=> %(qe)s::vector
        LIMIT %(pool)s
    """
    return _q(sql, params)


def search(query: str, project: str | None = None,
           types: list[str] | None = None, limit: int = 10) -> dict:
    """Hybrid keyword+semantic search returning a compact ranked index."""
    types = [t for t in (types or list(TYPE_CFG)) if t in TYPE_CFG]
    emb_literal = _vec(embeddings.embed_one(query))

    rank_lists: list[list[dict]] = []
    for t in types:
        rank_lists.append([dict(r, _t=t) for r in _kw_search(t, query, project)])
        if emb_literal is not None:
            rank_lists.append([dict(r, _t=t) for r in _vec_search(t, emb_literal, project)])

    # Reciprocal Rank Fusion across every ranker.
    scores: dict[tuple, float] = {}
    meta: dict[tuple, dict] = {}
    for rows in rank_lists:
        for rank, r in enumerate(rows):
            key = (r["_t"], str(r["id"]))
            scores[key] = scores.get(key, 0.0) + 1.0 / (config.RRF_K + rank + 1)
            meta[key] = r

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    results = []
    for (t, id_), score in ranked:
        r = meta[(t, id_)]
        results.append({
            "id": id_,
            "type": t,
            "title": r["title"],
            "ts": r["ts"].isoformat() if r["ts"] else None,
            "score": round(score, 5),
        })
    return {"query": query, "project": project,
            "semantic": emb_literal is not None, "results": results}


def get_details(ids: list[str]) -> list[dict]:
    """Fetch full rows (no embeddings) for a set of ids across all memory types."""
    ids = [str(i) for i in ids]
    if not ids:
        return []
    rows: list[dict] = []
    rows += _q("""SELECT id, 'observation' AS type, project, session_id, ts,
                         tool, kind, title, content
                  FROM observations WHERE id = ANY(%(ids)s::uuid[])""", {"ids": ids})
    rows += _q("""SELECT id, 'decision' AS type, project, session_id, created_at AS ts,
                         decision AS title, rationale, alternatives, tags, status
                  FROM decisions WHERE id = ANY(%(ids)s::uuid[])""", {"ids": ids})
    rows += _q("""SELECT id, 'knowledge' AS type, project, created_at AS ts,
                         title, content, category, tags, usage_count
                  FROM knowledge WHERE id = ANY(%(ids)s::uuid[])""", {"ids": ids})
    return [_clean(r) for r in rows]


def timeline(session_id: str | None = None, project: str | None = None,
             limit: int = 20) -> list[dict]:
    """Chronological observation stream, scoped to a session or a project."""
    if session_id:
        rows = _q("""SELECT id, 'observation' AS type, ts, tool, kind, title
                     FROM observations WHERE session_id = %(s)s
                     ORDER BY ts LIMIT %(l)s""", {"s": session_id, "l": limit})
    else:
        rows = _q("""SELECT id, 'observation' AS type, project, ts, tool, kind, title
                     FROM observations
                     WHERE (%(p)s::text IS NULL OR project = %(p)s)
                     ORDER BY ts DESC LIMIT %(l)s""", {"p": project, "l": limit})
    return [_clean(r) for r in rows]


def get_context(project: str, category: str | None = None) -> list[dict]:
    """Structured project facts (schema/naming/env/team/pattern)."""
    rows = _q("""SELECT category, key, value, source, updated_at
                 FROM context_items
                 WHERE project = %(p)s AND (%(c)s::text IS NULL OR category = %(c)s)
                 ORDER BY category, key""", {"p": project, "c": category})
    return [_clean(r) for r in rows]


def boot(project: str | None = None) -> dict:
    """Session-start context: active project, last session, live decisions, counts."""
    if project is None:
        row = _q("SELECT project FROM sessions ORDER BY started_at DESC LIMIT 1", fetch="one")
        project = row["project"] if row else None
    if project is None:
        return {"project": None,
                "message": "No memory yet. Call start_session to begin."}

    last = _q("""SELECT session_id, started_at, ended_at, summary, next_steps,
                        open_items, status
                 FROM sessions WHERE project = %(p)s
                 ORDER BY started_at DESC LIMIT 1""", {"p": project}, "one")
    decisions = _q("""SELECT id, decision, created_at FROM decisions
                      WHERE project = %(p)s AND status = 'active'
                      ORDER BY created_at DESC LIMIT 10""", {"p": project})
    recent = _q("""SELECT id, ts, title FROM observations
                   WHERE project = %(p)s ORDER BY ts DESC LIMIT 5""", {"p": project})
    counts = _q("""SELECT
        (SELECT count(*) FROM decisions     WHERE project = %(p)s)                       AS decisions,
        (SELECT count(*) FROM context_items WHERE project = %(p)s)                       AS context_items,
        (SELECT count(*) FROM sessions      WHERE project = %(p)s)                       AS sessions,
        (SELECT count(*) FROM observations  WHERE project = %(p)s)                       AS observations,
        (SELECT count(*) FROM knowledge     WHERE project = %(p)s OR project IS NULL)    AS knowledge
    """, {"p": project}, "one")

    return {
        "project": project,
        "last_session": _clean(last),
        "active_decisions": [_clean(d) for d in decisions],
        "recent_observations": [_clean(o) for o in recent],
        "counts": counts,
    }


def list_projects() -> list[dict]:
    rows = _q("""SELECT project, count(*) AS sessions, max(started_at) AS last_activity
                 FROM sessions GROUP BY project ORDER BY last_activity DESC""")
    return [_clean(r) for r in rows]


# --------------------------------------------------------------------------- #
# Capture (write-through)
# --------------------------------------------------------------------------- #
def start_session(project: str) -> dict:
    sid = "sess_" + _now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    _q("INSERT INTO sessions (session_id, project, status) VALUES (%(s)s, %(p)s, 'active')",
       {"s": sid, "p": project}, fetch=None)
    return {"session_id": sid, "project": project}


def record_observation(project: str, title: str, content: str,
                       tool: str | None = None, kind: str | None = None,
                       session_id: str | None = None) -> dict:
    emb = _vec(embeddings.embed_one(f"{title}\n{content}"))
    oid = str(uuid.uuid4())
    _q("""INSERT INTO observations (id, session_id, project, tool, kind, title, content, embedding)
          VALUES (%(id)s, %(s)s, %(p)s, %(tool)s, %(kind)s, %(title)s, %(content)s, %(emb)s::vector)""",
       {"id": oid, "s": session_id, "p": project, "tool": tool, "kind": kind,
        "title": title, "content": content, "emb": emb}, fetch=None)
    return {"id": oid}


def save_decision(project: str, decision: str, rationale: str | None = None,
                  alternatives: str | None = None, tags: list[str] | None = None,
                  session_id: str | None = None) -> dict:
    emb = _vec(embeddings.embed_one(f"{decision}\n{rationale or ''}"))
    did = str(uuid.uuid4())
    _q("""INSERT INTO decisions (id, project, decision, rationale, alternatives, tags, session_id, embedding)
          VALUES (%(id)s, %(p)s, %(d)s, %(r)s, %(a)s, %(tags)s, %(s)s, %(emb)s::vector)""",
       {"id": did, "p": project, "d": decision, "r": rationale, "a": alternatives,
        "tags": tags or [], "s": session_id, "emb": emb}, fetch=None)
    return {"id": did}


def supersede_decision(old_id: str, project: str, decision: str,
                       rationale: str | None = None, alternatives: str | None = None,
                       tags: list[str] | None = None, session_id: str | None = None) -> dict:
    _q("UPDATE decisions SET status = 'superseded', updated_at = now() WHERE id = %(id)s::uuid",
       {"id": old_id}, fetch=None)
    new = save_decision(project, decision, rationale, alternatives, tags, session_id)
    return {"superseded": old_id, "new_id": new["id"]}


def save_context(project: str, category: str, key: str,
                 value: str, source: str | None = None) -> dict:
    _q("""INSERT INTO context_items (project, category, key, value, source)
          VALUES (%(p)s, %(c)s, %(k)s, %(v)s, %(src)s)
          ON CONFLICT (project, category, key)
          DO UPDATE SET value = EXCLUDED.value, source = EXCLUDED.source, updated_at = now()""",
       {"p": project, "c": category, "k": key, "v": value, "src": source}, fetch=None)
    return {"project": project, "category": category, "key": key, "status": "upserted"}


def save_knowledge(title: str, content: str, category: str | None = None,
                   tags: list[str] | None = None, project: str | None = None) -> dict:
    emb = _vec(embeddings.embed_one(f"{title}\n{content}"))
    kid = str(uuid.uuid4())
    _q("""INSERT INTO knowledge (id, project, title, content, category, tags, embedding)
          VALUES (%(id)s, %(p)s, %(t)s, %(c)s, %(cat)s, %(tags)s, %(emb)s::vector)""",
       {"id": kid, "p": project, "t": title, "c": content, "cat": category,
        "tags": tags or [], "emb": emb}, fetch=None)
    return {"id": kid}


def end_session(session_id: str, summary: str | None = None, next_steps: str | None = None,
                open_items: list[str] | None = None, decisions_made: int | None = None) -> dict:
    _q("""UPDATE sessions SET
              ended_at       = now(),
              summary        = COALESCE(%(sum)s, summary),
              next_steps     = COALESCE(%(ns)s, next_steps),
              open_items     = COALESCE(%(oi)s::jsonb, open_items),
              decisions_made = COALESCE(%(dm)s, decisions_made),
              status         = 'completed'
          WHERE session_id = %(s)s""",
       {"s": session_id, "sum": summary, "ns": next_steps,
        "oi": json.dumps(open_items) if open_items is not None else None,
        "dm": decisions_made}, fetch=None)
    return {"session_id": session_id, "status": "completed"}
