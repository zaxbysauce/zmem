---
name: memory
description: >
  ZMem — ZCode multi-tier memory system. Provides cross-session recall, capture,
  and management of lessons, facts, conventions, and preferences. Use when you
  need to recall prior knowledge before a non-trivial task, capture a lesson at
  task end, or manage the memory store. Tier 0 (core.md/AGENTS.md) is auto-injected
  by the SessionStart hook; this skill operates Tier 2 (semantic store).
---

# Memory (ZMem)

ZCode's memory system has three tiers:
- **Tier 0 — Core:** `core.md` (user-level, in the plugin data dir) + `<repo>/AGENTS.md`
  (project-level). Auto-injected every session by the SessionStart hook. Edit
  `core.md` directly for stable rules/preferences. Keep <2KB.
- **Tier 2 — Semantic:** `store.sqlite` (in the plugin data dir). Cross-task lessons,
  facts, conventions, preferences. Operated by this skill via `scripts/store.py`.
- **Tier 4 — Procedural:** the skills library. (Extended later with evals + index.)

## Finding store.py
The SessionStart hook injects the absolute path to `store.py` into context each
session (look for `# Memory skill: invoke "...store.py" <subcommand>`). Use that
exact path. On Windows it will be a Windows-format path like
`C:\Users\...\plugins\data\zmem@...\skills\memory\scripts\store.py`.
If you cannot find the injected path, the script is at the plugin root under
`skills/memory/scripts/store.py`.

## When to use
- **Before a non-trivial task:** `recall` relevant past lessons. High-precision-first:
  if a retrieved lesson does not clearly apply, ignore it.
- **After a failure with a generalizable lesson:** `add` a lesson grounded in an
  external signal (test/compile/lint/reviewer/user). The reflection Stop hook will
  prompt you automatically when failures are detected.
- **When you learn a stable fact or convention:** `add` it.
- **When a memory is stale/wrong:** `supersede` it (tombstones it; keeps history).

## Commands

All commands run `python <store.py path> <subcommand>`. On Windows use `python`
(NOT `python3` — that is a Windows Store stub).

### recall — surface relevant memories (high-precision)
```
python <store.py> recall --query "<query>" [--namespace NS] [--limit 5] [--json]
```
Returns live (non-superseded) memories matching the query, filtered by confidence
floor (>=0.25) and namespace. Prefer `--namespace project:<basename>` to scope to
the current project; use `user:global` for cross-project.

### add — capture a memory
```
python <store.py> add \
  --namespace "project:<basename>" \
  --type <fact|lesson|convention|preference> \
  --content "<the knowledge, specific and actionable>" \
  --tags "comma,separated" \
  --signal <test|compile|lint|reviewer|user|none> \
  [--source-ref "file:<path>" | "session:<id>" | "db:<table>:<rowid>"]
```
Signal sets default confidence: test/compile/lint=high (0.85-0.9, promotable to
skills later), reviewer/user=medium (0.6), none=low (0.3, now above the 0.25
floor and reachable by recall).
Dedup-on-write: near-identical live content in the same namespace refreshes the
existing entry instead of duplicating.

### recent / search / supersede / list / get / stats
```
python <store.py> recent [--namespace NS] [--limit 5] [--min-confidence 0.5] [--json]
python <store.py> search --text "<text>" [--namespace NS] [--limit 10]
python <store.py> supersede --id <full-uuid> [--reason "..."]
python <store.py> list [--namespace NS] [--include-superseded]
python <store.py> get --id <uuid>
python <store.py> stats
```

## Hard rules
- **Never put secrets/credentials/PII in the store.** It is a local plaintext sqlite
  file. The write-time filter is advisory only (regex heuristic), not a guarantee.
- **Signal honesty:** `signal=none` means no external grounding — the lesson is the
  agent's self-opinion. Never set `signal=test` unless a test actually ran.
- **Wrap, do not replace:** this skill never writes to `tasks/<slug>/*.md` or
  `issue-traces/<issue>/*.md`. Those are durable-session-state's source of truth.
- **High-precision-first:** if a recalled memory does not clearly apply, ignore it.
  Retrieved-wrong memory hurts more than retrieved-nothing.
- **Source refs:** prefer immutable sources (`db:`, `session:`). For mutable markdown
  (`file:`), a content hash is stored and checked on recall — if the source changed
  since extraction, the memory's confidence is halved and flagged `[STALE SOURCE]`.
  `file:` paths may be Windows (`C:\Users\...`) or Cygwin (`/c/Users/...`) — both are
  auto-normalized. If a `file:` ref cannot be opened, a stderr warning is emitted.

## How recall works
FTS5 keyword match intersected with namespace filter and confidence floor, with
source-staleness demotion. Results are re-ranked by a **composite score** that
combines:

- **BM25 relevance** (55%) — the FTS5 keyword match score
- **Confidence** (20%) — grounded by signal tier (test/compile > reviewer/user > none)
- **Recency** (15%) — exponential decay with a 90-day half-life
- **Popularity** (10%) — retrieval frequency with diminishing returns (sqrt dampening)

Confidence is still a hard floor (below 0.25 is dropped before scoring).
Staleness demotion halves confidence, which feeds into the confidence component.
Keyword-first, not semantic — vector/embedding recall is a future optional tier.

The `rebuild-fts` subcommand rebuilds the FTS5 index from scratch (useful after
bulk imports or if the index drifts):
```
python <store.py> rebuild-fts
```

## The reflection loop (Loop 1)
The `zmem-reflect.sh` Stop hook checks the episodic db for failed tool calls
(status=error or exit_code!=0 on non-read-only tools) in the current session.
If found and no lesson references this session, it injects an additionalContext
prompt at stop time. It is **non-blocking** (exit 0) — it only reminds you.

Capture a lesson only if it generalizes to a future session facing a similar
situation. If the failure was a one-off (typo, transient), do nothing — the prompt
explicitly allows that. Do not capture in-trajectory refinement tweaks as durable
lessons; only capture what would help next time.
