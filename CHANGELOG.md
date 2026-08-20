# Changelog

All notable changes to the **zmem** plugin are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Released versions are marked with a git tag (`vX.Y.Z`) and a GitHub Release.
Installations discover new versions by comparing the `version` field in their
plugin manifest against the marketplace entry — see the *Upgrade* section of the
README.

## [Unreleased]

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

### Tests

- Added `tests/test_corrections.py`: rejection extraction (both transcript
  formats, with/without reason, multi-line + marker-stripped + newline-free,
  dedup by `tool_use_id`), non-CC schema fail-open, pattern-library coverage,
  `corrections` read-only + secret handling, and `failures` output shape
  stability. Extended `tests/test_failures.py` for the new `(details,
  rejections)` return and `rejections` key.

[#46]: https://github.com/zaxbysauce/zmem/issues/46
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
