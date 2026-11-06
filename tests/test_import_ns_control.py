"""Check C2 — issue #443 AC2: dataset import refuses control-character
namespaces; the import-store hindsight refusal is preserved; clean imports
still succeed.

Law under test: `store.py import-dataset` bulk-loads memory rows into an
isolated snapshot with raw SQL (dataset.py:import_dataset) — it never passes
through the writer's `_validate_namespace`, so a crafted dataset can plant a
namespace containing a control character (TAB / DEL). The writer path
(storelib/write.py `namespace_control_chars`) and the hindsight importer
already refuse that class; the dataset import must refuse it too:

- DISCRIMINATING (fails at base, verified): `import-dataset` over a
  checksum-consistent jsonl dataset whose single memory row carries
  namespace "user:global<TAB>injected" (and, separately,
  "user:global<DEL>injected") must EXIT NONZERO, name the refusal and the
  control character on stderr, and leave no row with that namespace in the
  destination snapshot. At base the import succeeds (exit 0) and the row
  lands verbatim — the discriminating legs fail there.
- DISCRIMINATING (fails at base, verified): a dataset mixing ONE clean
  "user:global" row with ONE "user:global<TAB>injected" row must be refused
  ATOMICALLY — nonzero exit AND a destination snapshot that either does not
  exist or holds ZERO memory rows (the clean row must not land beside the
  refused one). At base both rows land — both legs fail there.
- PRESERVING (passes at base, pinned): (i) a CLEAN namespace row imports
  fine; (ii) `import-store.py --source hindsight` over a JSONL line with a
  TAB namespace exits nonzero with 'control character' in stderr (this
  already holds at main).

The dataset fixture is built IN-PROCESS via storelib.dataset (row_checksum /
_compute_snapshot_hash / _write-family machinery) inside a module-scope
scratch env pinned BEFORE any storelib import, exactly the
tests/test_dataset_expiry_supp.py discipline; every store-touching drive is
the REAL CLI via subprocess.

Run: python -X utf8 tests/test_import_ns_control.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
IMPORT_STORE_PY = SCRIPTS_DIR / "import-store.py"
PYTHON = sys.executable

TAB_NS = "user:global\tinjected"
DEL_NS = "user:global\x7finjected"
TRAILING_TAB_NS = "user:global\t"
LEADING_TAB_NS = "\tuser:global"
CLEAN_NS = "user:global"

# -- env pinned BEFORE any storelib import AND before the issue-#244 store
# isolation guard loads (the guard reads the ambient env at import time).
_TMP = tempfile.mkdtemp(prefix="zmem-c2-")
os.environ.update({
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_BACKUP_DIR", "ZMEM_HOME", "CLAUDE_PLUGIN_DATA",
           "ZCODE_PLUGIN_DATA", "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
           "ZMEM_SESSION", "ZMEM_SESSION_ID"):
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
_PINNED_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD",
                "PYTHONIOENCODING", "ZMEM_BACKUP_DIR", "ZMEM_HOME",
                "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
                "ZMEM_SESSION", "ZMEM_SESSION_ID")
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
    env = {k: v for k, v in os.environ.items()
           if k not in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME",
                        "ZMEM_BACKUP_DIR", "CLAUDE_PLUGIN_DATA",
                        "ZCODE_PLUGIN_DATA", "ZCODE_PROJECT_DIR",
                        "CLAUDE_PROJECT_DIR", "ZMEM_SESSION",
                        "ZMEM_SESSION_ID")}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    return env


def _build_dataset(out_dir: Path, *, namespace: str, memory_id: str) -> str:
    """Write a checksum-consistent single-row jsonl dataset; return its
    source_snapshot_hash (built with storelib.dataset's own functions)."""
    row = _memory_row(namespace=namespace, memory_id=memory_id)
    families = {family: [] for family in ds.DATASET_FAMILIES}
    families["memories"] = [row]
    return _write_dataset(out_dir, families, namespaces=[namespace])


def _memory_row(*, namespace: str, memory_id: str) -> dict:
    row = {col: None for col in ds.MEMORY_COLUMNS}
    row.update({
        "id": memory_id,
        "namespace": namespace,
        "type": "fact",
        "content": f"c2 probe row for namespace {namespace!r}",
        "tags": "",
        "source_ref": "session:c2",
        "source_hash": "",
        "confidence": 0.9,
        "signal": "none",
        "valid_from": "",
        "valid_until": "",
        "superseded_at": None,
        "supersede_reason": None,
        "update_of": "",
        "merged_from": None,
        "applied_count": 0,
        "violated_count": 0,
        "retrieval_count": 0,
        "surfaced_count": 0,
        "ingestion_ts": "2026-11-06T00:00:00Z",
        "last_retrieved": None,
        "last_surfaced": None,
        "taint": "trusted_internal",
        "trust_score": 1.0,
    })
    row["row_checksum"] = ds.row_checksum(row)
    return row


def _episode_row(*, namespace: str, episode_id: str) -> dict:
    """One episodes-family record over storelib.dataset's EPISODE_COLUMNS
    (import requires id + namespace present and the id UUID-shaped; the
    remaining columns travel verbatim into the snapshot's episode table)."""
    row = {col: None for col in ds.EPISODE_COLUMNS}
    row.update({
        "id": episode_id,
        "namespace": namespace,
        "started_at": "2026-11-06T00:00:00Z",
        "ended_at": "2026-11-06T00:10:00Z",
        "summary_memory_id": "",
        "token_count": 0,
    })
    row["row_checksum"] = ds.row_checksum(row)
    return row


def _write_dataset(out_dir: Path, families: dict,
                   *, namespaces: list) -> str:
    """Stamp checksums are already set per row; compute the snapshot hash,
    write every family file (jsonl) and the manifest; return the hash."""
    snap = ds._compute_snapshot_hash(families, True)
    (out_dir / "data").mkdir(parents=True, exist_ok=True)
    for family, rows in families.items():
        stem = ds._FAMILY_FILES[family]
        with open(out_dir / "data" / f"{stem}.jsonl", "wb") as fh:
            for r in rows:
                fh.write(ds.canonical_row_bytes(r))
    manifest = {
        "schema_version": ds.DATASET_SCHEMA_VERSION,
        "format": ds.DATASET_FORMAT_JSONL,
        "source_snapshot_hash": snap,
        "row_counts": {family: len(rows)
                       for family, rows in families.items()},
        "namespaces": namespaces,
    }
    (out_dir / "manifest.json").write_bytes(ds.canonical_row_bytes(manifest))
    return snap


class ImportNamespaceControlBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-c2-leg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.env = _child_env(str(self.tmp))

    def _run_cli(self, argv, timeout=180):
        return subprocess.run([PYTHON, "-X", "utf8", *argv],
                              env=self.env, capture_output=True, text=True,
                              timeout=timeout)

    def _import_dataset(self, ds_dir: Path, revision: str, dest: Path):
        return self._run_cli(
            [str(STORE_PY), "import-dataset", str(ds_dir),
             "--revision", revision, "--dest", str(dest)])


class ImportNamespaceControlRefusalTest(ImportNamespaceControlBase):
    """DISCRIMINATING — at base the imports succeed and the rows land."""

    def _assert_refused(self, namespace: str, memory_id: str):
        ds_dir = self.tmp / f"ds-{memory_id[:8]}"
        revision = _build_dataset(ds_dir, namespace=namespace,
                                  memory_id=memory_id)
        dest = self.tmp / f"dest-{memory_id[:8]}"
        r = self._import_dataset(ds_dir, revision, dest)
        self.assertNotEqual(
            r.returncode, 0,
            f"C2: import-dataset must EXIT NONZERO for the "
            f"control-character namespace {namespace!r} (writer path and "
            f"hindsight importer already refuse this class; the dataset "
            f"bulk load must not become the bypass). Observed rc=0 — the "
            f"row landed. stdout:\n{r.stdout[-800:]!r}\n"
            f"stderr:\n{r.stderr[-800:]!r}")
        lowered = r.stderr.lower()
        self.assertIn(
            "control", lowered,
            f"C2: the refusal must name the control character. Observed "
            f"rc={r.returncode}, stderr:\n{r.stderr[-800:]!r}")
        self.assertRegex(
            lowered, r"refus",
            f"C2: the refusal must be worded as a refusal. Observed "
            f"rc={r.returncode}, stderr:\n{r.stderr[-800:]!r}")
        snap = dest / "snapshot.sqlite"
        if snap.is_file():
            conn = sqlite3.connect(str(snap))
            try:
                planted = conn.execute(
                    "SELECT COUNT(*) FROM memory WHERE namespace=?",
                    (namespace,)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(
                planted, 0,
                f"C2: a refused import left {planted} row(s) carrying the "
                f"control-character namespace {namespace!r} in "
                f"{snap}.")
        # else: nothing was materialized at all — the fail-closed shape.

    def test_refuses_tab_namespace(self):
        self._assert_refused(
            TAB_NS, "dddd4444-5555-4666-8777-88889999000a")

    def test_refuses_del_namespace(self):
        self._assert_refused(
            DEL_NS, "dddd4444-5555-4666-8777-88889999000b")

    def test_refuses_trailing_tab_namespace(self):
        # Boundary: the control character is a trailing TAB. The validation
        # must judge the ORIGINAL string, not a stripped form — at base this
        # namespace is ADMITTED (no namespace validation exists on the
        # dataset bulk load), which is exactly the gap this check pins.
        self._assert_refused(
            TRAILING_TAB_NS, "dddd4444-5555-4666-8777-88889999000d")

    def test_refuses_leading_tab_namespace(self):
        # Boundary: leading TAB — same original-string contract.
        self._assert_refused(
            LEADING_TAB_NS, "dddd4444-5555-4666-8777-88889999000e")

    def test_refuses_tab_namespace_in_episodes_family(self):
        # The refusal is not memories-only: an EPISODES record carrying a
        # tab namespace must refuse the WHOLE import. At base the episode
        # lands verbatim in the snapshot's episode table.
        namespace = TAB_NS
        ds_dir = self.tmp / "ds-episodes"
        families = {family: [] for family in ds.DATASET_FAMILIES}
        families["episodes"] = [_episode_row(
            namespace=namespace,
            episode_id="dddd4444-5555-4666-8777-88889999000f")]
        revision = _write_dataset(ds_dir, families, namespaces=[namespace])
        dest = self.tmp / "dest-episodes"
        r = self._import_dataset(ds_dir, revision, dest)
        self.assertNotEqual(
            r.returncode, 0,
            f"C2: import-dataset must EXIT NONZERO when an episodes-family "
            f"record carries the control-character namespace {namespace!r} "
            f"(the family lands in the snapshot's episode table verbatim at "
            f"base). Observed rc=0. stdout:\n{r.stdout[-800:]!r}\n"
            f"stderr:\n{r.stderr[-800:]!r}")
        self.assertIn(
            "control", r.stderr.lower(),
            f"C2: the episodes refusal must name the control character. "
            f"Observed rc={r.returncode}, stderr:\n{r.stderr[-800:]!r}")
        snap = dest / "snapshot.sqlite"
        if snap.is_file():
            conn = sqlite3.connect(str(snap))
            try:
                planted = conn.execute(
                    "SELECT COUNT(*) FROM episode WHERE namespace=?",
                    (namespace,)).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(
                planted, 0,
                f"C2: a refused import left {planted} episode row(s) "
                f"carrying the control-character namespace {namespace!r} "
                f"in {snap}.")

    def test_mixed_clean_and_malicious_snapshot_refused_atomically(self):
        # Atomicity: ONE clean "user:global" row and ONE TAB-namespace row
        # in the SAME dataset. The refusal must reject the WHOLE import —
        # the clean row must NOT land alongside the refused one (no partial
        # import). At base the import succeeds (rc 0) and BOTH rows land,
        # so both assertions below fail (discriminating).
        ds_dir = self.tmp / "ds-mixed"
        families = {family: [] for family in ds.DATASET_FAMILIES}
        families["memories"] = [
            _memory_row(namespace=CLEAN_NS,
                        memory_id="dddd4444-5555-4666-8777-888899990010"),
            _memory_row(namespace=TAB_NS,
                        memory_id="dddd4444-5555-4666-8777-888899990011"),
        ]
        revision = _write_dataset(ds_dir, families,
                                  namespaces=[CLEAN_NS, TAB_NS])
        dest = self.tmp / "dest-mixed"
        r = self._import_dataset(ds_dir, revision, dest)
        self.assertNotEqual(
            r.returncode, 0,
            f"C2: import-dataset must EXIT NONZERO for a dataset that mixes "
            f"a clean namespace row with a control-character namespace row "
            f"({TAB_NS!r}) — importing the clean row while admitting the "
            f"malicious one is the partial-import bypass. Observed rc=0. "
            f"stdout:\n{r.stdout[-800:]!r}\nstderr:\n{r.stderr[-800:]!r}")
        snap = dest / "snapshot.sqlite"
        landed = None
        if snap.is_file():
            conn = sqlite3.connect(str(snap))
            try:
                landed = conn.execute(
                    "SELECT COUNT(*) FROM memory").fetchone()[0]
            finally:
                conn.close()
        self.assertTrue(
            not snap.is_file() or landed == 0,
            f"C2: a refused mixed import must be ATOMIC — the destination "
            f"snapshot must not exist or must hold ZERO memory rows; the "
            f"clean row must not land while the malicious row is refused. "
            f"Observed {landed} memory row(s) in {snap}. "
            f"stdout:\n{r.stdout[-500:]!r}")


class CleanImportPreservedTest(ImportNamespaceControlBase):
    """PRESERVING — passes at base and must keep passing post-fix."""

    def test_clean_namespace_imports(self):
        memory_id = "dddd4444-5555-4666-8777-88889999000c"
        ds_dir = self.tmp / "ds-clean"
        revision = _build_dataset(ds_dir, namespace=CLEAN_NS,
                                  memory_id=memory_id)
        dest = self.tmp / "dest-clean"
        r = self._import_dataset(ds_dir, revision, dest)
        self.assertEqual(
            r.returncode, 0,
            f"C2 preserving: a CLEAN namespace row must still import. "
            f"Observed rc={r.returncode}\nstdout:\n{r.stdout[-800:]!r}\n"
            f"stderr:\n{r.stderr[-800:]!r}")
        snap = dest / "snapshot.sqlite"
        self.assertTrue(snap.is_file(),
                        f"C2 preserving: imported snapshot missing at {snap};"
                        f" stdout:\n{r.stdout[-500:]!r}")
        conn = sqlite3.connect(str(snap))
        try:
            rows = conn.execute(
                "SELECT id, namespace FROM memory").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [(memory_id, CLEAN_NS)],
                         f"C2 preserving: expected exactly the seeded clean "
                         f"row in the snapshot; observed {rows!r}")

    def test_import_store_hindsight_control_character_refused(self):
        """Pin the refusal that already holds at main (issue #443 AC2)."""
        mid = "cccc3333-4444-4555-8666-777788889900"
        jsonl = self.tmp / "hindsight.jsonl"
        jsonl.write_text(
            json.dumps({"id": mid, "kind": "kv",
                        "text": "c2 hindsight control-char probe",
                        "namespace": TAB_NS}) + "\n",
            encoding="utf-8")
        dest = self.tmp / "hindsight-dest"
        r = self._run_cli([str(IMPORT_STORE_PY), "--source", "hindsight",
                           "--input", str(jsonl), "--dest-dir", str(dest)])
        self.assertNotEqual(
            r.returncode, 0,
            f"C2 preserving: import-store hindsight must keep refusing the "
            f"control-character namespace (regression). Observed rc=0. "
            f"stdout:\n{r.stdout[-500:]!r}\nstderr:\n{r.stderr[-500:]!r}")
        self.assertIn(
            "control character", r.stderr,
            f"C2 preserving: refusal must mention 'control character'. "
            f"Observed stderr:\n{r.stderr[-500:]!r}")
        self.assertFalse(
            (dest / "store.sqlite").is_file(),
            f"C2 preserving: a refused hindsight import must not publish a "
            f"destination store; observed files: "
            f"{sorted(p.name for p in dest.iterdir()) if dest.is_dir() else []}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
