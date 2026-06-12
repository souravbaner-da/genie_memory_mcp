# Genie Code — Persistent Memory Protocol (MCP)

You have a persistent memory backed by the **`mcp-genie-memory` MCP server** (Lakebase +
pgvector). Use its tools to remember across sessions. Never write memory to files —
all memory lives behind the MCP tools below.

## Boot (every session start)

1. Call **`boot`** before answering anything. It returns the active project, the last
   session summary, open items, next steps, active decisions, recent observations, and
   counts. This is your working context — cheap and constant-size.
2. If `boot` returns `project: null`, there is no memory yet — call **`start_session`**
   once you know what the user is working on.
3. Note the `session_id` (call `start_session` if `boot` shows the last session is
   already `completed`) and pass it to every capture call this session.

Do **not** call `search` at boot. `boot` is enough to start.

## Retrieve (progressive disclosure — stay token-efficient)

When the user asks about the past, retrieve in layers:

1. **`search(query, project, types?, limit?)`** → compact ranked index (ids + titles).
   `types` ⊂ {observation, decision, knowledge}.
2. **`timeline(session_id?|project?)`** → chronological context around results.
3. **`get_details(ids)`** → full content for ONLY the ids you actually need.

Use **`get_context(project, category?)`** for structured facts (schema / naming / env /
team / pattern) and **`list_projects`** to see what exists.

Triggers → tool:
- "why did we…", "what did we decide…" → `search(types=['decision'])` → `get_details`
- "have we done this before…", "is there a pattern for…" → `search(types=['knowledge'])`
- "what schema / naming / env…" → `get_context`
- "where did we leave off…" → already in `boot`; else `timeline`

## Capture (write-through — do it immediately, in the same turn)

Saving is a tool call, not a conversational acknowledgement. When a trigger fires, CALL
THE TOOL right away.

- **Decision made** ("let's use…", "we'll go with…", "I chose…", or any resolved
  trade-off — including choices YOU make while generating code): call **`save_decision`**
  with `rationale` (why) and `alternatives` (what you rejected). To replace a prior
  decision, use **`supersede_decision`** (never delete).
- **Project fact stated** ("our schema is…", "the convention is…", "the cluster is…"):
  call **`save_context`** (category: schema | naming | env | team | pattern).
- **Reusable pattern / gotcha / snippet emerges**: call **`save_knowledge`**
  (`project=null` if it applies across projects).
- **A meaningful step completed** (file edited, query run, finding): call
  **`record_observation`** so the timeline stays complete.

Always pass `project` and the current `session_id`.

## Pack-up (session end)

When the user says "wrap up", "pack up", "done for now": call **`end_session`** with a
`summary`, `next_steps`, and `open_items` (list). Then confirm what was saved and what
the next session will boot with.

## Rules

1. `boot` first, every session. `end_session` last.
2. Save immediately, not at session end — decisions/context/knowledge persist the moment
   they're established.
3. Decisions are the most valuable memory — always include rationale and alternatives.
4. Never delete decisions — supersede them so reasoning history survives.
5. Prefer `search` → `get_details` over fetching everything; keep token use low.
6. If the MCP server is unavailable, continue working and tell the user deep memory is
   temporarily offline.
