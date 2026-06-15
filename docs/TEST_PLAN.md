# Genie Code Memory — End-to-End Test Plan

A multi-session conversation script to verify the `mcp-genie-memory` system across **all
tools and edge cases**, run inside **Genie Code (Agent mode)**. Each step lists the prompt,
the tool(s) that should fire, and what "correct" looks like.

> **Backend pre-verified.** Every scenario below was first run programmatically against the
> live MCP server: **20/21 checks passed** (the 1 "fail" was a wrong test assumption — see
> [Edge-case notes](#edge-case-notes)). This script confirms the same behaviors end-to-end
> through the Genie Code client.

> **Keep real memory clean:** these use throwaway projects `qa-test` / `qa-test-2`. Purge
> them when done (see [Cleanup](#cleanup)).

> **Want a realistic example instead of `qa-test`?** See
> [`FDE_WALKTHROUGH.md`](./FDE_WALKTHROUGH.md) — the same tools and behaviors driven through
> a multi-day customer POC, where the payoff is a new session booting with full continuity.
> This matrix stays deliberately minimal for systematic tool coverage; the walkthrough shows
> the *why*.

Tip: tools only fire in **Agent mode**. If Genie acknowledges without calling a tool,
prompt explicitly: *"use the mcp-genie-memory tools to …"*.

---

## Session A — Capture (decisions, context, knowledge, observations)

| # | Type this | Should call | Pass criteria |
|---|---|---|---|
| A1 | "Boot my memory for project **qa-test**." | `boot` | Reports project `qa-test`, "no memory yet" / counts all 0 |
| A2 | "We'll use **Delta Live Tables** for ingestion because it handles incremental loads and schema evolution. Save that decision." | `save_decision` | Tool trace shows `save_decision` with rationale + alternatives (not just an ack) |
| A3 | "Our bronze schema is **main.qa.bronze** and we name tables in **snake_case**. Remember both." | `save_context` ×2 | Two context rows: `schema`/`naming` |
| A4 | "Save a pattern: to dedupe a CDC stream, **MERGE on the primary key keeping the latest timestamp**." | `save_knowledge` | Knowledge row, category `pattern`, project `qa-test` |
| A5 | "Remember a cross-project gotcha: **Photon doesn't accelerate Python UDFs**. It applies to all projects." | `save_knowledge` (project = null) | Saved with no project (cross-project) |
| A6 | "Write SQL for the top 5 customers by revenue this quarter." | (work) + ideally `save_decision` | Self-recording: it may save its own approach decision. *Optional — note if it does/doesn't* |
| A7 | "Wrap up." | `end_session` | Confirms summary + next steps + open items saved |

---

## Session B — Recall (OPEN A NEW NOTEBOOK / FRESH CHAT)

The point: Session B has none of A's context except what memory returns.

| # | Type this | Should call | Pass criteria |
|---|---|---|---|
| B1 | "Boot my memory for **qa-test**." | `boot` | Shows A's last-session summary, open items, the active decision, counts > 0 |
| B2 | "Why did we choose DLT for ingestion?" | `search`→`get_details` | Returns the A2 rationale + alternatives |
| B3 | **(semantic)** "What's our approach for **incremental data loading**?" | `search` (semantic) | Recalls the DLT decision **even though you didn't say "DLT"** ← key test |
| B4 | "What's our bronze schema and naming convention?" | `get_context` | Returns `main.qa.bronze` + `snake_case` |
| B5 | "Have we figured out handling **duplicate CDC records**?" | `search`(knowledge)→`get_details` | Returns the MERGE pattern |
| B6 | "Anything I should know about **Photon performance**?" | `search`(knowledge) | Returns the cross-project UDF gotcha |

✅ If B1–B6 recall things only said in Session A, **cross-session memory works**.

---

## Session C — Update / supersede / upsert

| # | Type this | Should call | Pass criteria |
|---|---|---|---|
| C1 | "Actually, switch from DLT to **Lakeflow Declarative Pipelines** for ingestion — better governance. Update that decision." | `supersede_decision` | Old decision → `superseded`, new one → `active` |
| C2 | "Boot, then what's our **current** ingestion decision?" | `boot` / `search` | Returns ONLY Lakeflow (the superseded DLT one must NOT appear) ← key test |
| C3 | "Correction: our bronze schema is now **main.qa_v2.bronze**." | `save_context` (upsert) | Updates in place — must NOT create a 2nd `schema/bronze` row |
| C4 | "What's our bronze schema now?" | `get_context` | Exactly one entry, value `main.qa_v2.bronze` ← key test |

---

## Session D — Timeline, multi-project, isolation

| # | Type this | Should call | Pass criteria |
|---|---|---|---|
| D1 | "Show the timeline of what we did in qa-test." | `timeline` | Chronological list of this project's observations/steps |
| D2 | "Start a new project **qa-test-2** and boot." | (`end_session` qa-test) + `start_session`/`boot` | Packs up qa-test, fresh empty boot for qa-test-2 |
| D3 | "In this project, did we decide anything about ingestion?" | `search` | **Does NOT** return qa-test's decision (project isolation) ← key test |
| D4 | "Anything about Photon performance?" | `search`(knowledge) | **DOES** return the cross-project gotcha (project=null is global) ← key test |
| D5 | "List all my projects." | `list_projects` | Shows `qa-test` and `qa-test-2` |
| D6 | "Switch back to qa-test." | (pack up) + `boot` | Reconstructs qa-test context (last session, decisions) |

---

## Session E — Robustness / negative / explicit-invoke

| # | Type this | Should call | Pass criteria |
|---|---|---|---|
| E1 | "Search memory for our **Kubernetes deployment** strategy." (never discussed) | `search` | No *relevant* hit — Genie should say it has nothing on that. (Note: semantic search returns nearest neighbours, so it may surface loosely-related rows; Genie should judge relevance and say none apply.) |
| E2 | (if a save didn't fire) "**Use the mcp-genie-memory tools** to save that as a decision." | the named tool | Explicit invocation works |
| E3 | "Wrap up qa-test." | `end_session` | Final pack-up |

---

## Edge-case notes

What the programmatic suite proved (so if Genie misbehaves, it's a prompting/Agent-mode
issue, not the backend):

- **Semantic paraphrase recall** — a query with *zero keyword overlap* still recalls the
  right decision via pgvector (`semantic=true`). (B3)
- **Supersede** — superseded decisions are hidden from `boot` and `search`; only the active
  one is returned. History is preserved (never deleted). (C1–C2)
- **Upsert context** — same project/category/key updates in place; never duplicates. (C3–C4)
- **Project isolation** — decisions/observations are scoped to their project. (D3)
- **Cross-project knowledge** — `project = null` knowledge is found from any project. (D4)
- **Relevance floor (now configurable)** — by default semantic search returns nearest
  neighbours, so a nonsense query still returns the store's closest rows (low relevance) and
  the agent judges relevance — this matches claude-mem/Chroma behaviour. To make off-topic
  queries return *nothing*, set **`VECTOR_MIN_SCORE`** (min cosine similarity, `0`=off).
  **Deployed at `0.5`** (empirically tuned: gte-large-en scores ~0.30 for unrelated and
  ~0.59–0.71 for on-topic, so 0.5 filters off-topic while keeping relevant/adjacent — note
  `0.3` was a no-op for this model). Verified live: ANN-paraphrase + adjacent queries return
  hits; kubernetes/kafka and travel-policy queries return 0. So **step E1 now returns
  "nothing found"** for off-topic queries. Set in `databricks.yml` `config.env`.

## Cleanup

After testing, purge the throwaway projects (Lakebase SQL Editor):

```sql
DELETE FROM observations  WHERE project IN ('qa-test','qa-test-2');
DELETE FROM decisions     WHERE project IN ('qa-test','qa-test-2');
DELETE FROM context_items WHERE project IN ('qa-test','qa-test-2');
DELETE FROM sessions      WHERE project IN ('qa-test','qa-test-2');
DELETE FROM knowledge     WHERE project IN ('qa-test','qa-test-2') OR 'photon' = ANY(tags);
```
