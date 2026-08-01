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

### consolidate — merge near-duplicate memories
```
python <store.py> consolidate [--threshold 0.80] [--prune] [--dry-run] [--namespace NS]
```
Clusters live memories by embedding cosine similarity, picks the strongest as
keeper, merges metadata, and supersedes the rest with a persisted reason. Runs
automatically on SessionStart when the store has grown >20% since the last run
(min 7 days between runs). Use `--dry-run` to preview clusters without changes.
`--prune` also removes low-value never-retrieved memories (opt-in, never automatic).

Consolidation is **single-flighted**: concurrent runs (several sessions starting
at once) take an advisory lockfile in the store dir, and the losers skip cleanly
and exit 0 rather than clustering the same rows twice.

### backup — verified snapshot with retention
```
python <store.py> backup [--retention 7] [--out-dir DIR] [--if-due]
```
Writes `store-<UTC timestamp>.sqlite` into the backup dir (`--out-dir`, else
`$ZMEM_BACKUP_DIR`, else `<store dir>/backups`) using SQLite's Online Backup API,
which is safe while other sessions are writing to the store. The snapshot is
verified (`PRAGMA integrity_check` **and** a total/live row-count comparison
against the source) before it counts as a backup — if either check fails the
snapshot file is deleted, the command exits non-zero, and neither the
`last_backup` marker nor retention rotation happens.

Retention deletes only the **oldest** files matching `store-*.sqlite` beyond the
newest N. Nothing else in the directory is ever touched — including the
`prerestore-*` safety copies `restore` leaves behind. `--retention 0` disables
pruning.

`--if-due` makes the command a no-op unless `$ZMEM_BACKUP_INTERVAL_DAYS`
(default 1) has elapsed since the last successful backup; the SessionStart hook
uses it so the automatic snapshot is cheap almost every session. Without the
flag the backup always runs. Also single-flighted (its own lockfile).

### restore — recover the store from a snapshot
```
python <store.py> restore --from <snapshot.sqlite> [--force] [--out-dir DIR]
```
Refuses unless `--force` when a store already exists. Verifies the snapshot's
own `integrity_check` **before** touching the destination, then takes a
`prerestore-<timestamp>.sqlite` copy of the current store (your rollback path —
deliberately outside the retention glob so rotation can never prune it), clears
stale `-wal`/`-shm` sidecars, copies, and re-verifies the restored store.

Takes **both** maintenance locks (`backup` and `consolidate`) for its whole
duration, so it cannot race the automated background snapshot/consolidation the
SessionStart hook fires, and refuses a destination that is not on a local
filesystem (no UNC/network/OneDrive path). If either lock is held it exits **2**
without touching the destination — a skipped restore must never look like a
completed one. This does *not* serialize against a live interactive session's
own `add`/`recall` writes, which take no lock: still run `restore` when no
session is actively writing.

### export-pack — render a Tier 1 markdown memory pack
```
python <store.py> export-pack --namespace NS [--out FILE] [--project-limit 50] \
  [--global-limit 15] [--min-confidence 0.6] [--max-bytes 32768]
```
Renders live memories from `--namespace` and `user:global` (confidence DESC,
retrieval_count DESC, ingestion_ts DESC) as a hand-off markdown pack, e.g. for a
cloud/remote session with no store access. `--max-bytes` budgets the bullet
lines only: a bullet that would push past it is omitted whole (never
truncated), and smaller rows behind it are still emitted. Refuses (exit 2) if
both sections are empty.

### export-jsonl — export Tier 3 sync JSONL
```
python <store.py> export-jsonl [--out FILE] [--namespace NS] [--include-superseded]
```
Writes one memory row per line (no embeddings) for box-to-box sync via
`ingest-jsonl`. Default: all namespaces, live rows only; `--namespace` scopes
to one namespace, `--include-superseded` also exports tombstoned rows.

### ingest-jsonl — import Tier 3 sync JSONL
```
python <store.py> ingest-jsonl --in FILE [--source-ref REF] [--allow-tombstones]
```
Imports a JSONL file written by `export-jsonl`. Every row is validated before
touching the store, and a bad row is counted and reported by line number
rather than aborting the file. `--source-ref` overrides source_ref on every
row inserted this run (default: keep each row's own incoming source_ref).
`--allow-tombstones` lets an incoming superseded row tombstone a live local
row with the same id — off by default; use it only when the file is an
export of a store you trust as authoritative for those ids.

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

## Sole memory system on Claude Code (replace native)
When running under Claude Code with native memory turned off (`autoMemoryEnabled:
false` in `~/.claude/settings.json`, or `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`), ZMem
is the **only** memory system in play — it is not layered on top of CC's own
memory. In that mode:
- `store.py add` / `recall` (this skill) is the canonical, sole path for
  capturing and retrieving durable knowledge. Treat it exactly as you would
  native memory on any other host.
- **Do not hand-write** into CC's native `~/.claude/.../memory/` files or
  `MEMORY.md` — that mechanism is intentionally disabled, and writing to it
  would silently resurrect the duplicate-memory problem "replace native" exists
  to avoid.
- Project-level Tier 0 is `CLAUDE.md` (CC's own always-on mechanism, untouched
  by ZMem) — not `AGENTS.md`. The SessionStart hook does not inject `AGENTS.md`
  on Claude Code for this reason; it still does on ZCode.
- If a SessionStart nudge appears telling you native memory still looks
  enabled, that is informational for the user (a plugin cannot flip the
  setting itself) — no action needed from you beyond surfacing it once.

## The reflection loop (Loop 1)
The `zmem-reflect.sh` Stop hook checks the episodic db for failed tool calls
(status=error or exit_code!=0 on non-read-only tools) in the current session.
If found and no lesson references this session, it injects an additionalContext
prompt at stop time. It is **non-blocking** (exit 0) — it only reminds you.

Capture a lesson only if it generalizes to a future session facing a similar
situation. If the failure was a one-off (typo, transient), do nothing — the prompt
explicitly allows that. Do not capture in-trajectory refinement tweaks as durable
lessons; only capture what would help next time.
