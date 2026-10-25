"""Arm's-length checks for issue #291 (T-12 fleet ops on the broker machine).

Module-level pytest tests (the t-series convention, so the frozen bare
node-id drivers select them). Two frozen drivers bind the executable
contract; the implementing change is NOT visible to this file and must be
driven to match it:

- AC1 (refusal): ``python -m pytest
  tests/test_t12_fleet_ops.py::test_zmem_data_pointing_at_a_file_is_refused
  -q -p no:randomly -p no:cacheprovider``
- AC2 (doctor fleet family): ``python -m pytest
  tests/test_t12_fleet_ops.py::test_doctor_fleet_checks_present
  -q -p no:randomly -p no:cacheprovider``
- PRESERVING supplement (green at base AND head):
  ``tests/test_t12_fleet_ops.py::test_zmem_data_directory_still_resolves``

Every probe runs as a subprocess against a temp-dir env (store/remote vars
stripped first, scratch HOME) — these tests never touch the real
``~/.zmem``, never mutate production files, and never leave the machine.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
DOCTOR_PY = SCRIPTS_DIR / "doctor.py"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

# Store/remote vars REMOVED from the copied os.environ before any staged
# value is set (the scripts/host_canary.py STRIP_VARS precedent): an ambient
# store or remote setting must never steer a hermetic probe.
STRIP_VARS = (
    "ZMEM_STORE",
    "ZMEM_DATA",
    "CLAUDE_PLUGIN_DATA",
    "ZCODE_PLUGIN_DATA",
    "ZMEM_REMOTE_URL",
    "ZMEM_MCP_URL",
    "ZMEM_MCP_TOKEN",
    "ZMEM_MCP_TOKEN_FILE",
    "ZMEM_MCP_TOKENS_FILE",
    "ZMEM_CORE_MD",
    "ZMEM_FLEET_FDE_PROBE",
)

# The frozen fleet check-id vocabulary (issue #291 AC2): every fleet check
# the doctor must expose under the ``fleet-`` prefix in its flat checks list.
FLEET_CHECK_IDS = (
    "fleet-hub-reachable",
    "fleet-hello-compat",
    "fleet-principal",
    "fleet-op-queue",
    "fleet-snapshot-age",
    "fleet-tier0-local",
    "fleet-disk-encryption",
)
VALID_STATUSES = frozenset({"pass", "warn", "fail", "skip"})

# A bearer token planted in the staged principal map. The report must NEVER
# echo it (the mcp-token precedent: token values never appear in output).
PLANTED_TOKEN = "t12-planted-token-do-not-print-3f9a2c"

# Sentinel for "leave ZMEM_FLEET_FDE_PROBE unset" (probe the real OS).
_FDE_UNSET = object()


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _base_env(home: Path) -> dict:
    """os.environ copy with the store/remote vars stripped and a scratch HOME."""
    env = {k: v for k, v in os.environ.items() if k not in STRIP_VARS}
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    return env


def _stage_broker_machine(tmp: Path) -> tuple[Path, Path]:
    """Stage the hermetic broker-machine state under ``tmp``.

    Returns (data_dir, tokens_file). The data dir carries: a stray Tier-0
    ``core.md``, a fresh unexpired broker snapshot pair (the
    fleet/snapshot.py meta shape), and one pending op-queue entry (the
    fleet/opqueue.py layout: ``<data>/queue/ops-<principal>.jsonl``). The
    tokens file is a minimal VALID principal map in the exact shape
    hermes-plugin/server/auth.py ``_parse_principal_map`` accepts (the
    doctor parses the file directly; it never imports server auth).
    """
    data_dir = tmp / "data"
    tokens_file = tmp / "tokens.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()

    # Stray Tier-0 file: a local core.md beside the resolved store while
    # remote mode is on (the fleet check must warn about it).
    _write_text(data_dir / "core.md", "# t12 stray local core.md\n")

    # Fresh, unexpired snapshot pair (snapshot_revision / issued_at /
    # expires_at / row_count / principal; the sqlite half needs the memory
    # table to count as a snapshot at all).
    broker = data_dir / "broker"
    _write_text(
        broker / "snapshot.meta.json",
        json.dumps(
            {
                "snapshot_revision": 12,
                "issued_at": now - 60,
                "expires_at": now + 72 * 3600,
                "row_count": 3,
                "principal": "t12-broker",
            }
        ),
    )
    snap = broker / "snapshot.sqlite"
    conn = sqlite3.connect(str(snap))
    try:
        conn.execute(
            "CREATE TABLE memory (id TEXT PRIMARY KEY, namespace TEXT, "
            "content TEXT, superseded_at TEXT)"
        )
        conn.commit()
    finally:
        conn.close()

    # One small pending op-queue entry (one JSON entry per line).
    entry = {
        "op_id": "t12op00000000000000000000000001",
        "op": "add",
        "args": {"content": "t12 fleet doctor op-queue fixture row"},
        "depends_on": None,
        "client_seq": 1,
        "status": "pending",
        "enqueued_at": now - 30,
    }
    _write_text(data_dir / "queue" / "ops-local.jsonl", json.dumps(entry) + "\n")

    _write_text(
        tokens_file,
        json.dumps(
            {
                PLANTED_TOKEN: {
                    "principal": "t12-broker",
                    "host": "t12-workstation",
                    "harness": "claude-code",
                    "namespaces": ["project:t12-fleet"],
                    "capabilities": ["read", "write"],
                }
            }
        ),
    )
    return data_dir, tokens_file


def _fleet_env(tmp: Path, data_dir: Path, tokens_file: Path, fde) -> dict:
    """The hermetic ``doctor --fleet`` env for the staged broker machine."""
    env = _base_env(tmp / "home")
    env["ZMEM_DATA"] = str(data_dir)
    env["ZMEM_MCP_TOKENS_FILE"] = str(tokens_file)
    # Closed local port: deterministic connection refused on Windows and
    # Linux; never an external network call.
    env["ZMEM_REMOTE_URL"] = "http://127.0.0.1:1"
    # Keep the sibling non-fleet checks hermetic and deterministic on any
    # runner (the doctor subprocess conventions in tests/test_doctor.py):
    # scratch HOME is already pinned, model probes point at a missing dir
    # with autodownload off, and the native-memory check's documented kill
    # switch keeps its verdict from depending on the real ~/.claude.
    env["ZMEM_MODELS_DIR"] = str(tmp / "missing-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if fde is not _FDE_UNSET:
        env["ZMEM_FLEET_FDE_PROBE"] = str(fde)
    return env


def _run_doctor_fleet(env: dict):
    """Run ``doctor.py --fleet --format json``; guard the base-leg failure.

    A crash surfaces as its traceback, and the base tree's argparse refusal
    (exit 2 ``unrecognized arguments: --fleet`` on stderr, empty stdout)
    surfaces HERE as a failure with the stderr attached — never as a
    confusing JSONDecodeError in the caller (the PRR-014 guard pattern).
    """
    result = subprocess.run(
        [PYTHON, "-X", "utf8", str(DOCTOR_PY), "--fleet", "--format", "json"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "Traceback" not in result.stderr, result.stderr
    assert result.stdout.strip(), (
        f"doctor --fleet wrote no JSON on stdout (rc={result.returncode}) "
        f"stderr tail={result.stderr[-400:]!r}"
    )
    return result, json.loads(result.stdout)


def _fleet_check(report: dict, check_id: str) -> dict:
    for check in report.get("checks", []):
        if check.get("id") == check_id:
            return check
    raise AssertionError(f"doctor --fleet report has no {check_id} check")


def _fleet_statuses(report: dict) -> dict:
    statuses = {
        c.get("id"): c.get("status")
        for c in report.get("checks", [])
        if isinstance(c.get("id"), str) and c["id"].startswith("fleet-")
    }
    missing = [cid for cid in FLEET_CHECK_IDS if cid not in statuses]
    assert not missing, (
        f"doctor --fleet report is missing fleet checks {missing}; "
        f"found {sorted(statuses)}"
    )
    return statuses


def _assert_exit_iff_no_fail(result, report: dict) -> None:
    """Doctor's documented exit contract: 0 iff no fail-status checks."""
    any_fail = any(c.get("status") == "fail" for c in report["checks"])
    assert (result.returncode == 0) == (not any_fail), (
        f"exit code must be 0 iff no fail checks (rc={result.returncode}, "
        f"fails={[c['id'] for c in report['checks'] if c.get('status') == 'fail']})"
    )


# ---------------------------------------------------------------------------
# AC1: ZMEM_DATA naming an existing regular file is refused loudly
# ---------------------------------------------------------------------------


def test_zmem_data_pointing_at_a_file_is_refused():
    """ZMEM_DATA at a regular file: refuse with exit 2 AND name ZMEM_STORE.

    The store CLI's config-error exit code stays 2, and the combined output
    must point the operator at ``ZMEM_STORE`` as the variable that takes a
    FILE path, instead of a misleading maintenance-lock message about a lock
    file under ``<file>/.zmem-maintenance.lock``. RED at the base tree (the
    exit code is already 2 but the message never names ZMEM_STORE); GREEN
    once the #291 fix lands.
    """
    with tempfile.TemporaryDirectory(prefix="zmem-t12-refuse-") as td:
        tmp = Path(td)
        home = tmp / "home"
        home.mkdir()
        target = tmp / "a-regular-file.txt"
        target.write_text(
            "this is a regular file, not a data dir\n", encoding="utf-8"
        )
        assert target.is_file(), "fixture must be a regular file"

        env = _base_env(home)
        env["ZMEM_DATA"] = str(target)
        result = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "recent", "--limit", "1"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 2, (
            f"refusal must keep exit code 2 (rc={result.returncode}) "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "ZMEM_STORE" in (result.stdout + result.stderr), (
            "the refusal message must name ZMEM_STORE as the file-path "
            f"variable (stdout={result.stdout!r} stderr={result.stderr!r})"
        )


def test_zmem_data_directory_still_resolves():
    """PRESERVING supplement: a DIRECTORY ZMEM_DATA keeps <dir>/store.sqlite.

    The #291 refusal is only for the regular-file shape; the directory
    shape of the resolution chain (ZMEM_DATA > ... > <dir>/store.sqlite) is
    unchanged by the fix, at both the base and the fixed tree.
    """
    with tempfile.TemporaryDirectory(prefix="zmem-t12-dir-") as td:
        tmp = Path(td)
        home = tmp / "home"
        data_dir = tmp / "data"
        home.mkdir()
        data_dir.mkdir()

        env = _base_env(home)
        env["ZMEM_DATA"] = str(data_dir)
        env["PYTHONPATH"] = str(SCRIPTS_DIR)
        result = subprocess.run(
            [
                PYTHON,
                "-X",
                "utf8",
                "-c",
                "import host; print(host.resolve_store_path())",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr
        assert "Traceback" not in result.stderr, result.stderr
        resolved = result.stdout.strip().splitlines()[-1].strip()
        assert os.path.normcase(resolved) == os.path.normcase(
            str(data_dir / "store.sqlite")
        ), (
            "ZMEM_DATA naming a directory must still resolve "
            f"<dir>/store.sqlite (got {resolved})"
        )


# ---------------------------------------------------------------------------
# AC2: doctor --fleet reports the broker fleet family, wired sanely
# ---------------------------------------------------------------------------


def test_doctor_fleet_checks_present():
    """``doctor --fleet`` reports the seven-check fleet family on a broker.

    For the staged hermetic state the contract is: (a) all seven fleet
    checks appear in the JSON report with a status in {pass, warn, fail,
    skip}; (b) hub reachability FAILS for the deliberately unreachable URL;
    (c) the stray local core.md check WARNS while remote mode is on; (d)
    full-disk encryption FAILS with the probe seam ZMEM_FLEET_FDE_PROBE=off
    while a snapshot exists, PASSES with =on, and an unset seam (real-OS
    probe) yields a valid report without crashing; the principal check
    PASSES for the valid map without ever echoing the planted token; the
    op-queue check reports a numeric depth; and (e) stdout is valid JSON
    and doctor exits 0 iff no check failed. RED at the base tree (argparse
    rejects --fleet with exit 2 unrecognized arguments); GREEN once the
    #291 fix adds the fleet mode.
    """
    tmp = Path(tempfile.mkdtemp(prefix="zmem-t12-fleet-"))
    try:
        data_dir, tokens_file = _stage_broker_machine(tmp)

        # -- probe seam =off: a snapshot exists and FDE is off -> FAIL -----
        result_off, report_off = _run_doctor_fleet(
            _fleet_env(tmp, data_dir, tokens_file, "off")
        )
        statuses_off = _fleet_statuses(report_off)

        # (a) all seven present with a sane status
        for cid in FLEET_CHECK_IDS:
            assert statuses_off[cid] in VALID_STATUSES, (
                f"{cid}: {statuses_off[cid]}"
            )

        # (b) the deliberately unreachable hub URL fails
        assert statuses_off["fleet-hub-reachable"] == "fail", _fleet_check(
            report_off, "fleet-hub-reachable"
        )

        # (c) stray local core.md in remote mode is a warn
        assert statuses_off["fleet-tier0-local"] == "warn", _fleet_check(
            report_off, "fleet-tier0-local"
        )

        # principal: the valid staged map passes, and the planted bearer
        # token is never echoed anywhere in the report.
        assert statuses_off["fleet-principal"] == "pass", _fleet_check(
            report_off, "fleet-principal"
        )
        assert PLANTED_TOKEN not in (result_off.stdout + result_off.stderr), (
            "token values must never appear in the doctor report"
        )

        # op-queue: reports a depth NUMBER (thresholds are the implementer's
        # to choose; the wiring must expose the count).
        opq = _fleet_check(report_off, "fleet-op-queue")
        depth = opq.get("details", {}).get("depth")
        assert not isinstance(depth, bool), f"depth must be an int, got {depth!r}"
        assert isinstance(depth, int), (
            f"fleet-op-queue must report a numeric depth: {opq}"
        )
        assert depth >= 0, opq

        # (d) FDE off while a snapshot exists -> fail
        assert statuses_off["fleet-disk-encryption"] == "fail", _fleet_check(
            report_off, "fleet-disk-encryption"
        )

        # (e) valid JSON on stdout (parsed above) and the documented exit
        # code: 0 iff no fail-status checks.
        _assert_exit_iff_no_fail(result_off, report_off)

        # -- probe seam =on: FDE reported on -> PASS ------------------------
        result_on, report_on = _run_doctor_fleet(
            _fleet_env(tmp, data_dir, tokens_file, "on")
        )
        statuses_on = _fleet_statuses(report_on)
        assert statuses_on["fleet-disk-encryption"] == "pass", _fleet_check(
            report_on, "fleet-disk-encryption"
        )
        _assert_exit_iff_no_fail(result_on, report_on)

        # -- probe seam unset: the real-OS probe must not crash the report --
        result_unset, report_unset = _run_doctor_fleet(
            _fleet_env(tmp, data_dir, tokens_file, _FDE_UNSET)
        )
        statuses_unset = _fleet_statuses(report_unset)
        assert statuses_unset["fleet-disk-encryption"] in VALID_STATUSES, (
            _fleet_check(report_unset, "fleet-disk-encryption")
        )
        _assert_exit_iff_no_fail(result_unset, report_unset)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
