# Fleet parity fixtures (issue #280, Workstream T PR 1 of 12)

Parity fixture skeleton for T-04 (one authorization-aware selection policy)
and T-08 (per-machine broker): the SAME recorded payloads and seed rows are
replayed against the local store and the remote broker so envelope equality
is asserted against identical inputs, not two hand-copied variants.

## Contract

- `moments.json` carries exactly one canonical recorded payload per
  DISTINCT moment value declared by `docs/fleet-capability-matrix.json`
  (13 launcher verbs + 3 hermes callbacks + 2 explicit moments = 18; none
  excluded). `tests/test_t01_fleet_matrix.py::test_fleet_fixtures_match_the_matrix`
  enforces this: the payload set must equal the matrix's moment set, each
  payload must be a non-empty object, and each must carry provenance.
- **Provenance policy:** every payload is *authored against* a named, real
  contract artifact — the cited `file:line` is the shape authority the
  payload was written to match, not a recording it was copied from. The
  one launcher fixture that carries real recorded fields
  (`tests/fixtures/launcher/claude/input.json`) is cited where its fields
  apply. No payload claims to be a recording of a live session. (The
  original "harvested from" wording was inaccurate: most cited launcher
  fixture files are 3-byte `{}` expected-output stubs.)
- `seed.jsonl` is the deterministic seeded-store recipe: one JSON object
  per line, each with a `step` and a `store.py add` `argv`. Replay with an
  isolated store — never the operator store:
  `ZMEM_STORE=<scratch>/store.sqlite <python> skills/memory/scripts/store.py <argv...>`
  per line, in step order, where `<python>` is any Python 3.11+
  interpreter (`py -3` on Windows, `python3` on macOS/Linux).
- **Envelope equality (T-04/T-08):** for each moment in `moments.json`,
  drive the local path and the broker path with the same payload plus the
  seeded store, then compare the rendered envelopes. Row ids are minted
  `uuid4` at write time (`storelib/write.py:1427`) and rendered into the
  envelope header (`storelib/inject.py:238-247`), and `store.py add` has no
  `--id` flag — so two independently seeded stores are **not** byte-identical.
  Assert equality *modulo the volatile row id* (or normalize the id
  segment out); do not weaken the comparison to a fuzzy diff, and do not
  treat an id-only difference as a contract violation.
- Session id `00000000-0000-4000-8000-000000000280` and namespace
  `project:fixture-280` are reserved for these fixtures; do not reuse them
  in other suites. Multi-principal / authorization-parity coverage (a
  second namespace or scope on the recall and explicit-recall payloads) is
  T-04's suite, not this file's — the single shared identity here is
  deliberate so envelope equality isolates the delivery path.
- Payload paths are Windows-shaped (`C:\fleet-fixture\...`) because that is
  the fleet's primary install surface; a POSIX replay should substitute
  `/fleet-fixture/...` equivalents. CI runs an ubuntu leg, so any future
  replay test must parameterize the path root rather than assuming either.

## Replay precondition for `pre-verify`

The Hermes `pre_verify` callback gates on in-process callback state
(`tool_count > 0` and not yet prompted — `hermes-plugin/__init__.py:3269-3275`),
so a standalone replay of the `pre-verify` payload always takes the `{}`
early-return branch. Drive one `post-tool-call` payload for the same
`session_id` first; the recorded `pre-verify` payload then exercises the
gated path.
