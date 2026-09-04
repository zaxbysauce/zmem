# Changelog

All notable changes to the **zmem** plugin are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Released versions are marked with a git tag (`vX.Y.Z`) and a GitHub Release.
Installations discover new versions by comparing the `version` field in their
plugin manifest against the marketplace entry — see the *Upgrade* section of the
README.

## [Unreleased]

## [0.16.0] — 2026-09-04

The Phase-2 popularity fix from the proactive-memory epic (#100): passive
recall can no longer write ranking popularity for rows that never reached the
model (#114), plus the miss-rate kill-switch discrimination the P2 onboarding
owns (#133).

### Changed

- **memory** (issue #114, P2-3): ranking popularity now reads
  `retrieval_count` only. Passive `--no-bump` surfaces are still RECORDED
  (`surfaced_count`/`last_surfaced`, issue #21 — promote/prune/consolidate
  consume them) but they no longer feed `compute_score`, so passive pulls
  cannot inflate their own ranking. Weights unchanged at
  relevance .55 / confidence .20 / recency .15 / popularity .10 — no
  redistribution; explicit reads are the honest interim popularity signal
  until #124 lands applied/violated counters. Gold delta (CI eval command,
  `scripts/eval_runner.py --gold eval/gold.jsonl`, before vs after):
  hit_at_k 1.0 → 1.0, mrr 0.947917 → 0.947917, as_of_accuracy 1.0 → 1.0,
  injection_omit_rate 1.0 → 1.0, per-item diffs none.
- **memory** (issue #114, P2-3): new `--for-injection` flag on `recall` and
  `recent` — the passive injection lane. The selective inject gate and the
  token budget now run INSIDE the single store subprocess (after MMR, rerank,
  entity cards, link expansion and unfold; before telemetry); the returned
  rows are exactly the rendered set; `surfaced_count` advances only for
  rendered QUERY-MATCHED rows (link/unfold neighbors render but never count);
  the `--json` envelope gains `reason` (the #87 closed set, or `injected`)
  and `candidate_ids` (pre-gate ids, the bg-log `all=` pre-image) under the
  flag only, so plain-path output stays byte-identical. `recent_memory` also
  gains the `no_telemetry` seam for symmetry with `recall_memory`. No second
  ack process (one store.py start ~1.5 s against a 15 s hook timeout, #121).
- **hooks** (issue #114): the recall body and the SessionStart Tier-2 lane
  pass `--for-injection` and stop gate/budget-ing locally — the store already
  did it, in the same process that writes the telemetry. The bg-log decision
  line format is unchanged; `all=` is now pinned to the PRE-gate candidate
  set everywhere (the session-start writer previously logged the post-gate
  rows — aligned to the convention the #94/#105 miss-rate join matches
  against); session-start lines now carry `reason=` and are written whenever
  the pull produced an envelope — including budget-drop and empty-pool
  silences, which previously logged nothing on this surface.
- **doctor** (issue #133): `--miss-rate` classifies `reason=disabled`
  bg-log lines (the ZMEM_INJECT=0 kill-switch marker) into a distinct
  `counts.disabled` bucket, excluded from the numerator AND denominator with
  a caveat — a disabled window now reads as "switch off", not 100% miss.
- **tests** (issue #133): near-miss whitespace pins (`"0 "`/`" 0"`, plus
  enabled near-misses) for the two kill-switch sites that only had
  literal-"0" coverage (the Hermes reflect hook and MCP `session_start`),
  behaviorally where the optional deps exist and via an always-running
  source pin otherwise. New `tests/test_zero_write_passive.py` pins the
  #114 acceptance criteria (gate/budget-dropped rows untouched; rendered
  rows counted exactly once per decision; five pinned-clock recalls with
  stable scores; `--explain`/eval zero-write; the flag/telemetry truth
  table).
- Schema unchanged at v13 — `surfaced_count`/`last_surfaced` columns are
  retained, nothing dropped; older released clients keep working against
  the shared store until they pull this version.

## [0.15.0] — 2026-09-03

Two P0 safety mechanisms from the proactive-memory epic (#100): the release
gate that makes the 0.13.1 eight-merge stall structurally impossible to
repeat (#106), and the passive-injection kill switch + documented rollback
that Phase-2 ranking changes on live stores require (#110).

### Added
- **Release drift gate (issue #106, P0-1 durable half)**: a new
  `--check-unreleased-drift` mode in `scripts/release_gate.py`, wired into
  `.github/workflows/ci.yml` for pushes to main only — a merge that leaves
  content under `## [Unreleased]` while its HEAD carries no version bump now
  FAILS the build (the exact history that left PRs #86–#104 unserved at
  static 0.13.1 while every installed cache stayed behind). Escape:
  `[skip release]` in the merge subject (chosen over a path-filter
  allowlist — explicit operator choice, works at any checkout depth; ci.yml
  checkout gains `fetch-depth: 2` for the HEAD~1 bump comparison, and
  release.yml is untouched). The default gate mode's behavior and stdout are
  byte-identical to before; new tests cover the drift parser, a synthetic
  repo reproducing the #106 history, and the ci.yml wiring.
- **Passive-injection kill switch (issue #110, P0-5)**:
  `ZMEM_INJECT=0` silences every passive recall-injection surface at once —
  the shared recall body (all modes), the SessionStart hook (Tier 2 AND
  Tier 0 — it emits its empty `{}` envelope), the Hermes provider
  (`prefetch`, the `zmem_session_start` tool twin, and the system-prompt
  `core.md` block), the Hermes reflect hook's delivery paths, and MCP
  `session_start`. Each silenced surface emits its empty envelope and logs
  `status=silent reason=disabled` (a NEW `INJECT_REASON_DISABLED` constant,
  deliberately kept OUT of `INJECT_SILENT_REASONS` so classifier semantics
  are untouched). Only the literal `0` disables — the
  `ZMEM_QUERY_CONTEXT` convention. Capture paths (correction capture,
  failure capture, ops ring, convention counters, session-cadence
  maintenance) never consult the switch, and the capture-side prompt hooks
  (Stop/SubagentStop reflect, convention nudges, capture prompts) stay
  active by design — they prompt capture, they do not inject recalled
  memory; the Hermes reflect hook is the documented divergence (a #110-
  named gated surface whose delivery carries recalled query-context).
  Parked pre-tool fences and armed nudge markers survive the switch and
  deliver on the first enabled run. MCP / Hermes `session_start` keep their
  exact 9-key envelope shape with `reason=disabled`, `context=""`.
- **doctor `inject-switch` line (issue #110)**: reports whether passive
  injection is disabled (WARN) so a confused operator sees the cause
  immediately.
- **README "Disabling passive injection & rolling a host back" (issue
  #110)**: what the switch silences, what keeps running, per-host rollback
  (Claude Code / ZCode version-pinned cache dirs + `installed_plugins.json`;
  Codex checkout-at-a-tag), and rollback/refresh verification via the
  `zmem-bg.log` `reason=` discriminator (the #106 field proof; the automated
  per-host canary stays with #108).

### Compatibility
- No store schema change (`SUPPORTED_SCHEMA_VERSION` stays 13): older
  installed clients are unaffected. The new `reason=disabled` bg-log lines
  are append-only text whose `reason=` value existing parsers (e.g. the
  miss-rate join) already treat as free-form.

## [0.14.0] — 2026-09-03

This release moves the accumulated `[Unreleased]` train downstream: the
Hermes-first plugin surface and remote prefetch (#102/#71), the pre-tool
inject + subagent task-text recall + Hermes `pre_llm_call` delivery (#92),
the ops-ring query-context lane it depends on, and the miss-rate measurement
(#94). The installed plugin caches pick all of it up on the next pull.

### Added
- **Miss-rate measurement (issue #94, P0-1)**: the number the proactive-memory
  epic gates on — "a failure occurred, a matching memory existed, nothing
  surfaced" — is now measurable. Two parts: (1) every `zmem-bg.log` decision
  line (both writers — the shared hook body and the session-start hook) ends
  with `sid=<sanitized session id>` (`[^A-Za-z0-9._-]` → `_`, cap 128,
  `sid=unknown` when the host sent none), so a mined failure can be bound to
  the injection decisions of its own session; (2) `doctor --miss-rate
  --store <snapshot>` joins mined tool failures (ZCode episodic db — with
  the failed command recovered from the db's `part` table — and/or
  transcript JSONL globs) against a read-only snapshot of the store
  (telemetry-disabled recall, `link_hops=0`) and the bg log's injected
  lines, classifying missed / surfaced (sid) / surfaced (legacy) /
  capture-gap / no-query with first-class coverage metrics
  (`query_source`, `sid_coverage_pct`, `no_query_pct`). New `doctor
  --store` flag repoints the whole report at a snapshot store. The join
  REQUIRES an explicit `--store` (an ambient ZMEM_STORE/ZMEM_DATA can
  never point it at the live store) and refuses the host-default store
  even when given explicitly; the snapshot recipe covers `store.sqlite`
  plus any `-wal`/`-shm`, `zmem-bg.log`, and the `ops/` ring dir. It
  never writes (mode=ro
  everywhere; CLI `--no-bump` alone still bumps, the join disables
  telemetry entirely), and never creates or migrates a store. No schema
  bump; no ranking or floor changes — this measures, it does not tune.
- **Hermes remote passive prefetch** (issue #71 A): with `ZMEM_MCP_URL` (+ token)
  set on a remote Hermes box, the `pre_llm_call` reflect hook runs the new
  `hermes-plugin/server/mcp_client.py` subprocess and fetches the MCP server's
  `session_start` tool — the passive `--no-bump`, fenced, token-budgeted
  prefetch — so a fleet Hermes with Hindsight still enabled gets zmem context
  before each turn. No second protocol (same MCP surface); fail-open on a
  missing `mcp` lib, bad token, refused connection, or timeout
  (`ZMEM_MCP_TIMEOUT`, default 8s). README's "planned v2" stub is retired.
- **Hermes correction-capture parity** (issue #71 D): the reflect hook now
  classifies the user turn with the same `corrections.detect_patterns` rules
  the Claude/ZCode/Codex capture hook uses and appends to the same
  schema-versioned sidecar queue (`host: "hermes"`); closeout remains the sole
  store write authority. `<5`-char bail, fail-open, per-session dedup;
  `ZMEM_HERMES_CORRECTIONS=0` kill switch.
- **Hermes plugin manifest + doctor check** (issue #71 B): `plugin.yaml` is a
  real memory-provider manifest (hooks = the MemoryProvider ABC hooks the
  provider implements); doctor grows the `hermes-plugin` surface check
  (manifest parity, register() entry point, hook scripts, guarded MCP-server
  importability, remote-mode token/mcp requirements).
- **Automatic near-miss namespace rekey** (issue #71 C): every store-opening
  command rekeys rows stranded under a global near-miss namespace to
  `user:global` (silent on healthy stores; `ZMEM_AUTO_REKEY=0` or
  `--no-auto-rekey` opts out). MCP `add` honors the opt-in
  `ZMEM_MCP_DEFAULT_NS` configured default. Namespace contract documented
  (`user:global` fleet facts; `project:<canonical-git-remote>` projects).
- **Capture-mode provenance allowlist** (issue #71 F): `auto` no longer
  refuses `db:`/`hindsight:`/`session:`/`zmem-queue:`/`file:<relative>`
  source_refs for hash-like shapes; credential shapes still refuse (PEM,
  key=value, `gh*_`/AKIA), `file:` absolute remainders refuse, content
  scanning unchanged, and a structured `source_ref_allowlisted` warning is
  surfaced.
- **Consolidation polarity refinement** (issue #71 G): mixed negation
  polarity no longer auto-parks a CONSOLIDATE cluster — pairs classified as
  same-predicate RESTATEMENTS merge (the field report's preference-vs-lesson
  and restated-constraint false positives), true contradictions
  ("is live" / "is not live") still park, and divergent pairs (historical vs
  current facts, different subjects) park conservatively. The WRITE-time
  guard (`dedup_polarity_conflict`) intentionally KEEPS the #61 contract —
  any polarity flip stays a conflict (own row + `contradicts` link), which
  the eval corpus's discrimination pairs depend on; only the consolidate
  cluster decision is refined. PR-review hardening (PRR-009): the
  negation-target discriminator runs in EVERY band — an always/never flip
  over ≥6 shared predicate tokens parks (never silently merges); the
  documented trade is that positive restatements of a negated rule whose
  shared verb sits right after the negator park too (pre-refinement
  behavior) instead of merging.
- **doctor second-stores check + `promote-store`** (issue #71 E): doctor
  FAILS when a leftover second store (`~/.zcode/memory`, legacy plugin dirs)
  holds live rows missing from the canonical store; `promote-store --from`
  merges one in (source ids preserved — idempotent; newer source schemas
  refused). PR-review round: the merge also carries v11 associative links
  and v13 episode containers/memberships (column-intersected, INSERT OR
  IGNORE), preserves merged_from/trust_score/applied_count/violated_count
  from v11+ sources, runs under the writer lease, and exits non-zero when
  any row is malformed or fails to apply.
- **mine-history Codex + Hermes adapters** (issue #71 I): `--source codex`
  parses a curated Codex MEMORY.md (User preferences / Reusable knowledge /
  Failures and how to do differently; `raw_memories.md` refused outright) and
  `--source hermes` mines Hermes session JSONL user turns — review-queue
  candidates only, dedup-key idempotent, never auto-writes.
- **#93 follow-up sweep** (A1/A2/A3/B3–B8/C4): eval-runner env leaks
  restored/stripped; ONNX fixture test skips loudly with versions; backup
  sweep re-stats before unlink (TOCTOU); recall-body dir-less `ZMEM_STORE`
  guard + honest bullet-7 docstring; secret-shape false-positive cost and
  eval `ZMEM_QUERY_CONTEXT` neutrality documented.

### Fixed
- **Miss-rate report hardening** (post-review sweep on #94): the transcript
  failure miner reads BOTH `session_id` and `sessionId` record keys (the
  dominant real Claude Code shape is camelCase-only — sid attribution was
  dead on that lane) and classifies exactly like `store.py failures`
  (sibling `toolUseResult` "Error…" strings count; user rejections stay
  excluded). `--miss-window-before/after` reject negative values (an
  inverted window silently classified everything missed), `--miss-limit`
  rejects values below 1, a present-but-unreadable episodic db is a loud
  caveat + warn (never a silent zero), a failed store recall is excluded
  with a caveat (never counted as capture-gap), an all-capture-gap run
  says its denominator is null, the refusal guard now also catches
  hard-link aliases of the host-default store, and writer A falls back to
  the session env chain when the event JSON omits `session_id` (both
  decision-line writers attribute to the same session on every path).
- **Test isolation (#93 A1 residue)**: `tests/test_inject_silent_reason.py`
  and `tests/test_pretool_inject.py` now strip `ZMEM_EMBED_PROFILE` /
  `ZMEM_TEST_NOW` / `ZMEM_AUTO_REKEY` from child envs like
  `tests/test_ops_tokens.py` already did — a single-process multi-file
  runner (pytest) can no longer inherit the eval runner's fake embedder or
  pinned clock across files.
- **Consolidate commit race**: the per-cluster COMMIT now uses the shared
  busy_retry-backed `_commit` like every writer path (issue #55 §7 follow-up;
  rollback stays the atomic backstop).
- **MCP server docstring** listed 5 tools; now lists all nine.
- **Stale Codex host-capability claims retired** (issue #90 closure):
  upstream Codex shipped a full hooks system — `PreToolUse` accepts
  `hookSpecificOutput.additionalContext` (model-visible, non-blocking;
  openai/codex#19385 was resolved by openai/codex#20692) and
  `PreCompact`/`SubagentStart` exist (Codex hooks reference:
  https://learn.chatgpt.com/docs/hooks). SKILL.md's inject-parity section,
  the pretool wrapper's header comment, and the registration test's
  rationale no longer claim the host rejects pre-tool context. No
  registration change: Codex stays unregistered until #95 wires it
  (verification-first, behind the miss-rate baseline #94).

### Added (earlier in this train)
- **Pre-tool inject** (issue #90, #85 direction C — host-probed): new
  `hooks/zmem-pretool-recall.sh` + shared-body `pretool` mode derive the
  recall query from the TOOL INPUT itself (the command/file path about to
  run — the only moment that sees `git stash pop` before it executes).
  Registered on ZCode and Claude (`PreToolUse`, matcher
  `Edit|Write|MultiEdit|NotebookEdit|Bash`); never denies; fully silent when
  nothing qualified. Claude additionally parks the fence in a pending
  sidecar the next UserPromptSubmit run must deliver — the sidecar covers
  older hosts that ignore pre-tool `additionalContext` (documented and
  supported since Claude Code 2.1.9, where it lands alongside the tool
  result; pausing is permission-decision-driven only — worst case one
  duplicate, never a lost delivery). Codex stays unregistered (at ship
  time upstream rejected `hookSpecificOutput.additionalContext`,
  openai/codex#19385 — since superseded: upstream shipped a full hooks
  system and the wiring is tracked in #95).
- **SubagentStart task-text recall** (issue #90, #85 direction D): the
  shared body's new `subagent` mode prefers the delegated task text
  (prompt/task/description) over the query-less recent pull when the host
  event carries it; falls back otherwise. ZCode has no SubagentStart /
  PreCompact events (exactly seven supported events — a host-implemented
  gap, not a probe artifact that could expire; not inert registrations).
- **Hermes `pre_llm_call` operation-context delivery** (issue #90, #85 C):
  the reflect hook delivers ring-tail recall as context when the session's
  query-context ring grew since the last delivery (at-most-once per ring
  timestamp; `ZMEM_QUERY_CONTEXT=0` kill switch). Hermes has no pre-tool
  event, so this lands after the producing call — stated.
- **Decision-point checkpoint skill contract** (issue #90, #85 direction E):
  the memory and closeout skills REQUIRE an explicit `recall --query` before
  stash-consume / `git reset --soft` / push / cited-file edits, with a hit
  treated as blocking review; pinned by doc-drift-style tests. No
  `store.py checkpoint` subcommand.
- **Query context: prior-turn operation tokens on the passive inject query**
  (issue #88, #85 direction 2): the PostToolUse hooks record each
  Edit/Write/Bash event to a byte-capped per-session ring
  (`<data>/ops/<session>.log`, trimmed to the newest 64 lines past 64 KB)
  storing only tool name + allowlisted tokens (git subcommand chains,
  test-runner verbs, edited-path basenames — never raw commands; bare-word
  argument values and secret-shaped tokens are dropped); the
  UserPromptSubmit body and the Hermes `prefetch` compose
  that ring into the query with the ops slice reserved INSIDE the 500-char
  cap. `ZMEM_QUERY_CONTEXT=0` kill switch (stops collection AND
  composition); `ops=N` on the `zmem-bg.log`
  line; `store.py sweep` collects stale rings; decision-point gold bucket
  (fixture rowids 65–70) asserts the #85-shaped prompts retrieve the
  hazard lessons WITH ops context and miss without it.
- **Ops-lane dir resolution tail parity** (issue #88 follow-up): the hook
  body's single resolver now walks the ring writer's four explicit-env cases
  in the same order (`ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA >
  ZCODE_PLUGIN_DATA` before the `~/.zmem` fallback), and `expanduser`
  applies on EVERY side of the lane — host.py expanded all four
  explicit-env values all along, the shared bash helper
  (`hooks/lib/zmem-tilde-expand.sh`, sourced by both writer hooks) routes
  any tilde-resolved data dir through `expanduser`, and the reader does the
  same — so a tilde-valued var resolves identically everywhere instead of
  splitting writer from reader. The `zmem-bg.log` write goes through that
  resolver — a non-launcher environment that only sets a plugin-data var no
  longer silently no-ops the ops lane (the log co-locates with the ring it
  describes). The session-start hook's own resolution (core.md, markers,
  its bg-log writers) follows the same chain, so every diagnostic and
  Tier-0 artifact co-locates in those environments as well. Launcher
  deployments are unaffected (the launcher always exports `ZMEM_DATA`).
  Test suites strip the plugin-data vars like `test_sweep` and pin the
  plugin-data precedence both directions; the decision-point eval hit-rank
  pin tightened to the issue's rank 1–2 bar.

- **`recall --explain`** (issue #82): read-only retrieval debugger with
  `--target` (id/prefix/fragment) and a machine-readable `--json` envelope;
  zero writes, closed `EXPLAIN_REASONS` set, fail-open on tracer errors.
- **Change-intent lineage unfold** (issue #82): explicit recall on
  change-intent queries appends budgeted `[PREVIOUSLY]` `update_of`
  predecessors (`ZMEM_UNFOLD_TOP_K`/`_MAX_HOPS`/`_BUDGET`, `--no-unfold`
  opt-out). Hooks, `--no-bump`, and search-shaped surfaces never unfold;
  extras are never popularity-bumped.
- **`scripts/eval_self_corpus.py`** (issue #82): self-corpus recall probe —
  `--store` required, host-default store refused with a backup remediation,
  fully passive.
- **`docs/CLAIMS-AUDIT.md`** (issue #82): every public README claim mapped to
  a path + symbol, a committed eval artifact, or an explicit aspirational row;
  the "hooks never write the store" wording is qualified to the accurate
  "hooks never `add` rows" claim.
- Four high-precision prompt-injection patterns (role hijack, concealment,
  instruction-override paraphrases, store-mutation imperatives) — picked up at
  emit time by every existing store, no migration.

### Fixed
- **Hook/Hermes silent inject no longer blames the bar when the pool was
  empty** (issue #85, direction 1 per #87): the UserPromptSubmit / PreCompact /
  SubagentStart hook body, the Hermes `session_start` tool, and its MCP twin
  now name WHICH gate fired. The hook body's retrieved-empty one-liner is
  `no durable memories retrieved for this prompt.`; the Hermes/MCP session
  variant is `no durable memories retrieved for this session.` (both cover
  empty pool / passive-filter omit); gate rejection keeps the byte-identical
  `no durable memories met the inject bar.`; a token-budget wipe says so in
  its own words. `zmem-bg.log` lines carry `reason=` (closed
  `INJECT_SILENT_REASONS` set in schema_meta) and `omitted=N` when the
  passive injection-risk filter dropped rows. No ranking, floor, budget, or
  omit membership change; no schema change.

### Tests
- `tests/test_explain_recall.py`: every explain reason, zero-write and
  fail-open contracts, source-scan ratchets.
- `tests/test_chain_unfold.py`: change-intent regex positive/negative bar,
  explicit-only unfold, budget/hop caps, telemetry exclusion, namespace
  isolation.
- `tests/test_eval_self_corpus.py`: refusals (missing/`--store`, home path,
  nonexistent path) and byte-passivity.
- Eval honesty: `retraction`/`polarity`/`change-intent` gold buckets with
  `explicit`-flag items (fixture rowids 51-64; ids 1-50 frozen), passivity
  proof for the explicit eval seam, bucket/coverage assertions updated.
- Surface/characterization/doc-drift guards for the new flags, the qualified
  hooks-write wording, and the frozen `KNOWN_SUBCMDS` list.

## [0.13.1] — 2026-08-28

### Added
- **Schema forward-compat window** (issue #65 follow-up): an older client no
  longer hard-refuses a store whose schema is newer but ADDITIVE-ONLY.
  `schema_meta.FORWARD_COMPAT_SCHEMA_VERSION` (default: same as
  `SUPPORTED_SCHEMA_VERSION`) defines the ceiling; between the two the client
  proceeds for memory read/write with a one-time stderr NOTICE (newer-only
  features unavailable). Above the ceiling it still refuses, now with an
  actionable message, and `ZMEM_ALLOW_NEWER_SCHEMA=1` overrides at the
  operator's own risk. Motivation: a shared store that migrated ahead of some
  installed clients must not brick those clients' memory writes until they
  update. Pinned by `tests/test_schema_forward_compat.py` (includes the
  older-client-stores-on-newer-store scenario); the fail-closed
  newer-version contract is unchanged by default in 0.13.1
  (`test_store_hardening.py`).

## [0.13.0] — 2026-08-27

This release folds in the previously-Unreleased work from issues #63 and #64
(below) alongside issue #65.

### Added
- **Schema v12 — Voyager usage-feedback counters** (issue #64):
  `memory.applied_count` / `memory.violated_count` (INTEGER NOT NULL
  DEFAULT 0). Written ONLY by the new explicit `feedback` CLI — hooks,
  `--no-bump` recall, PreCompact, and Hermes prefetch never advance them
  (source-scan ratcheted). Round-tripped by export/ingest (absent → 0 on
  pre-v12 files; malformed values refused fail-closed), included in
  `get --json`/recall output, and probed by a new doctor `voyager-counters`
  check

- **`feedback` CLI + enforce promote ladder** (issue #64, 9.4):
  `store.py feedback --id <uuid> --applied|--violated` (both flags refused
  exit-2; missing/tombstoned id exit-1). Ladder enforced in `promote`:
  eligible ⇔ `applied_count >= 3` AND `violated_count == 0` (replaces the
  retrieval/surfaced usage floor — passive exposure is no longer an
  eligibility input); a violated_count crossing to 2 applies a ONE-TIME
  −0.15 `trust_score` drop (`TRUST_VIOLATION_FLOOR_DROP`, clamped at 0;
  `signal` never auto-changed; never re-applied by ingest). `promote` with
  no eligible rows stays a clear no-op

- **Offline eval harness** (issue #64, 9.1/9.2/9.5): `scripts/eval_runner.py`
  (the canonical command CI runs) over `eval/gold.jsonl` — 30 items across
  six buckets (as-of updates, injection omission, entity aliases, namespace
  isolation, contested guidance, ordinary FTS) naming ids minted by the new
  deterministic 50-row fixture builder `tests/fixtures/eval_store.py`.
  Reports hit@k / MRR / as-of accuracy / injection-omit rate as JSON;
  `--store` REQUIRED (never touches the operator home store); auto-builds
  the corpus at the given path; model-absent by construction (fake embedder,
  pinned `ZMEM_TEST_NOW`); exit 0 regardless of scores, optional
  `--fail-under` off in CI. New CI steps run it and upload the JSON report
  as a workflow artifact (`if-no-files-found: warn`, score never gates)

- **Public-corpus eval adapters** (issue #64, 9.3): `scripts/eval_adapters.py`
  converts on-disk LongMemEval/LoCoMo-shaped corpora into gold JSONL;
  a missing corpus prints `skipped: ...` and exits 0 (CI downloads nothing);
  synthetic 3-row toy fixtures under `tests/fixtures/adapters/` prove the
  converters with no copyrighted text

- **`tune-weights --dry-run`** (issue #64, 9.6): evaluates the shipped
  W_BM25/W_CONFIDENCE/W_RECENCY/W_POPULARITY against a gold set, hill-climbs
  a deterministic candidate set (candidates passed through compute_score's
  new internal `weights` parameter — module globals never mutated), and
  prints suggested weights summing to 1.0. Writes nothing; no `--apply`
  exists — applying is a documented manual edit (SKILL.md §tune-weights)

### Tests
- New standalone suites: `tests/test_feedback_promote.py`,
  `tests/test_eval_runner.py`, `tests/test_tune_weights.py`
- Updated: characterization (KNOWN_SUBCMDS + re-recorded data hashes for the
  new counter fields), `test_jsonl_sync.py` (counter round-trip/legacy/
  malformed), `test_doc_drift.py` (feedback/eval/tune-weights doc needles),
  `test_promote.py` (ladder-based eligibility seeding),
  `test_storelib_exports.py` (v12 exports)

- **Embedding profiles registry** — `embed_profiles.py`: shipped `minilm`
  (Xenova/all-MiniLM-L6-v2 ONNX, 384-d, checksum-pinned) and test-only `fake`
  (deterministic 16-d placeholder vectors hashed from the content_norm form;
  doctor warns loudly on any non-temporary store using it) selected via
  `ZMEM_EMBED_PROFILE`; unknown profiles refuse exit-2 (issue #63, 8.2/8.5)

- **`reembed --all`** — full store rebuild under a selected profile with
  single-transaction dim conversion (crash-safe, idempotent, dry-run,
  batch-paced stderr progress, telemetry/content untouched); flagless
  backfill keeps its byte-identical legacy contract (issue #63, 8.3)

- **Doctor `embeddings_health` + checksum note** — profile/dim vs store match,
  rows with/without embeddings, shipped-profile inventory, deep checksum
  verification with the Xenova-ONNX-vs-PyTorch note, fake-on-real-store and
  zero-embedded-with-hybrid warnings, dedicated checksum-mismatch
  recommendation (issue #63, 8.1/8.4)

- **Optional cross-encoder rerank** — `ZMEM_CROSS_ENCODER=1` +
  `ZMEM_CROSS_ENCODER_MODEL=<local .onnx>`; post-MMR rerank on explicit CLI
  `recall` only, structurally unreachable from hooks/PreCompact/recent/
  search aliases/--no-bump, silent degrade on missing model, injectable
  test scorer via `storelib.cross_encoder.set_scorer` (issue #63, 8.6)

### Tests
- New standalone suites: `tests/test_embed_profiles.py`,
  `tests/test_embedder_checksum.py`, `tests/test_reembed.py`,
  `tests/test_cross_encoder.py`; extended `tests/test_doctor.py`

**Issue #65 — host and MCP completeness**

Added:
- **Schema v13 — episode storage** (issue #65, 10.7): `episode(id, namespace,
  started_at, ended_at, summary_memory_id, token_count)` +
  `episode_memory(episode_id, memory_id, added_at)` containers. Purely
  ADDITIVE migration (two CREATE TABLE IF NOT EXISTS; no `memory` column
  changes). New CLI: `episode-open` / `episode-add` / `episode-close
  [--summary]` / `episode-list [--json]`; `get --json` carries `episodes`
  linkage; doctor `episode-tables` check reports counts; export/ingest
  round-trip episodes via a `kind` discriminator (memory rows gain
  `"kind": "memory"`; legacy kind-less files still ingest). `episode` is NOT
  an ALLOWED_TYPES member — it is a container, not a memory type. Members
  must be LIVE at attach; memberships are append-only; close is refused on a
  closed episode; `token_count` sums the shared `row_token_cost` over LIVE
  members at close

- **MCP + Hermes session tools** (issue #65, 10.5 — D4 contract):
  `session_start` (passive prefetch: `--no-bump` so `retrieval_count` never
  advances, injection-risk/`untrusted_web` rows omitted, Phase 3 fence +
  0.5 recent floor, token-budget honored) and `session_end` (default is a
  NO-WRITE ack; an optional note writes exactly one row via the standard add
  path with capture-mode auto) on BOTH surfaces (`session_start`/
  `session_end` MCP tools, `zmem_session_start`/`zmem_session_end` Hermes
  tools)

- **Scoped MCP tokens** (issue #65, 10.2): `ZMEM_MCP_TOKEN_FILE` accepts a
  JSON object `{"token", "namespaces": [...]}`; requests outside the
  allow-list fail closed with the stable `namespace_not_allowed` error;
  namespace-less reads are denied for scoped tokens; the implicit
  user:global union is suppressed unless `user:global` is in the list. The
  unscoped operator token (env var or bare file) remains full-access —
  pre-v13 behavior unchanged. Malformed JSON / empty or invalid namespace
  lists / near-miss `global` scopes are hard exit-2 config errors. New
  doctor `mcp-token` check warns `unscoped_token: true` on operator tokens
  and never reports the token value (issue #65, 10.10)

- **Token budget** (issue #65, 10.9): `ZMEM_INJECT_TOKEN_BUDGET` (default
  1500, 4-chars/token heuristic). Hooks (UserPromptSubmit, PreCompact,
  SubagentStart, SessionStart Tier 2) and both session_start tools stop
  adding bullets at the budget; `decision`/`constraint` rows are never
  dropped; lowest-score `signal=none` rows drop first. The hook bg-log line
  gains `tokens=<used>/<budget>`. Read `--json` envelopes report
  `tokens_used`/`tokens_budget` (`storelib/inject.py` is the single
  implementation)

- **Read envelope + structured write warnings** (issue #65, 10.8):
  `recall`/`recent`/`search --json` emit `{"results", "count", "omitted",
  "injection_risk", "tokens_used", "tokens_budget"}` so hosts stop guessing
  from stderr (`search` gains `--json`); `add`/`update` gain `--json`
  printing `{"id", "result", "warnings"}` with structured
  `{"type": "redacted", "count": N}` entries; MCP add/update and Hermes
  add/update surface the structured warnings (Hermes previously dropped
  them). One redaction helper (`storelib.write.redact_text`) serves CLI,
  MCP, Hermes, mine-history, organize, and episode summary writes; a
  redaction failure in auto mode refuses the write (fail-closed)

- **Surface parity** (issues #65 10.1/10.3/10.4, closes #38 I1/I4/I5): MCP
  `add` validates namespace shape fail-fast (CLI-identical rules); MCP
  `update` + Hermes `zmem_update` gain the `namespace` override (parity
  with `update --namespace`; guarded by the token scope); Hermes
  `zmem_search` pins `--link-hops 0` (the CLI search contract) and unwraps
  the read envelope; Hermes `prefetch` unwraps the envelope

- **Closeout redaction feedback** (issue #65, 10.6): the closeout skill
  requires one count-based operator line after a redaction —
  `zmem: redacted <N> secret-like value(s) from the captured memory (value
  not shown).` — derived from the warning count, never the value

Tests (issue #65): new standalone suites `tests/test_mcp_auth.py`,
`tests/test_session_tools.py`, `tests/test_redaction.py`,
`tests/test_episodes.py`, `tests/test_token_budget.py`; extended
`tests/test_mcp_server.py`, `tests/test_doctor.py`.

## [0.12.0] — 2026-08-26

### Added

- **Sleep-time organize + SessionStart wiring** (issue #62, schema-stable — no
  migration, older clients keep working):
  - New `organize` subcommand — the session-cadence job that replaces the
    `consolidate` call at SessionStart (the `consolidate` CLI remains for
    manual runs; the Hermes session-end hook dispatches organize too, so all
    three surfaces run the same maintenance act). Bounds an episode to the
    most recent live non-summary rows
    (`ZMEM_ORGANIZE_EPISODE_BOUND`, default 256), backfills missing
    entity links and `memory_link` edges on working rows, runs consolidate's
    EXACT cluster/absorb/contested machinery on that episode (sharing one
    gate implementation, the `last_consolidation` cadence meta keys, and the
    single-flight lock — organize and consolidate are two entry points to one
    maintenance act, so on a given store at most one maintenance run happens
    per cadence window), then adds sleep-time deliverables: deterministic
    keeper compression (`ZMEM_KEEPER_COMPRESS_CHARS`, default 4000) applied
    BEFORE topic identity is keyed, then a topic hierarchy over the
    post-compression live rows via the shared neighbor predicate and
    hierarchical extractive summaries (real `summary,topic` rows, confidence
    0.5, member ids in `merged_from`, identified structurally by
    `source_ref` + `merged_from` — never the mutable tags column — with an
    idempotent Phase-4 update and stale-overlap supersession), an optional
    idle gate (`ZMEM_ORGANIZE_IDLE_HOURS`, default 0) and unrecalled prune
    pass-through (`--prune`). LLM-free by default;
    `--dry-run`/`--json` report per-step would-be counts and `--dry-run`
    writes nothing.
  - Optional LOCAL NLI judge (`ZMEM_NLI_CMD`, issue #62 7.5): when set,
    consolidate's mixed-polarity contested clusters consult it before
    parking — only an `entailment` verdict on every polarity-flagged pair
    (any two members whose negation polarity differs) un-parks; any other
    verdict or failure parks (never auto-merges; a judge failure is a
    distinguishable stderr diagnostic, and on Windows backslash paths in the
    template are normalized before parsing). Unset = byte-identical
    behavior.
  - Unrecalled-prune extension (issue #62 7.6): `consolidate --prune` — and
    therefore `organize --prune` — may additionally qualify a live row whose
    `last_surfaced` is older than `ZMEM_UNRECALLED_DAYS` (default 30);
    `signal != none` is never pruned.
  - The replaced inline consolidate neighbor loop is now the shared
    `_gather_neighbors` predicate used by BOTH the consolidate seed loop and
    the organize related-graph — one decision, two call sites, behavior
    identical (all pre-existing consolidate tests pass unchanged).

## [0.11.0] — 2026-08-25

### Added

- **Schema v11 — associative links (A-MEM lite) + trust_score** (issue #61,
  all six mandatory tasks, no stubs):
  - New `memory_link(src_id, dst_id, relation, score, created_at,
    UNIQUE(src_id, dst_id, relation))` edge table with a relation CHECK over
    `related|supports|contradicts|updates|extends|derives` (directed, no
    self-links) and `memory.trust_score REAL NOT NULL DEFAULT 1.0`. Lossless
    v10→v11 migration (migrated rows read trust 1.0; existing `merged_from`
    values normalized in place). New `storelib/links.py` owns the surface.
  - **Automatic link generation on every `add`/`update`**: the namespace-aware
    neighbors dedup already computed (embedding cosine, or Jaccard when the
    model is absent — model-absent stores link too) above
    `ZMEM_LINK_THRESHOLD` (default 0.75) become `related` edges, stored both
    directions. When the consolidate polarity signatures disagree the pair
    links as `contradicts` instead and the dedup MERGE IS SKIPPED — "always
    X" no longer absorbs "never X" at write time. Deterministic; no LLM (no
    `ZMEM_LINK_LLM` knob exists).
  - **trust_score deltas**: one contradicts event = −0.10 to BOTH rows; a
    `supports` link or a polarity-agreeing duplicate re-add (corroborating
    add) = +0.05; clamped to [0.0, 1.0] so ten contradictions land at exactly
    0.0. `confidence`/`signal` are never touched by linking. Sync ingest
    restores trust verbatim and never re-applies deltas (re-ingest stays an
    exact no-op).
  - **Attribute evolution**: each linked neighbor unions the new row's tags
    and re-derives its entity links. Content, confidence, signal, and
    retrieval_count are never rewritten (no content-rewrite helper exists).
  - **Budgeted 1-hop recall expansion** (`--link-hops 0|1` default 1,
    `--link-budget N` default 2): after MMR, `related`/`supports` neighbors
    are appended up to the budget; `contradicts` neighbors only if they
    survive the confidence floor, tagged `[CONTESTED LINK]` (JSON:
    `contested_link`). Namespace-contained, `--as-of`-respecting, never
    double-counting; expansion rows never advance telemetry (popularity
    stays a query-match signal). `search`/`recent` never expand.
  - **Inspection/curation CLI**: `links --id UUID [--json]` (missing id
    exits 1, the `get` contract) and `links --add --id A --id B --relation R
    [--score S] [--reason ...]` (the sanctioned insertion path for the typed
    relations; `--relation contradicts|supports` adjusts trust and therefore
    REQUIRES `--reason` — exit 2 without it, the `contradict` convention);
    `contradict --id A --id B --reason ...` (required reason, pair + trust
    event, never merges/deletes/rewrites; the schema has no reason column so
    the reason is validated + echoed, not persisted).
  - **Sync round-trip**: `export-jsonl` now carries `trust_score` and each
    row's outgoing `links` (created_at preserved); `ingest-jsonl` validates
    both fail-closed and applies links in a post-row pass
    (`links_added`/`links_skipped` in the summary). Extra keys are
    backward-compatible — a pre-v11 client ingests a v11 export cleanly and
    simply drops the new fields.
  - **merged_from is a de-duplicated first-seen list** (issue #61 6.6): the
    consolidate writer, sync ingest, and the v11 migration all normalize
    through one shared helper; empty stays empty, no id is ever lost, and no
    `memory_merge` table was added.
  - Doctor gains the `link-tables` check (table + column + trust range);
    `get --json` shows `trust_score`; SKILL.md/CUTOVER.md document everything
    above.

### Changed

- Write-time dedup now applies the polarity guard before merging (a
  contradicting near-duplicate inserts as its own row + `contradicts` links
  instead of refreshing the existing entry).

## [0.10.1] — 2026-08-25

### Added

- **Automated releases — the release contract is now self-enforcing**
  (fixes the recurring "merged but never published" failure: v0.8.5 /
  v0.8.6 / v0.8.8 were never tagged, and v0.9.0 / v0.10.0 merged as
  manifest bumps with no tag or GitHub Release, so release-tracking
  downstream installs never saw them until the releases were retrofitted
  by hand):
  - New `.github/workflows/release.yml`: on every push to main it runs the
    gate and, when the version has moved, tags the MAIN COMMIT that carries
    it (never a PR-branch head — squash merges orphan branch-head tags) and
    publishes the GitHub Release with the CHANGELOG section as notes.
    Merges without a version bump are no-ops; the run is idempotent
    (existing tag ⇒ skip) and serialized by a concurrency group.
  - New `scripts/release_gate.py`: discovers EVERY tracked host-facing
    manifest (`git ls-files`, not a hardcoded list), fails the run on any
    version disagreement (partial bump) or a missing matching CHANGELOG
    section, and emits the release decision + notes for the workflow.
    Runnable standalone for local pre-flight.
  - New `tests/test_release_parity.py`: manifest parity + CHANGELOG
    alignment against the live repo, unit coverage of the gate's parsing,
    and structural pins on the workflow (main-push trigger, `contents:
    write`, no `pull_request_target`, `--target ${{ github.sha }}`, gate
    invocation).
  - README release/upgrade section updated: releases are cut automatically
    when a version bump merges to main; the host-side plugin refresh stays
    a deliberate user action (documented flow unchanged).
- **Deterministic frozen surfaces** (flake caught by the full loop during
  this work): the characterization recall hash flipped on a day boundary
  with zero code change — composite `_score` embeds continuously-decaying
  recency, so its 4-decimal rounding drifts every few days (latent since
  the freeze's inception). New `ZMEM_TEST_NOW` seam in recall scoring
  (ISO-8601, naive read as UTC, stderr warning on garbage, wall clock when
  absent); the characterization suite pins it to the fixture sentinel so
  the frozen recall surface is time-invariant; hash re-captured with the
  pinned clock; seam honored + byte-determinism test added.
- Release-workflow hardening from the final-critic round: publish-step
  idempotency is RELEASE existence (`gh release view`) rather than bare
  tag existence, so a human-pushed bare tag heals on the next merge; both
  actions are SHA-pinned (this workflow holds a contents:write token,
  unlike ci.yml's documented read-only tag-pinning waiver).

## [0.10.0] — 2026-08-24

### Added

- **Schema v10: entity identity, third RRF signal, MMR diversity** (issue
  [#60], SOTA PR 5/10). All seven mandatory tasks shipped, with no stubs and
  no unwired code:
  - Three new tables — `entity(id, kind, canonical_name, created_at,
    updated_at)` with kinds person/project/tool/preference/other,
    `entity_alias(entity_id, alias_norm)` with a GLOBAL `UNIQUE(alias_norm)`,
    and `memory_entity(memory_id, entity_id, role)` with
    `UNIQUE(memory_id, entity_id)`. The v9→v10 migration is lossless for
    memory rows and BACKFILLS entities for every existing row (tombstoned
    rows included — `--as-of` recall reaches history through the entity
    lane). New module `storelib/entity.py` is the single owner of the logic.
  - **Deterministic entity extractor on write** (no LLM, no model download,
    no `ZMEM_ENTITY_LLM` knob by design): explicit `entity:Name` /
    `entity:<kind>:Name` tags, the `project:<suffix>` namespace suffix,
    `--tags` tokens (`kind:Name` or plain), backtick-quoted spans in content
    (kind `tool`), and CamelCase identifiers of 2+ humps (kind `other`).
    Stopwords (`the`, `and`, `use`, …) and path/URL-shaped tokens never
    become entities; `person` entities are only ever minted by an explicit
    `entity:person:` tag. Alias normalization reuses the exact
    `content_norm` function; a paraphrase re-add links to the SAME entity
    ids (alias-first upsert, first-seen kind wins). Runs inside the write
    transaction at every insert site (add, update, both ingest-jsonl paths)
    and re-derives at every mutation site that changes content/tags/
    namespace (dedup tag-union merges, consolidate absorb, both namespace
    re-key paths).
  - **Entity matching as the third RRF list** at recall: the query runs
    through the same extractor, and plain query tokens are matched
    read-only against stored aliases. Matched memories join
    `_rrf_fuse` as a third ranked list (rank = number of matched entities,
    then recency) under the same namespace filter and the same `--as-of`
    temporal predicate as the other lanes. Model-absent by design — the
    fusion block now runs whenever ANY non-FTS lane produced ids, so the
    entity lane is live on stores without the embedding runtime. Unknown
    alias = empty third list; RRF stays per-id additive. Entity-only
    matches score with a relevance proxy (matched/total query entities).
  - **Entity cards + `entity-list`**: recall `--json` rows gain
    `entities: [{id, kind, name}]`; the fenced hook render shows at most
    THREE names per row (never ids) — Hermes prefetch render matches; MCP
    tools pass the JSON through. `get --id` shows the row's links.
    `store.py entity-list [--kind] [--json]` is the documented inspection
    surface for humans and doctor.
  - **MMR diversity after RRF** (issue 5.5): each recall tier re-orders its
    candidate set with Maximal Marginal Relevance before `--limit` — near
    paraphrases stop crowding out distinct facts. Similarity: embedding
    cosine when both rows have embeddings (pure-stdlib unpack mirroring the
    writer's `struct.pack` format), else Jaccard on `content_norm` tokens.
    Lambda default 0.7, env `ZMEM_MMR_LAMBDA` (1.0 == no diversity),
    `--no-mmr` opt-out on `recall`; `search` inherits MMR through the
    shared recall path.
  - **`store.py entity-merge --from ID --to ID [--confirm]`**: manual
    reconciliation of duplicate entities. Dry-run by default (plan printed,
    nothing written); with `--confirm` aliases and memory links move to the
    target in one transaction (collisions counted, never overwritten) and
    the source entity is deleted. Refuses unknown ids, `--from == --to`,
    and kind mismatches. Nothing auto-merges entities of any kind, ever.
  - **JSONL sync decision** (documented + tested): entity links are NOT
    carried in export-jsonl — they are store-local derived data like
    embeddings and content_norm; `ingest-jsonl` REBUILDS them by running the
    same deterministic extractor, so two stores ingesting the same rows
    derive the same entities with no cross-store id collisions.
  - Doctor gained an `entity-tables` check (presence + non-vacuous counts);
    CUTOVER.md and SKILL.md updated (schema v10, three-signal retrieval
    rewrite, entity-list/entity-merge sections).

### Tests

- New `tests/test_entity.py` (schema/UNIQUE constraints, migration backfill,
  extractor acceptance + negatives, third-list recall incl. alias-after-merge
  and `--as-of`, cards/fence caps, entity-list, entity-merge, mutation-site
  re-derivation, two-store JSONL rebuild parity) and `tests/test_mmr.py`
  (crowding acceptance, `--no-mmr`, lambda semantics, env parsing, Jaccard
  model-absent path, known-bytes cosine). `test_jsonl_sync.py`,
  `test_doc_drift.py`, `test_storelib_exports.py`, and
  `test_store_characterization.py` extended for the new surface;
  `test_schema_version.py` needed no edit (it reads the version constant
  dynamically and the docs were bumped to v10).

[#60]: https://github.com/zaxbysauce/zmem/issues/60

## [0.9.0] — 2026-08-24

### Added

- **Schema v9: append-only knowledge lineage** (issue [#59], SOTA PR 4/10).
  All seven mandatory tasks shipped, with no stubs and no unwired code:
  - `store.py update` / `store.py invalidate` — schema-validated, append-only
    history. `update` re-creates the row with `update_of` linkage, tombstones
    the old row (`superseded_at` + `valid_until`), and inherits provenance
    (folding into an existing live duplicate when one is found); `invalidate`
    REQUIRES a reason and tombstones with the closed `valid_from`/`valid_until`
    interval.
  - `--as-of ISO-8601` on `recall`/`recent`/`search` now applies the FULL
    temporal predicate via `_as_of_temporal_predicate`: `valid_from` is
    INCLUSIVE, `valid_until` is EXCLUSIVE, and the hard `superseded_at IS
    NULL` filter is dropped when as_of is set — a point-in-time read recovers
    content that was valid at that instant. Lane bound: in hybrid mode such a
    row is reachable only through the lexical (FTS5) lane; the vector lane
    keeps its live-only pool (documented in SKILL.md).
  - New `type` values `decision` and `constraint` (closed enum, word-exact
    ratchet), accepted end to end by capture tooling.
  - Taint provenance with a closed three-rank model
    (`trusted_internal < untrusted_tool < untrusted_web`): worst-of lineage
    through update re-creation and consolidate absorb; unknown taint refused;
    `[UNTRUSTED TOOL]` / `[UNTRUSTED WEB]` markers on the explicit recall
    text path; passive (`--no-bump`) surfaces omit `untrusted_web` exactly
    like injection-risk rows.
  - Hermes and MCP agent surfaces default new memories to `untrusted_tool`
    and validate taint against the shared `schema_meta` enum.
  - Wiring everywhere the feature has a surface: CLI, Hermes, MCP tools,
    JSONL ingest/sync (authoring + validation), doctor checks, CUTOVER.md,
    and SKILL.md.

### Fixed

- Swarm-pr-review round (run 20260824-pr70, all findings validated + challenged):
  - **Append-only history is write-once**: a second `invalidate`/`supersede` on
    an already-tombstoned row is refused (exit 2) instead of moving
    `valid_until` forward and replacing the audit reason (PRR-B).
  - **Worst-of taint now covers every merge**: a duplicate `add` or
    `ingest-jsonl` row that folds into an existing keeper upgrades the keeper's
    taint (worst-of), and a legacy v8 row with absent taint derives from its
    signal exactly like the migration backfill (PRR-A/G/H).
  - `update` applies the capture policy BEFORE the size cap (matching `add`),
    so auto-redacted content the add path accepts is no longer rejected
    (PRR-C); `invalidate --reason` must be non-empty (PRR-I).
  - `ingest-jsonl` accepts a genuine `1970-01-01T00:00:00Z` timestamp (the
    epoch-zero/parse-failure sentinel collision) and refuses a tombstone whose
    `valid_until` is LATER than its `superseded_at` (a row cannot outlive its
    own tombstone; an authored earlier end stays preserved) (PRR-E/F).
  - Hybrid `--as-of` recall temporal-filters the vector candidate pool before
    RRF fusion (with over-fetch), so future-dated rows cannot crowd valid
    candidates out of the fixed KNN window (PRR-K).
  - Hermes `zmem_add`/`zmem_update` pass `--capture-mode auto` (secret
    redaction parity with MCP) and neither remote surface returns raw
    subprocess stderr to clients any more (stable `[zmem]` refusal lines pass
    through a sanitizer; anything else is truncated) (PRR-L/M).
  - `--content -` reads content from stdin on `add`/`update`, and the
    Hermes/MCP surfaces pipe oversize payloads through it — Windows argv caps
    far below the 65536-char content limit (PRR-P).
  - Test-fixture truth: the characterization builder no longer stamps a past
    `valid_until` onto LIVE rows (their "never expires" marker is `''`), and
    the frozen hashes/comments were re-captured truthfully (PRR-Q/X); vacuous
    ratchets tightened (Hermes wiring, dedup-fold worst-of, doc-drift needles,
    malformed-row absence checks) (PRR-R/S/T/U/V).

- Replaced the earlier `valid_until` placeholder ("Phase 4") with the real
  predicate; the `_recall_one_tier`/`_recent_one_tier`/`_fetch_by_ids`
  docstrings no longer claim a placeholder exists.

### Known issues

- Historic content valid only *before* a supersede is not reachable via the
  semantic vector lane in hybrid `--as-of` recall (the vector candidate pool
  is live-only); re-run with `--no-hybrid` to include the lexical lane.

[#59]: https://github.com/zaxbysauce/zmem/issues/59

## [0.8.9] — 2026-08-24

### Added

- **Retrieval correctness: namespace KNN, hybrid default, inject safety**
  (issue [#58], SOTA PR 3/10). All nine task families shipped:
  - Shared namespace-aware vec0 KNN helper (`ZMEM_VEC_NS_OVERFETCH`, default 8)
    for recall AND write-time semantic dedup — a foreign namespace can no longer
    crowd out same-namespace vector slots.
  - **Hybrid recall is now the DEFAULT when embeddings are available.**
    `--no-hybrid` forces the lexical lane; `--hybrid` remains a parsing alias.
    `search` (CLI, MCP, Hermes tool) stays keyword-only by contract.
  - `prompt-injection-risk` rows are omitted from every passive (`--no-bump`)
    surface and prefixed `[INJECTION RISK]` on explicit recall; the patterns
    re-run at emit time as defense in depth.
  - Every hook that inlines memory text (UserPromptSubmit recall, SubagentStart,
    SessionStart Tier 2, PreCompact, Hermes prefetch) renders a
    non-executable fence + "untrusted notes" disclaimer with full provenance
    (id / confidence / signal / namespace / type / source_ref).
  - `--as-of ISO-8601` on `recall` / `recent` / `search` (any zone offset is
    normalized to the UTC instant; the `valid_until` half of the predicate is
    a Phase 4 placeholder).
  - Selective inject: grounded signals (test/compile/lint/reviewer/user) inject
    at the 0.25 floor; only `signal=none` is tightened to 0.4. Three named,
    env-overridable floors live in `schema_meta.py` (single source of truth).
    The `injected|silent` decision is logged to `zmem-bg.log` (size-capped).
  - Claude Code `PreCompact` re-inject (read-only, `--no-bump`), sourced from
    the new shared `hooks/lib/zmem-recall-body.py` so all hook renders share
    one implementation. ZCode/Codex configs unchanged (no such event).
  - Doctor checks `hybrid-default` and `vec-ns-overfetch` (namespace-scoped,
    sqlite-vec-aware).

### Fixed

- Review-found defects closed in the same release: hook recall queries now
  parse the `prompt` field from the JSON event (was: raw JSON as the query);
  doctor's two new checks work on real stores (NameError + tuple-row crashes);
  `--as-of` filters the hybrid vector lane too; non-UTC offsets compare as
  instants; `--no-hybrid` wins when combined with `--hybrid`; the recall-body
  budget truncation actually truncates; the inject log honors `ZMEM_DATA`
  overrides; session-start's import-failure path omits Tier 2 instead of
  emitting unfenced text.

### Known issues

- `hooks.zcode.json` registers no `SubagentStart` event, so subagent recall
  fires only on Claude Code / Codex (pre-existing host capability gap).
- The launcher's hard-budget truncation can still slice a fence mid-block at
  the transport layer (pre-existing prefix cut).

[#58]: https://github.com/zaxbysauce/zmem/issues/58

## [0.8.8] — 2026-08-23

### Changed

- **Behavior-identical split of `store.py` into the `storelib` package**
  (issue [#57], SOTA PR 2/10). The 6773-line `skills/memory/scripts/store.py`
  is now a 50-line re-export shim; all behavior lives under
  `skills/memory/scripts/storelib/` as the modules `schema`, `write`, `recall`,
  `consolidate`, `sync`, `backup`, `promote`, `mine`, and `cli`, with a
  re-exporting `__init__.py`. Every hook, MCP subprocess, Hermes provider,
  SKILL.md and test that invokes `store.py` / `import store` keeps working
  unchanged.
- **Live `store.X` reads preserved via module `__getattr__` forwarding to
  `storelib`** — the shim exposes the full surface including live
  `__embeddings` / `__NS_MIGRATION_CHECKOUTS` /
  `CONSOLIDATE_MAX_ROWS_PER_NAMESPACE` / `__absorb_into_keeper` /
  `__degraded_embedding_warned` forwards for the mutable globals consumers
  mock or read.
- **Per-load `storelib._refresh_env_state()`** restores the pre-split
  contract that each `store.py` load re-derives `STORE_PATH` / `CORE_MD_PATH`
  from `ZMEM_STORE` / `ZMEM_DATA` and applies any present `ZMEM_*` tunable
  override (the owning submodules re-parse env-derived tunables on each
  load; default values are never overwritten).

### Added

- **Behavior-freeze characterization suite**
  (`tests/test_store_characterization.py`, issue #57, task 2.1): hashes the
  data-surface output of `stats`/`list`/`recall`/`export-jsonl` on a fixed
  fixture store (model-absent, LF + time normalized, embeddings-status
  sentinel-normalized) so future refactors can prove behavior identity. The
  CLI surface is verified by structural assertions on
  `KNOWN_SUBCMDS` + per-subcommand custom-flag presence (argparse
  HelpFormatter's wrap/blank placement is platform-divergent even at fixed
  `COLUMNS=100` and cannot be frozen as a stable cross-platform hash, so the
  contract is the structural surface rather than rendered text).
- **Export-surface inventory** (`tests/test_storelib_exports.py`,
  issue #57, task 2.6): freezes the ~200-name `store.*` API surface from the
  pre-split monolith and asserts every name still resolves on the shim,
  including the recall-weight invariant `W_BM25 + W_CONFIDENCE + W_RECENCY
  + W_POPULARITY == 1.0`.

### Notes for downstream consumers

- **No action required.** `import store; store.X` continues to resolve every
  name that resolved on `0.8.7`. `from storelib.X import Y` is also now
  available as a structural-import surface; consumer code is free to migrate
  to direct submodule imports at its own pace but need not.
- The shim calls `storelib._refresh_env_state()` at every load, so env-derived
  knobs (`ZMEM_STORE`, `ZMEM_MODELS_DIR`, `ZMEM_*` tunables) behave
  identically to `0.8.7` for every consumer.

## [0.8.7] — 2026-08-22

### Added

- **Transcript correction mining core** (issue [#46], PR 1/4 of the
  claude-reflect port). Ported the MIT claude-reflect correction-pattern
  library as a host-agnostic module `skills/memory/scripts/corrections.py`
  (strong/weak/tiered English + CJK patterns, guardrails, false-positive vetoes,
  explicit `remember:` handling).
- **`store.py failures` now splits user rejections out of genuine failures.** A
  Claude Code tool rejection (`The user doesn't want to proceed`) is no longer
  counted as a failed tool call; it is reported in a new `rejections:
  [{tool, reason}]` array, with the user's stated reason extracted (multi-line
  reasons joined, marker stripped, newline-free for fence-integrity). The Stop
  hook (`hooks/zmem-reflect.sh`) and the SubagentStop hook
  (`hooks/zmem-subagent-reflect.sh`) surface these as a distinct fenced section
  with a `--signal user` hint, and the failure count no longer miscounts
  rejections. The ZCode db substrate and unknown/non-CC schemas fail open to
  empty `rejections`, so their prompt output is unchanged.
- **New `store.py corrections --transcript <path>` subcommand** (read-only, CC
  transcript format): mines detected corrections, sanitizes message text, and
  applies the capture-policy secret redaction/annotation.
- **New dev harness `scripts/pattern_harness.py`** for tuning the correction
  patterns against real transcripts (offline, stdlib-only, never shells out to
  a model).
- **Live correction capture** (issue [#47], PR 2/4 of the claude-reflect port).
  A new `capture-correction` hook (registered 2nd under `UserPromptSubmit` on
  Claude Code, ZCode, and Codex) queues mid-session user corrections
  ("no, use X", "remember: …") into a namespace-scoped plaintext sidecar queue
  (`<store-data-dir>/queue/<ns>.json`) — hooks never write the store. New
  `store.py queue-list` / `queue-clear` subcommands (store-independent,
  pre-connect); `queue-clear` requires exactly one of `--id`/`--all`/
  `--drop-stale`. SessionStart shows a pending (non-stale) candidate count; the
  closeout skill gained a Step 0.5 queue-review pass that writes only what
  clears the bar via `add --signal user`.
- **Cold-start bootstrap** (issue [#48], PR 3/4 of the claude-reflect port).
  New read-only `store.py mine-history` subcommand mines HISTORICAL Claude Code
  transcripts (`~/.claude/projects/**/*.jsonl`, incl. `agent-*.jsonl`) into one
  merged candidate report — corrections, user rejections (with reasons), and
  cross-session aggregated tool-error patterns. Ported `TOOL_ERROR_EXCLUDE_
  PATTERNS` + `PROJECT_SPECIFIC_ERROR_PATTERNS` + `aggregate_errors` into
  `corrections.py`, whose occurrence→weight mapping is named `review_priority`
  (review ORDERING, never a zmem confidence). Flags: `--transcript-dir`,
  `--all-projects`, `--days`, `--min-count`, `--limit`, `--queue`, `--json`.
  Never writes the store in any mode; `--queue` appends `source=history-mine`
  candidates (corrections + `error_pattern` kinds) to the #47 sidecar queue for
  closeout review. A box with no `~/.claude` exits cleanly (rc 1, message, no
  traceback). New module `history_mining.py` (discovery/folder-encoding/dedup/
  queue-synthesis); README gained a "Bootstrap / cold start" section.
- **Contradiction-aware consolidate** (issue [#49], PR 4/4 of the
  claude-reflect port). Similarity alone ranks "always X" / "never X" as
  near-duplicates, so `consolidate` could merge a memory's own refutation into
  the row it contradicts. A deterministic stdlib negation-polarity heuristic
  (ported concept from claude-reflect's `detect_contradictions`, MIT) now marks
  mixed-polarity clusters **contested**: never auto-merged — not even with
  `--force` — and always reported (per-cluster block + summary line + a new
  `--json` machine-readable run report whose stdout stays strictly
  parseable, human prose moved to stderr; override runs print a
  merged-CONTESTED trace instead of the contested block). `--merge-contested` is the explicit
  override for confirmed heuristic false positives. Contested members stay
  live and neighbor-eligible for later clusters. The closeout skill's Step 4
  now routes contested clusters to Step 3 `supersede` + recapture instead of
  merging. Works identically on the lexical (no-embeddings) and cosine
  clustering paths, and for every host including Hermes via the shared store.
- **Commit-boundary closeout nudge** (issue [#49]). The convention-capture
  PostToolUse hook (Claude Code, ZCode, Codex) now also nudges the closeout
  skill on the first `git commit` of a session (not `--amend`) — the strongest
  natural "unit of work finished" moment (concept ported from claude-reflect's
  `post_commit_reminder.py`, MIT). The nudge uses its own per-session cooldown
  marker (independent of the cadence nudge in both directions, reaped by
  `sweep`), still counts the call toward the cadence interval, and enriches
  with the pending #47 correction-queue count when one exists (degrades
  silently otherwise). All hook invariants kept: fail-open, exit 0, sentinel
  envelope, and no memory writes — the only store touch is the pre-existing
  per-session cadence-counter increment in the `meta` table.
- **Two new doctor checks** (issue [#49]). `tier0-size` reports lines/bytes of
  the always-injected Tier-0 files (`core.md` via the canonical
  `host.resolve_core_md_path()`, plus the ZCode project `AGENTS.md`) and warns
  above fixed 200-line / 16KB constants — an overgrown Tier-0 silently eats
  the context budget (threshold concept from claude-reflect, MIT).
  `session-retention` reports Claude Code's transcript retention window
  (`cleanupPeriodDays`, default 30, `settings.local.json` overriding) with the
  raise-it remediation for historical-mining users, and reports a clean
  "not applicable — no Claude Code installation detected" skip on CC-less
  boxes (port of claude-reflect's `get_cleanup_period_days`, MIT). Neither
  check can fail the report; doctor stays read-only and fail-open.

- **Documentation truth pass (issue [#56], first PR of the SOTA train).** Every
  agent-facing contract now matches code at schema v8 / plugin 0.8.7: SKILL.md
  and CUTOVER.md claim the current schema v8 (were v6 / v7); SKILL.md documents
  the shipped `--hybrid` BM25+vector recall (RRF fusion, k=60, fail-open to
  FTS5 when the embedding runtime is absent) and drops the stale
  "vector/embedding recall is a future optional tier" sentence; `--hybrid`,
  `--no-bump`, and `--include-global` are documented together with the
  passive-vs-explicit bump split (hooks and Hermes prefetch are passive surface
  events; explicit CLI/MCP/Hermes recall bumps `retrieval_count`, issue #21);
  the `add` signal-tier text matches code (`none`=0.2 below the 0.25 recall
  floor by design, #36 M3); PLAN.md carries an executed-history banner (which
  discloses that the §10b execution log lacks P3/P7/P9 entries, points at their
  in-tree surfaces, and appends retrospective entries for those three) and
  corrects the stale claim that `userConfig.storeDirectory` is unwired (#38 I6 —
  `zmem-launch.js` consumes `CLAUDE_PLUGIN_OPTION_STOREDIRECTORY`; `ZMEM_DATA`
  still wins when both are set).

- **`get --id` not-found exit contract is documented — and `get` no longer
  crashes on embedded rows (#38 I7).** A missing id exits 1 with the stable
  stderr line `[zmem] no memory with id <id>` (found: exit 0 + JSON) — the
  same not-found code as `supersede`, now stated in `--help` and the module
  docstring instead of being an undocumented accident. Additionally, on hosts
  where the optional embedding runtime is installed, `get --id <existing>`
  tracebacked with `TypeError: Object of type bytes is not JSON serializable`
  (the row's embedding BLOB went through `json.dumps` raw) — invisible to the
  model-absent CI matrix and caught by the new contract test on a
  model-present host. Binary columns now render as a `<N-byte blob>` marker
  so `get` always emits JSON and exits 0 for an existing id.

- **Python floor is 3.11.** README, the memory skill, doctor messaging, and
  script docstrings now state Python 3.11+ (CI and the Hermes lane run 3.11).
  Doctor WARNS below 3.11 (previously: hard-fail below 3.8, silent pass on
  3.8–3.10).

- **Repo hygiene (#38 I12).** `.swarm/` and `graphify-out/` are gitignored;
  the 33 tracked graphify cache files (whose keys embedded absolute operator
  home paths) are untracked; every remaining absolute home path in tracked
  files was replaced with a placeholder/env-var form, enforced by test.

### Fixed

- **Embeddings now resolve to the shared model cache without an env var.** The
  models-dir resolver (`embeddings._resolve_models_dir`) now falls back to the
  box-wide shared cache (`<store data dir>/../models`, e.g. `~/.zmem/models`)
  when the plugin's bundled `skills/memory/models` dir lacks the (gitignored)
  `minilm.onnx`. Resolution order: `ZMEM_MODELS_DIR` override → bundled
  if it has the model → shared cache if it has the model → bundled default. A
  fresh checkout or host whose model lives once under `~/.zmem` now keeps
  semantic recall working with no configuration. The embedding/availability and
  consolidate test suites pin `ZMEM_MODELS_DIR` at module scope so they stay
  deterministic on hosts where the shared cache is present.

- **Queue hardening** (from an independent swarm-pr-review): `queue-clear` no
  longer silently wipes the whole namespace when invoked without a selector;
  the namespace filename encoding is now collision-free for all Unicode (one
  `_xNN` token per UTF-8 byte, lossy-no-raise for lone surrogates); a failed
  whole-queue unlink reports "failed (queue untouched)" instead of a fabricated
  "cleared N"; capture acknowledgement is emitted only when the append
  persisted; queue files/dirs are best-effort owner-only hardened (chmod/icacls)
  including on the rename window and on pre-existing dirs; and README/SKILL.md
  document the queue path as derived from the store data dir rather than a
  fixed `~/.zmem` path.

- **`export-pack --max-bytes` help now matches the (safer) behavior (#38 I8).**
  The help claimed structural text was exempt from the budget; in code the
  budget covers the whole rendered pack (structural framing included) and only
  framing appended after the budget walk (an empty later section's heading and
  the trailing omitted-count note) can exceed it. Help text, SKILL.md,
  docs/CLOUD.md (the Tier 1 contract the help points at — still carried the
  pre-#37-L1 claim until this pass), and a help-vs-behavior test now state the
  truth; no behavior changed.

- **MCP `recall` docstring documents the intentional `retrieval_count` bump on
  explicit tool recall (#38 I2).** Behavior is unchanged (and pinned by
  `tests/test_surface_consistency.py`): explicit tool reads bump by design;
  only the docstring note was missing, so the tested design stopped being
  re-discovered as a bug.

### Tests

- Added `tests/test_corrections.py`: rejection extraction (both transcript
  formats, with/without reason, multi-line + marker-stripped + newline-free,
  dedup by `tool_use_id`), non-CC schema fail-open, pattern-library coverage,
  `corrections` read-only + secret handling, and `failures` output shape
  stability. Extended `tests/test_failures.py` for the new `(details,
  rejections)` return and `rejections` key.

- Added `tests/test_schema_version.py` and `tests/test_doc_drift.py` (issue
  [#56]): a doc-vs-code ratchet pinning the schema-version claims in SKILL.md
  and CUTOVER.md against `schema_meta.SUPPORTED_SCHEMA_VERSION`, the
  recall-flag documentation (`--hybrid`/`--no-bump`), the PLAN.md
  storeDirectory correction, the Python 3.11 floor, and a
  no-absolute-home-path scan over all tracked files. Extended
  `tests/test_doctor.py` (warn below the 3.11 floor), `tests/test_mcp_server.py`
  (MCP `search` ≡ `recall` equivalence on identical args, #38 I13),
  `tests/test_surface_consistency.py` (MCP recall docstring documents the bump;
  `get --id` exit contract, including a model-free direct-BLOB test pinning the
  `<N-byte blob>` marker and a superseded-row forensic-read pin), and
  `tests/test_export_pack.py` (`--max-bytes` help-vs-behavior pin). The
  floor-claim ratchet matches the plus-suffixed, wordy "requires", and >=
  phrasings, with positive/negative controls.

[#46]: https://github.com/zaxbysauce/zmem/issues/46
[#56]: https://github.com/zaxbysauce/zmem/issues/56
[#47]: https://github.com/zaxbysauce/zmem/issues/47
[#48]: https://github.com/zaxbysauce/zmem/issues/48
[#49]: https://github.com/zaxbysauce/zmem/issues/49

## [0.8.4] — 2026-08-12

### Fixed
- **`consolidate --dry-run` no longer reports a completed merge it did not
  perform** (issue [#44]). The final summary line now uses a mode-dependent
  verb: a dry run prints `[zmem] would merge N memories + (dry run — no
  changes)` while a real run prints `[zmem] merged N memories`. Previously both
  modes printed `merged N memories`, differing only by a trailing parenthetical,
  which caused agents/operators skimming a dry run to report a merge that never
  happened (a self-reinforcing false-closeout failure). The `--prune` summary
  (`would prune`/`pruned`) was corrected in the same way. The code now matches
  the contract the docstring and `skills/memory/SKILL.md` already documented.
  The `skills/closeout/SKILL.md` trust-signal instruction was updated in tandem
  to reference `would merge N`.

### Tests
- Added a regression assertion to `DryRunPreviewTest` pinning that a dry run
  whose clusters pass reports `would merge` and never the bare past-tense
  `merged`.
- Added a companion test pinning that a dry run with `--prune` reports
  `would prune` (never past-tense `pruned`).

### Distribution
- Bumps the plugin version so marketplace version-comparison can signal an
  update to installed clients. Once `v0.8.4` is cut from the merge commit of
  this change, installed plugin caches — which pin a version directory — will
  have a resolvable target to advance to. This will be the project's first
  tagged release; prior `0.8.x` development versions shipped no git tags or
  GitHub Releases. See the note below.

[#44]: https://github.com/zaxbysauce/zmem/issues/44

## Prior development versions (0.8.0 – 0.8.3)

These versions were bumped in the plugin manifests but were **not** tagged as
GitHub Releases, so they cannot be reliably attributed to exact feature sets
after the fact. The git history on `main` is the source of truth for their
contents — run `git log` for the full detail. In rough summary, the 0.8.0–0.8.3
development span delivered: non-lossy `consolidate` with confidence-weighted
keeper selection; the consolidate cadence gate with `--force` and dry-run
modelling; degraded-embeddings surfacing; the per-session sentinel sweep reaper;
hook-recall surfaces blended into ranking/prune/promote; hostile-injection test
isolation; executable memory-script handling; the `/closeout` and
`/closeout-remote` slash commands; user:global union into project recall; and
the Hermes and Codex adapters. No per-issue attributions are listed here because,
absent release tags, mapping individual PRs to exact version bumps cannot be
done from the manifest history alone.
