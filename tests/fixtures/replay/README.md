# Replay fixture and evaluator contract

The committed replay snapshot, decision log, expected report, and
`eval/baseline-replay.json` are immutable test inputs. Tests consume those
bytes; they do not run the generator or replace the expected report. The
report is a bounded measurement artifact and is not a live-host efficacy
claim.

## Evaluator boundary

`scripts/eval_replay.py` accepts one standalone SQLite snapshot and one active
decision log. It opens the staged snapshot read-only, refuses a non-empty WAL,
operator-store paths and resolved/hard-link aliases, and verifies every input
again before writing an output atomically. Rotated logs, ops rings, failure
databases, and ambient data directories are never discovered. The optional
`--transcript PATH` argument is repeatable and accepts only explicit regular
JSONL files; transcript bytes are included in the domain-separated,
length-framed input digest.

Transcript input is bounded to 16 files, 4 MiB per file, 32 MiB total, and
100,000 lines per file. Prompt and failure observations are window-filtered
against the latest valid decision timestamp and passed through the existing
miss/reference helpers. Only later, usable observations with a normalized,
known same-session ID make a delivered decision reference-eligible. Missing or
empty observation substrates are reported on stderr as unavailable; numeric
zeroes in empty-denominator fields are not measured success or failure.

Before importing store libraries, the evaluator derives its scoring clock from
the latest valid decision-log timestamp and installs that value as
`ZMEM_TEST_NOW`. It clears ambient `ZMEM_*`, Claude-plugin, and ZCode-plugin
overrides, then installs explicit staged `<store>`, `<data>`, `<home>`, and `<model>` paths, fake
embeddings, downloads-disabled behavior, and a pinned MMR default. Thus an
operator's current date, recall knobs, model cache, or plugin routing cannot
change a replay of the same bytes.

The launcher's legacy `outer_timeout=1` watchdog diagnostic is recognized only
in its exact writer shape and is reported as an explicit stderr exclusion, not
as a decision row. It does not contribute to report counts, the latest valid
decision timestamp, or `generated_at`; malformed or unknown `zmem-hook` rows
still fail closed. The original diagnostic bytes remain part of `input_digest`.

The closed report covers exactly these eight buckets:

```
claude          × session_start, user_prompt, pretool, precompact
hermes-provider × session_start, user_prompt, pretool, precompact
```

Rows are stably sorted. `input_metadata` contains only `version` and
`parsed_rows`; the top-level `usable_observation` marker records whether a
validated transcript/failure observation overlaps a selected report's
same-session miss-attribution window; reference-precision deliberately accepts
later observations outside that window. It is an availability marker, not a
claim that every reference-precision row has matching evidence. All valid
parsed rows participate in mixed nonempty-version validation before time-window
or lane projection. Other valid lanes/moments are excluded from the eight rows
with deterministic stderr diagnostics.
Timing uses nearest-rank p50/p95. Baseline ratchets validate finite aggregate
metrics and emit a valid breached report before exit 1; malformed inputs,
measurement errors, and invalid ratchets exit 2 without replacing an existing
output.

## Maintainer regeneration and review

Regenerate candidates only in fresh scratch directories after the selected
release manifest is finalized:

```powershell
python tests/fixtures/replay/generate.py `
  --manifest release-manifest.json `
  --store <scratch>\store.sqlite `
  --log <scratch>\decisions.log `
  --expected <scratch>\expected.json `
  --baseline <scratch>\baseline-replay.json
```

The generator invokes the actual `tests/fixtures/eval_store.py` builder with
`ZMEM_TEST_NOW=2026-06-01T00:00:00Z` before builder imports/subprocesses,
`ZMEM_EMBED_PROFILE=fake`, model downloads disabled, and explicit scratch
`<store>`, `<data>`, `<home>`, and `<model>` paths. It then applies replay-fixture-only
canonicalization: stable UUID-shaped entity IDs and references, runtime-only
timestamp pinning, preserved historical validity windows, full reference
integrity checks, vector metadata rebuild, journal cleanup, checkpoint, and
`VACUUM`.

For each selected runtime, make two fresh builds and compare finalized store,
log, and report SHA-256 values plus the complete normalized logical corpus.
Record Python, SQLite, and optional-extension versions with those digests.
Review the candidates and the independent fixed-count/timing oracle before a
deliberate maintainer copy replaces committed artifacts. Never overwrite an
existing committed oracle from the generator or from tests.
