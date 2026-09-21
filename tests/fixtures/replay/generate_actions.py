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
    it. Tests may invoke this module only with scratch destinations to exercise
    the maintainer-tool safety checks; they never replace committed artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
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


def _reject_unsafe_destination(path: Path) -> None:
    """Reject traversal, links, and reparse points in a trusted output tree.

    The generator is a local maintainer tool. Its supported destinations are
    repository paths and fresh paths below the process temp directory; keeping
    that trust floor explicit avoids system-link false positives and prevents
    a nested existing junction from hiding an unsafe ancestor. The lexical
    walk is a preflight check, not descriptor-relative protection against an
    attacker replacing an ancestor during the final publication system call.
    """
    if ".." in Path(str(path)).parts:
        raise RuntimeError(f"unsafe fixture destination contains parent traversal: {path}")

    candidate = Path(os.path.abspath(str(path)))
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    candidate_text = os.path.normcase(str(candidate))
    trusted_roots = (
        Path(os.path.abspath(str(ROOT))),
        Path(os.path.abspath(tempfile.gettempdir())),
    )
    floor = None
    for root in trusted_roots:
        root_text = os.path.normcase(str(root))
        try:
            if os.path.commonpath((candidate_text, root_text)) == root_text:
                floor = root
                break
        except ValueError:
            continue
    if floor is None:
        raise RuntimeError(
            f"fixture destination must be under the repository or temp root: {path}"
        )

    while candidate != floor:
        if os.path.lexists(str(candidate)):
            try:
                info = os.lstat(str(candidate))
            except OSError as exc:
                raise RuntimeError(
                    f"cannot inspect fixture destination component: {candidate}"
                ) from exc
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & reparse_flag
            ):
                raise RuntimeError(
                    f"fixture destination contains a symlink or reparse point: {candidate}"
                )
        parent = candidate.parent
        if parent == candidate:
            break
        candidate = parent


def _write_checked(path: Path, data: bytes) -> None:
    """Publish atomically without clobbering a concurrent creator.

    The temporary file is fully written and fsynced before an exclusive hard
    link publishes it. A concurrent creator therefore either wins first (and
    must contain identical bytes) or cannot be overwritten. This remains a
    local maintainer-tool preflight; descriptor-relative final-system-call
    protection is outside this standard-library helper's portability boundary.
    """
    _reject_unsafe_destination(path)
    if path.exists():
        _reject_unsafe_destination(path)
        existing = path.read_bytes()
        if existing == data:
            return
        raise RuntimeError(
            f"refusing to replace {path}: committed bytes differ from generated "
            f"(existing {len(existing)}B, generated {len(data)}B); regenerate "
            "deliberately after reviewing the drift"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    _reject_unsafe_destination(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), suffix=".tmp")
    temporary_path = Path(temporary)
    try:
        _reject_unsafe_destination(temporary_path)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        _reject_unsafe_destination(path)
        try:
            os.link(str(temporary_path), str(path))
        except FileExistsError:
            _reject_unsafe_destination(path)
            existing = path.read_bytes()
            if existing != data:
                raise RuntimeError(
                    f"refusing to replace {path}: committed bytes differ from generated "
                    f"(existing {len(existing)}B, generated {len(data)}B); regenerate "
                    "deliberately after reviewing the drift"
                )
        except OSError as exc:
            raise RuntimeError(f"cannot publish fixture {path} exclusively: {exc}") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot write fixture {path}: {exc}") from exc
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass


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
    _reject_unsafe_destination(actions_out)
    _reject_unsafe_destination(expected_out)
    actions_bytes = _json_bytes(_actions_payload())
    with tempfile.TemporaryDirectory(prefix="zmem-actions-build-") as raw:
        scratch = Path(raw)
        staged_actions = scratch / "actions.json"
        staged_actions.write_bytes(actions_bytes)
        report_candidate = scratch / "actions-expected.json"
        evaluator = ROOT / "scripts" / "eval_replay.py"
        try:
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
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"--actions evaluator timed out after {exc.timeout}s") from exc
        if result.returncode:
            raise RuntimeError(f"--actions evaluator failed ({result.returncode}): {result.stderr}")
        expected_bytes = report_candidate.read_bytes()
    _reject_unsafe_destination(actions_out)
    _reject_unsafe_destination(expected_out)
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
