"""Maintainer-only generator for the committed #156 action fixtures.

Writes ``actions.json`` (the recorded delivered/evidence observation rows)
and ``actions-expected.json`` (the exact ``--actions`` report bytes produced
by ``scripts/eval_replay.py`` over the committed #155 replay snapshot). This
module is the ONLY writer for those two files: when a committed copy already
exists, the generator compares bytes first and fails without replacing, so
drift between the implementation and the committed oracle is a hard error.

``ZMEM_TEST_NOW`` pins the documented fixture epoch (2026-06-01T00:00:00Z)
per the issue contract; the evaluator's own report clock derives from the
committed decision log, so the pin documents the epoch rather than driving
it. Tests consume committed bytes and never invoke this module.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = Path(__file__).resolve().parent
EVAL_PIN_TS = "2026-06-01T00:00:00Z"
TRIGGER = "git stash pop"
UNRELATED = "bun test"

DELIVERED_ROWS = [
    {
        "id": "e0000000-0000-4000-8000-000000000101",
        "session_id": "session-R-applied",
        "timestamp": "2026-06-01T00:00:00Z",
        "operation": TRIGGER,
    },
    {
        "id": "e0000000-0000-4000-8000-000000000102",
        "session_id": "session-R-violated",
        "timestamp": "2026-06-01T00:00:00Z",
        "operation": TRIGGER,
    },
    {
        "id": "e0000000-0000-4000-8000-000000000103",
        "session_id": "session-R-unrelated",
        "timestamp": "2026-06-01T00:00:00Z",
        "operation": TRIGGER,
    },
    {
        "id": "e0000000-0000-4000-8000-000000000104",
        "session_id": "session-R-late",
        "timestamp": "2026-06-01T00:00:00Z",
        "operation": TRIGGER,
    },
]

EVIDENCE_ROWS = [
    {
        "session_id": "session-R-applied",
        "timestamp": "2026-06-01T00:01:00Z",
        "event_kind": "success",
        "operation": TRIGGER,
    },
    {
        "session_id": "session-R-violated",
        "timestamp": "2026-06-01T00:01:00Z",
        "event_kind": "failure",
        "operation": TRIGGER,
    },
    {
        "session_id": "session-R-unrelated",
        "timestamp": "2026-06-01T00:01:00Z",
        "event_kind": "success",
        "operation": UNRELATED,
    },
    {
        "session_id": "session-R-late",
        "timestamp": "2026-06-01T01:00:00Z",
        "event_kind": "success",
        "operation": TRIGGER,
    },
]


def _actions_payload() -> dict:
    return {
        "delivered_rows": [dict(row) for row in DELIVERED_ROWS],
        "evidence_rows": [dict(row) for row in EVIDENCE_ROWS],
    }


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n").encode("utf-8")


def _write_checked(path: Path, data: bytes) -> None:
    """Replace ``path`` only when its committed bytes equal ``data`` or it is absent."""
    if path.exists():
        existing = path.read_bytes()
        if existing == data:
            return
        raise RuntimeError(
            f"refusing to replace {path}: committed bytes differ from generated "
            f"(existing {len(existing)}B, generated {len(data)}B); regenerate "
            "deliberately after reviewing the drift"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _env(scratch: Path) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("ZMEM_") or key in {"CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"}:
            env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(scratch / "ambient.sqlite"),
        "ZMEM_DATA": str(scratch / "data"),
        "ZMEM_HOME": str(scratch / "home"),
        "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_EMBED_PROFILE": "fake",
        "ZMEM_TEST_NOW": EVAL_PIN_TS,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    return env


def generate(actions_out: Path, expected_out: Path) -> dict[str, str]:
    actions_bytes = _json_bytes(_actions_payload())
    with tempfile.TemporaryDirectory(prefix="zmem-actions-build-") as raw:
        scratch = Path(raw)
        staged_actions = scratch / "actions.json"
        staged_actions.write_bytes(actions_bytes)
        report_candidate = scratch / "actions-expected.json"
        evaluator = ROOT / "scripts" / "eval_replay.py"
        result = subprocess.run(
            [sys.executable, str(evaluator),
             "--store", str(FIXTURE_DIR / "store.sqlite"),
             "--log", str(FIXTURE_DIR / "decisions.log"),
             "--days", "30", "--actions",
             "--actions-input", str(staged_actions),
             "--json-out", str(report_candidate)],
            cwd=str(ROOT), env=_env(scratch),
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode:
            raise RuntimeError(f"--actions evaluator failed ({result.returncode}): {result.stderr}")
        expected_bytes = report_candidate.read_bytes()
    _write_checked(actions_out, actions_bytes)
    _write_checked(expected_out, expected_bytes)
    return {
        "actions_sha256": hashlib.sha256(actions_bytes).hexdigest(),
        "expected_sha256": hashlib.sha256(expected_bytes).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="tests/fixtures/replay/generate_actions.py")
    parser.add_argument("--actions", dest="actions", type=str, required=True, help="output path for actions.json")
    parser.add_argument("--expected", dest="expected", type=str, required=True, help="output path for actions-expected.json")
    args = parser.parse_args()
    digests = generate(Path(args.actions), Path(args.expected))
    print(json.dumps(digests, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
