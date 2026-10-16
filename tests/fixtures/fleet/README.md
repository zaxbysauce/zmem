# Fleet parity fixtures (issue #280, Workstream T PR 1)

Parity fixture skeleton for T-04 (one authorization-aware selection policy)
and T-08 (per-machine broker): the SAME recorded payloads and seed rows are
replayed against the local store and the remote broker so envelope equality
is asserted against identical inputs, not two hand-copied variants.

## Contract

- `moments.json` carries exactly one canonical recorded payload per
  DISTINCT moment value declared by `docs/fleet-capability-matrix.json`
  (13 launcher verbs + 3 hermes callbacks + 2 explicit moments = 18; none
  excluded). Every payload records its `source` — the launcher test,
  fixture, or CLI surface it was harvested from — so "harvested" cannot
  degrade into "invented". A PR that adds a matrix moment adds its payload
  here in the same change; a payload without provenance fails review.
- `seed.jsonl` is the deterministic seeded-store recipe: the exact
  `store.py add` argv sequence (fixed namespace `project:fixture-280`,
  fixed `--source-ref` keys, fixed tag set) that builds the store both
  sides of a parity assertion read. No binary store is committed; replay
  the recipe instead:
  `py -3 skills/memory/scripts/store.py <argv...>` per line, in step
  order, against an isolated store (`ZMEM_STORE` pointing at a scratch
  file, never the operator store).
- Envelope-equality assertions (T-04/T-08): for each moment in
  `moments.json`, drive the local path and the broker path with the same
  payload plus the seeded store, then compare the rendered envelopes
  byte-for-byte at the same hub revision. Differences are contract
  violations, not tuning opportunities — fix the path or amend
  `docs/FLEET.md`, never the fixture alone.
- Session id `00000000-0000-4000-8000-000000000280` and namespace
  `project:fixture-280` are reserved for these fixtures; do not reuse them
  in other suites.
