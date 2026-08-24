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
- **Tier 0 — Core:** `core.md` (user-level, in the canonical shared store dir)
  + `<repo>/AGENTS.md` (project-level on ZCode). Auto-injected every session by
  the SessionStart hook. Edit `core.md` directly for stable rules/preferences.
  Keep <2KB.
- **Tier 2 — Semantic:** `store.sqlite` (in the plugin data dir). Cross-task lessons,
  facts, conventions, preferences. Operated by this skill via `scripts/store.py`.
- **Tier 4 — Procedural:** the skills library. (Extended later with evals + index.)

## Finding store.py
The SessionStart hook injects the absolute path to `store.py` into context each
session (look for `# Memory skill: invoke "...store.py" <subcommand>`). Use that
exact path. On Windows it will be a Windows-format path like
`C:\Users\...\plugins\data\zmem@...\skills\memory\scripts\store.py`
(the `...` segments are elision placeholders, not real paths).
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

Store commands run `python <store.py path> <subcommand>`; `doctor` is the one
separate diagnostic script shown below. On Windows use `python`
(NOT `python3` — that is a Windows Store stub).

### doctor — read-only install diagnostics
```
python <doctor.py path> [--project <repo>] [--repo-root <zmem-repo>] [--format human|json|both]
```
Read-only preflight for cutover and operator debugging. It never mutates the
store or host config. Checks:
- resolved store path and split-brain env/config risk
- local/non-OneDrive store path safety
- Python version (supported floor 3.11) + SQLite FTS5
- Node and a usable Git Bash/Cygwin shell on Windows
- best-effort read/write access to the store path
- schema compatibility against current v8
- Claude/Codex native-memory conflicts via read-only config inspection
- canonical namespace for the provided project
- host surface presence (Claude plugin, ZCode plugin, memory skill; repo-local
  Codex adapter files are optional until that lane exists)

Use it before first install, before cutover, and after any store-path or hook
surface change.

### recall — surface relevant memories (high-precision)
```
python <store.py> recall --query "<query>" [--namespace NS] [--limit 5]
                          [--include-global] [--global-limit 3] [--hybrid]
                          [--no-hybrid] [--no-bump] [--as-of ISO-8601] [--json]
```
Returns live (non-superseded) memories matching the query, filtered by confidence
floor (>=0.25) and namespace. Prefer `--namespace project:<basename>` to scope to
the current project; use `user:global` for cross-project.

#### Confidence floors (issue #58, 3.8)

Three distinct floors live on the recall path. Each reflects a different
surface's precision-vs-coverage tradeoff. They are env-overridable; the
constants live in `schema_meta.py`.

| Constant | Default | Env override | Used by |
|---|---|---|---|
| `INJECT_FLOOR_PROMPT_DEFAULT` | 0.25 | `ZMEM_INJECT_FLOOR_PROMPT` | `recall` (UserPromptSubmit / PreCompact). Hard floor on FTS/vec results — anything below is dropped before scoring. |
| `INJECT_FLOOR_RECENT_DEFAULT` | 0.5 | `ZMEM_INJECT_FLOOR_RECENT` | `recent` (SessionStart / subagent recall). Tighter because the surface is high-confidence recent material, not query-best match. |
| `INJECT_FLOOR_GATE_NONE` | 0.4 | `ZMEM_INJECT_FLOOR_GATE_NONE` | Hook selective-inject gate. `signal=none` rows must clear this floor; grounded-signal rows (`test`/`compile`/`lint`/`reviewer`/`user`) keep the 0.25 floor. |

The three floors are intentional. Do not silently unify them. The
selective-inject gate (3.8) is a hook-only filter; it does not change
the Python recall path.

`--include-global` (opt-in) ALSO surfaces up to `--global-limit` query-relevant
rows from the `user:global` tier, merged project-first so a global row never
crowds out a project row. The three automatic hooks pass this so cross-project
lessons reach project-scoped sessions. Without it, behaviour is strict-namespaced
(byte-identical to before). When you want the global tier unioned in but still
want a per-tier budget, use `recall --namespace project:<x> --include-global`
rather than going unscoped.

`--hybrid` (opt-in) adds a vector lane on top of the FTS5/BM25 keyword lane:
the query is embedded and matched against stored embeddings (sqlite-vec KNN),
then both lanes' rankings are fused with Reciprocal Rank Fusion (RRF, k=60).
It requires the optional embedding runtime (onnxruntime + tokenizers, model
lazy-downloaded and checksum-verified) and fails open: when the runtime or
model is unavailable, recall silently uses plain keyword ranking — same
results as without the flag, never an error.

`--no-bump` (opt-in) makes a recall **passive**: it records a surface event
(`surfaced_count`/`last_surfaced`) instead of advancing `retrieval_count`/
`last_retrieved`. The explicit-vs-passive split is the system contract: the
three automatic hooks and the Hermes provider prefetch are passive surfaces
(they pass `--no-bump` — a background injection is not a usefulness signal);
explicit recall — this CLI without `--no-bump`, the MCP `recall`/`search`
tools, and the Hermes `MemoryProvider` search tool — intentionally bumps
`retrieval_count`, because an explicit read IS evidence the memory was useful
(issue #21).

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
Signal sets default confidence: test/compile=0.9, lint=0.85, reviewer/user=0.6
(medium), none=0.2 — deliberately BELOW the 0.25 recall floor: an ungrounded
lesson is the agent's self-opinion and never surfaces in default recall (still
reachable via `search --text`, which applies no confidence floor, or
`recent --min-confidence 0`) (#36 M3).
Dedup-on-write: near-identical live content in the same namespace refreshes the
existing entry instead of duplicating.
Namespace validation: obvious misspellings of the global namespace (`global`,
`userglobal`, `users:global`, …) are rejected at `add` time AND on `ingest-jsonl`
sync import with a message naming the canonical `user:global` — such rows would
be unreachable from the automatic hooks. `project:<x>` and arbitrary namespaces
pass through untouched. Legacy rows already stranded under a near-miss namespace
(before this guard existed) can be remediated with `rekey-namespace
--near-miss-global --confirm` (see below).

### recent / search / supersede / list / get / stats
```
python <store.py> recent [--namespace NS] [--limit 5] [--min-confidence 0.5]
                         [--include-global] [--global-limit 3] [--as-of ISO-8601]
                         [--json]
python <store.py> search --text "<text>" [--namespace NS] [--limit 10]
                        [--include-global] [--global-limit 3] [--no-bump]
                        [--as-of ISO-8601]
python <store.py> supersede --id <full-uuid> [--reason "..."]
python <store.py> list [--namespace NS] [--include-superseded]
python <store.py> get --id <uuid>
python <store.py> stats
```
`recent`/`search` accept the same `--include-global`/`--global-limit` pair as
`recall` (project-first global tier union). `recent` now ALSO honours v5
migration aliases (so `recent --namespace <old pre-v5 key>` finds rows migrated
to the new key). `search` now accepts `--no-bump` for a *passive* query that records a
surface on `surfaced_count` (never advancing `retrieval_count`) instead of bumping like
`recall` (issue #21).

### reembed — backfill semantic embeddings
```
python <store.py> reembed
```
Backfills missing embeddings for live memories when the optional embedding
runtime and model are available. Existing embeddings are preserved.

### promote — review and install a reusable skill
```
python <store.py> promote --dry-run [--namespace NS]
python <store.py> promote --id <uuid> --confirm [--description "..."]
python <store.py> promote --id <uuid> --confirm --install-approved
```
`--confirm` writes a review candidate; `--install-approved` is the additional
explicit gate that installs the reviewed skill into Codex, Claude Code, and
ZCode skill directories. Promotion is never an unattended hook action.

### consolidate — merge near-duplicate memories
```
python <store.py> consolidate [--threshold 0.80] [--prune] [--dry-run] [--namespace NS] [--force] [--merge-contested] [--json]
```
Clusters live memories by embedding cosine similarity (Jaccard token overlap when
embeddings are unavailable), picks a keeper, merges metadata, and supersedes the
rest with a persisted reason. Runs automatically on SessionStart once 7 days
have elapsed since the last run OR the live store has grown >20% since then (an
automatic run is skipped only when the last run was <7 days ago AND growth was
<20%; both bounds are env-tunable via `ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS` /
`ZMEM_CONSOLIDATE_GROWTH_THRESHOLD`). When the gate declines, the run prints a
single `[zmem] consolidate: skipped by cadence gate (...)` line (never silent)
and changes nothing. Use `--dry-run` to preview clusters and the exact merge
decision per row without mutating the store — `--dry-run` models the cadence
gate too, so a gated dry run reports `would skip by cadence gate` rather than
`would merge N`, and a dry run that says "would merge" implies a real run that
merges. Pass `--force` to bypass the cadence gate and run consolidation now
(this is the only intentional bypass; `--threshold` does not affect the gate).

Keeper selection: within a cluster, the survivor is the row with the highest
`confidence * (retrieval_count + surfaced_count)` product — total surface events,
blend-aware for hook-surfaced memories (ties broken by confidence, then earliest
ingestion) (issue #21). Absorbed rows are NOT lost on merge: a near-duplicate that is not
fully subsumed by the keeper has its ENTIRE text appended to the keeper's content
(live-recallable + FTS-indexed), and its id is recorded in the keeper's
`merged_from` provenance column, so consolidated memory is non-lossy. Note this
is a whole-text append, not a unique-tail diff — a partially-overlapping row
duplicates the span it already shares with the keeper (deliberate
over-preservation, bounded by the size cap below). Reordered same-token phrasing
(e.g. "call foo before bar" vs "call bar before foo") is likewise preserved as
distinct content — only genuinely subsumed/duplicate text is omitted.

Size cap: a keeper's content is never allowed to exceed
`INGEST_MAX_CONTENT_CHARS` (the JSONL round-trip ceiling, 65536). An absorb that
would push the keeper over the cap is NOT appended — its id is recorded with a
`:truncated` marker in `merged_from` and the absorbed row is still superseded;
that is the one bounded case where an absorbed row's text is not migrated live
(it remains only in the tombstoned history row). `--dry-run` shows would-lose
tokens for this case.
`--prune` also removes low-value NEVER-surfaced and never-retrieved memories
(`retrieval_count = 0` AND `surfaced_count = 0`, low confidence, old, `signal = none`;
opt-in, never automatic). A hook-surfaced memory is protected — `retrieval_count = 0`
alone is not evidence of unused (issue #21).

Consolidation is **single-flighted**: concurrent runs (several sessions starting
at once) take an advisory lockfile in the store dir, and the losers skip cleanly
and exit 0 rather than clustering the same rows twice.

### rekey-namespace — remediate stranded namespace rows (admin)
```
python <store.py> rekey-namespace --near-miss-global [--to user:global] [--dry-run] --confirm
python <store.py> rekey-namespace --from <old-namespace> --to <new-namespace> [--dry-run] --confirm
```
Rewrites the `namespace` column of live rows. The primary use is remediating
legacy rows stranded under a global near-miss namespace (`global`,
`userglobal`, …) that pre-date the `add`/`ingest-jsonl` write-time guard — such
rows are unreachable from the automatic hooks. `--near-miss-global` rekeys EVERY
live near-miss row to `--to` (default `user:global`); `--from` rekeys one exact
namespace. `--to` may not itself be a near-miss. `--confirm` is required to
write (without it, or with `--dry-run`, the command only previews). Superseded
rows are left untouched (history preserved).

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

### sweep — prune stale per-session cooldown sentinels
```
python <store.py> sweep [--marker-dir DIR] [--max-age-days 7] [--dry-run]
```
Removes the `.capture-prompted-<session>` / `.convention-prompted-<session>`
cooldown markers the capture/convention hooks leave in the data dirs (issue #23).
A marker is only meaningful for the session named in its filename, so anything
older than `--max-age-days` (default `$ZMEM_SENTINEL_SWEEP_DAYS`, else 7) is
garbage. Sweeps the union of every dir the two hooks can write into (their
resolution chains differ), never touching anything that is not a sentinel-prefixed
file. Idempotent and fail-open. The SessionStart hook fires it detached each
session so the markers stay bounded; `--dry-run` counts without deleting.

### corrections — mine user corrections from a transcript (read-only)
```
python <store.py> corrections --transcript <path> [--json]
```
Scans a Claude Code transcript JSONL for user corrections. (User *rejections*
are a separate signal — reported by `store.py failures` and surfaced by the
Stop / SubagentStop reflection hooks.) Prints `{"count": N, "items": [{message,
type, patterns, confidence, sentiment, decay_days}]}`. **Read-only** — this
command never opens or writes the store; candidates are reviewed by an
agent/human before any `add` (signal honesty). Corrections are detected by the
ported correction-pattern library (`skills/memory/scripts/corrections.py`, from
the MIT claude-reflect project). This parses **Claude Code transcript format
only**; other hosts' histories are out of scope. In `ZMEM_CAPTURE_MODE=auto`
likely-secret text is redacted; in `manual` matching items are annotated
`"secret_warning": true` but kept verbatim for review. (`--json` is accepted for
parity with the issue's syntax; output is always JSON.)

### queue-list / queue-clear — review live-captured corrections (read-only / clear)
```
python <store.py> queue-list --namespace NS [--json]
python <store.py> queue-clear --namespace NS [--id ID ...] [--all] [--drop-stale]
```
The `capture-correction` hook (UserPromptSubmit, Claude Code / ZCode / Codex)
queues mid-session user corrections ("no, use X", "remember: ...") into a
namespace-scoped sidecar file (`<store-data-dir>/queue/<ns>.json`, default
`~/.zmem/queue/<ns>.json`; follows the store's `ZMEM_STORE`/`ZMEM_DATA` override
chain, not a fixed path) — hooks never write the
store. `queue-list` shows the pending candidates for review (emit them into the
store via `add --signal user` only if they clear the closeout rubric);
`queue-clear` removes processed ids (`--id`), empties the queue (`--all`), or
prunes stale low-confidence items (`--drop-stale`) — exactly one selector is
required (a flag-less invocation is rejected, not a silent full wipe). Both are store-independent
(they never open or write the store), so they work even if the store is locked or
missing.

### export-pack — render a Tier 1 markdown memory pack
```
python <store.py> export-pack --namespace NS [--out FILE] [--project-limit 50] \
  [--global-limit 15] [--min-confidence 0.6] [--max-bytes 32768]
```
Renders live memories from `--namespace` and `user:global` (confidence DESC,
retrieval_count + surfaced_count DESC, ingestion_ts DESC) as a hand-off markdown pack,
e.g. for a cloud/remote session with no store access. `--max-bytes` is a UTF-8
byte budget over the whole rendered pack (structural framing counts toward
the cap): a bullet that would push past it is omitted whole (never
truncated), smaller rows behind it are still emitted, and only framing
appended after the walk (an empty section's heading and the trailing
omitted-count note) can exceed the budget. Refuses (exit 2) if
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
python <store.py> ingest-jsonl --in FILE [--source-ref REF] [--allow-tombstones] [--capture-mode auto|reviewed|manual]
```
Imports a JSONL file written by `export-jsonl`. Every row is validated before
touching the store, and a bad row is counted and reported by line number
rather than aborting the file. `--source-ref` overrides source_ref on every
row inserted this run (default: keep each row's own incoming source_ref).
`--allow-tombstones` lets an incoming superseded row tombstone a live local
row with the same id — off by default; use it only when the file is an
export of a store you trust as authoritative for those ids.

`--capture-mode` routes every inserted row through the same capture policy as
`add` (a sync file is remote-authored data that can otherwise plant a poisoned
memory surfacing verbatim into model context, or store secret-like text). The
default resolves like `add` (`ZMEM_CAPTURE_MODE` env or `manual`): verbatim
content with prompt-injection-risk tagging (a row matching an injection
pattern is tagged `prompt-injection-risk` so it can be reviewed, in ALL modes).
Use `--capture-mode auto` when ingesting an untrusted/remote sync file: it
additionally redacts secret-like content/tags (tagged `auto-redacted`) and
refuses rows whose `source_ref` looks like a secret (counted as
`capture_refused` in the summary, NOT stored). `reviewed`/`manual` keep the
original text with an advisory notice.

## Hard rules
- **Never put secrets/credentials/PII in the store.** It is a local plaintext sqlite
  file. The write-time filter is advisory only (regex heuristic), not a guarantee.
- **`doctor.py` is read-only.** It should inspect, never repair. Do not let it
  create the store, rewrite config, or "fix" hook trust for the operator.
- **Legacy namespace cutover:** before first v5 use, set
  `ZMEM_NS_MIGRATION_MAP` to a JSON object mapping each old namespace to a live
  checkout path, for example `{"project:oldname":"C:/src/owner/repo"}`. The
  migration re-derives canonical remote-based keys from those checkouts; it
  retries the map on later opens, but an omitted entry remains under its old key
  until the map is supplied.
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

Optional **hybrid recall** (`--hybrid`) adds a vector/embedding lane on top of
this keyword pipeline: the two lanes' rankings are fused with Reciprocal Rank
Fusion (RRF, k=60), so a memory BM25 missed can still surface via embedding
similarity. It needs the optional onnxruntime/tokenizers runtime and embedding
model; without them recall fails open to plain FTS5 keyword ranking (see the
`recall` command section above for the full contract).

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

## Codex shared-store mode
For Codex, the safe cutover shape is:
- **Disable Codex native memories yourself** in `~/.codex/config.toml`; do not
  let zmem auto-edit Codex config.
- **Use one canonical physical store path** shared with the plugin hosts.
- **Treat `ZMEM_TIER0=native` as the target shape**: keep Codex's own project
  instruction surface native, and use zmem for shared Tier 2 durable memory.
- **Do not assume Codex can write `~/.zmem`.** If the shared store path is
  outside Codex's writable roots, add a writable root or use a local broker
  that owns the store and mediates read/write operations.
- **Reapprove hooks after hook-surface changes.** If a repo-local Codex hook
  adapter is added later, trust the project and reapprove that surface as part
  of cutover.

## The reflection loop (Loop 1)
The `zmem-reflect.sh` Stop hook checks the episodic db for failed tool calls
(status=error or exit_code!=0 on non-read-only tools) in the current session.
If found and no lesson references this session, it injects an additionalContext
prompt at stop time. It is **non-blocking** (exit 0) — it only reminds you.

Capture a lesson only if it generalizes to a future session facing a similar
situation. If the failure was a one-off (typo, transient), do nothing — the prompt
explicitly allows that. Do not capture in-trajectory refinement tweaks as durable
lessons; only capture what would help next time.
