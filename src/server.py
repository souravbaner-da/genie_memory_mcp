"""FastMCP server exposing Genie Code memory tools over streamable-HTTP.

Hosted as a Databricks App; the MCP endpoint is served at /mcp. Tool docstrings
double as the descriptions Genie Code uses to decide when to call each tool.
"""
from __future__ import annotations

from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import config, memory

# stateless_http: every request is independent (no sticky sessions) — the right
# choice behind a load-balanced Databricks App.
#
# transport_security: FastMCP's built-in DNS-rebinding protection rejects any
# request whose Origin/Host isn't allowlisted (localhost-only by default) with a
# 403 BEFORE CORS/the handler run. Genie Code's browser sends the workspace Origin,
# so without this "Add MCP server" fails with "could not be added". The SDK matches
# origins LITERALLY (only ":*" port wildcards), so we disable rebinding protection by
# default (safe: the app is remote and gated by Databricks Apps OAuth) and allow a
# strict exact-origin allowlist via env. (Separate from, and prior to, the Starlette
# CORSMiddleware added to the ASGI app below.)
if config.ALLOWED_ORIGINS:
    _transport_security = TransportSecuritySettings(
        allowed_origins=config.ALLOWED_ORIGINS,
        allowed_hosts=config.ALLOWED_HOSTS,
    )
else:
    _transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

mcp = FastMCP("genie-memory", stateless_http=True, transport_security=_transport_security)


# --- Boot ---------------------------------------------------------------- #
@mcp.tool()
def boot(project: Optional[str] = None) -> dict:
    """Call FIRST at session start. Returns the active project, the last session
    summary, open items, next steps, currently-active decisions, recent
    observations, and memory counts. Cheap, constant-size context."""
    return memory.boot(project)


# --- Retrieval (progressive disclosure) ---------------------------------- #
@mcp.tool()
def search(query: str, project: Optional[str] = None,
           types: Optional[list[str]] = None, limit: int = 10) -> dict:
    """Hybrid keyword+semantic search over memory. Returns a COMPACT ranked
    index (id, type, title, score) — not full content. Follow up with
    get_details(ids) for the items you actually need. types may include any of:
    observation, decision, knowledge."""
    return memory.search(query, project=project, types=types, limit=limit)


@mcp.tool()
def get_details(ids: list[str]) -> list[dict]:
    """Fetch full records for specific ids returned by search() or timeline().
    Use this only for the handful of ids you need (token-efficient)."""
    return memory.get_details(ids)


@mcp.tool()
def timeline(session_id: Optional[str] = None, project: Optional[str] = None,
             limit: int = 20) -> list[dict]:
    """Chronological observation stream for a session (session_id) or the most
    recent activity in a project. Use to reconstruct what happened and when."""
    return memory.timeline(session_id=session_id, project=project, limit=limit)


@mcp.tool()
def get_context(project: str, category: Optional[str] = None) -> list[dict]:
    """Structured project facts. category is one of: schema, naming, env, team,
    pattern (omit for all)."""
    return memory.get_context(project, category=category)


@mcp.tool()
def list_projects() -> list[dict]:
    """List all projects in memory with session counts and last activity."""
    return memory.list_projects()


# --- Capture (write-through) --------------------------------------------- #
@mcp.tool()
def start_session(project: str) -> dict:
    """Begin a session for a project. Returns a session_id to pass to subsequent
    capture calls. Call this near the start of work."""
    return memory.start_session(project)


@mcp.tool()
def record_observation(project: str, title: str, content: str,
                       tool: Optional[str] = None, kind: Optional[str] = None,
                       session_id: Optional[str] = None) -> dict:
    """Record a granular observation about work just done (a tool action, a file
    edited, a query run, a finding). This is the per-step capture layer."""
    return memory.record_observation(project, title, content, tool=tool,
                                      kind=kind, session_id=session_id)


@mcp.tool()
def save_decision(project: str, decision: str, rationale: Optional[str] = None,
                  alternatives: Optional[str] = None, tags: Optional[list[str]] = None,
                  session_id: Optional[str] = None) -> dict:
    """Save a design/technical decision IMMEDIATELY when one is made. Always
    include rationale (why) and alternatives (what was rejected)."""
    return memory.save_decision(project, decision, rationale=rationale,
                                alternatives=alternatives, tags=tags, session_id=session_id)


@mcp.tool()
def supersede_decision(old_id: str, project: str, decision: str,
                       rationale: Optional[str] = None, alternatives: Optional[str] = None,
                       tags: Optional[list[str]] = None, session_id: Optional[str] = None) -> dict:
    """Replace a prior decision: marks old_id superseded (never deleted) and
    saves the new decision. Put why the old one was replaced in rationale."""
    return memory.supersede_decision(old_id, project, decision, rationale=rationale,
                                     alternatives=alternatives, tags=tags, session_id=session_id)


@mcp.tool()
def save_context(project: str, category: str, key: str,
                 value: str, source: Optional[str] = None) -> dict:
    """Upsert a structured project fact. category: schema | naming | env | team |
    pattern. Same project/category/key updates rather than duplicates."""
    return memory.save_context(project, category, key, value, source=source)


@mcp.tool()
def save_knowledge(title: str, content: str, category: Optional[str] = None,
                   tags: Optional[list[str]] = None, project: Optional[str] = None) -> dict:
    """Save a reusable pattern, snippet, gotcha, or reference. Leave project null
    for cross-project knowledge. category: pattern | snippet | gotcha | reference."""
    return memory.save_knowledge(title, content, category=category, tags=tags, project=project)


@mcp.tool()
def end_session(session_id: str, summary: Optional[str] = None,
                next_steps: Optional[str] = None, open_items: Optional[list[str]] = None,
                decisions_made: Optional[int] = None) -> dict:
    """Pack-up: finalize a session with a summary, next steps, and open items so
    the next session boots with continuity."""
    return memory.end_session(session_id, summary=summary, next_steps=next_steps,
                              open_items=open_items, decisions_made=decisions_made)


# ASGI app for uvicorn / Databricks Apps. MCP is mounted at /mcp.
app = mcp.streamable_http_app()

# Genie Code calls this app cross-origin from the workspace UI, so allow the
# Databricks browser origins (the request is still gated by the Bearer token).
from starlette.middleware.cors import CORSMiddleware  # noqa: E402

app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https://.*\.(cloud\.databricks\.com|databricksapps\.com|azuredatabricks\.net|gcp\.databricks\.com)",
    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id"],
    allow_credentials=True,
)
