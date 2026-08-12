# Changelog

All notable changes to the **zmem** plugin are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Released versions are marked with a git tag (`vX.Y.Z`) and a GitHub Release.
Installations discover new versions by comparing the `version` field in their
plugin manifest against the marketplace entry — see the *Upgrade* section of the
README.

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
