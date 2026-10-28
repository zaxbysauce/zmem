"""Workstream T PR 1 (issue #280): the fleet capability-matrix contract.

These are the frozen acceptance checks C2/C3/C4 of the issue #280 trace.
Each is RED at base (docs/fleet-capability-matrix.json does not exist, so
loading it raises FileNotFoundError) and GREEN once the matrix ships. The
matrix is the single source of truth later Workstream T PRs cite (T-08
broker, T-09 op queue, T-11 offline snapshot add the rows they implement);
these tests fail whenever a hooks/hooks.*.json manifest registers an event
the matrix does not account for, so coverage gaps cannot reopen silently.

The assertions here are IDENTITY checks, not presence checks: the matrix's
row set must equal the grid implied by the live manifests, the hermes
plugin's register_hook surface, and the explicit-tool contract; the input
names must come from a closed vocabulary; and the launcher column must
match what the manifests actually wire.  Renaming or deleting a pinned
input, dropping a row, or inventing a row all fail.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
MATRIX_PATH = REPO_ROOT / "docs" / "fleet-capability-matrix.json"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "fleet"
PLUGIN_SRC = REPO_ROOT / "hermes-plugin" / "__init__.py"

ALLOWED_LOCALITY = {"client_local", "hub_authoritative"}
ALLOWED_STATUS = {"registered", "capability_gap"}
ALLOWED_HUB_OPS = {"select", "ingest", "flush"}
SUPPORTED_HARNESSES = ("claude", "codex", "zcode", "hermes", "opencode",
                        "deepseek")
# Floors pinned by issue #280 AC4 (fleet-hub install facts at filing, 2026-10-05).
# hermes 0.21.1 is the in-repo cdf4c76 source pin; zcode 3.15.3 is the probed
# dev-box host (ZCode.exe ProductVersion 3.15.3.8237) — provenance in
# docs/FLEET.md.  Raising any floor is a contract change: update this dict,
# the matrix rows, and the FLEET.md provenance table together.
PINNED_MIN_VERSIONS = {
    "claude": "2.1.283",
    "codex": "0.160.0",
    "opencode": "1.18.30",
    "hermes": "0.21.1",
    "zcode": "3.15.3",
    "deepseek": "0.2.1-rc.2",
}
# Issue #280 AC3 names these inputs and their classes.  A matrix that
# renames or drops one is renegotiating the contract, not filling it in.
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
# Closed input vocabulary: the pinned nine plus every other input the
# contract defines.  A name outside this set is either a typo or an
# undeclared input, and fails.
EXTRA_INPUTS = {
    "namespace": "client_local",
    "query": "client_local",
    "tool_input": "client_local",
    "tool_output": "client_local",
    "memory payload": "client_local",
}
INPUT_VOCABULARY = dict(PINNED_LOCALITY, **EXTRA_INPUTS)
INPUT_VOCABULARY["principal authorization"] = "hub_authoritative"

# The explicit-tool lane is a contract fact rather than something derivable
# from a manifest; every other moment in the grid is derived below.
EXPLICIT_MOMENTS = ("explicit-recall", "explicit-write")
DEGRADED_PREFIX = "Degrade, never block:"


def _degraded_templates():
    """The exact degraded strings the generator emits.  Loading them from the
    generator (rather than restating them) keeps the matrix's single writer
    authoritative and makes the degraded check exact instead of prefix-only.
    A missing generator yields an empty set so the module still imports on a
    tree that predates it (the base leg then fails on the absent matrix, not
    on this import) and the prefix check remains as the floor."""
    gen_path = REPO_ROOT / "docs" / "generate-fleet-matrix.py"
    if not gen_path.is_file():
        return set()
    spec = importlib.util.spec_from_file_location("_fleet_matrix_generator",
                                                  gen_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    templates = {
        module.INJECT, module.REFLECT, module.CAPTURE, module.SESSION_END,
        module.EXPLICIT_RECALL, module.OPENCUE_GAP,
    }
    assert all(t.startswith(DEGRADED_PREFIX) for t in templates), (
        "every generator degraded template must open with the "
        f"{DEGRADED_PREFIX!r} commitment"
    )
    return templates
MATRIX_VERSION = 1
DEGRADED_TEMPLATES = _degraded_templates()
# hub_ops is the contract T-08/T-09 implement against: each moment class
# declares exactly one operation.  A row whose hub_ops disagrees with its
# moment class is a contract violation, not a formatting difference.
MOMENT_HUB_OPS = {
    "session-start": ["select"],
    "recall": ["select"],
    "pretool-recall": ["select"],
    "posttoolbatch-recall": ["select"],
    "subagent-recall": ["select"],
    "precompact": ["select"],
    "postcompact": ["select"],
    "pre-llm-call": ["select"],
    "explicit-recall": ["select"],
    "capture-correction": ["ingest"],
    "convention-capture": ["ingest"],
    "capture-failure": ["ingest"],
    "post-tool-call": ["ingest"],
    "reflect": ["ingest"],
    "subagent-reflect": ["ingest"],
    "pre-verify": ["ingest"],
    "explicit-write": ["ingest"],
    "session-end": ["flush"],
}


def _load_matrix():
    # Deliberately unguarded: with the matrix absent this raises
    # FileNotFoundError, which is the frozen RED state of checks C2-C4.
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _rows(matrix):
    assert isinstance(matrix, dict), (
        "matrix must be an object with matrix_version and rows"
    )
    assert matrix.get("matrix_version") == MATRIX_VERSION, (
        f"matrix_version must be {MATRIX_VERSION}, got "
        f"{matrix.get('matrix_version')!r}"
    )
    rows = matrix["rows"]
    assert isinstance(rows, list) and rows, "matrix carries no rows"
    return rows


def _manifest_wiring():
    """harness -> {launcher verb: hook_event}, derived from the manifests."""
    wiring = {}
    for manifest in sorted(HOOKS_DIR.glob("hooks.*.json")):
        harness = manifest.name[len("hooks."):-len(".json")]
        data = json.loads(manifest.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        assert isinstance(hooks, dict) and hooks, (
            f"{manifest.name} has no non-empty hooks object — the "
            f"hooks.*.json glob treats every match as a harness manifest, "
            f"so a config or backup file placed there must be renamed out "
            f"of the glob"
        )
        verbs = {}
        for event, entries in hooks.items():
            for entry in entries:
                for hook in entry.get("hooks", []):
                    verb = None
                    args = hook.get("args")
                    if isinstance(args, list) and len(args) >= 2 \
                            and str(args[-2]).endswith("zmem-launch.js"):
                        verb = str(args[-1])
                    if verb is None:
                        parts = str(hook.get("command", "")).replace(
                            '"', " ").split()
                        if len(parts) >= 2 and parts[-2].endswith(
                                "zmem-launch.js"):
                            verb = parts[-1]
                    if verb:
                        assert verb not in verbs or verbs[verb] == event, (
                            f"{manifest.name} wires {verb} under two events"
                        )
                        verbs[verb] = event
        wiring[harness] = verbs
    return wiring


def _hermes_callbacks():
    """The callback names hermes-plugin actually registers (fail closed)."""
    assert PLUGIN_SRC.is_file(), "hermes-plugin/__init__.py is missing"
    names = []
    for line in PLUGIN_SRC.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("register_hook("):
            continue
        inner = stripped[len("register_hook("):].strip()
        for quote in ('"', "'"):
            if inner.startswith(quote):
                names.append(inner[1:inner.index(quote, 1)])
                break
    assert names, "no register_hook(...) calls found in hermes-plugin"
    return {name.replace("_", "-") for name in names}


def _expected_moments():
    """Every moment the grid must cover, derived from the live surfaces."""
    wiring = _manifest_wiring()
    # Native dsh/Cordis wiring parity (issue #374): the deepseek adapter
    # registers these events in deepseek-plugin/index.mjs — no shell-hook
    # manifest exists. Keep in sync with _NATIVE_WIRING in the generator.
    wiring.setdefault("deepseek", {
        "session-start": "agent/created",
        "recall": "agent/pre-step",
        "pretool-recall": "tools/pre-execute",
        "capture-failure": "tools/result",
        "reflect": "agent/turn-stopping",
        "precompact": "compaction/start",
    })
    moments = set()
    for verbs in wiring.values():
        moments.update(verbs)
    moments.update(m.replace("_", "-") for m in _hermes_callbacks())
    moments.update(EXPLICIT_MOMENTS)
    return moments


def test_matrix_covers_every_registered_hook_event():
    """AC2: every event key registered in every hooks/hooks.*.json has a
    matching matrix row marked registered. RED at base (matrix absent),
    GREEN after the fix; a future harness manifest that adds an event
    fails here until the matrix gains its row."""
    events = {h: set(v.values()) for h, v in _manifest_wiring().items()}
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
    AND are present, every input name comes from the closed vocabulary,
    every row carries a non-empty AC1 degraded cell, and every degraded
    cell opens with the degrade-never-block commitment. RED at base,
    GREEN after the fix."""
    rows = _rows(_load_matrix())
    seen_names = set()
    for i, row in enumerate(rows):
        where = f"row {i} ({row.get('harness')}/{row.get('moment')})"
        degraded = row.get("degraded")
        assert isinstance(degraded, str) and degraded.strip(), (
            f"{where} has an empty degraded cell"
        )
        if DEGRADED_TEMPLATES:
            assert degraded in DEGRADED_TEMPLATES, (
                f"{where} degraded cell is not one of the generator's "
                f"{len(DEGRADED_TEMPLATES)} declared templates — a blocking "
                f"or free-text degraded behaviour is a contract violation"
            )
        else:
            assert degraded.startswith(DEGRADED_PREFIX), (
                f"{where} degraded cell must open with {DEGRADED_PREFIX!r}"
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
            assert name in INPUT_VOCABULARY, (
                f"{where} input {name!r} is outside the closed vocabulary "
                f"{sorted(INPUT_VOCABULARY)}"
            )
            if name in INPUT_VOCABULARY:
                assert locality == INPUT_VOCABULARY[name], (
                    f"{where} input {name!r} must be "
                    f"{INPUT_VOCABULARY[name]!r} (issue #280 AC3), not "
                    f"{locality!r}"
                )
            seen_names.add(name)
    missing_pins = sorted(set(PINNED_LOCALITY) - seen_names)
    assert not missing_pins, (
        f"the issue-named inputs are absent from every row: {missing_pins}"
    )


def test_matrix_pins_supported_versions():
    """AC4: every harness row pins min_version, all five floors hold, the
    row set is exactly the grid implied by the live manifests plus the
    hermes callback surface plus the explicit-tool lane (no missing,
    duplicate, or phantom rows), status and hub_ops come from closed
    vocabularies, the launcher column matches what the manifests wire,
    and every opencode row stays capability_gap until #66 lands. RED at
    base, GREEN after the fix."""
    wiring = _manifest_wiring()
    # Native dsh/Cordis wiring parity (issue #374): the deepseek adapter
    # registers these events in deepseek-plugin/index.mjs — no shell-hook
    # manifest exists. Keep in sync with _NATIVE_WIRING in the generator.
    wiring.setdefault("deepseek", {
        "session-start": "agent/created",
        "recall": "agent/pre-step",
        "pretool-recall": "tools/pre-execute",
        "capture-failure": "tools/result",
        "reflect": "agent/turn-stopping",
        "precompact": "compaction/start",
    })
    rows = _rows(_load_matrix())

    # -- floors ---------------------------------------------------------
    per_harness_floor = {}
    for i, row in enumerate(rows):
        where = f"row {i} ({row.get('harness')}/{row.get('moment')})"
        min_version = row.get("min_version")
        assert isinstance(min_version, str) and min_version.strip(), (
            f"{where} lacks min_version"
        )
        harness = row.get("harness")
        moment = row.get("moment")
        floor = per_harness_floor.setdefault(harness, min_version)
        assert floor == min_version, (
            f"{where} min_version {min_version!r} disagrees with the "
            f"{floor!r} pinned on other {harness} rows"
        )
        assert row.get("status") in ALLOWED_STATUS, (
            f"{where} status {row.get('status')!r} is not one of "
            f"{sorted(ALLOWED_STATUS)}"
        )
        hub_ops = row.get("hub_ops")
        assert isinstance(hub_ops, list) and hub_ops, (
            f"{where} hub_ops must be a non-empty list"
        )
        for op in hub_ops:
            assert op in ALLOWED_HUB_OPS, (
                f"{where} hub_ops entry {op!r} is not one of "
                f"{sorted(ALLOWED_HUB_OPS)}"
            )
        expected_ops = MOMENT_HUB_OPS.get(moment)
        assert expected_ops is not None, (
            f"{where} moment {moment!r} has no declared hub_ops class"
        )
        assert hub_ops == expected_ops, (
            f"{where} hub_ops {hub_ops!r} does not match the "
            f"{moment!r} class {expected_ops!r}"
        )
    for harness, pinned in PINNED_MIN_VERSIONS.items():
        assert per_harness_floor.get(harness) == pinned, (
            f"{harness} floor is {per_harness_floor.get(harness)!r}, "
            f"expected the issue-#280 pinned floor {pinned!r}"
        )
    for harness in SUPPORTED_HARNESSES:
        assert harness in per_harness_floor, (
            f"matrix lacks a {harness} harness row"
        )

    # -- row identity: exactly the expected grid ------------------------
    expected_moments = _expected_moments()
    seen_pairs = {}
    seen_triples = set()
    for i, row in enumerate(rows):
        where = f"row {i} ({row.get('harness')}/{row.get('moment')})"
        harness = row.get("harness")
        moment = row.get("moment")
        assert harness in SUPPORTED_HARNESSES, (
            f"{where} names a harness outside {list(SUPPORTED_HARNESSES)}"
        )
        assert moment in expected_moments, (
            f"{where} moment {moment!r} is not one of the expected moments "
            f"{sorted(expected_moments)}"
        )
        triple = (harness, moment, row.get("hook_event"))
        assert triple not in seen_triples, (
            f"{where} duplicates the matrix row for {triple}"
        )
        seen_triples.add(triple)
        seen_pairs.setdefault((harness, moment), []).append(row)
    missing_pairs = sorted(
        (h, m) for h in SUPPORTED_HARNESSES for m in expected_moments
        if (h, m) not in seen_pairs
    )
    assert not missing_pairs, (
        f"supported harnesses lack rows for these (harness, moment) "
        f"cells: {missing_pairs}"
    )

    # -- registered claims must match the real surfaces -----------------
    callbacks = _hermes_callbacks()
    for (harness, moment), pair_rows in sorted(seen_pairs.items()):
        wired_event = wiring.get(harness, {}).get(moment)
        for row in pair_rows:
            where = (f"row ({harness}/{moment}/"
                     f"{row.get('hook_event')})")
            if moment in EXPLICIT_MOMENTS:
                registered = harness != "opencode"
            elif moment in callbacks:
                registered = harness == "hermes"
            else:
                registered = (wired_event is not None
                              and row.get("hook_event") == wired_event)
            wanted = "registered" if registered else "capability_gap"
            assert row.get("status") == wanted, (
                f"{where} says {row.get('status')!r}, wanted {wanted!r}"
            )
            launcher = row.get("launcher")
            if registered:
                if moment in EXPLICIT_MOMENTS:
                    assert launcher in ("store.py recall", "store.py add"), (
                        f"{where} launcher {launcher!r} is not an explicit "
                        f"tool invocation"
                    )
                elif moment in callbacks:
                    assert launcher == row.get("hook_event"), (
                        f"{where} launcher {launcher!r} must name the "
                        f"registered callback"
                    )
                else:
                    assert launcher == moment, (
                        f"{where} launcher {launcher!r} must be the moment "
                        f"verb"
                    )
            else:
                assert launcher is None, (
                    f"{where} is a capability_gap and must not name a "
                    f"launcher (got {launcher!r})"
                )

    # -- the codex double-fire policy note is machine-checked ----------
    for row in rows:
        if row.get("harness") == "codex" and row.get("moment") in (
                "session-start", "recall"):
            note = str(row.get("notes") or "")
            assert "15266" in note and "never by version gating" in note, (
                f"row ({row.get('harness')}/{row.get('moment')}) must carry "
                f"the openai/codex#15266 double-fire policy note "
                f"(unconditional ledger/reservation dedupe, never version "
                f"gating); got {note!r}"
            )

    # -- opencode stays a declared gap until #66 lands ------------------
    for i, row in enumerate(rows):
        if row.get("harness") == "opencode":
            assert row.get("status") == "capability_gap", (
                f"row {i} (opencode/{row.get('moment')}) claims "
                f"{row.get('status')!r}; OpenCode ships no adapter (issue "
                f"#66) so every row must stay capability_gap"
            )


def test_fleet_fixtures_match_the_matrix():
    """The parity fixtures are contract data: one recorded payload per
    matrix moment, each with provenance, and a valid JSONL seed recipe."""
    expected = {r["moment"] for r in _rows(_load_matrix())}
    moments_path = FIXTURES_DIR / "moments.json"
    assert moments_path.is_file(), f"{moments_path} is missing"
    data = json.loads(moments_path.read_text(encoding="utf-8"))
    entries = data["moments"] if isinstance(data, dict) else data
    assert isinstance(entries, list) and entries, "moments.json has no entries"
    seen = set()
    for i, entry in enumerate(entries):
        moment = entry.get("moment")
        assert moment in expected, (
            f"moments.json entry {i} names {moment!r}, which the matrix "
            f"does not declare"
        )
        assert moment not in seen, (
            f"moments.json carries two payloads for {moment!r}"
        )
        seen.add(moment)
        payload = entry.get("payload")
        assert isinstance(payload, dict) and payload, (
            f"moments.json entry {i} ({moment}) has an empty payload"
        )
        source = entry.get("source")
        assert isinstance(source, str) and source.strip(), (
            f"moments.json entry {i} ({moment}) has no provenance"
        )
    missing = sorted(expected - seen)
    assert not missing, (
        f"moments.json has no payload for these matrix moments: {missing}"
    )
    seed_path = FIXTURES_DIR / "seed.jsonl"
    assert seed_path.is_file(), f"{seed_path} is missing"
    for i, line in enumerate(seed_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            step = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"seed.jsonl line {i} is not valid JSON: {exc}"
            ) from exc
        assert isinstance(step.get("argv"), list) and step["argv"], (
            f"seed.jsonl line {i} has no argv"
        )


def test_ingest_rows_carry_authorization():
    """#363 AC1: every ingest row declares the authorization input it needs.

    A row whose hub_ops includes ``ingest`` is a WRITE path — the hub, not
    the client, owns the decision — so the matrix must name the principal
    authorization that input consults: an input whose name contains
    "authorization" classified ``hub_authoritative`` (the frozen shape is
    ``{"name": "principal authorization", "locality":
    "hub_authoritative"}``).  RED at base (no ingest row carries it), GREEN
    once the #363 matrix update lands; a later harness that grows an ingest
    row without the input fails here, so the write-path authorization
    contract cannot silently reopen."""
    rows = _rows(_load_matrix())
    offenders = []
    ingest_rows = 0
    for i, row in enumerate(rows):
        if "ingest" not in (row.get("hub_ops") or []):
            continue
        ingest_rows += 1
        where = f"row {i} ({row.get('harness')}/{row.get('moment')})"
        auth_inputs = [
            inp for inp in row.get("inputs") or []
            if "authorization" in str(inp.get("name", "")).lower()
        ]
        if not auth_inputs:
            offenders.append(f"{where}: no authorization input")
            continue
        for inp in auth_inputs:
            if inp.get("locality") != "hub_authoritative":
                offenders.append(
                    f"{where}: authorization input {inp.get('name')!r} is "
                    f"{inp.get('locality')!r}, not hub_authoritative"
                )
    assert ingest_rows > 0, (
        "the matrix declares no ingest rows at all — the grid must contain "
        "the capture/reflect/explicit-write moments (issue #280)"
    )
    assert not offenders, (
        f"every hub_ops ingest row ({ingest_rows} of {len(rows)}) must carry "
        f"an input whose name contains 'authorization' with locality "
        f"'hub_authoritative' (issue #363 AC1); offenders: {offenders}"
    )


_CHECKS = tuple(
    fn for name, fn in sorted(globals().items())
    if name.startswith("test_") and callable(fn)
)

def _assert_no_check_after_runner():
    """The direct runner is the module's last statement, so a check appended
    below it would be bound after main() has already run and would never
    execute — the exact silent-skip this file exists to prevent.  Parse our
    own source and fail if any check (a test_* function OR a class carrying
    test_* methods, e.g. a unittest.TestCase) sits below the runner."""
    import ast
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    runner_line = None
    late = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            if runner_line is not None and node.lineno > runner_line:
                late.append(node.name)
        elif isinstance(node, ast.ClassDef):
            methods = [n.name for n in node.body
                       if isinstance(n, ast.FunctionDef)
                       and n.name.startswith("test_")]
            if methods and runner_line is not None \
                    and node.lineno > runner_line:
                late.append(f"{node.name} ({', '.join(sorted(methods))})")
        elif isinstance(node, ast.If):
            segment = ast.get_source_segment(source, node.test) or ""
            if "__name__" in segment and "__main__" in segment:
                runner_line = node.lineno
    assert runner_line is not None, "the __main__ runner guard is missing"
    assert not late, (
        f"these checks are defined below the runner and would never run "
        f"under CI's direct python loop: {sorted(late)} — move them above "
        f"the runner (the file's last statement)"
    )


def main():
    """CI runs `python tests/test_<file>.py` directly (no pytest), so the
    guardrail must bite there too.  Every test_* function in this module
    runs — the list is derived from globals() after the whole module body
    has executed, so a check defined anywhere in the file is included and
    cannot be silently skipped."""
    failures = []
    for name, check in sorted(globals().items()):
        if not name.startswith("test_") or not callable(check):
            continue
        try:
            check()
        except Exception as exc:  # noqa: BLE001 - report every failure
            failures.append(f"{name}: {exc}")
    try:
        _assert_no_check_after_runner()
    except Exception as exc:  # noqa: BLE001 - report every failure
        failures.append(f"_assert_no_check_after_runner: {exc}")
    if failures:
        print("\n".join(failures))
        return 1
    print(f"{len(_CHECKS)} fleet-matrix checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
