"""Workstream T PR 1 (issue #280): the fleet capability-matrix contract.

These are the frozen acceptance checks C2/C3/C4 of the issue #280 trace.
Each is RED at base (docs/fleet-capability-matrix.json does not exist, so
loading it raises FileNotFoundError) and GREEN once the matrix ships. The
matrix is the single source of truth later Workstream T PRs cite (T-08
broker, T-09 op queue, T-11 offline snapshot add the rows they implement);
these tests fail whenever a hooks/hooks.*.json manifest registers an event
the matrix does not account for, so coverage gaps cannot reopen silently.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
MATRIX_PATH = REPO_ROOT / "docs" / "fleet-capability-matrix.json"

ALLOWED_LOCALITY = {"client_local", "hub_authoritative"}
SUPPORTED_HARNESSES = ("claude", "codex", "zcode", "hermes", "opencode")
# Floors pinned by issue #280 AC4 (vsan-1 install facts at filing, 2026-10-05).
# The hermes floor stays data-only (0.21.1 = the in-repo cdf4c76 source pin);
# zcode's 3.15.3 is the probed dev-box host — provenance in docs/FLEET.md.
PINNED_MIN_VERSIONS = {
    "claude": "2.1.283",
    "codex": "0.160.0",
    "opencode": "1.18.30",
}
# Issue #280 AC3 names these inputs and their classes; a matrix that renames
# or reclassifies them is renegotiating the contract, not filling it in.
PINNED_LOCALITY = {
    "git remote": "client_local",
    "Tier 0 files": "client_local",
    "transcript": "client_local",
    "agent transcript": "client_local",
    "cwd": "client_local",
    "rows": "hub_authoritative",
    "embeddings": "hub_authoritative",
    "ledger": "hub_authoritative",
    "policy": "hub_authoritative",
}


def _load_matrix():
    # Deliberately unguarded: with the matrix absent this raises
    # FileNotFoundError, which is the frozen RED state of checks C2-C4.
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _rows(matrix):
    rows = matrix["rows"] if isinstance(matrix, dict) else matrix
    assert isinstance(rows, list) and rows, "matrix carries no rows"
    return rows


# Hermes delivers its moments through memory-provider callbacks, not a
# hooks manifest; these three are the only non-manifest events that may be
# claimed registered. OpenCode ships no adapter at all (issue #66), so
# nothing on it may claim registered until that lands.
HERMES_CALLBACK_EVENTS = {"pre_llm_call", "post_tool_call", "pre_verify"}


def _manifest_events():
    """harness name (from the hooks.<harness>.json filename) -> event set."""
    events = {}
    for manifest in sorted(HOOKS_DIR.glob("hooks.*.json")):
        harness = manifest.name[len("hooks."):-len(".json")]
        data = json.loads(manifest.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        assert isinstance(hooks, dict) and hooks, (
            f"{manifest.name} registers no events"
        )
        events[harness] = set(hooks)
    return events


def test_matrix_covers_every_registered_hook_event():
    """AC2: every event key registered in every hooks/hooks.*.json has a
    matching matrix row marked registered. RED at base (matrix absent),
    GREEN after the fix; a future harness manifest that adds an event
    fails here until the matrix gains its row."""
    events = _manifest_events()
    rows = _rows(_load_matrix())
    registered = {
        (r.get("harness"), r.get("hook_event"))
        for r in rows if r.get("status") == "registered"
    }
    missing = sorted(
        (harness, event)
        for harness, evs in events.items()
        for event in evs
        if (harness, event) not in registered
    )
    assert not missing, (
        f"hook manifests register events with no 'registered' matrix row: "
        f"{missing}"
    )


def test_matrix_inputs_are_classified():
    """AC3: every matrix input is classified client_local or
    hub_authoritative, the issue-named inputs carry their pinned classes
    (so T-08 cannot silently move a client-local read to the hub), and
    every row carries a non-empty degraded cell (the AC1 consistency-model
    statement). RED at base, GREEN after the fix."""
    rows = _rows(_load_matrix())
    for i, row in enumerate(rows):
        where = f"row {i} ({row.get('harness')}/{row.get('moment')})"
        degraded = row.get("degraded")
        assert isinstance(degraded, str) and degraded.strip(), (
            f"{where} has an empty degraded cell"
        )
        inputs = row.get("inputs")
        assert isinstance(inputs, list) and inputs, (
            f"{where} lists no inputs"
        )
        for inp in inputs:
            name = inp.get("name")
            locality = inp.get("locality")
            assert locality in ALLOWED_LOCALITY, (
                f"{where} input {name!r} locality {locality!r} "
                f"is not one of {sorted(ALLOWED_LOCALITY)}"
            )
            if name in PINNED_LOCALITY:
                assert locality == PINNED_LOCALITY[name], (
                    f"{where} input {name!r} must be {PINNED_LOCALITY[name]!r} "
                    f"(issue #280 AC3), not {locality!r}"
                )


def test_matrix_pins_supported_versions():
    """AC4: every harness row pins min_version, the three filing-pinned
    floors hold, every supported harness accounts for every manifest event
    (registered or capability_gap, never silently omitted), and no row may
    claim "registered" for an event its harness does not register. RED at
    base, GREEN after the fix."""
    events = _manifest_events()
    rows = _rows(_load_matrix())
    harnesses = {r.get("harness") for r in rows}
    for harness in SUPPORTED_HARNESSES:
        assert harness in harnesses, f"matrix lacks a {harness} harness row"

    per_harness_floor = {}
    union_events = set().union(*events.values()) if events else set()
    pair_rows = {}
    for i, row in enumerate(rows):
        where = f"row {i} ({row.get('harness')}/{row.get('moment')})"
        min_version = row.get("min_version")
        assert isinstance(min_version, str) and min_version.strip(), (
            f"{where} lacks min_version"
        )
        harness = row.get("harness")
        floor = per_harness_floor.setdefault(harness, min_version)
        assert floor == min_version, (
            f"{where} min_version {min_version!r} disagrees with the "
            f"{floor!r} pinned on other {harness} rows"
        )
        hook_event = row.get("hook_event")
        if hook_event is None or harness not in SUPPORTED_HARNESSES:
            continue
        if row.get("status") == "registered":
            if harness in events:
                assert hook_event in events[harness], (
                    f"{where} claims 'registered' but {harness}'s manifest "
                    f"does not register {hook_event!r}"
                )
            elif harness == "hermes":
                assert hook_event in HERMES_CALLBACK_EVENTS, (
                    f"{where} claims 'registered' outside the Hermes "
                    f"callback surface {sorted(HERMES_CALLBACK_EVENTS)}"
                )
            else:
                raise AssertionError(
                    f"{where} claims 'registered' but {harness} ships no "
                    f"adapter and no hooks manifest"
                )
        if hook_event in union_events:
            pair_rows.setdefault((harness, hook_event), []).append(row)

    for harness, pinned in PINNED_MIN_VERSIONS.items():
        assert per_harness_floor.get(harness) == pinned, (
            f"{harness} floor is {per_harness_floor.get(harness)!r}, "
            f"expected the issue-#280 pinned floor {pinned!r}"
        )

    # FLEET.md's opencode sentence is normative: no adapter ships (#66), so
    # EVERY opencode row stays capability_gap until that lands — including
    # the explicit-tool rows whose hook_event is None.
    for i, row in enumerate(rows):
        if row.get("harness") == "opencode":
            assert row.get("status") == "capability_gap", (
                f"row {i} (opencode/{row.get('moment')}) claims "
                f"{row.get('status')!r}; OpenCode ships no adapter (issue "
                f"#66) so every row must stay capability_gap"
            )

    for harness in SUPPORTED_HARNESSES:
        for event in sorted(union_events):
            rows_for_pair = pair_rows.get((harness, event))
            assert rows_for_pair, (
                f"supported harness {harness} lacks any row for {event} "
                f"(registered or capability_gap)"
            )
            registers = event in events.get(harness, set())
            wanted = "registered" if registers else "capability_gap"
            verb = "registers" if registers else "does not register"
            for row in rows_for_pair:
                status = row.get("status")
                assert status == wanted, (
                    f"{harness} {verb} {event}; the matrix row says "
                    f"{status!r} (wanted {wanted!r})"
                )
                assert str(row.get("degraded", "")).strip(), (
                    f"{harness}/{event} {wanted} row lost its degraded cell"
                )


if __name__ == "__main__":
    # CI runs `python tests/test_<file>.py` directly (no pytest), so the
    # guardrail must bite there too: run the three checks and exit nonzero
    # on the first failure set. Pytest collection is unchanged.
    failures = []
    for _name in ("test_matrix_covers_every_registered_hook_event",
                  "test_matrix_inputs_are_classified",
                  "test_matrix_pins_supported_versions"):
        try:
            globals()[_name]()
        except AssertionError as exc:
            failures.append(f"{_name}: {exc}")
    if failures:
        print("\n".join(failures))
        raise SystemExit(1)
    print("3 fleet-matrix checks passed")
