"""Served-code drift detection by content hash (issue #107, Workstream A PR 2).

Covers the issue's three acceptance criteria plus the backward-compat and
operator-notice contracts:

- AC1: evaluate() reports drifted for a tree with one modified hook file and
  matched for an unmodified copy (doctor-level equivalent lives in
  tests/test_doctor_drift.py).
- AC2: the drift check never fails the hook path — a missing/unreadable
  manifest degrades to unknown, exit 0, no bg-log line (asserted via the CLI
  and end-to-end through zmem-session-start.sh).
- AC3: the drift line is written AT MOST ONCE per session (marker file),
  asserted by driving the drift CLI twice and the session-start hook twice.
- Operator channel (issue addendum): on drift, session-start carries a
  systemMessage in the sentinel payload (never in additionalContext).
- Backward compat: the bg-log line uses the zmem-drift prefix, which the
  0.14-0.16 miss-rate parser ignores (it skips lines without zmem-hook).
- Import safety: importing drift.py never touches or creates any store.

All stores are throwaway temp stores; ambient zmem env is stripped from every
child process. The operator's real store is never touched.

Runs standalone: python tests/test_drift.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
DRIFT_PY = SCRIPTS / "drift.py"
SESSION_START = REPO_ROOT / "hooks" / "zmem-session-start.sh"

sys.path.insert(0, str(SCRIPTS))
import drift  # noqa: E402

PYTHON = sys.executable

# Canonical ops-lane rule (same as miss_rate / recall-body).
SID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

DRIFT_LINE_RE = re.compile(
    r"^\[(\d+)\] zmem-drift served=([0-9a-f]{8}) release=([0-9a-f]{8}) files=(\d+)$"
)

HOSTILE_VERSION = "0.9.9'\"; semicolon path"


def _sanitize(sid: str) -> str:
    return SID_SAFE_RE.sub("_", sid)[:128] or "unknown"


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _make_tree(root: Path) -> dict:
    """A minimal served tree across all five surface prefixes.

    Returns the files written (relpath -> bytes) so tests can re-derive the
    manifest after mutating the tree."""
    files = {
        "hooks/zmem-recall.sh": b"#!/usr/bin/env bash\n# recall\n",
        "hooks/lib/zmem-recall-body.py": b"# body\n",
        "skills/memory/scripts/store.py": b"# store shim\n",
        "skills/memory/SKILL.md": b"# memory skill\n",
        "hermes-plugin/plugin.yaml": b"name: zmem\n",
        "scripts/host_canary.py": b"# canary shim\n",
    }
    for rel, data in files.items():
        _write_bytes(root / rel, data)
    # Excluded noise: must never enter the surface enumeration.
    _write_bytes(root / "skills/memory/scripts/__pycache__/x.cpython-311.pyc", b"\x00pyc")
    _write_bytes(root / "hooks/lib/__pycache__/y.cpython-311.pyc", b"\x00pyc")
    # Off-surface: never hashed.
    _write_bytes(root / "README.md", b"# readme\n")
    _write_bytes(root / "skills/memory/models/minilm.onnx", b"\x00model")
    return files


def _write_manifest(root: Path, version: str = "9.9.9") -> dict:
    """Emit release-manifest.json over the CURRENT tree, exactly the way
    release_gate --emit-manifest does (same drift functions)."""
    files = drift.tree_hashes(root)
    manifest = {
        "version": version,
        "algorithm": "sha256-crlf-norm",
        "files": files,
        "digest": drift.aggregate(files),
    }
    path = root / "release-manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return manifest


def _drift_lines(tmp: str | Path) -> list:
    path = Path(tmp) / "zmem-bg.log"
    if not path.is_file():
        return []
    return [ln for ln in path.read_text(encoding="utf-8").splitlines()
            if "zmem-drift" in ln]


def _run_cli(*args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    # Strip every ambient ZMEM_* var (F-013): the drift CLI must be
    # env-independent in tests, not silently steered by the developer shell.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ZMEM")
           and k not in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")}
    return subprocess.run(
        [PYTHON, str(DRIFT_PY), *args],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def _run_log_once(root: Path, data_dir: Path, sid: str,
                  timeout: int = 60) -> subprocess.CompletedProcess:
    return _run_cli("log-once", "--root", str(root),
                    "--data-dir", str(data_dir), "--sid", sid, timeout=timeout)


class DriftSurfaceTest(unittest.TestCase):
    """Unit layer: surface enumeration, hashing, aggregate, evaluate()."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-drift107-"))
        self.root = self.tmp / "served"
        self.root.mkdir()
        _make_tree(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_surface_enumeration_excludes_pycache_and_offsurface(self):
        files = drift.surface_files(self.root)
        self.assertEqual(files, sorted(files), "surface must be sorted")
        self.assertIn("hooks/zmem-recall.sh", files)
        self.assertIn("skills/memory/scripts/store.py", files)
        self.assertIn("skills/memory/SKILL.md", files)
        self.assertIn("hermes-plugin/plugin.yaml", files)
        # 0.18.0 (issue #108 review): scripts/ is a drift surface — the
        # canary must be integrity-visible.
        self.assertIn("scripts/host_canary.py", files)
        for rel in files:
            self.assertNotIn("__pycache__", rel.split("/"))
            self.assertFalse(rel.endswith((".pyc", ".pyo")))
        self.assertNotIn("README.md", files)
        self.assertNotIn("skills/memory/models/minilm.onnx", files)
        # POSIX separators in every relpath (manifest key stability).
        for rel in files:
            self.assertNotIn("\\", rel)

    def test_crlf_and_lf_hash_identically(self):
        lf = self.tmp / "lf.txt"
        crlf = self.tmp / "crlf.txt"
        lf.write_bytes(b"line1\nline2\n")
        crlf.write_bytes(b"line1\r\nline2\r\n")
        self.assertEqual(drift.file_hash(lf), drift.file_hash(crlf))
        self.assertIsNotNone(drift.file_hash(lf))

    def test_missing_file_hashes_none(self):
        self.assertIsNone(drift.file_hash(self.tmp / "absent.txt"))

    def test_aggregate_deterministic_and_sorted(self):
        a = {"x": "1", "y": "2"}
        b = {"y": "2", "x": "1"}
        self.assertEqual(drift.aggregate(a), drift.aggregate(b))
        self.assertRegex(drift.aggregate(a), r"^[0-9a-f]{64}$")

    def test_evaluate_matched(self):
        _write_manifest(self.root)
        result = drift.evaluate(self.root)
        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["differing_count"], 0)
        self.assertEqual(result["differing"], [])
        self.assertEqual(result["served"], result["release"])
        self.assertTrue(result["files_compared"] >= 5)

    def test_evaluate_drifted_one_modified_hook_file(self):
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(
            b"#!/usr/bin/env bash\n# recall MODIFIED\n")
        result = drift.evaluate(self.root)
        self.assertEqual(result["status"], "drifted")
        self.assertEqual(result["differing_count"], 1)
        self.assertEqual(result["differing"], ["hooks/zmem-recall.sh"])
        self.assertNotEqual(result["served"], result["release"])

    def test_evaluate_drifted_missing_and_extra(self):
        _write_manifest(self.root)
        (self.root / "hooks/lib/zmem-recall-body.py").unlink()
        _write_bytes(self.root / "hooks/zmem-new-hook.sh", b"#!/bin/sh\n")
        result = drift.evaluate(self.root)
        self.assertEqual(result["status"], "drifted")
        self.assertEqual(
            sorted(result["differing"]),
            ["hooks/lib/zmem-recall-body.py", "hooks/zmem-new-hook.sh"])
        self.assertEqual(result["differing_count"], 2)

    def test_evaluate_unknown_when_manifest_missing(self):
        result = drift.evaluate(self.root)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["release"])

    def test_evaluate_unknown_when_manifest_malformed(self):
        (self.root / "release-manifest.json").write_text("{not json",
                                                         encoding="utf-8")
        self.assertEqual(drift.evaluate(self.root)["status"], "unknown")
        (self.root / "release-manifest.json").write_text('{"version": "1"}',
                                                         encoding="utf-8")
        self.assertEqual(drift.evaluate(self.root)["status"], "unknown")

    def test_evaluate_unreadable_file_counts_as_differing(self):
        _write_manifest(self.root)
        target = str(self.root / "hooks/zmem-recall.sh")
        real_hash = drift.file_hash
        with unittest.mock.patch.object(
                drift, "file_hash",
                side_effect=lambda p: None if str(p) == target else real_hash(p)):
            result = drift.evaluate(self.root)
        self.assertEqual(result["status"], "drifted")
        self.assertIn("hooks/zmem-recall.sh", result["differing"])

    def test_evaluate_lists_first_ten_differing_paths(self):
        _write_manifest(self.root)
        for i in range(12):
            _write_bytes(self.root / f"hooks/extra-{i:02d}.sh", b"#!/bin/sh\n")
        result = drift.evaluate(self.root)
        self.assertEqual(result["differing_count"], 12)
        self.assertEqual(len(result["differing"]), 10)


import unittest.mock  # noqa: E402  (used by the test above)


class DriftCliTest(unittest.TestCase):
    """CLI layer: check + log-once semantics (AC2/AC3)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-drift107-cli-"))
        self.root = self.tmp / "served"
        self.root.mkdir()
        _make_tree(self.root)
        self.data = self.tmp / "data"
        self.data.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_check_prints_json_exit_zero(self):
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        r = _run_cli("check", "--root", str(self.root))
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["status"], "drifted")
        self.assertEqual(payload["differing_count"], 1)

    def test_check_exit_zero_without_manifest(self):
        r = _run_cli("check", "--root", str(self.root))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "unknown")

    def test_log_once_writes_line_and_marker_on_drift(self):
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        r = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["status"], "drifted")
        self.assertTrue(payload["logged"])
        self.assertTrue(payload["system_message"])
        lines = _drift_lines(self.data)
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], DRIFT_LINE_RE)
        marker = drift._marker_path(self.data, "sess-a")
        self.assertTrue(marker.is_file())

    def test_log_once_at_most_once_per_session(self):
        """AC3: two log-once calls with the same sid -> exactly one line."""
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        for _ in range(2):
            r = _run_log_once(self.root, self.data, "sess-a")
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(_drift_lines(self.data)), 1)
        # A different session logs its own line (fresh marker key).
        _run_log_once(self.root, self.data, "sess-b")
        self.assertEqual(len(_drift_lines(self.data)), 2)

    def test_log_once_matched_tree_no_line_but_marker(self):
        _write_manifest(self.root)
        first = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)["status"], "matched")
        # The marker short-circuits the second call (status "already"): the
        # drift evaluation runs at most once per session id even when there
        # is nothing to report.
        second = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(second.returncode, 0, second.stderr)
        second_payload = json.loads(second.stdout)
        self.assertEqual(second_payload["status"], "matched")
        self.assertTrue(second_payload["already"])
        self.assertEqual(_drift_lines(self.data), [])
        self.assertTrue(drift._marker_path(self.data, "sess-a").is_file())

    def test_already_path_still_surfaces_operator_notice(self):
        """F-008: when a recall-body fallback won the marker race, the
        session-start call re-evaluates (logged=False) and still carries the
        operator system_message — the notice is never silently lost."""
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh" + bytes([10]))
        first = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(json.loads(first.stdout)["status"], "drifted")
        self.assertEqual(len(_drift_lines(self.data)), 1)
        second = _run_log_once(self.root, self.data, "sess-a")
        payload = json.loads(second.stdout)
        self.assertTrue(payload["already"])
        self.assertFalse(payload["logged"])
        self.assertEqual(payload["status"], "drifted")
        self.assertIn("system_message", payload)
        self.assertEqual(len(_drift_lines(self.data)), 1,
                         "already path must never duplicate the line")

    def test_log_once_missing_manifest_never_blocks(self):
        """AC2: no manifest -> unknown, exit 0, no line, marker still set."""
        r = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "unknown")
        self.assertEqual(_drift_lines(self.data), [])

    def test_log_once_unwritable_data_dir_exit_zero(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("not a dir", encoding="utf-8")
        r = _run_log_once(self.root, blocker, "sess-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(json.loads(r.stdout)["status"], ("error", "unknown"))

    def test_log_once_sanitizes_sid_in_marker(self):
        hostile = 'sess-<>hostile&id "x"'
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        r = _run_log_once(self.root, self.data, hostile)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(
            drift._marker_path(self.data, hostile).is_file())
        self.assertEqual(len(_drift_lines(self.data)), 1)

    def test_marker_names_collision_free_for_prefix_sharing_sids(self):
        """CS-1: two distinct sids sharing 200 sanitized chars must NOT
        collide on one marker — each logs its own drift line."""
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        long_a = "sess-" + "a" * 200
        long_b = "sess-" + "a" * 197 + "bcd"
        self.assertNotEqual(drift._marker_path(self.data, long_a),
                            drift._marker_path(self.data, long_b))
        for sid in (long_a, long_b):
            r = _run_log_once(self.root, self.data, sid)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(json.loads(r.stdout)["status"], "drifted")
        self.assertEqual(len(_drift_lines(self.data)), 2)

    def test_manifest_non_string_hash_values_degrade_to_unknown(self):
        """CS-3/M1-1: a corrupt manifest (non-string hash values) must be
        treated as unreadable (unknown), never as whole-tree drift."""
        (self.root / "release-manifest.json").write_text(
            json.dumps({"version": "9.9.9",
                        "files": {"hooks/zmem-recall.sh": 12345}}),
            encoding="utf-8")
        self.assertEqual(drift.evaluate(self.root)["status"], "unknown")
        (self.root / "release-manifest.json").write_text(
            json.dumps({"version": "9.9.9",
                        "files": {"hooks/zmem-recall.sh": {"a": 1}}}),
            encoding="utf-8")
        self.assertEqual(drift.evaluate(self.root)["status"], "unknown")

    def test_no_subcommand_keeps_stdout_pure(self):
        """CS-5: a mis-invoked drift.py must never put non-JSON on stdout
        (session-start captures stdout as DRIFT_JSON and parses it)."""
        r = _run_cli()
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")
        self.assertTrue(r.stderr.strip())

    def test_marker_path_as_directory_recovers(self):
        """M2-4: a stray DIRECTORY at the marker path must not suppress the
        session's drift line forever — it is removed and detection proceeds."""
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        marker = drift._marker_path(self.data, "sess-a")
        marker.mkdir(parents=True)
        r = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["status"], "drifted")
        self.assertTrue(payload["logged"])
        self.assertEqual(len(_drift_lines(self.data)), 1)
        self.assertTrue(marker.is_file())

    def test_failed_log_append_releases_marker_for_retry(self):
        """CS-2: if the bg-log append fails after the marker was created, the
        marker is released so a later call retries instead of leaving a
        marker with no drift line."""
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        marker = drift._marker_path(self.data, "sess-a")
        # RO dir: marker create succeeds (file in RO dir? no) — instead point
        # --data-dir at a dir where zmem-bg.log cannot be appended: make the
        # log path a DIRECTORY, so open(..., "a") raises OSError.
        (self.data / "zmem-bg.log").mkdir()
        r = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["status"], "drifted")
        self.assertFalse(payload["logged"])
        self.assertFalse(marker.exists(),
                         "marker must be released when the log write fails")
        # Retry with the blocker removed: the line is written then.
        (self.data / "zmem-bg.log").rmdir()
        r2 = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        self.assertTrue(json.loads(r2.stdout)["logged"])
        self.assertEqual(len(_drift_lines(self.data)), 1)

    def test_corrupt_manifest_degrades_to_unknown_with_status(self):
        """F-009/F-012: a manifest present but failing its algorithm or
        digest integrity gate is CORRUPT (manifest_status corrupt), never
        whole-tree drift and never indistinguishable from absent."""
        _write_manifest(self.root)
        manifest_path = self.root / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Algorithm mismatch.
        manifest["algorithm"] = "md5"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = drift.evaluate(self.root)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["manifest_status"], "corrupt")
        # Digest inconsistent with its own file hashes.
        manifest["algorithm"] = "sha256-crlf-norm"
        manifest["digest"] = "f" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = drift.evaluate(self.root)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["manifest_status"], "corrupt")
        # Absent file stays absent.
        manifest_path.unlink()
        result = drift.evaluate(self.root)
        self.assertEqual(result["manifest_status"], "absent")

    def test_golden_hash_vector(self):
        """Cubic P2 anti-self-fulfilling pin: file_hash must equal an
        independently precomputed SHA-256, not merely agree with itself."""
        import hashlib
        f = self.tmp / "golden.txt"
        f.write_bytes(b"zmem drift golden vector" + bytes([10]))
        expected = hashlib.sha256(b"zmem drift golden vector" + bytes([10])).hexdigest()
        self.assertEqual(drift.file_hash(f), expected)

    def test_nonempty_marker_directory_left_alone(self):
        """Cubic run-2 P2: a NON-EMPTY directory at the marker path may hold
        unrelated data — log_once must not delete it; report error instead."""
        _write_manifest(self.root)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh" + bytes([10]))
        marker = drift._marker_path(self.data, "sess-a")
        marker.mkdir()
        (marker / "unrelated.txt").write_text("keep me", encoding="utf-8")
        r = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["status"], "error")
        self.assertTrue((marker / "unrelated.txt").is_file(),
                        "unrelated data must survive")
        self.assertEqual(_drift_lines(self.data), [])

    def test_hostile_version_sanitized_in_system_message(self):
        """ST-1: newlines/ANSI escapes in a hostile manifest version must not
        reach the operator systemMessage (display-forgery class)."""
        hostile_version = "9.9.9\nFAKE LOG LINE\x1b[31mRED"
        _write_manifest(self.root, version=hostile_version)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        r = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        msg = json.loads(r.stdout)["system_message"]
        self.assertNotIn("\n", msg)
        self.assertNotIn("\x1b", msg)
        self.assertIn("?", msg)

    def test_system_message_survives_hostile_manifest_version(self):
        """The systemMessage is generated inside drift.py from manifest fields;
        a hostile version string must round-trip as data (never executed or
        split), proving the argv plumbing contract."""
        _write_manifest(self.root, version=HOSTILE_VERSION)
        (self.root / "hooks/zmem-recall.sh").write_bytes(b"#!/bin/sh\n")
        r = _run_log_once(self.root, self.data, "sess-a")
        self.assertEqual(r.returncode, 0, r.stderr)
        payload = json.loads(r.stdout)
        self.assertEqual(payload["status"], "drifted")
        self.assertIn(HOSTILE_VERSION, payload["system_message"])


class MissRateCompatTest(unittest.TestCase):
    """The 0.14-0.16 bg-log parser must ignore zmem-drift lines."""

    def test_parse_bg_log_ignores_drift_lines(self):
        from storelib import miss_rate
        tmp = Path(tempfile.mkdtemp(prefix="zmem-drift107-mr-"))
        try:
            log = tmp / "zmem-bg.log"
            log.write_text(
                "[1700000000] zmem-hook status=injected reason=injected "
                "ids=['m1'] all=['m1'] sid=sess-a\n"
                "[1700000030] zmem-drift served=aaaaaaaa release=bbbbbbbb "
                "files=3\n",
                encoding="utf-8")
            parsed = miss_rate.parse_bg_log(log)
            self.assertEqual(len(parsed), 1)
            self.assertEqual(parsed[0].get("status"), "injected")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DriftImportSafetyTest(unittest.TestCase):
    """Importing drift.py must be side-effect-free (no store touch)."""

    def test_import_never_touches_store_paths(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-drift107-imp-"))
        try:
            canary_store = tmp / "canary" / "store.sqlite"
            code = (
                "import sys; sys.path.insert(0, %r); import drift; "
                "print('ok')" % str(SCRIPTS)
            )
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith("ZMEM")}
            env["ZMEM_STORE"] = str(canary_store)
            env["ZMEM_DATA"] = str(tmp / "canary")
            r = subprocess.run([PYTHON, "-c", code], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stdout.strip(), "ok")
            self.assertFalse(canary_store.exists(),
                             "importing drift must not create any store")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SessionStartDriftE2ETest(unittest.TestCase):
    """End to end: the real session-start hook against a fixture served tree.

    Fixture = real hooks/ + a real drift.py, no store.py (every store-touching
    block in the hook is isfile-guarded, so the hook degrades to the minimal
    payload path — exactly the kill-switch-like shape where ONLY the drift
    systemMessage rides)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-drift107-e2e-"))
        self.root = self.tmp / "served"
        self.root.mkdir()
        shutil.copytree(REPO_ROOT / "hooks", self.root / "hooks",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.root / "skills" / "memory" / "scripts").mkdir(parents=True)
        shutil.copy2(DRIFT_PY, self.root / "skills" / "memory" / "scripts" / "drift.py")
        self.data = self.tmp / "data"
        self.data.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_hook(self, sid: str, extra: dict | None = None) -> tuple:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("no bash on PATH")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM") and k not in (
                   "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
                   "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")}
        env.update({
            "ZMEM_ROOT": str(self.root),
            "ZMEM_DATA": str(self.data),
            "ZMEM_HOST": "zcode",
            "ZMEM_SESSION": sid,
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONUTF8": "1",
        })
        env.update(extra or {})
        r = subprocess.run(
            [bash, str(self.root / "hooks" / "zmem-session-start.sh")],
            capture_output=True, text=True, env=env, timeout=120)
        return r

    @staticmethod
    def _sentinel_payload(stdout: str) -> dict:
        start = "<<<ZMEM_JSON>>>"
        end = "<<<END>>>"
        i = stdout.rfind(end)
        if i < 0:
            return {}
        j = stdout.rfind(start, 0, i)
        if j < 0:
            return {}
        try:
            return json.loads(stdout[j + len(start):i])
        except ValueError:
            return {}

    def _drift_fixture(self) -> None:
        _write_manifest(self.root)
        (self.root / "hooks" / "zmem-recall.sh").write_bytes(b"#!/bin/sh\n")

    def test_drift_line_once_and_system_message(self):
        """AC3 + addendum: line exactly once per sid; systemMessage rides the
        sentinel payload; additionalContext never carries drift text."""
        self._drift_fixture()
        r1 = self._run_hook("sess-a")
        self.assertEqual(r1.returncode, 0, r1.stderr[-800:])
        payload = self._sentinel_payload(r1.stdout)
        self.assertNotEqual(payload, {}, "sentinel payload missing")
        self.assertIn("systemMessage", payload)
        self.assertIn("zmem", payload["systemMessage"])
        self.assertNotIn("zmem-drift", payload.get("additionalContext") or "")
        r2 = self._run_hook("sess-a")
        self.assertEqual(r2.returncode, 0, r2.stderr[-800:])
        self.assertEqual(len(_drift_lines(self.data)), 1,
                         "drift line must be written at most once per session")

    def test_matched_tree_no_line_no_message(self):
        _write_manifest(self.root)
        r = self._run_hook("sess-a")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertEqual(_drift_lines(self.data), [])
        self.assertNotIn("systemMessage", self._sentinel_payload(r.stdout))

    def test_no_manifest_unknown_no_line_exit_zero(self):
        """AC2 end to end: missing manifest never blocks the hook."""
        r = self._run_hook("sess-a")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertEqual(_drift_lines(self.data), [])
        self.assertNotIn("systemMessage", self._sentinel_payload(r.stdout))

    def test_kill_switch_still_reports_drift_to_operator(self):
        """ZMEM_INJECT=0: no model context, but the drift line and the
        operator systemMessage still fire (drift runs BEFORE the kill
        switch)."""
        self._drift_fixture()
        r = self._run_hook("sess-a", {"ZMEM_INJECT": "0"})
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertEqual(len(_drift_lines(self.data)), 1)
        payload = self._sentinel_payload(r.stdout)
        self.assertIn("systemMessage", payload)
        self.assertNotIn("additionalContext", payload)

    def test_tree_without_drift_py_exits_clean(self):
        """A pre-0.17 served tree (no drift.py) must behave exactly as today."""
        (self.root / "skills" / "memory" / "scripts" / "drift.py").unlink()
        r = self._run_hook("sess-a")
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        self.assertEqual(_drift_lines(self.data), [])
        self.assertNotIn("systemMessage", self._sentinel_payload(r.stdout))


class RecallBodyGlueTest(unittest.TestCase):
    """TF-2: the recall-body fallback glue (_maybe_log_drift) — marker
    honored without spawning, argv correctness, fail-open on spawn error.

    The module is loaded via importlib spec_from_file_location (the hyphenated
    filename cannot be imported; module level is constants + defs + a
    __main__ guard, so this is import-safe)."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zmem_recall_body_drift_glue",
            str(REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"))
        cls.body = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.body)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-drift107-glue-"))
        self.data = self.tmp / "data"
        self.data.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_marker_honored_without_spawn(self):
        import unittest.mock
        marker = drift._marker_path(self.data, "sess-a")
        marker.write_text("checked\n", encoding="utf-8")
        with unittest.mock.patch.object(
                self.body.subprocess, "run",
                side_effect=AssertionError("must not spawn")):
            self.body._maybe_log_drift("sess-a")

    def test_spawn_argv_and_env(self):
        import unittest.mock
        seen = {}
        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen["timeout"] = kwargs.get("timeout")
            class _R:
                returncode = 0
            return _R()
        with unittest.mock.patch.object(self.body.subprocess, "run",
                                        side_effect=fake_run):
            with unittest.mock.patch.dict(
                    os.environ, {"ZMEM_DATA": str(self.data),
                                 "ZMEM_STORE": str(self.data / "s.sqlite")}):
                self.body._maybe_log_drift("sess-glue")
        argv = seen["argv"]
        self.assertEqual(argv[1], str(DRIFT_PY))
        self.assertEqual(argv[2:4], ["log-once", "--data-dir"])
        self.assertEqual(argv[4], str(self.data))
        self.assertEqual(argv[5:7], ["--sid", "sess-glue"])
        self.assertEqual(seen["timeout"], 5)

    def test_spawn_failure_swallowed(self):
        import unittest.mock
        marker = drift._marker_path(self.data, "sess-a")
        with unittest.mock.patch.object(
                self.body.subprocess, "run",
                side_effect=OSError("boom")):
            with unittest.mock.patch.dict(
                    os.environ, {"ZMEM_DATA": str(self.data)}):
                # Must not raise.
                self.body._maybe_log_drift("sess-a")
        self.assertFalse(marker.exists())


class SweepPrefixPinTest(unittest.TestCase):
    """RP-3: the session sweep must reap drift markers (unbounded-growth
    guard). Pinned via a subprocess so the storelib import never resolves a
    store from this test process's env."""

    def test_drift_markers_in_sweep_prefixes(self):
        code = (
            "import sys; sys.path.insert(0, %r); "
            "from storelib.backup import SENTINEL_PREFIXES; "
            "assert '.drift-checked-' in SENTINEL_PREFIXES, SENTINEL_PREFIXES"
        ) % str(SCRIPTS)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM")}
        env["ZMEM_STORE"] = str(Path(tempfile.mkdtemp()) / "s.sqlite")
        r = subprocess.run([PYTHON, "-c", code], env=env,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
