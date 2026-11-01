"""Frozen acceptance checks C1-C3 (issue #353): memory id shape is a closed
set at EVERY writer.

- C1 SyncIngestIdShapeTest — storelib.sync._validate_sync_row must REJECT
  an id of 36 hex chars + newline. At base _INGEST_ID_RE.match() accepts it
  (Python's `$` matches before a trailing newline):
      AssertionError: the trailing-newline id was accepted at base
- C2 ImportStoreIdShapeTest — import-store.py's sqlite path (run_import)
  must REFUSE a source store whose memory.id is non-uuid. At base it is
  copied verbatim into the destination:
      AssertionError: the malformed id was copied at base
- C3 DatasetIdShapeTest — storelib.dataset.import_dataset must REFUSE a
  snapshot carrying a non-uuid memory id. At base it is inserted:
      AssertionError: the malformed id was inserted at base

The post-fix refusal SHAPE (exception vs counted refusal) is the fix's
choice: C2/C3 assert only that the bad id is absent from the destination
afterwards AND that the operation signaled rejection (raised, or reported
a refusal count > 0 / kept-count 0).

Hermetic discipline: every class pins ZMEM_STORE (a FILE path inside a
fresh mkdtemp dir), ZMEM_DATA, ZMEM_MODELS_DIR (nonexistent) and
ZMEM_MODEL_AUTODOWNLOAD=0 in setUpClass BEFORE the first storelib import
anywhere in the process (storelib freezes STORE_PATH at import), restoring
in tearDownClass. Fix-added surfaces are touched only inside test methods.

Run (the file is its own runner; flags like -v are ignored):
    python tests/test_followup_id_shape.py [ClassName]
    e.g. python tests/test_followup_id_shape.py SyncIngestIdShapeTest
Prints a final CHECK-PASS / CHECK-FAIL line.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

_HERMETIC_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                  "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_ALLOW_NEWER_SCHEMA")

BAD_ID = "bad id with spaces"


class _HermeticTestCase(unittest.TestCase):
    """Fresh hermetic env per class, pinned before any storelib import."""

    _tmpdir: str | None = None
    _saved_env: dict | None = None

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp(prefix=f"zmem-{cls.__name__}-")
        cls._saved_env = {k: os.environ.get(k) for k in _HERMETIC_KEYS}
        os.environ["ZMEM_STORE"] = str(Path(cls._tmpdir) / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(Path(cls._tmpdir) / "data")
        os.environ["ZMEM_MODELS_DIR"] = str(Path(cls._tmpdir) / "no-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ.pop("ZMEM_ALLOW_NEWER_SCHEMA", None)
        if str(SCRIPTS_DIR) not in sys.path:
            sys.path.insert(0, str(SCRIPTS_DIR))

    @classmethod
    def tearDownClass(cls):
        if cls._saved_env is not None:
            for k, v in cls._saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            cls._saved_env = None
        if cls._tmpdir is not None:
            shutil.rmtree(cls._tmpdir, ignore_errors=True)
            cls._tmpdir = None


class SyncIngestIdShapeTest(_HermeticTestCase):
    """C1: the ingest gate must reject a trailing-newline id."""

    def test_trailing_newline_id_is_rejected(self):
        from storelib import sync  # hermetic env pinned above (base symbol)

        row = {
            "id": "0" * 36 + "\n",          # 36 hex chars + LF: passes
            "namespace": "project:id-shape",  # re `$` but not fullmatch
            "content": "followup id-shape check row",
            "type": "fact",                 # plain ALLOWED_TYPES member,
        }                                  # never entity_card
        accepted = False
        try:
            sync._validate_sync_row(dict(row))
            accepted = True
        except ValueError:
            accepted = False                # the contract: rejected
        if accepted:
            raise AssertionError(
                "the trailing-newline id was accepted at base")


class ImportStoreIdShapeTest(_HermeticTestCase):
    """C2: import-store.py's sqlite path must refuse a non-uuid memory id."""

    @staticmethod
    def _build_source_store(source: Path) -> None:
        from storelib import schema as schema_mod

        conn = sqlite3.connect(str(source))
        try:
            schema_mod.init_db(conn)
            schema_mod.migrate(conn)
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, "
                "ingestion_ts, confidence, signal, taint, tags) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("f" * 8 + "-" + "f" * 4 + "-4" + "f" * 2 + "-8" + "f" * 2
                 + "-" + "f" * 12, "project:import-shape", "fact",
                 "benign alpha note", "2026-01-02T03:04:05Z", 0.5, "none",
                 "trusted_internal", ""))
            conn.commit()
            # Hand-shape the defect: a valid-schema store whose one memory
            # row carries a non-uuid id.
            conn.execute("UPDATE memory SET id=?", (BAD_ID,))
            conn.commit()
        finally:
            conn.close()

    def test_sqlite_import_refuses_non_uuid_id(self):
        src_dir = Path(self._tmpdir) / "source"
        src_dir.mkdir(parents=True, exist_ok=True)
        source = src_dir / "store.sqlite"
        self._build_source_store(source)

        dest_dir = Path(self._tmpdir) / "dest"

        # import-store.py lives beside storelib, not inside it — load it by
        # path (its module body resolves the store path, so the hermetic
        # env pin above is load-bearing here too).
        spec = importlib.util.spec_from_file_location(
            "zmem_import_store_followup", str(SCRIPTS_DIR / "import-store.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        raised: Exception | None = None
        result = None
        try:
            result = mod.run_import(source, dest_dir)
        except Exception as exc:  # refusal shape is the fix's choice
            raised = exc

        dest_store = dest_dir / "store.sqlite"
        bad_present = False
        if dest_store.exists():
            conn = sqlite3.connect(str(dest_store))
            try:
                bad_present = conn.execute(
                    "SELECT 1 FROM memory WHERE id=?", (BAD_ID,)
                ).fetchone() is not None
            finally:
                conn.close()
        refused = raised is not None or (
            isinstance(result, dict)
            and (result.get("quarantined", 0) > 0
                 or result.get("dest_total_rows", -1) == 0))
        print(f"[C2] raised={raised!r} result={result!r} "
              f"bad_present={bad_present}")
        if bad_present or not refused:
            raise AssertionError("the malformed id was copied at base")


class DatasetIdShapeTest(_HermeticTestCase):
    """C3: dataset bulk insert must refuse a non-uuid memory id."""

    def test_dataset_import_refuses_non_uuid_id(self):
        from storelib import dataset as ds

        row = {
            "id": BAD_ID, "namespace": "project:dataset-shape",
            "type": "fact", "content": "dataset id-shape check row",
            "tags": "", "source_ref": "", "source_hash": "",
            "confidence": 0.5, "signal": "none", "valid_from": "",
            "valid_until": "", "superseded_at": None,
            "supersede_reason": "", "update_of": "", "merged_from": None,
            "applied_count": 0, "violated_count": 0, "retrieval_count": 0,
            "surfaced_count": 0, "ingestion_ts": "2026-01-02T03:04:05Z",
            "last_retrieved": None, "last_surfaced": None,
            "taint": "trusted_internal", "trust_score": 1.0,
        }
        families = {"memories": [row], "episodes": [],
                    "episode_members": [], "links": []}
        source_hash = ds._compute_snapshot_hash(families, False)
        row["export_snapshot_id"] = source_hash
        row["generator_revision"] = ds.GENERATOR_REVISION
        row["row_checksum"] = ds.row_checksum(row)

        dataset_dir = Path(self._tmpdir) / "dataset"
        (dataset_dir / "data").mkdir(parents=True)
        with open(dataset_dir / "data" / "memories-000.jsonl", "wb") as fh:
            fh.write(ds.canonical_row_bytes(row))
        for stem in ("episodes-000", "episode_members-000", "links-000"):
            (dataset_dir / "data" / f"{stem}.jsonl").write_bytes(b"")
        manifest = {
            "schema_version": ds.DATASET_SCHEMA_VERSION,
            "source_snapshot_hash": source_hash,
            "namespaces": ["project:dataset-shape"],
            "row_counts": {"memories": 1, "episodes": 0,
                           "episode_members": 0, "links": 0},
            "format": ds.DATASET_FORMAT_JSONL,
        }
        with open(dataset_dir / "manifest.json", "wb") as fh:
            fh.write(ds.canonical_row_bytes(manifest))

        dest_dir = Path(self._tmpdir) / "snapshot"
        raised: Exception | None = None
        summary = None
        try:
            summary = ds.import_dataset(
                source=str(dataset_dir), revision=source_hash,
                dest_dir=str(dest_dir))
        except Exception as exc:  # refusal shape is the fix's choice
            raised = exc

        snapshot = dest_dir / "snapshot.sqlite"
        bad_present = False
        if snapshot.exists():
            conn = sqlite3.connect(str(snapshot))
            try:
                bad_present = conn.execute(
                    "SELECT 1 FROM memory WHERE id=?", (BAD_ID,)
                ).fetchone() is not None
            finally:
                conn.close()
        refused = raised is not None or (
            isinstance(summary, dict) and summary.get("memories") == 0)
        print(f"[C3] raised={raised!r} summary={summary!r} "
              f"bad_present={bad_present}")
        if bad_present or not refused:
            raise AssertionError("the malformed id was inserted at base")


if __name__ == "__main__":
    _names = [a for a in sys.argv[1:] if not a.startswith("-")]
    _loader = unittest.TestLoader()
    if _names:
        _suite = unittest.TestSuite(
            _loader.loadTestsFromName(n, sys.modules[__name__])
            for n in _names)
    else:
        _suite = _loader.loadTestsFromModule(sys.modules[__name__])
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    print("CHECK-PASS" if _result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if _result.wasSuccessful() else 1)
