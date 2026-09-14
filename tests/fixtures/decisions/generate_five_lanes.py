#!/usr/bin/env python3
"""Generate the deterministic issue #153 five-lane decision fixture.

The fixture is deliberately independent of a store, embedding model, or host
configuration.  It exercises the real miss-rate parser and report projection,
then writes the two explicitly requested artifacts atomically.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_LOG = Path(__file__).with_name("five-lanes.log")
DEFAULT_EXPECTED = Path(__file__).with_name("five-lanes.expected.json")
LANES = ("claude", "codex", "zcode", "hermes-provider", "hermes-compat")
# The fixture follows the runtime moment order from the issue.  Report rows
# are sorted independently by the production matrix builder.
MOMENTS = ("session_start", "user_prompt", "pretool", "precompact")
MOMENT_TIMINGS = {
    "session_start": 10,
    "user_prompt": 20,
    "pretool": 30,
    "precompact": 40,
}
UUIDS = (
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
    "00000000-0000-4000-8000-000000000003",
)
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
FIXED_EPOCH = 1780272000  # 2026-06-01T00:00:00Z
FIXED_NAMESPACE = "project:fixture"


def _release_version() -> str:
    """Read and validate the current checkout's release semver."""
    manifest = ROOT / "release-manifest.json"
    try:
        obj = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"invalid release manifest: {exc}")
    version = obj.get("version") if isinstance(obj, dict) else None
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise SystemExit("invalid release manifest version; refusing fixture outputs")
    return version


def _line(index: int, lane: str, moment: str, version: str) -> str:
    """Render one fixed-format enriched decision line."""
    # Stable timestamp, IDs, and timings make both artifacts reproducible.
    timestamp = FIXED_EPOCH
    if index == 0:
        status, reason, ids, all_ids, extra = (
            "silent", "already-delivered", [], list(UUIDS), " exc=3")
    elif index in (1, 2):
        status, reason, ids, all_ids, extra = (
            "silent", "empty-pool", [], [], "")
    else:
        memory_id = UUIDS[(index - 3) % len(UUIDS)]
        status, reason, ids, all_ids, extra = (
            "injected", "injected", [memory_id], [memory_id], "")
    sid = f"fixture-{lane}"
    return (
        f"[{timestamp}] zmem-hook status={status} reason={reason}"
        f" ids={ids} all={all_ids}{extra} sid={sid} "
        f"moment={moment} lane={lane} ver={version} "
        f"t_ms={MOMENT_TIMINGS[moment]}\n"
    )


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace one output atomically after all validation has completed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.",
                                   suffix=".tmp", dir=str(path.parent))
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def generate(log_path: Path, expected_path: Path) -> str:
    """Return the expected JSON text and atomically update both artifacts."""
    # Version validation is intentionally the first filesystem operation that
    # could lead to either requested output.  Invalid manifests therefore
    # leave existing artifacts byte-for-byte untouched.
    version = _release_version()
    raw_log = "".join(
        _line(index, lane, moment, version)
        for index, (lane, moment) in enumerate(
            (pair for lane in LANES for pair in
             ((lane, moment) for moment in MOMENTS)))
    ).encode("utf-8")

    # Use the production parser and matrix builder against a private temporary
    # path, not a hand-copied parser implementation.
    sys.path.insert(0, str(ROOT / "skills" / "memory" / "scripts"))
    from storelib import miss_rate  # type: ignore

    with tempfile.TemporaryDirectory(prefix="zmem-153-fixture-") as scratch:
        candidate = Path(scratch) / "decisions.log"
        candidate.write_bytes(raw_log)
        parsed = miss_rate.parse_bg_log(candidate)
    if len(parsed) != 20:
        raise SystemExit(f"fixture parser returned {len(parsed)} lines, expected 20")
    if any(row.get("ver") != version or not isinstance(row.get("t_ms"), int)
           or row["t_ms"] < 0 for row in parsed):
        raise SystemExit("fixture parser did not retain valid attribution")
    matrix = miss_rate.build_decision_matrix(parsed)
    if len(matrix) != 20:
        raise SystemExit("fixture report projection did not produce 20 rows")

    # Preserve the parser's complete fields but make the expected decision
    # list deterministic in the same lexicographic order as the report rows.
    parsed = sorted(parsed, key=lambda row: (row.get("lane") or "",
                                             row.get("moment") or ""))
    expected_obj = {
        "decision_lines": parsed,
        "fixture": {
            "generated_at": "2026-06-01T00:00:00Z",
            "namespace": FIXED_NAMESPACE,
            "version": version,
        },
        "matrix": matrix,
    }
    expected_text = json.dumps(expected_obj, indent=2, sort_keys=True) + "\n"
    # Both replacements happen only after parsing and report validation.  Each
    # replacement is atomic; a malformed version never reaches this point.
    _atomic_write(log_path, raw_log)
    _atomic_write(expected_path, expected_text.encode("utf-8"))
    return expected_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--expected", type=Path, default=DEFAULT_EXPECTED)
    args = parser.parse_args(argv)
    expected = generate(args.log, args.expected)
    print("generated %s (%d bytes)" % (args.expected, len(expected.encode("utf-8"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
