"""AC3 (#414-3): dataset export/import must round-trip memory_expiry
(frozen acceptance check C3).

Contract this check pins (issue #414 item 3): ``store.py export-dataset``
emits a ``memory_expiry`` family and ``store.py import-dataset`` restores
those records onto LIVE STATE PARENTS ONLY, dest-wins.  Legs (real CLI
subprocess round-trip, per the executed probe):
1. a seeded live state row + its memory_expiry record round-trip: the
   imported snapshot has memory>=1 AND memory_expiry>=1.  At base the
   export has no expiry family -> memory_expiry==0 -> RED.
2. parent-outcome-aware: the state row is tombstoned in the source
   (``store.py invalidate``), exported and imported with
   --include-tombstones; the tombstoned parent is present in the snapshot
   but gains NO expiry record.
3. dest-wins is not reachable through the documented import seam (import
   builds an ISOLATED snapshot via .tmp + atomic replace), so the
   reachable fallback is pinned: re-importing the same dataset twice
   yields IDENTICAL expiry rows (idempotent; existing record wins).
4. absence tolerance: an export whose data dir carries NO expiry family
   file must still import (older producers); once the fix ships the family
   file, simulating a verifying older-producer manifest requires the
   plan's family/hash seam -> LEG-REQUIRES-PLAN-SEAM skip.

Subprocess-only: no storelib import in this process, so the operator
store is never contactable; every leg pins ZMEM_STORE/ZMEM_DATA to a
fresh temp dir in the CHILD env.  No network; no model files.  Runs
standalone (CI convention): ``python -X utf8 tests/test_dataset_expiry.py``
"""

from __future__ import annotations
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable
FUTURE = "2099-01-01T00:00:00Z"
_UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{12})")


def _child_env(tmp: str) -> dict:
    env = {**os.environ}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "no-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    for k in ("ZMEM_BACKUP_DIR", "ZMEM_HOME", "CLAUDE_PLUGIN_DATA",
              "ZCODE_PLUGIN_DATA", "ZMEM_MCP_TOKENS_FILE"):
        env.pop(k, None)
    return env


def _run(env, *args):
    proc = subprocess.run([PYTHON, "-X", "utf8", str(STORE_PY), *args],
                          env=env, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise AssertionError(
            f"CLI harness failure: store.py {args[:2]} exited "
            f"{proc.returncode}: stdout={proc.stdout[-300:]!r} "
            f"stderr={proc.stderr[-300:]!r}")
    return proc.stdout


class _SeededStore:
    """One hermetic CLI store holding ONE live state row + expiry record."""

    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-c3-")
        self.env = _child_env(self.tmp)
        _run(self.env, "init")
        out = _run(self.env, "add", "--namespace", "user:global",
                   "--type", "state",
                   "--content", "c3 check state: endpoint api-c3.example.com",
                   "--tags", "c3", "--source", "c3", "--signal", "test",
                   "--expiry-class", "endpoint", "--inject-until", FUTURE)
        match = _UUID_RE.search(out)
        if match:
            self.state_id = match.group(1)
        else:  # fallback: resolve the single state row from the store
            conn = sqlite3.connect(self.env["ZMEM_STORE"])
            row = conn.execute(
                "SELECT id FROM memory WHERE type='state'").fetchone()
            conn.close()
            assert row, "state row not seeded"
            self.state_id = row[0]

    def expiry_rows(self, db_path=None):
        conn = sqlite3.connect(db_path or self.env["ZMEM_STORE"])
        try:
            return set(conn.execute("SELECT memory_id, expiry_class, "
                                    "inject_until FROM memory_expiry")
                       .fetchall())
        finally:
            conn.close()

    def export(self, out_dir, *extra):
        _run(self.env, "export-dataset", out_dir,
             "--all-namespaces", "--yes", *extra)
        return json.loads((Path(out_dir) / "manifest.json")
                          .read_text(encoding="utf-8"))

    def import_snapshot(self, export_dir, revision, dest_dir, *extra):
        _run(self.env, "import-dataset", export_dir, "--revision", revision,
             "--dest", dest_dir, *extra)
        snap = Path(dest_dir) / "snapshot.sqlite"
        assert snap.is_file(), f"imported snapshot missing: {snap}"
        return snap

    def close(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def _snapshot_census(snap: Path):
    conn = sqlite3.connect(str(snap))
    try:
        return (conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM memory_expiry")
                .fetchone()[0])
    finally:
        conn.close()


class DatasetExpiryRoundTripTest(unittest.TestCase):
    """export-dataset/import-dataset must carry memory_expiry."""

    def setUp(self):
        self.store = _SeededStore()
        self.addCleanup(self.store.close)
        assert self.store.expiry_rows(), "seed must produce a record"

    def test_leg1_expiry_records_round_trip(self):
        export_dir = os.path.join(self.store.tmp, "export")
        manifest = self.store.export(export_dir)
        snap = self.store.import_snapshot(
            export_dir, manifest["source_snapshot_hash"],
            os.path.join(self.store.tmp, "imported"))
        memory, expiry = _snapshot_census(snap)
        self.assertGreaterEqual(
            memory, 1, "leg 1: the state row itself must round-trip")
        self.assertGreaterEqual(
            expiry, 1,
            "leg 1: the state row round-tripped but its memory_expiry "
            f"record did not (imported snapshot {snap}: memory={memory}, "
            f"memory_expiry={expiry}) — the re-landed row is ungated and "
            "un-updatable")

    def test_leg2_tombstoned_parent_gains_no_record(self):
        _run(self.store.env, "invalidate", "--id", self.store.state_id,
             "--reason", "fact retired by c3 leg 2")
        export_dir = os.path.join(self.store.tmp, "export-audit")
        manifest = self.store.export(export_dir, "--include-tombstones")
        snap = self.store.import_snapshot(
            export_dir, manifest["source_snapshot_hash"],
            os.path.join(self.store.tmp, "imported-audit"),
            "--include-tombstones")
        conn = sqlite3.connect(str(snap))
        try:
            sid = self.store.state_id
            parent = conn.execute(
                "SELECT superseded_at FROM memory WHERE id=?",
                (sid,)).fetchone()
            records = conn.execute(
                "SELECT COUNT(*) FROM memory_expiry WHERE memory_id=?",
                (sid,)).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(
            parent, "leg 2: the tombstoned parent must be present in the "
            "audit import (otherwise the leg asserts nothing)")
        self.assertEqual(
            records, 0,
            "leg 2: a TOMBSTONED parent gained an expiry record on import "
            "— records restore onto LIVE STATE PARENTS ONLY")

    def test_leg3_reimport_is_idempotent(self):
        export_dir = os.path.join(self.store.tmp, "export")
        manifest = self.store.export(export_dir)
        dest = os.path.join(self.store.tmp, "imported-twice")
        rev = manifest["source_snapshot_hash"]
        rows_first = self.store.expiry_rows(
            self.store.import_snapshot(export_dir, rev, dest))
        rows_second = self.store.expiry_rows(
            self.store.import_snapshot(export_dir, rev, dest))
        self.assertEqual(
            rows_first, rows_second,
            "leg 3: re-importing the same dataset must yield IDENTICAL "
            "expiry rows (idempotent; an existing record wins)")

    def test_leg4_absence_tolerance_family_file_optional(self):
        export_dir = os.path.join(self.store.tmp, "export-legacy")
        manifest = self.store.export(export_dir)
        data_dir = Path(export_dir) / "data"
        names = [p.name.lower() for p in data_dir.iterdir()] \
            if data_dir.is_dir() else []
        if any("expiry" in n for n in names):
            raise unittest.SkipTest(
                "LEG-REQUIRES-PLAN-SEAM: the export now carries an expiry "
                "family file; simulating an older producer (family file "
                "absent AND manifest still verifying) requires the plan's "
                "family/hash seam")
        snap = self.store.import_snapshot(
            export_dir, manifest["source_snapshot_hash"],
            os.path.join(self.store.tmp, "imported-legacy"))
        memory, _ = _snapshot_census(snap)
        self.assertGreaterEqual(
            memory, 1,
            "leg 4: an export WITHOUT an expiry family must still import")


if __name__ == "__main__":
    unittest.main(verbosity=2)
