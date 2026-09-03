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
- **When a memory is stale/wrong:** `supersede` it for a general tombstone; use the
  dedicated **`invalidate`** command when a fact is *no longer true* (it REQUIRES a
  reason so the correction is auditable); use **`update`** to revise a memory
  append-only (the old row is tombstoned, a new live row links back via
  `update_of`, and point-in-time `--as-of` recall still sees the old content).

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
- schema compatibility against current v13
- v9 append-only lineage columns present (`valid_until`/`update_of`/`taint`)
- v10 entity identity tables present and non-vacuous (`entity`/`entity_alias`/
  `memory_entity`); inspect deeper with `store.py entity-list`
- v11 link surface present (`memory_link` table + `memory.trust_score` in
  range [0,1]); inspect deeper with `store.py links --id <uuid>`
- v13 episode storage present with counts (`episode-tables` check) and the
  MCP token scope advisory (`mcp-token` check: warns `unscoped_token: true`
  on full-access operator tokens, never reports the token value)
- Claude/Codex native-memory conflicts via read-only config inspection
- canonical namespace for the provided project
- host surface presence (Claude plugin, ZCode plugin, memory skill; repo-local
  Codex adapter files are optional until that lane exists)

Use it before first install, before cutover, and after any store-path or hook
surface change.

#### doctor `--miss-rate` — the miss-rate join (issue #94)

```
python <doctor.py> --miss-rate --store <snapshot-store.sqlite> [--miss-db PATH]
                   [--miss-transcripts GLOB ...] [--miss-bg-log PATH]
                   [--miss-window-before 1800] [--miss-window-after 300]
                   [--miss-limit 200] [--miss-verbose] [--format json]
```
Opt-in check that measures the miss rate — "a failure occurred in a session,
a matching memory existed in the store, and nothing surfaced at that moment".
It joins mined tool failures (ZCode episodic db via `--miss-db`, transcript
JSONL globs via `--miss-transcripts`) against a snapshot of the store
(recall with telemetry fully disabled, `link_hops=0`) and the bg log's
injection decision lines — BOTH shapes: writer A's `reason=injected` and
the session-start writer's `status=injected` lines (that writer has never
carried `reason=`). Definitions (pinned):
- `missed` — failure + floor-passing store match + no injection of a matched
  row in the window ⇒ counts toward the miss rate.
- `capture-gap` — failure + NO store match (write-side problem, counted
  separately).
- `surfaced (sid)` — an injected line whose `sid=` proves it is this
  session's decision carries a matched row id.
- `surfaced (legacy)` — same evidence on a pre-#94 sid-less line or a
  `sid=unknown` line (window+overlap attribution only;
  `miss_rate_strict_sid` excludes it).
- `no-query` — nothing derivable for the failure (no recovered operation and
  no ops ring): unmeasurable, excluded from every rate. On boxes where the
  ops-ring lane is not yet deployed this bucket can dominate until rings
  accumulate.

REFUSES to run without an explicit `--store`, and refuses the
host-default store even when given explicitly — the join reads session
data, so snapshot first: copy `store.sqlite` AND any
`store.sqlite-wal`/`-shm` beside it into a temp dir (plus `zmem-bg.log`
and the `ops/` ring dir when present), then pass that path. exit 1 from
`--miss-rate` most often means exactly that:
snapshot the store and re-run with `--store`; exit 1 from
other checks means a real diagnostic failed.
Read-only everywhere: the
store/db open `mode=ro`, the report never writes, and a missing/old store is
an error (never created, never migrated). Memory content stays out of the
default output (ids/namespaces only; `--miss-verbose` adds a short preview).

### recall — surface relevant memories (high-precision)
```
python <store.py> recall --query "<query>" [--namespace NS] [--limit 5]
  [--link-hops 0|1] [--link-budget N]
                          [--include-global] [--global-limit 3] [--hybrid]
                          [--no-hybrid] [--no-mmr] [--no-bump]
                          [--as-of ISO-8601] [--no-unfold] [--json]
python <store.py> recall --query "<query>" --explain [--target ID|FRAGMENT]
                          [--json]
```
Returns live (non-superseded) memories matching the query, filtered by confidence
floor (>=0.25) and namespace — unless `--as-of ISO-8601` is given, in which case
it is a point-in-time read that returns rows **valid at that instant** (a row
superseded after that instant may be included; a row created after it is not;
see the `--as-of` notes below). Prefer `--namespace project:<basename>` to scope to
the current project; use `user:global` for cross-project.

#### Retrieval debugger: `--explain` / `--target` (issue #82)

`recall --explain` re-runs the REAL pipeline (same lanes, same floors, same
merge) and prints one blameline per verdict explaining why a row did or did
not surface. It is an operator diagnostic in the same spirit as `doctor.py`:
ZERO writes (the telemetry path is never reached — stricter than
`no_telemetry`), it never unfolds, it never fails a recall (a thrown tracer
degrades to the `explain_unavailable` verdict and the results still print).
`--target` takes a memory id (full or unambiguous prefix) or a content
fragment (case-insensitive substring, then token overlap >= 0.7); multiple
matches produce one verdict per id, never a guess. With `--json` the read
envelope gains an `explain` object: `query`, `target`, the effective
settings (`namespace`, `limit`, `include_global`, `global_limit`, `no_mmr`,
`no_bump`, `as_of`, `hybrid`), and `verdicts[]` with
`id`/`reason`/`rank`/`score`/`detail`.

Verdict reasons are a CLOSED set (`EXPLAIN_REASONS` in `storelib/recall.py`):

| reason | meaning |
|---|---|
| `found` | in the presented result set at `rank` (1-based) |
| `below_limit` | retrieved and scored but beyond `--limit` |
| `below_floor` | the row's confidence is below the effective floor (the recall `min_confidence` parameter, default `CONFIDENCE_FLOOR`) |
| `omitted_injection` | would have ranked; dropped because `no_bump` and prompt-injection risk |
| `omitted_untrusted_web` | would have ranked; dropped because `no_bump` and `taint=untrusted_web` |
| `namespace` | lives outside the query's expanded namespace set (and `--include-global` did not admit it) |
| `superseded` | live-only recall and `superseded_at` is set (`detail.successor_id` names the live replacement when one exists) |
| `not_valid_at_as_of` | `--as-of` set and the validity interval does not contain it |
| `vec_lane_miss` | `--as-of` + hybrid: the row was valid then, but the vec KNN pool is live-rows-only (the SKILL.md as-of caveat — this is why history-on-hooks is not "just pass `--as-of`") |
| `not_in_pool` | not in the FTS, vec, or entity candidate pool at over-fetch depth |
| `not_in_db` | no row matched `--target` (`detail.neighbors` lists up to 5 nearest live rows by token overlap) |
| `explain_unavailable` | the tracer threw; results still returned (fail-open) |

#### Change-intent lineage unfold — explicit recall only (issue #82)

Already-stored lineage (`update_of` from `update`) becomes visible on the
EXPLICIT path: when the query reads as change-intent ("what changed about X",
"why did we switch to Y", "we used to Z") AND the presented results contain a
live head of a tombstoned predecessor, recall appends the predecessor chain as
budgeted extra rows tagged `[PREVIOUSLY]` (JSON keys `unfold_of` +
`unfold_hop`, 1 = immediate predecessor). Extras never count against
`--limit`, never enter the telemetry bump set (popularity rewards query
matches, not neighbors), never cross namespaces, and are never silently
dropped for taint — the explicit surface prefixes `[INJECTION RISK]` /
`[UNTRUSTED WEB]` like any other row.

It runs ONLY when all of these hold: not `--no-bump` (hooks, PreCompact,
Hermes prefetch, and eval defaults never unfold), not `--no-unfold`, the
query matches the compiled change-intent regexes (deterministic, no LLM), and
`link_hops >= 1` (the same contract that keeps `search` — CLI, MCP, and
Hermes `_tool_search` — byte-identical; MCP `recall` unfolds for free).
`--explain` describes lineage in verdict detail but never injects rows.

| Env var | Default | Meaning |
|---|---|---|
| `ZMEM_UNFOLD_TOP_K` | `3` | max presented hits to walk backward from (clamped 1-10) |
| `ZMEM_UNFOLD_MAX_HOPS` | `3` | max `update_of` hops per chain (clamped 1-10) |
| `ZMEM_UNFOLD_BUDGET` | `4` | hard cap on total `[PREVIOUSLY]` extras per recall (clamped 1-20) |

#### Confidence floors (issue #58, 3.8)

Three distinct floors live on the recall path. Each reflects a different
surface's precision-vs-coverage tradeoff. They are env-overridable; the
constants live in `schema_meta.py`.

| Constant | Default | Env override | Used by |
|---|---|---|---|
| `INJECT_FLOOR_PROMPT_DEFAULT` | 0.25 | `ZMEM_INJECT_FLOOR_PROMPT` | `recall` (UserPromptSubmit / PreCompact). Hard floor on FTS/vec results — anything below is dropped before scoring. |
| `INJECT_FLOOR_RECENT_DEFAULT` | 0.5 | `ZMEM_INJECT_FLOOR_RECENT` | `recent` (SessionStart / subagent recall). Tighter because the surface is high-confidence recent material, not query-best match. |
| `INJECT_FLOOR_GATE_NONE_DEFAULT` | 0.4 | `ZMEM_INJECT_FLOOR_GATE_NONE` | Hook selective-inject gate. `signal=none` rows must clear this floor; grounded-signal rows (`test`/`compile`/`lint`/`reviewer`/`user`) keep the 0.25 floor. |

The three floors are intentional. Do not silently unify them. The
selective-inject gate (3.8) is a hook-only filter; it does not change
the Python recall path.

When a passive inject surfaces nothing, it names WHICH gate fired (issue #87 /
#85 direction 1): `no durable memories retrieved for this prompt.` means the
candidate pool was empty OR every retrieved row was dropped by the passive
injection-risk filter — the one-liner is shared by design (the model is never
taught that omitted rows existed); the `zmem-bg.log` line distinguishes them.
`no durable memories met the inject bar.` means rows reached the
selective-inject gate and none passed. A token-budget wipe says so in its own
words (`memories withheld: the injection token budget (ZMEM_INJECT_TOKEN_BUDGET)
dropped every candidate row.`). Hermes/MCP `session_start` use the session
variants (`no durable memories retrieved for this session.` and
`session memories withheld: ...`).
`zmem-bg.log` carries the same cut per decision line: every
`zmem-hook` line has `reason=` (`reason=empty-pool`, `reason=omitted`,
`reason=below-bar`, `reason=budget-drop`, `reason=injected`), plus `omitted=N`
when the passive injection-risk filter dropped rows. The closed set lives in
`schema_meta.py` (`INJECT_SILENT_REASONS`). Since issue #94 every line also
ends with `sid=<sanitized session id>` (`[^A-Za-z0-9._-]` → `_`, cap 128;
`sid=unknown` when the host sent none) — the session key doctor's
`--miss-rate` join binds failures to injections with. Field order:
`status`, `reason`, `omitted=`, `ids=`, `all=`, `tokens=`, `ops=`, `sid=`
(last, always present).

#### Query context (prior-turn operation tokens) — issue #88 / #85 direction 2

Decision-point prompts are prose with zero lexical overlap with the
operation-adjacent lessons that matter; the retrieval signal lives in the
tool commands the session is executing. The PostToolUse hooks
(convention-capture on the coding hosts, `post_tool_call` on Hermes) append
each Edit/Write/Bash event to a per-session ring at
`<data>/ops/<session>.log`, storing ONLY the tool name plus allowlisted
tokens (git subcommand chains, test-runner verbs, edited-path basenames) —
never a raw command dump, stdout, or argument values that are bare words;
secret-shaped tokens (common credential prefixes like `ghp_`/`sk-`/`xox?-`)
are dropped as well. The ring is byte-capped: past 64 KB it is trimmed to
the newest 64 lines. The UserPromptSubmit body and the Hermes `prefetch`
compose that ring into the query, with the ops tail occupying a fixed
reserved slice INSIDE the 500-char cap (never appended past it; the
separator shares the slice, so no token is severed).
`ZMEM_QUERY_CONTEXT=0` is the lane kill switch — it stops BOTH composition
and ring collection (an operator disabling the lane expects no sidecar
writes). `zmem-bg.log` lines carry `ops=N` when tokens augmented
the query. Explicit surfaces (`recall --query`, `search`) are unchanged.
Limitation (deliberate, see #88): this helps LATER turns only — the first
tool call of a turn still runs before any operation context exists.
Cost note (#93 B4): credential-prefix-shaped tokens (`sk-`, `npm_`, `AKIA`,
…) are dropped from the ops query EVEN when they are legitimate filenames —
fail-safe direction (signal loss, never a leak); rename a real path that
collides. The eval composer ignores `ZMEM_QUERY_CONTEXT` by design (#93 B6):
evals must stay deterministic and immune to ambient env, so the kill switch
does not change their queries.

#### Passive-injection kill switch (ZMEM_INJECT=0) — issue #110 / P0-5

`ZMEM_INJECT=0` disables EVERY passive-injection surface: the shared recall
body (all modes — user_prompt, pretool, subagent, precompact/recent), the
SessionStart hook (Tier 2 recall AND Tier 0 — under the switch SessionStart
emits its empty `{}` envelope), the Hermes provider (`prefetch`, the
`zmem_session_start` tool twin, and the system-prompt `core.md` block), the
Hermes reflect hook's delivery paths, and MCP `session_start`. Each silenced
surface emits its empty envelope and logs `status=silent reason=disabled`
(`reason=disabled` is written only by this switch, never by silent-reason
classification — it lives beside `injected` as `INJECT_REASON_DISABLED`, not
in `INJECT_SILENT_REASONS`). Only the literal `0` (whitespace-tolerated)
disables — the `ZMEM_QUERY_CONTEXT` convention; `false`/`no`/empty leave
injection enabled. Capture paths never consult the switch: correction
capture, the ops ring, convention counters, failure capture, and
session-cadence maintenance keep writing. Parked pre-tool fences and armed
nudge markers are left in place and deliver on the first enabled run.
`doctor` shows the state (`inject-switch` line, WARN when disabled).

#### Pre-tool inject — issue #90 / #85 direction C

On hosts whose pre-tool contract was probed and confirmed (ZCode: documented;
Claude: emitted plus a pending sidecar the next prompt must deliver), a
PreToolUse hook (`zmem-pretool-recall.sh`, matcher
`Edit|Write|MultiEdit|NotebookEdit|Bash`) derives the recall query from the
tool input ITSELF — the command or file path about to run — and injects
matching hazard lessons before the tool executes. Pre-tool
`additionalContext` is documented on Claude Code (since 2.1.9 it lands
alongside the tool result; pausing is `permissionDecision`-driven only) —
the pending sidecar covers older hosts that ignore the field, and requires
the host event's `session_id` (without it the direct emit is the only
delivery). The hook NEVER denies (a surfaced hazard is information, not
grounds to block a legitimate command) and stays fully silent when nothing
qualified. `ZMEM_QUERY_CONTEXT=0` silences every query-context lane, this
one included. Hermes delivers the equivalent on `pre_llm_call` (after the
fact of the producing call), best-effort per ring CURSOR
`(ts, event-count)` — same-second events still deliver; a transient store
failure after the at-most-once marker skips that cursor's delivery. Hermes
delivery's namespace follows the hook chain `ZMEM_MCP_NAMESPACE` →
`ZMEM_NAMESPACE` → `user:global` (issue #71 review: one chain for prefetch,
recall, and correction capture — Hermes hook events themselves carry no
namespace); project-scoped operation context delivers
on the coding-host PreToolUse surface. All query-context persistence
(rings, delivery markers, pending fences) lives under `<data>/ops/`
sidecars and never grows the store's tables. Codex pre-tool injection is
deliberately unwired: upstream Codex has since shipped a full hooks system
(`PreToolUse` accepts `hookSpecificOutput.additionalContext` — model-visible,
non-blocking — and `PreCompact`/`SubagentStart` exist; openai/codex#19385 was
resolved; Codex hooks reference: https://learn.chatgpt.com/docs/hooks), so
the old "host rejects pre-tool context" claim is retired;
wiring is tracked in #95 (verification-first) behind the miss-rate
baseline (#94).

Inject surface parity (host facts, not aspirations): Claude Code registers
SubagentStart (task-text recall when the event carries the delegated
prompt) and PreCompact; Codex registers SubagentStart/SubagentStop, and
upstream now also ships PreToolUse/PreCompact context injection — but zmem
wires those only in #95 (verification-first), so they stay unregistered
until then. **ZCode supports exactly
seven hook events — SessionStart, UserPromptSubmit, PreToolUse,
PermissionRequest, PostToolUse, PostToolUseFailure, Stop — so SubagentStart
and PreCompact are host gaps on ZCode** (an unsupported event name would be
dead config under the host's strict schema, so they are documented here
instead of registered). If ZCode grows either event, wire
`zmem-subagent-recall.sh` / `zmem-precompact.sh` immediately.

#### Decision-point checkpoints (REQUIRED skill contract) — #85 direction E

When an agent is about to run one of the named hazardous operations, the
skill/workflow driving it MUST first run an explicit recall and treat any
hit as blocking review (read the lesson before proceeding; do not skip it
because the task feels urgent):

- before `git stash pop` / any stash-consume — `recall --query "git stash
  pop foreign stash conflict"` (a blind pop can apply a foreign stash)
- before `git reset --soft` (squash assembly) — `recall --query "git reset
  soft origin main stale tree"` (a fetch may have moved the base)
- before `git push` — `recall --query "git push stale tree fetch rebase
  verify"` (verify the tree against the fetched base first)
- before editing any file named by a stored citation/ratchet lesson —
  `recall --query "<path basename> ratchet citation re-pin"` (cited-file
  edits have local gate batteries)

These checkpoints complement the pre-tool hook; they are not a substitute
for it (an agent improvising a raw command is exactly who the hook covers).

#### Schema forward-compatibility (issue #65 follow-up)

A client refuses a store whose `schema_version` is above its ceiling.
`FORWARD_COMPAT_SCHEMA_VERSION` (schema_meta) is that ceiling: a maintenance
release of an older client line may raise it to an ADDITIVE-ONLY newer schema
(new side tables, no `memory` changes), letting the older client keep storing
and recalling memories — with a one-time stderr NOTICE — until it updates.
Above the ceiling the refusal names the update, and
`ZMEM_ALLOW_NEWER_SCHEMA=1` overrides at the operator's own risk.

#### Injection token budget (issue #65, 10.9)

`ZMEM_INJECT_TOKEN_BUDGET` (default 1500, estimated at 4 chars/token — no
tokenizer is bundled) caps the memories injected by the hooks
(UserPromptSubmit / PreCompact / SubagentStart / SessionStart Tier 2) and by
the `session_start` MCP/Hermes tools. When the budget is hit, bullet
admission stops: lowest-score `signal=none` rows drop first, and
`decision`/`constraint` rows are NEVER dropped (once only they remain,
enforcement stops — they are kept even over budget). The hook bg-log line
reports `tokens=<used>/<budget>`, and every read `--json` envelope reports
`tokens_used`/`tokens_budget`. `ZMEM_CTX_BUDGET` (character cap) remains the
hard outer truncation on the rendered block.

`--include-global` (opt-in) ALSO surfaces up to `--global-limit` query-relevant
rows from the `user:global` tier, merged project-first so a global row never
crowds out a project row. The three automatic hooks pass this so cross-project
lessons reach project-scoped sessions. Without it, behaviour is strict-namespaced
(byte-identical to before). When you want the global tier unioned in but still
want a per-tier budget, use `recall --namespace project:<x> --include-global`
rather than going unscoped.

**Hybrid is the DEFAULT when embeddings are available** (issue #58 3.3): the
query is embedded and matched against stored embeddings (sqlite-vec KNN),
then both lanes' rankings are fused with Reciprocal Rank Fusion (RRF, k=60).
`--hybrid` is kept as an explicit no-op alias for older invocations;
`--no-hybrid` forces the lexical (FTS5/BM25-only) lane. The embedding
runtime (onnxruntime + tokenizers, model lazy-downloaded and
checksum-verified) is optional and fails open: when it is unavailable,
recall silently uses plain keyword ranking — never an error.

**Entity matching is the THIRD RRF list and needs no model** (v10, issue #60
5.3): at recall the query is run through the same deterministic entity
extractor used on write, plus plain query tokens are matched against stored
entity aliases. Memories linked to a matched entity join the fusion as a
third ranked list (rank = number of matched entities, then recency). This is
how the alias `rg` recalls `ripgrep`-linked memories even when BM25 terms
miss — and it is live on model-absent stores by default. An unknown alias is
simply an empty third list; the lexical (and, when available, vector) lanes
fuse exactly as before.

**MMR diversity is the DEFAULT** (v10, issue #60 5.5): after RRF fusion and
composite scoring, the candidate set is re-ordered with Maximal Marginal
Relevance before `--limit` is applied, so a cluster of near-paraphrase
duplicates cannot crowd out a distinct fact. Similarity is embedding cosine
when both rows have embeddings, else Jaccard on normalized content tokens —
so MMR also works model-absent. `--no-mmr` returns pure composite-score
order (four paraphrases instead of three + the distinct fact). The tradeoff
knob is lambda: default 0.7, env `ZMEM_MMR_LAMBDA` (0.0 = maximize

**Embedding profiles & rerank** (`ZMEM_EMBED_PROFILE`, `ZMEM_CROSS_ENCODER`,
`ZMEM_CROSS_ENCODER_MODEL`): `ZMEM_EMBED_PROFILE` selects a row from the
registry in `skills/memory/scripts/embed_profiles.py` — unknown values refuse
with exit 2 before any store work, and a value whose dimension differs from
the store's committed vectors refuses until `reembed --all` converts it.
`ZMEM_CROSS_ENCODER=1` enables the optional cross-encoder rerank on explicit
CLI `recall` invocations only (never hooks / `recent` / PreCompact /
`search`-aliases / `--no-bump` runs), pointing `ZMEM_CROSS_ENCODER_MODEL` at a
LOCAL pair-scoring `.onnx` plus sibling `tokenizer.json`. A missing or broken
model degrades silently to un-reranked results — rerank can never fail a
recall. No public cross-encoder hash ships because none was verifiable;
there is likewise NO unverified-load escape hatch for the main model
(`ZMEM_MODEL_ALLOW_UNVERIFIED` does not exist).
diversity, 1.0 = no diversity — identical ordering to `--no-mmr`).
`--no-mmr` and `--no-hybrid` are independent flags and can be combined.

Recall rows carry **entity cards**: `recall --json` rows include
`entities: [{id, kind, name}]`, and the fenced hook render shows at most
three entity NAMES per row (never ids). `get --id` shows the same links.

**1-hop link expansion is the DEFAULT** (v11, issue #61 6.3): after MMR,
each recalled memory's `related`/`supports` links are walked one hop and
up to `--link-budget` (default 2) extra neighbor rows are appended — a
memory that never matched the query but is associative-neighbor of a
match still surfaces. `contradicts` neighbors are included only when
they survive the confidence floor and are tagged `[CONTESTED LINK]` in
the fenced render (`contested_link: true` in JSON). Expansion rows carry
`link_relation` / `link_of` / `link_score` / `contested_link` keys; rows
that matched the query themselves never carry them, and expansion rows
never advance telemetry (popularity rewards query matches, not link
neighbors — so expansion cannot distort later rankings). `--link-hops 0`
disables the walk; `--link-budget 0` is equivalent. `search` and `recent`
never expand.

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
  --type <fact|lesson|convention|preference|decision|constraint> \
  --content "<the knowledge, specific and actionable>" \
  --tags "comma,separated" \
  --signal <test|compile|lint|reviewer|user|none> \
  [--taint <trusted_internal|untrusted_tool|untrusted_web>] \
  [--source-ref "file:<path>" | "session:<id>" | "db:<table>:<rowid>"]
```
Signal sets default confidence: test/compile=0.9, lint=0.85, reviewer/user=0.6
(medium), none=0.2 — deliberately BELOW the 0.25 recall floor: an ungrounded
lesson is the agent's self-opinion and never surfaces in default recall (still
reachable via `search --text`, which applies no confidence floor, or
`recent --min-confidence 0`) (#36 M3).
Dedup-on-write: near-identical live content in the same namespace refreshes the
existing entry instead of duplicating.
Dedup has a polarity guard (v11, issue #61 6.2): a near-identical hit whose
negation polarity DISAGREES ("always X" vs "never X") is NOT merged — both
rows stay live, link as `contradicts`, and each loses 0.10 trust. A
polarity-AGREEING duplicate re-add is corroboration: the existing entry
refreshes as before AND gains +0.05 trust.
Link generation on write (v11, issue #61 6.2): after every `add`/`update`,
the same namespace-aware neighbors dedup already computed (embedding cosine
when the model is present, else Jaccard token overlap — model-absent stores
link too) above `ZMEM_LINK_THRESHOLD` (default 0.75) become `related` edges
(stored both directions). Each linked neighbor also absorbs the new row's
TAGS and re-derives its entity links (attribute evolution) — content,
confidence, signal, and retrieval_count are never touched. Deterministic;
no LLM (a `ZMEM_LINK_LLM` knob deliberately does not exist).
`add --json` (v13) prints a structured write result — `{"id", "result":
"stored"|"deduped", "warnings"}` — with warnings as structured objects
(`{"type": "redacted", "count": N}` for automatic secret redactions); the
MCP and Hermes add surfaces use it. Default output is unchanged.
Namespace validation: obvious misspellings of the global namespace (`global`,
`userglobal`, `users:global`, …) are rejected at `add` time AND on `ingest-jsonl`
sync import with a message naming the canonical `user:global` — such rows would
be unreachable from the automatic hooks. `project:<x>` and arbitrary namespaces
pass through untouched. Legacy rows already stranded under a near-miss namespace
(before this guard existed) can be remediated with `rekey-namespace
--near-miss-global --confirm` (see below).

### update / invalidate — append-only revision and contradiction corrections (v9, issue #59)
```
python <store.py> update --id <full-uuid> --content "<new content>" \
  [--namespace NS] [--type ...] [--tags ...] [--source-ref ...] [--confidence F]
  [--signal ...] [--taint ...] [--capture-mode manual|reviewed|auto]
python <store.py> invalidate --id <full-uuid> --reason "<why the fact is no longer true>"
```
- **`update`** is append-only knowledge revision: it creates a NEW live row with the
  new content, tombstones the target row (`superseded_at=now, valid_until=now,
  supersede_reason='updated'`), and links the new row back to it via `update_of`.
  Namespace/type/tags/source_ref/confidence/signal inherit from the target unless
  overridden; the old row's content is NEVER mutated. An unknown or
  already-superseded id is refused (exit 2, nothing written). Dedup runs against
  OTHER live rows (the replaced row is excluded, so even an unchanged-content
  "update" creates history). Point-in-time recall (`--as-of` before the update)
  returns the OLD content; after returns the NEW.
- **`invalidate`** is `supersede` with a REQUIRED `--reason` — the preferred way to
  record "this fact is no longer true" so the correction is auditable (issue #59,
  4.3). `supersede` remains for general tombstones (consolidated/pruned rows)
  where a reason is optional. The reason must be NON-EMPTY (whitespace-only is
  refused, exit 2), and tombstones are write-once: a second `invalidate`/
  `supersede` on an already-tombstoned row is refused (exit 2) — re-tombstoning
  would move `valid_until` forward and falsify point-in-time history.
- **Large content**: `--content -` (the literal dash) reads the content from
  stdin on both `add` and `update`. Windows caps command-line argv far below the
  65536-char content limit, so pipe near-limit payloads instead of passing them
  as an argument (the Hermes/MCP surfaces do this automatically).

### Provenance trust (`--taint`) — v9 (issue #59, 4.7)
Every memory carries a **taint** rank marking the trust of its ORIGIN:
`trusted_internal` (human/closeout/test-grounded) < `untrusted_tool` (agent or
MCP/Hermes-authored) < `untrusted_web` (web-fetched). There is deliberately no
fourth rank: an unknown taint value is **refused** at every write surface (the
write path never silently coerces it). The closed enum is enforced by a CHECK
constraint AND by application-side validation (CLI argparse choices, ingest
validator, Hermes/MCP boundary checks) — all sourced from the single
`schema_meta.ALLOWED_TAINTS`.

The taint default depends on the surface:
- CLI `add`: derived from `--signal` — grounded signals (`test`/`compile`/`lint`/
  `reviewer`/`user`) default to `trusted_internal`; `signal=none` (the agent's
  self-opinion) defaults to `untrusted_tool`. An explicit `--taint` overrides.
- Hermes/MCP tools (`zmem_add` / the MCP `add` tool): default to EXPLICIT
  `untrusted_tool` unless the caller passes a taint (e.g. `untrusted_web` for a
  web fetch, or `trusted_internal` for a human-grounded note).
Taint propagates **worst-of forward through lineage**: `update` re-creation and
`consolidate` absorb both set the surviving row's taint to the worse of the two
merged sources (a merged row never DOWNGRADES a riskier member), and a
duplicate `add` / `ingest-jsonl` row that folds into an existing keeper applies
the same worst-of to the keeper. A tombstone
(`supersede`/`invalidate`) preserves the row's taint — it creates no new row.

Recall surfaces the taint so trust is visible, mirroring prompt-injection-risk:
- The **explicit** text path prefixes rows with `[UNTRUSTED TOOL]` (taint
  `untrusted_tool`) or `[UNTRUSTED WEB]` (taint `untrusted_web`).
- The **passive/auto-inject** path (`--no-bump` — used by the automatic hooks and
  Hermes prefetch) OMITS `untrusted_web` rows entirely, exactly like
  `prompt-injection-risk` rows (an untrusted web-sourced memory is not trusted
  enough to inject passively). `untrusted_tool` rows DO surface passively — they
  are agent-authored and thus safer than web content — and are flagged on the
  explicit path instead. The hook scripts need no edits: they already pass
  `--no-bump` and inherit this store-side filter.

### recent / search / supersede / update / invalidate / list / get / stats
```
python <store.py> recent [--namespace NS] [--limit 5] [--min-confidence 0.5]
                         [--include-global] [--global-limit 3] [--as-of ISO-8601]
                         [--json]
python <store.py> search --text "<text>" [--namespace NS] [--limit 10]
                        [--include-global] [--global-limit 3] [--no-bump]
                        [--as-of ISO-8601] [--json]
python <store.py> supersede --id <full-uuid> [--reason "..."]
python <store.py> invalidate --id <full-uuid> --reason "..."
python <store.py> update --id <full-uuid> --content "<new content>" [overrides...]
python <store.py> list [--namespace NS] [--include-superseded]
python <store.py> get --id <uuid>
python <store.py> stats
```
Read envelope (v13, issue #65 10.8): `recall`/`recent`/`search --json` print
`{"results", "count", "omitted", "injection_risk", "tokens_used",
"tokens_budget"}` instead of a bare list — `omitted` counts rows the
`--no-bump` inject filter dropped (injection-risk + `untrusted_web`), so
hosts stop guessing from stderr. Library callers still get the bare row list
from the Python API.

`update --json` mirrors `add --json` (`{"id", "result", "created_new",
"warnings"}`). `get` (always JSON) includes the v13 `episodes` key — the
episodes this memory belongs to (always a list, possibly empty).

`recent`/`search` accept the same `--include-global`/`--global-limit` pair as
`recall` (project-first global tier union). `recent` now ALSO honours v5
migration aliases (so `recent --namespace <old pre-v5 key>` finds rows migrated
to the new key). `search` now accepts `--no-bump` for a *passive* query that records a
surface on `surfaced_count` (never advancing `retrieval_count`) instead of bumping like
`recall` (issue #21).

`--as-of ISO-8601` (v9, issue #59 4.4) on `recall`/`recent`/`search` returns rows
**valid at that instant**: `valid_from <= as_of AND (valid_until empty OR
valid_until > as_of)`. `valid_from` is INCLUSIVE, `valid_until` is EXCLUSIVE, and
empty `valid_until` means "never expires". Because a tombstone writes
`valid_until` at the same instant as `superseded_at`, a point-in-time as-of may
return rows that were later superseded (they were valid then) — only rows whose
validity interval contains `as_of` surface. This is what makes `update` history
readable: `--as-of` before the update returns the OLD content.

`--as-of` recoverability is **lane-bounded** in hybrid mode: a historically
tombstoned row that was valid at `as_of` is reachable only through the lexical
(FTS5/BM25) lane. The vector lane's candidate pool always filters
`superseded_at IS NULL` (live-only), so a row superseded before `as_of` cannot
surface via semantic KNN even though it was valid at that instant. Model-absent
runs are lexical-only and unaffected; a hybrid run that misses old content via
the vec lane should be re-run with `--no-hybrid` to include the FTS lane.


### reembed — backfill or rebuild semantic embeddings
```
python <store.py> reembed
python <store.py> reembed --all [--profile NAME] [--batch N] [--dry-run]
```
Flagless form (unchanged contract): backfills embeddings for live memories that
are MISSING them when the optional embedding runtime and model are available.
Existing embeddings are preserved; no runtime means a graceful skip.

`--all` rebuilds EVERY live memory's vector under the selected profile —
the operator-grade converter when you switch profiles:
- `--profile NAME` selects from the shipped registry (`minilm`, `fake`; see
  "Embedding profiles" below). Default: active `ZMEM_EMBED_PROFILE` or
  `minilm`. Requires `--all` — `--profile` without it refuses with the exact
  conversion command, never a silent no-op. `--all --profile fake`
  requires `--confirm` ONLY when the store already holds non-fake committed
  vectors (the overwrite is irreversible-in-practice without a working model
  later); fresh/empty stores convert freely.
- If the profile's dimension differs from what the store holds, `memory_vec`
  is recreated at the new dimension INSIDE one transaction — a crash mid-run
  rolls back to the pristine pre-run state, so a half-dim index is impossible.
  Every command then re-verifies profile-vs-store dimension before it embeds
  (mismatch = exit 2 with the exact remediation line).
- Idempotent: a second identical run changes 0 rows. `retrieval_count`,
  `content`, and every other column stay untouched — only embedding bytes,
  their `embedding_model` marker, and `embedded_at` are rewritten, plus the
  `embedding_profile` meta key recording the last completed conversion.
- `--batch N` paces stderr progress lines ONLY (display chunks inside the
  single transaction — batches are never separate commits). For very large
  stores run during an idle window: the single transaction briefly grows the
  WAL by roughly the size of all rebuilt vectors (~1.5 KB/row at 384-dim).
- `--dry-run` reports how many of the live rows would change and writes
  nothing (no writer lease, no meta write).

Idempotency detail: change detection keys on
`embedding IS NULL OR blob dim mismatch OR embedding_model marker differs`,
so switching `--profile` back and forth always reports honestly instead of
silently treating rows as current.

### Embedding profiles — shipped registry (`embed_profiles.py`)

| Profile | hf_id | dim | sha256 | Purpose |
|---|---|---|---|---|
| `minilm` | `Xenova/all-MiniLM-L6-v2` | 384 | `bbd7b466…46f0c5` (Xenova **ONNX export** blob) | Operator default; semantic recall/dedup |
| `fake` | — | 16 | — (no files/network by design) | Model-absent tests/CI ONLY; deterministic placeholders; doctor warns on non-temporary stores |

Notes:
- The published pin covers the Xenova ONNX export (`onnx/model.onnx`), NOT the
  sentence-transformers PyTorch weights — different builds of the same checkpoint
  hash differently. `checksum_mismatch` therefore means "wrong build installed";
  doctor prints this note verbatim. Verification has NO escape hatch:
  `ZMEM_MODEL_ALLOW_UNVERIFIED` does not exist and never will.
- A third ONNX profile was evaluated and deliberately OMITTED per the release
  rule: no Qwen3/Nomic local ONNX artifact with a personally verified
  hf_id+dim+sha256 could be pinned at authoring time. "A name with empty
  sha256 is a stub" is forbidden — add one only with verified facts.

#### Compatibility ledger for profile conversion (read before switching)

- **Older zmem releases on a converted store:** versions without this change
  hardcode `float[384]` and do not know about `embedding_profile`. On a store
  converted to another dimension they keep opening it, keep writing rows, and
  silently lose/degrade vector recall (the vec lane's failures are swallowed
  there). Conversion is per-machine deliberate maintenance — coordinate box
  upgrades around it.
- **Hook surfaces during mismatch:** while the active profile's dim differs
  from committed data, every hook that recalls (`--no-bump`) receives an EMPTY
  inject envelope; the refusal reason lands only on the child's stderr, which
  hooks do not surface. This refuse-over-wrong-vectors posture is deliberate:
  lexical-only degradation inside a wrong-dim store would surface misplaced
  confidence rather than silence. Run `reembed --all` (or restore the prior
  profile) to clear the state.
- **`doctor` default-run behavior changed by design** in the same release:
  the embeddings check now deep-verifies the model pin (mtime-keyed cached
  hashing), so its JSON can newly report `checksum_ok:false` where older
  versions stayed silently `null`.
- **Concurrency during `reembed --all`:** the whole rebuild holds one SQLite
  write transaction for its duration; concurrent writers wait (busy-timeout),
  readers proceed on WAL snapshots. Schedule large conversions for idle
  windows.

**Cross-encoder trust note** (issue #63 review round): `ZMEM_CROSS_ENCODER_MODEL`
loads an operator-supplied local ONNX file with NO checksum pin — none was
publishable offline. Treat that path with the same caution as any executable;
doctor's `embeddings_health.cross_encoder` block surfaces enabled/model-file
state so a missing-model silent degrade is visible.

### episode-open / episode-add / episode-close / episode-list — session containers (v13, issue #65 10.7)

```
python <store.py> episode-open --namespace "project:<basename>" [--json]
python <store.py> episode-add --episode <uuid> --memory <uuid> [--json]
python <store.py> episode-close --episode <uuid> [--summary] [--json]
python <store.py> episode-list [--namespace NS] [--json]
```

An **episode** groups the memories captured during one working session
(schema v13: `episode` + `episode_memory` tables). It is a CONTAINER, not a
memory type — `episode` is deliberately absent from `--type` values.

- `episode-open` creates the row (`ended_at` empty until close) and prints
  its id; this is the only creator — no empty-table seeding.
- `episode-add` attaches a LIVE memory (`superseded_at IS NULL`; a
  tombstoned id is refused exit-2) to an OPEN episode. Idempotent per pair;
  memberships are append-only (a later supersede never removes one).
- `episode-close` sets `ended_at`, computes `token_count` (the same
  `row_token_cost` the injection budget uses, summed over LIVE members at
  close), and with `--summary` writes one extractive first-sentence summary
  row via the standard `add` path (capture-mode auto, so the shared
  redaction helper runs) linked by `summary_memory_id`. A closed episode
  cannot be re-closed or re-opened (append-only).
- `episode-list` prints episodes newest-first with member counts
  (`--json` for rows). `get --json` on a memory lists its episodes.

Episodes round-trip through `export-jsonl`/`ingest-jsonl` via a `kind`
discriminator (`"memory"` on memory rows; separate `episode` and
`episode_memory` records). Legacy kind-less files still ingest as
memory rows; an older client consuming a new export should filter to
`kind == "memory"` lines. Doctor reports counts (`episode-tables`).

### session_start / session_end — MCP + Hermes pairing tools (v13, issue #65 10.5)

Both surfaces expose `session_start` / `session_end` (MCP tools) and
`zmem_session_start` / `zmem_session_end` (Hermes tools) with the same
contract:

- **`session_start(namespace?, limit=3)`** — passive prefetch returning a
  fenced, provenance-tagged context block (Phase 3 fence + selective-inject
  rules, 0.5 recent floor). It runs `recent --no-bump`: `retrieval_count`
  NEVER advances (only the surface event is recorded — pinned by
  tests/test_session_tools.py), injection-risk and `untrusted_web` rows are
  omitted, and `ZMEM_INJECT_TOKEN_BUDGET` is honored. The response reports
  `ids`, `omitted`, `tokens_used`, `tokens_budget`. Namespace omitted
  resolves to the surface's own default — `user:global` on MCP, the session
  namespace on Hermes (a deliberate divergence: the Hermes provider is
  session-scoped, the network server is not).
- **`session_end(note?, namespace?)`** — default is a NO-WRITE
  acknowledgement so clients can pair start/end freely; it NEVER organizes
  or consolidates (run the explicit CLI for that). With a `note`, exactly
  one row is written via the standard `add` path (`fact`, `signal none`,
  `taint untrusted_tool`, capture-mode auto so redaction runs), defaulting
  to the server/session namespace.

### Scoped MCP tokens (v13, issue #65 10.2)

`ZMEM_MCP_TOKEN` (env) is always an UNSCOPED operator token — full access to
every namespace, exactly the pre-v13 behavior. To scope it, point
`ZMEM_MCP_TOKEN_FILE` at a JSON object:

```json
{"token": "<secret>", "namespaces": ["project:zmem", "user:global"]}
```

Requests outside the allow-list fail closed with the stable
`namespace_not_allowed` error. Note: `supersede`/`invalidate` are
id-addressed and deliberately NOT namespace-confined (a scoped token
holding an id may tombstone it) — namespace confinement applies to the
namespace-bearing tools. Scoped tokens MUST pass an allowed namespace
explicitly on every read — a namespace-less read spans the whole store and
is denied — and the implicit `user:global` union on scoped reads is
suppressed unless `user:global` is itself in the list. Malformed JSON, an
empty `namespaces` list, or near-miss scopes like `"global"` are hard
startup errors (exit 2). A bare (non-JSON) token file stays unscoped, as does
a JSON object with `namespaces` absent or `null`.
Doctor warns `unscoped_token: true` on operator tokens (issue #65, 10.10)
and never prints the token value.

### promote — review and install a reusable skill
```
python <store.py> promote --dry-run [--namespace NS]
python <store.py> promote --id <uuid> --confirm [--description "..."]
python <store.py> promote --id <uuid> --confirm --install-approved
```
`--confirm` writes a review candidate; `--install-approved` is the additional
explicit gate that installs the reviewed skill into Codex, Claude Code, and
ZCode skill directories. Promotion is never an unattended hook action.

The promote ladder (issue #64, schema v12) decides which lessons are
candidates. A lesson is eligible only when its EXPLICIT usage feedback says it
works and nothing says it doesn't:
- `applied_count >= 3` AND `violated_count == 0` → eligible (see `feedback`).
- Any `violated_count > 0` blocks eligibility; the 2nd violation has already
  dropped the row's `trust_score` by `TRUST_VIOLATION_FLOOR_DROP` (0.15,
  clamped at 0.0, applied exactly once at the violated_count 1→2 crossing).
  `signal` is NEVER changed by feedback — trust_score is the usage ledger,
  signal is provenance.
- Passive exposure does NOT count: `retrieval_count`/`surfaced_count`
  (hooks, `--no-bump` recall) are not eligibility inputs. Only the explicit
  `feedback` CLI advances the counters, so nothing auto-promotes.
- The other candidate bars are unchanged: `type=lesson`, grounded signal
  (test/compile/lint), `confidence >= 0.85`, live row.
`promote --dry-run` with no eligible rows is a no-op that prints
`[zmem] no promotion candidates found` (exit 0).

### feedback — record explicit usage feedback (Voyager counters)
```
python <store.py> feedback --id <uuid> --applied
python <store.py> feedback --id <uuid> --violated
```
Increments exactly one of the row's `applied_count` / `violated_count`
(schema v12) and prints a one-line JSON summary
(`id`, `verdict`, both counters, `trust_score`, `trust_dropped`). This is the
ONLY writer of those counters anywhere — hooks, `--no-bump` recall,
PreCompact, and Hermes prefetch never advance them.
Exit codes: 0 success; 1 target missing or not live (tombstoned rows refuse);
2 usage error (both or neither of `--applied`/`--violated`, missing `--id`).

### eval — offline recall-quality harness
```
python scripts/eval_runner.py --store <path> [--gold eval/gold.jsonl] [--k 5] [--fail-under X] [--json-out PATH]
```
Runs every gold item through the REAL recall pipeline and prints one JSON
report: `hit_at_k`, `mrr`, `as_of_accuracy`, `injection_omit_rate` (+ per-bucket
and per-item detail). `--store` is REQUIRED — the runner never resolves the
default home store; point it at a throwaway path and it auto-builds the
deterministic 64-row fixture corpus (the ids `eval/gold.jsonl` names) there.
The gold set covers the six issue-#64 buckets (as-of knowledge updates,
injection omission, entity aliases, namespace isolation, contested/superseded
guidance, ordinary FTS hits; >= 5 items each) plus the issue-#82 honesty
buckets (>= 3 items each): `retraction` (an `invalidate --reason`-ed fact must
vanish at current time yet surface under `--as-of` before the tombstone),
`polarity` (two LIVE contradicting rows — both sides surface), and
`change-intent` (explicit-path lineage unfold). An optional `"explicit": true`
gold flag runs an item on the explicit path (`no_bump=false`, still zero-write
via `no_telemetry`) so the change-intent unfold is measured; passive twins on
the same rows pin that hooks never unfold. The original 30 ids (rowids 1-50)
are the frozen #64 contract; #82 appends rowids 51-64.
Exit 0 regardless of scores (quality is collected, never
gating); `--fail-under` is optional and OFF in CI; 2 on operational errors
(invalid gold set names the offending item).
The run is model-absent by construction: the fake embedder
(`ZMEM_EMBED_PROFILE=fake`) and a pinned clock (`ZMEM_TEST_NOW`) make ranking
deterministic on any machine. Evaluation is passive (`no_bump`) and measures
retrieval quality — link expansion and MMR presentation are excluded
(`link_hops=0`, `no_mmr`).
Self-corpus probe — measure recall against YOUR corpus (issue #82):
```
python scripts/eval_self_corpus.py --store <store-copy> [--k 5,20] [--limit 100] [--json-out PATH]
```
`--store` is REQUIRED and the runner REFUSES the host default store path
(exit 2 with a remediation: take a `store.py backup` snapshot and probe the
copy) and refuses nonexistent paths before creating anything. Probes are
templated deterministically from the store's own live rows (first sentence /
distinctive tokens / entity aliases — domain-agnostic, no LLM). Every probe
is fully passive (`no_telemetry`, `no_bump`, `link_hops=0`, `no_mmr`; the
unfold structurally cannot fire) so the probed store stays byte-identical.
The report may embed operator content — `--json-out` is the only write and
reports are gitignored; never commit one.
Public-corpus adapters convert on-disk corpora into gold JSONL and skip
cleanly when the corpus is absent (CI downloads nothing):
```
python scripts/eval_adapters.py --adapter longmemeval --input <path> --out <path>
python scripts/eval_adapters.py --adapter locomo --input <path> --out <path>
```
A missing `--input` prints `skipped: ...` and exits 0. Synthetic 3-row toy
fixtures under `tests/fixtures/adapters/` prove both converters.

### tune-weights — suggest recall scoring weights (dry-run only)
```
python <store.py> tune-weights --dry-run --gold eval/gold.jsonl [--k 5]
```
Evaluates the shipped composite weights (W_BM25/W_CONFIDENCE/W_RECENCY/
W_POPULARITY) against a gold set, hill-climbs a small deterministic candidate
set, and prints JSON with `current` and `suggested` weight vectors (always
summing to 1.0) plus the metrics each achieves. The evaluation is read-only
and it WRITES NOTHING — there is deliberately no `--apply` (the store must
already be at the current schema: opening any store.py subcommand migrates
it, as always). Applying a suggestion is a MANUAL edit of the W_*
constants at the top of `skills/memory/scripts/storelib/recall.py`, keeping
the sum at 1.0. Uses the CURRENT store (env-resolved like every subcommand —
in tests/CI that is a fixture store via `ZMEM_STORE`, never the operator
store). Exit 0 even when the current weights already win; 2 on an invalid
gold set, a missing `--dry-run`, or an evaluation failure.

### consolidate — merge near-duplicate memories
```
python <store.py> consolidate [--threshold 0.80] [--prune] [--dry-run] [--namespace NS] [--force] [--merge-contested] [--json]
```
Clusters live memories by embedding cosine similarity (Jaccard token overlap when
embeddings are unavailable), picks a keeper, merges metadata, and supersedes the
rest with a persisted reason. The automatic SessionStart cadence now invokes
**`organize`** (issue #62, 7.7) — which runs consolidate's EXACT same cluster
logic on a bounded episode — so the 7-day / 20%-growth gate below protects the
schedule REGARDLESS of entry point: organize and consolidate share the same
`last_consolidation` meta keys and the same single-flight lock. An automatic
run is skipped only when the last run was <7 days ago AND growth was <20%; both
bounds are env-tunable via `ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS` /
`ZMEM_CONSOLIDATE_GROWTH_THRESHOLD`. The `consolidate` CLI remains available for
explicit/manual scope runs. When the gate declines, the run prints a
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
`merged_from` provenance column, so consolidated memory is non-lossy
(v11, issue #61 6.6: the column is maintained as a DE-DUPLICATED,
first-seen-order id list — the same absorbed id is never recorded twice,
and the v11 migration normalized pre-existing duplicates losslessly). Note this
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
alone is not evidence of unused (issue #21). Unrecalled extension (issue #62,
7.6): a live row whose `last_surfaced` is OLDER than `ZMEM_UNRECALLED_DAYS`
(default 30) qualifies EVEN with `surfaced_count > 0` — a surface event that has
itself gone stale loses its issue #21 protection; `NULL last_surfaced` keeps the
`surfaced_count = 0` rule unchanged and `signal != none` is never pruned.

Consolidation is **single-flighted**: concurrent runs (several sessions starting
at once) take an advisory lockfile in the store dir, and the losers skip cleanly
and exit 0 rather than clustering the same rows twice.

### organize — sleep-time organization (SessionStart cadence job)
```
python <store.py> organize [--prune] [--dry-run] [--force] [--json]
```
The SessionStart sleep-time maintenance job (issue #62, 7.7): the
`session-cadence` batch that the start hook launches detached runs ORGANIZE, not
consolidate. It bounds an "episode" of work to the most recent live rows,
backfills any enrichment those rows are missing, runs consolidate's identical
cluster/absorb/contested machinery on exactly that set, then adds sleep-time
deliverables: a topic hierarchy, hierarchical extractive summaries, and
extractive compression of the rows consolidation just grew. Everything is
deterministic and LLM-free by default; there is no `--threshold`/`--namespace`
to keep the two maintenance entry points aligned (see the consolidate section:
organize and consolidate SHARE one cadence gate implementation, one cadence
clock, and one single-flight "consolidate" lock, so on a given store at most
one maintenance run happens per cadence window — whether it was triggered
from SessionStart, a manual CLI call, or the Hermes session-end hook).

Pipeline (in order):
1. **Shared cadence gate** — the SAME 7-day / 20%-growth gate as consolidate
   (ONE shared implementation, `_cadence_gate_skipped`, so the two entry
   points can never drift), using consolidate's `last_consolidation` meta keys
   (two entry points, one clock). `--force` is the only bypass. A gated run
   prints `[zmem] organize: skipped by cadence gate (...)` and changes
   nothing; `--dry-run` models the gate (`would skip by cadence gate`).
2. **Optional idle gate** — `ZMEM_ORGANIZE_IDLE_HOURS` (default 0 = off). When
   set, organize refuses to run unless the store has had no live-memory activity
   (max of ingestion_ts/last_retrieved/last_surfaced) for at least that many
   hours: sleep-time jobs should not churn a store that is mid-use. A skip prints
   `[zmem] organize: skipped by idle gate (...)`.
3. **Working set / episode bound** — the N most recent live rows
   (`ZMEM_ORGANIZE_EPISODE_BOUND`, default 256, clamped to the per-namespace
   consolidation cap), box-wide. 0 disables real work. organize's own summary
   rows are EXCLUDED from the episode — keyed on the structural
   `source_ref` `organize:` prefix, never the (mutable) tags column, so
   summaries are the pipeline's OUTPUT and never its input. The `organize:`
   prefix is therefore RESERVED for summary rows: a manually-added row whose
   `--source-ref` begins with `organize:` is excluded from episodes the same
   way.
4. **Entity backfill** — every working row missing `memory_entity` links gets
   them via the deterministic extractor (idempotent; rows with nothing
   extractable are counted as candidates but link nothing).
5. **Link backfill** — every working row missing `memory_link` edges gets them
   via the standard write-time linker (`related`/`contradicts` at
   `ZMEM_LINK_THRESHOLD`).
6. **Episodic consolidation** — `consolidate` on EXACTLY the working set
   (keeper selection, absorb, contested guard + optional NLI judge, bounded by
   the same per-namespace cap). The vector neighbor lookup is bounded to the
   episode too — an out-of-episode near-duplicate in the global vec0 index is
   never pulled in. See the consolidate section for semantics.
7. **Compression** — the keepers consolidation actually GREW, when their
   content exceeds `ZMEM_KEEPER_COMPRESS_CHARS` (default 4000; 0/negative
   degrades to the default — it is a cap, not a switch), are compressed
   deterministically: order-preserving UNIQUE sentences, dropping only from
   the TAIL on whole-sentence boundaries (never mid-sentence; the pre-compress
   text stays on the superseded history row and `merged_from` provenance is
   carried to the replacement). Compression runs BEFORE topic identity is
   recorded: compression replaces the keeper's id (append-only update), so
   keying topics first would leave every later run with a different key and a
   duplicate summary.
8. **Topics + summaries** — a related-graph over the POST-COMPRESSION live
   working rows using the SAME neighbor predicate at the same threshold
   consolidate would use; leftover singletons are grouped by SHARED entity
   (A-MEM lite). Every live NON-summary working row lands in exactly one
   topic. Topics of ≥3 members get a **hierarchical extractive summary**: a
   REAL row (`type=fact`, tags exactly `summary,topic`, `signal=none`,
   confidence 0.5, `source_ref=organize:<members>`,
   `merged_from=<members>`), built from the first sentence of each member
   (de-duplicated, capped, never truncated mid-sentence). Members stay live;
   the summary is FTS/entity/search recallable. Idempotent: a re-run finds the
   existing summary by its STRUCTURAL identity (`source_ref` + `merged_from` —
   never the mutable tags column) and UPDATES it (Phase 4 of the issue)
   instead of duplicating; when a topic's membership legitimately changed,
   the stale overlapping summary is superseded (`reorganized into a changed
   topic`) rather than left as a live orphan. Summary writes never propagate
   the `summary,topic` marker onto neighboring user rows. If the
   update/create would fold into a dedup target, organize logs and skips
   rather than rewriting a stranger's row.
9. **Unrecalled prune (pass-through)** — `--prune` enables the `consolidate
   --prune` rule, extended (issue #62, 7.6): a live row also qualifies when its
   `last_surfaced` is OLDER than `ZMEM_UNRECALLED_DAYS` (default 30) even with
   `surfaced_count > 0` — a surface event that has itself gone stale loses its
   issue #21 protection (both boundary comparisons are made in a
   format-agnostic way, so the store's ISO-8601 timestamps compare correctly
   against the cutoff). `signal != none` is never pruned; never automatic.

`--dry-run` writes NOTHING (no meta, no backfill, no summaries) and the `--json`
report carries per-step would-be counts (`entity_backfill`, `link_backfill`,
`topics`, `summaries`, `compressed`, `pruned`, `episode_ids`, `consolidate`
sub-report).  A concurrent organize/consolidate loses cleanly (exit 0, a
`- skipped` notice, or `{"error": "organize lock busy"}` under `--json`).

Optional local NLI judge (7.5): when `ZMEM_NLI_CMD` is set to an argv template
(e.g. `ZMEM_NLI_CMD='python /path/to/your/local-judge.py'`), consolidate's
contested branch consults it for mixed-polarity clusters; only an `entailment`
verdict on EVERY polarity-flagged pair — ANY two members whose negation
polarity differs, not only pairs anchored on the keeper — un-parks the merge,
and any other verdict or failure parks it (never auto-merges; a failure is
reported on stderr as a distinguishable NLI diagnostic). The judge receives
the two first-sentences on stdin (one per line, UTF-8) and prints a verdict
word. On Windows, backslashes in the template are normalized to forward
slashes before parsing (POSIX-style backslash stripping would otherwise
mangle `C:\path\judge.py`; quote paths containing spaces). Unset =
byte-identical behavior (no NLI); a deterministic regex polarity remains the
fallback for a response you judge ambiguous.

Schema compatibility: organize NEVER changes the schema. All of its outputs use
pre-existing columns (`merged_from`, `source_ref`, `tags`, `confidence`,
`source_hash`), so a store stamped at an older schema version keeps opening
normally in an older client until the new version is installed — verify a
template store with `store.py organize --dry-run` before upgrading.

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

Since issue #71 C this remediation also runs AUTOMATICALLY: every
store-opening command rekeys global-near-miss rows to `user:global` before
dispatching (silent on healthy stores; `ZMEM_AUTO_REKEY=0` or
`--no-auto-rekey` opts out). The explicit command remains for previews and
for kill-switch deployments. Namespace contract: fleet facts live in
`user:global`; project facts in `project:<canonical-git-remote>`; MCP `add`
without a namespace uses `ZMEM_MCP_DEFAULT_NS` when the operator set it,
else `user:global` — a bare `global` is never invented.

### promote-store — merge a leftover second store (admin, issue #71 E)
```
python <store.py> promote-store --from <path-to-store.sqlite> [--dry-run]
```
One-shot merge of a leftover second store (e.g. `~/.zcode/memory/store.sqlite`)
into the canonical one. Read-only on the source; source ids are PRESERVED so
re-runs are no-ops; a NEWER source schema is refused. doctor's
`second-stores` check fails when any live row exists outside the canonical
store and recommends this command.

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
chain, not a fixed path) — these recall/capture hooks never `add` rows and
write nothing else to the store themselves (the one sanctioned exception is
the DETACHED SessionStart `session-cadence` maintenance task, which runs
organize/backup/sweep on its own lease). `queue-list` shows the pending candidates for review (emit them into the
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
Entity links are deliberately NOT carried in the JSONL (they are store-local
derived data, like embeddings and content_norm): the receiving store rebuilds
them by re-running the deterministic extractor on ingest — see below.

v13 (issue #65, 10.7): every row carries a `kind` discriminator (`"memory"`);
episodes round-trip as additional `"episode"` and `"episode_memory"`
records when any exist (memberships are emitted only when both endpoints are
in the exported set, so the file is self-consistent). `ingest-jsonl` treats
a missing `kind` as `"memory"` (legacy files ingest unchanged) and applies
records in dependency order (memory → episode → membership) regardless of
file order; re-ingesting the same file is an exact no-op.

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

v11 (issue #61) notes: rows carry `trust_score` (restored verbatim, default
1.0 for pre-v11 files; a PRESENT but non-numeric/non-finite value makes the
row malformed — refused, counted, not stored) and outgoing `links` (validated fail-closed, applied
after every row lands so an edge may reference a later row;
`links_added`/`links_skipped` in the summary). Ingest deliberately applies
NO trust deltas and NO auto link generation — it is deterministic state
restore, so re-ingesting the same file is an exact no-op (the +0.05
corroboration is an organic `add`/`update`-only signal). The dedup path DOES
apply the write-time polarity guard: a remote row that contradicts its
dedup hit inserts as its own live row instead of merging (contradictions
never merge, on any surface).

`--capture-mode` routes every inserted row through the same capture policy as
`add` (a sync file is remote-authored data that can otherwise plant a poisoned
memory surfacing verbatim into model context, or store secret-like text). The
default resolves like `add` (`ZMEM_CAPTURE_MODE` env or `manual`): verbatim
content with prompt-injection-risk tagging (a row matching an injection
pattern is tagged `prompt-injection-risk` so it can be reviewed, in ALL modes;
issue #82 widened the pattern set with four high-precision
instruction-to-the-model shapes — role hijack ("you are now the ..."),
concealment ("do not mention this"), instruction-override paraphrases
("ignore all previous rules/guidelines"), and store-mutation imperatives
("update your knowledge base / the memory store") — precision-first, so
ordinary coding lessons like "update the lockfile" never tag; the read path
re-classifies EVERY row at emit time regardless of store age).
Use `--capture-mode auto` when ingesting an untrusted/remote sync file: it
additionally redacts secret-like content/tags (tagged `auto-redacted`) and
refuses rows whose `source_ref` looks like a secret (counted as
`capture_refused` in the summary, NOT stored). `reviewed`/`manual` keep the
original text with an advisory notice.

Issue #71 F: in `auto` mode, `source_ref`s with a structured provenance
scheme — `db:`, `hindsight:`, `session:`, `zmem-queue:`, and `file:` with a
RELATIVE path (or a well-known stem like `codex-MEMORY.md`) — skip the
generic hash-shape refusal (a 32-hex db row id is legitimate provenance).
Credential shapes (key=value pairs, PEM headers, `gh*_`/AKIA tokens) still
refuse on allowlisted refs, `file:` absolute remainders still refuse, and
content/tags scanning is unchanged. The write result carries a structured
`source_ref_allowlisted` warning so the relaxation is visible.

Every ingested row ALSO runs the deterministic entity extractor (the same
one `add` uses), so entity identity is rebuilt locally instead of carried
(v10, issue #60): two stores ingesting the same rows derive the same
entities, keyed by normalized alias, with no cross-store id collisions.

### entity-list — inspect entity identity (v10, issue #60)
```
python <store.py> entity-list [--kind person|project|tool|preference|other] [--json]
```
Lists the entities the deterministic extractor minted: id, kind, canonical
name, all normalized aliases, and how many memories link to each. `--kind`
filters to one kind; `--json` emits `[{id, kind, name, aliases, links}]`.
This is the inspection surface for humans and doctor — use it to see what
the third RRF lane is actually matching.

### entity-merge — reconcile duplicate entities (v10, issue #60)
```
python <store.py> entity-merge --from <entity-id> --to <entity-id> [--confirm]
```
Merges two entities: the `--from` entity's aliases and memory links move to
`--to`, then `--from` is deleted. DRY RUN BY DEFAULT — without `--confirm`
nothing is written and the plan (moves, alias/link collisions with the
target, the deletion) is printed. With `--confirm` the write happens in one
transaction; a target that already has an alias or link keeps its own
(collisions are counted, never overwritten).

Refusals (exit 2, nothing written): unknown id on either side, `--from` ==
`--to`, or a kind mismatch — merging a `person` into a `tool` would silently
re-classify history. Nothing in zmem AUTO-merges entities, ever (the issue's
"ZMem will not auto-merge people" rule); person→person merges are allowed
but only as this explicit manual command.

Typical flow: `entity:Name` tags or backticked mentions created two
entities for the same thing (e.g. `rg` and `ripgrep`); find both ids with
`entity-list`, dry-run the merge, then apply with `--confirm`. Afterwards
the alias `rg` resolves to the surviving entity and recall's third list
returns all its linked memories.

### links — inspect (or curate) a memory's associative links (v11, issue #61)
```
python <store.py> links --id <uuid> [--json]           # list every edge
python <store.py> links --add --id <a> --id <b> \
    --relation <related|supports|contradicts|updates|extends|derives> \
    [--score S] [--reason "..."]
```
List mode prints every `memory_link` edge touching the memory, both
directions (`out` = the memory is the source; JSON rows carry
`{src, dst, direction, other, relation, score, created_at}`). A missing id
exits 1 with the same stable stderr line as `get`.

`--add` is the operator-curated insertion path for the typed relations
(and for the trust-carrying `supports`/`contradicts`). Symmetric
relations (`related`/`supports`/`contradicts`) insert BOTH directions;
typed relations (`updates`/`extends`/`derives`) keep their one authored
direction. Self-links and cross-namespace pairs are refused; re-adding an
existing edge is an exact no-op (idempotent, no second trust delta).
`--relation contradicts|supports` adjusts `trust_score`, so those inserts
REQUIRE `--reason` — the same deliberate-use guard `contradict` enforces
(validated and echoed, not persisted).

Links are generated automatically on every `add`/`update` (see
`ZMEM_LINK_THRESHOLD` below) — deterministic, no LLM (no `ZMEM_LINK_LLM`
knob exists; link generation never calls a model).

### contradict — record a contradiction (v11, issue #61)
```
python <store.py> contradict --id <a> --id <b> --reason "<why they conflict>"
```
Inserts a `contradicts` pair (both directions) and applies the trust event
to BOTH rows: `trust_score` −0.10 each, clamped to [0.0, 1.0] (ten
contradictions land a row at exactly 0.0, never below). Neither row is
merged, deleted, or rewritten — content, confidence, and signal are
untouched. `--reason` is REQUIRED (the `invalidate` deliberateness
convention); the v11 schema has no reason column, so the reason is
validated and echoed but deliberately not persisted. Re-running the same
contradict is an exact no-op. A polarity-disagreeing `add` that lands in
the dedup window does this automatically (see `add`).

**trust_score semantics** (v11): every memory starts at 1.0. A
`contradicts` event lowers BOTH rows by 0.10; a `supports` link or a
polarity-agreeing duplicate re-add (corroborating add, CLI `add`/`update`
only — sync ingest never re-applies deltas) raises the keeper by 0.05.
Always clamped to [0.0, 1.0]; visible in `get --json`, `export-jsonl`,
and `doctor`. `confidence`/`signal` are never changed by linking — they
are provenance inputs; trust_score is the contradiction ledger.

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
Retrieval is a **three-signal pipeline**: FTS5/BM25 keyword match (always),
vector KNN over stored embeddings (when the optional embedding runtime is
available), and entity matching (v10 — always, no model needed): the query's
identifiers and plain tokens are matched against stored entity aliases. The
three lanes' rankings are fused with Reciprocal Rank Fusion (RRF, k=60,
per-id additive: a memory appearing in several lanes accumulates each lane's
contribution), then re-ranked by a **composite score** that combines:

- **BM25 relevance** (55%) — the FTS5 keyword match score (vector-only rows
  use cosine similarity; entity-only rows use the fraction of matched query
  entities)
- **Confidence** (20%) — grounded by signal tier (test/compile > reviewer/user > none)
- **Recency** (15%) — exponential decay with a 90-day half-life
- **Popularity** (10%) — retrieval frequency with diminishing returns (sqrt dampening)

Candidates are namespace-filtered (the tier's expanded alias set) and subject
to the confidence floor (below 0.25 is dropped before scoring). Staleness
demotion halves confidence, which feeds into the confidence component.

**Entity matching (v10, issue #60 5.3)** is the third lane: the query runs
through the same deterministic extractor used at write time, and plain query
tokens are additionally matched against stored entity aliases. Memories
linked to matched entities join RRF ranked by (number of matched entities,
recency), with the same namespace filter and the same `--as-of` temporal
predicate as the other lanes — like the vector lane, an entity match in a
FOREIGN namespace never leaks into the querying tier (it is found by its own
tier's run, e.g. the global tier under `--include-global`). Works
model-absent by design; an unknown alias contributes nothing.

**Link expansion (v11, issue #61 6.3)**: after MMR and the passive-surface
filters, each result's `related`/`supports` edges are walked ONE hop and up
to `--link-budget` (default 2) neighbors are appended to the result set
(never duplicating an already-present row, never crossing the tier's
namespace set, and always respecting the `--as-of` predicate — a neighbor
invalid at the requested instant does not surface). `contradicts`
neighbors must additionally survive the confidence floor and are tagged
`[CONTESTED LINK]`. Expansion is bounded (one indexed lookup per result
row, budget-capped) — no PageRank or graph propagation of any kind.

**MMR diversity (v10, issue #60 5.5)**: after the composite sort, and before
`--limit`, Maximal Marginal Relevance re-orders each tier's candidates —
picking the best-scoring row first, then trading relevance against
similarity-to-already-picked (lambda: 0.7 default, `ZMEM_MMR_LAMBDA` env,
`--no-mmr` to disable, 1.0 == no diversity). Row-to-row similarity is
embedding cosine when both rows have embeddings, else Jaccard on normalized
content tokens — so near-paraphrase clusters stop crowding out distinct
facts even on model-absent stores.

**Entity cards (v10, issue #60 5.4)**: every recall row carries its linked
entities — `entities: [{id, kind, name}]` in `recall --json`, at most three
names per row in the fenced hook render. `entity-list` inspects the entity
tables; `entity-merge` reconciles duplicates.

**Hybrid recall is the default** when the embedding runtime is available
(auto-enabled; `--no-hybrid` forces lexical-only): a vector/embedding lane
runs on top of this keyword pipeline and its ranking fuses with the lexical
and entity lanes via the RRF above, so a memory BM25 missed can still
surface via embedding similarity. It needs the optional
onnxruntime/tokenizers runtime and embedding model; without them recall
fails open to plain keyword + entity ranking (see the `recall` command
section above for the full contract).

**Entity extraction at write time is deterministic** (no LLM): explicit
`entity:Name` / `entity:<kind>:Name` tags, the `project:<suffix>` namespace
suffix, `--tags` tokens (`kind:Name` or plain), backtick-quoted spans in
content (kind `tool`), and CamelCase identifiers (2+ humps, kind `other`).
Stopwords (`the`, `and`, `use`, …) and path/URL-shaped tokens are never
entities; `person` entities exist only via an explicit `entity:person:`
tag. The first kind seen for an alias wins; reconcile with `entity-merge`.

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
