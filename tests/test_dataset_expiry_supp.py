"""SUPPLEMENT to AC3 (#414-3): the dataset expiry family's tamper-consistent
and legacy shapes (trace 414-416 supplement).

The frozen C3 pins the plain round-trip; this supplement pins the two
shapes C3 cannot reach:

- (a) NON-VACUOUS parent-outcome leg: a real export of a seeded store is
  doctored — via the module's OWN functions only — to carry an EXTRA
  ``memory_expiry`` record for a TOMBSTONED state parent (correct
  ``row_checksum``, family rewritten via ``_write_family``, manifest
  re-stamped by recomputing ``_compute_snapshot_hash`` over the five
  families read back).  The doctored-but-consistent artifact therefore
  VERIFIES at import — and the tombstoned parent's record must STILL not
  land (#324 parent-outcome rule), while the live row's round-trips.
- (b) committed-fixture absence tolerance: the manifest-v1 fixture
  (tests/fixtures/dataset, the tests/test_dataset.py:600-626 discipline)
  predates the family — it carries NO memory_expiry file; importing it
  via the real CLI must succeed with ZERO memory_expiry rows.

Isolation: env pinned to a per-run temp store BEFORE any storelib import;
sys.modules purged; storelib.dataset imported IN-PROCESS (the doctoring
legs need its functions) while every store touch runs through the real
store.py CLI subprocess (the frozen C3 discipline).  Standalone:
``python -X utf8 tests/test_dataset_expiry_supp.py``
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
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "dataset"
PYTHON = sys.executable
FUTURE = "2099-01-01T00:00:00Z"
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                     r"[0-9a-f]{12})")

# -- env pinned BEFORE any storelib import AND before the issue-#244 store
# isolation guard loads (the guard reads the ambient env at import time).
_TMP = tempfile.mkdtemp(prefix="zmem-c3s-")
os.environ.update({
    "ZMEM_HOME": str(REPO_ROOT),
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODELS_DIR": os.path.join(_TMP, "no-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_BACKUP_DIR", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
           "ZMEM_MCP_TOKENS_FILE"):
    os.environ.pop(_k, None)
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

_SAVED_MODULES = {key: value for key, value in list(sys.modules.items())
                  if key == "storelib" or key.startswith("storelib.")}
for _key in _SAVED_MODULES:
    del sys.modules[_key]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
    _PATH_INSERTED = True
else:
    _PATH_INSERTED = False
_PINNED_KEYS = ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING",
                "ZMEM_BACKUP_DIR", "CLAUDE_PLUGIN_DATA",
                "ZCODE_PLUGIN_DATA", "ZMEM_MCP_TOKENS_FILE")
_SAVED_ENV = {key: os.environ.get(key) for key in _PINNED_KEYS}

from storelib import dataset as ds  # noqa: E402


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the ambient env / module cache (pytest co-run citizenship)."""
    for key in [key for key in list(sys.modules)
                if key == "storelib" or key.startswith("storelib.")]:
        del sys.modules[key]
    sys.modules.update(_SAVED_MODULES)
    if _PATH_INSERTED:
        try:
            sys.path.remove(str(SCRIPTS_DIR))
        except ValueError:  # pragma: no cover - defensive
            pass
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(_TMP, ignore_errors=True)


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
                          env=env, capture_output=True, text=True, timeout=180)
    if proc.returncode != 0:
        raise AssertionError(
            f"CLI harness failure: store.py {args[:2]} exited "
            f"{proc.returncode}: stdout={proc.stdout[-300:]!r} "
            f"stderr={proc.stderr[-300:]!r}")
    return proc.stdout


def _add_state(env, content, until=FUTURE, cls="endpoint"):
    out = _run(env, "add", "--namespace", "user:global", "--type", "state",
               "--content", content, "--tags", "c3s", "--source", "c3s",
               "--signal", "test", "--expiry-class", cls,
               "--inject-until", until)
    match = UUID_RE.search(out)
    assert match, f"add produced no id: {out!r}"
    return match.group(1)


def _expiry_rows(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return set(conn.execute(
            "SELECT memory_id, inject_until, expiry_class "
            "FROM memory_expiry").fetchall())
    finally:
        conn.close()


class DatasetExpirySupplementTest(unittest.TestCase):
    """The doctored-but-consistent and family-absent dataset shapes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-c3s-leg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = _child_env(self.tmp)
        _run(self.env, "init")

    def _export(self, out_dir, *extra):
        _run(self.env, "export-dataset", out_dir,
             "--all-namespaces", "--yes", *extra)
        manifest = json.loads(
            (Path(out_dir) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["format"], "parquet")
        return manifest

    def test_leg_e_import_refuses_invalid_expiry_values(self):
        """F4 (second review): a checksum-consistent dataset carrying a
        NON-ISO inject_until (or an unknown expiry_class) must be refused
        with the designed DatasetError at import — never silently dropped
        (a non-ISO deadline parses to epoch 0.0 = permanent withhold) and
        never stored raw. Same craft machinery as leg a."""
        live_id = _add_state(
            self.env, "c3s leg-e live state: endpoint api-e.example.com")
        export_dir = os.path.join(self.tmp, "export-e")
        manifest = self._export(export_dir)
        fmt = manifest["format"]
        families = ds._read_export_families(
            Path(export_dir), fmt, ds.DatasetError)
        base = dict(families["memory_expiry"][0])
        assert base["memory_id"] == live_id, base

        for name, mutate in (
                ("non-iso inject_until",
                 lambda r: r.update(inject_until="not-a-timestamp")),
                ("unknown expiry_class",
                 lambda r: r.update(expiry_class="not-a-class")),
                ("non-string inject_until",
                 lambda r: r.update(inject_until=None))):
            row = dict(base)
            mutate(row)
            row["row_checksum"] = ds.row_checksum(row)
            families["memory_expiry"] = [row]
            ds._write_family(Path(export_dir), "memory_expiry",
                             [row], fmt)
            reread = ds._read_export_families(
                Path(export_dir), fmt, ds.DatasetError)
            new_hash = ds._compute_snapshot_hash(reread, True)
            m2 = dict(manifest)
            m2["source_snapshot_hash"] = new_hash
            m2["export_snapshot_id"] = new_hash
            (Path(export_dir) / "manifest.json").write_bytes(
                ds.canonical_row_bytes(m2))
            dest = os.path.join(self.tmp, "imported-e-" + name[:6])
            proc = _run.__globals__  # noqa: F841 - noqa silencer
            r = subprocess.run(
                [sys.executable, "-X", "utf8",
                 str(REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"),
                 "import-dataset", export_dir, "--revision", new_hash,
                 "--dest", dest],
                env=self.env, capture_output=True, text=True, timeout=120)
            self.assertNotEqual(
                r.returncode, 0,
                f"leg e ({name}): the import must FAIL on a malformed "
                f"expiry record; stdout={r.stdout[-200:]}")
            self.assertIn(
                "malformed memory_expiry record", r.stderr,
                f"leg e ({name}): the refusal must name the malformed "
                f"record; stderr={r.stderr[-300:]}")

    def test_leg_a_doctored_record_for_tombstoned_parent_not_restored(self):
        live_id = _add_state(
            self.env, "c3s live state: endpoint api-c3s-live.example.com")
        dead_id = _add_state(
            self.env, "c3s doomed state: endpoint api-c3s-doomed.example.com")
        _run(self.env, "invalidate", "--id", dead_id,
             "--reason", "fact retired by c3s supplement")
        # Sanity: the write path cleared the tombstoned parent's record —
        # the source carries ONLY the live row's record.
        self.assertEqual(
            _expiry_rows(self.env["ZMEM_STORE"]),
            {(live_id, FUTURE, "endpoint")},
            "seed sanity: invalidate must leave exactly the live record")

        export_dir = os.path.join(self.tmp, "export")
        manifest = self._export(export_dir, "--include-tombstones")
        fmt = manifest["format"]
        families = ds._read_export_families(
            Path(export_dir), fmt, ds.DatasetError)
        self.assertEqual(
            [tuple(r[c] for c in ds.EXPIRY_COLUMNS)
             for r in families["memory_expiry"]],
            [(live_id, FUTURE, "endpoint")],
            "leg a: the honest export carries exactly the live record")

        # -- doctor: fabricate a record for the TOMBSTONED state parent,
        # checksummed and re-stamped with the module's own functions so the
        # artifact still verifies end-to-end.
        fabricated = {"memory_id": dead_id, "inject_until": FUTURE,
                      "expiry_class": "metric"}
        fabricated["row_checksum"] = ds.row_checksum(fabricated)
        families["memory_expiry"].append(fabricated)
        ds._write_family(Path(export_dir), "memory_expiry",
                         families["memory_expiry"], fmt)
        reread = ds._read_export_families(
            Path(export_dir), fmt, ds.DatasetError)
        self.assertIn(
            (dead_id, FUTURE, "metric"),
            [tuple(r[c] for c in ds.EXPIRY_COLUMNS)
             for r in reread["memory_expiry"]],
            "leg a pre-condition: the input export must contain the "
            "doctored tombstoned-parent record (read back BEFORE import)")
        new_hash = ds._compute_snapshot_hash(reread, True)
        manifest["source_snapshot_hash"] = new_hash
        manifest["export_snapshot_id"] = new_hash
        manifest["row_counts"]["memory_expiry"] = \
            len(reread["memory_expiry"])
        (Path(export_dir) / "manifest.json").write_bytes(
            ds.canonical_row_bytes(manifest),)

        dest = os.path.join(self.tmp, "imported")
        _run(self.env, "import-dataset", export_dir,
             "--revision", new_hash, "--dest", dest,
             "--include-tombstones")
        snap = Path(dest) / "snapshot.sqlite"
        self.assertTrue(snap.is_file(), "imported snapshot missing")
        conn = sqlite3.connect(str(snap))
        try:
            dead_present = conn.execute(
                "SELECT superseded_at FROM memory WHERE id=?",
                (dead_id,)).fetchone()
            dead_records = conn.execute(
                "SELECT COUNT(*) FROM memory_expiry WHERE memory_id=?",
                (dead_id,)).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNotNone(
            dead_present,
            "leg a: the tombstoned parent must be IN the audit import "
            "(otherwise the leg asserts nothing)")
        self.assertIsNotNone(dead_present[0],
                             "leg a: the parent must actually be tombstoned")
        self.assertEqual(
            dead_records, 0,
            "leg a: a doctored-but-VERIFYING memory_expiry record for a "
            "TOMBSTONED state parent was restored — records land on LIVE "
            "STATE parents only")
        self.assertEqual(
            _expiry_rows(snap), {(live_id, FUTURE, "endpoint")},
            "leg a: the live row's record must round-trip and be the ONLY "
            "record in the imported snapshot")

    def test_leg_b_manifest_v1_fixture_imports_without_expiry_family(self):
        work = os.path.join(self.tmp, "fixture-dataset")
        shutil.copytree(FIXTURE_DIR, work)
        shutil.copy2(os.path.join(work, "manifest-v1.json"),
                     os.path.join(work, "manifest.json"))
        family_files = sorted(
            p.name for p in (Path(work) / "data").iterdir())
        self.assertNotIn(
            "memory_expiry-000.jsonl", family_files,
            "leg b pre-condition: the committed manifest-v1 fixture must "
            f"not carry the family (data dir: {family_files})")
        with open(os.path.join(work, "manifest.json"), "rb") as fh:
            revision = json.loads(
                fh.read().decode("utf-8"))["source_snapshot_hash"]
        dest = os.path.join(self.tmp, "fixture-snap")
        _run(self.env, "import-dataset", work, "--revision", revision,
             "--dest", dest)
        snap = Path(dest) / "snapshot.sqlite"
        self.assertTrue(snap.is_file(), "imported snapshot missing")
        conn = sqlite3.connect(str(snap))
        try:
            memories = conn.execute(
                "SELECT COUNT(*) FROM memory").fetchone()[0]
            expiry = conn.execute(
                "SELECT COUNT(*) FROM memory_expiry").fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(
            memories, 0,
            "leg b: the family-less legacy fixture must still import rows")
        self.assertEqual(
            expiry, 0,
            "leg b: a dataset without the memory_expiry family must import "
            "with ZERO expiry rows (absence tolerated, nothing invented)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
