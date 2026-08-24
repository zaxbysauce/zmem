# Changelog

All notable changes to the **zmem** plugin are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Released versions are marked with a git tag (`vX.Y.Z`) and a GitHub Release.
Installations discover new versions by comparing the `version` field in their
plugin manifest against the marketplace entry — see the *Upgrade* section of the
README.

## [Unreleased]

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
