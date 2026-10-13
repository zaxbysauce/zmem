"""Schema 15 `state` type and the additive `memory_expiry` side table (issue #173).

Five classes, one per acceptance concern:

- StateSchemaTest   — AC1 surface + the TTL/validation pure helpers.
- StateMigrationTest— AC1/AC6 v14->v15 migration and its atomic rollback.
- StateWriteTest    — AC2/AC3 writer defaults, the transition matrix, exact errors.
- StateCleanupTest  — AC7 every tombstone/absorb path drops the side row in-txn.
- StateSyncTest     — AC5/AC6 the `memory_expiry` JSONL wire round-trip.

Every store-touching test pins the four isolation env vars to a GUID temp dir
BEFORE importing storelib (the operator store is never opened) and passes
`now=` to writers so TTL math is deterministic rather than clock-dependent.
"""

from __future__ import annotations

import atexit
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys_path_before = str(ROOT / "skills" / "memory" / "scripts")

import sys

sys.path.insert(0, sys_path_before)

_ROUTE_ENV_KEYS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODEL_URL",
    "ZMEM_STATE_ENDPOINT_DAYS", "ZMEM_STATE_SETTING_DAYS",
    "ZMEM_STATE_METRIC_DAYS", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
)
_IMPORT_SANDBOX = Path(tempfile.mkdtemp(prefix="zmem-state-expiry-import-"))
atexit.register(shutil.rmtree, _IMPORT_SANDBOX, ignore_errors=True)
_IMPORT_VALUES = {
    "ZMEM_STORE": str(_IMPORT_SANDBOX / "store.sqlite"),
    "ZMEM_DATA": str(_IMPORT_SANDBOX / "data"),
    "ZMEM_MODELS_DIR": str(_IMPORT_SANDBOX / "models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "HOME": str(_IMPORT_SANDBOX / "home"),
    "USERPROFILE": str(_IMPORT_SANDBOX / "home"),
    "APPDATA": str(_IMPORT_SANDBOX / "appdata"),
    "LOCALAPPDATA": str(_IMPORT_SANDBOX / "localappdata"),
}
# NOTE: storelib is deliberately NOT imported here. `storelib.schema` freezes
# STORE_PATH (= _resolve_store_path()) once at import, so whichever test module
# imports it first pins the store path for the WHOLE pytest session, and
# `storelib.cli` then holds a reference to that same frozen module. If this
# file imported storelib at module level, `test_issue183_acceptance_evidence.
# py`'s in-process `import storelib.cli` would inherit this module's frozen
# path and its `session-cadence` probe would sweep the wrong store, reporting
# zero expired evidence and failing `report["evidence_expired"] == 1`.
# Deferring the import keeps THIS module's own store resolution honest (each
# test re-pins the env and reloads storelib.schema before touching a store),
# but it cannot make this file invisible to test_issue183's in-process probe:
# `storelib/__init__.py` transitively imports `storelib.cli`, so the FIRST
# `import storelib.*` anywhere in a process freezes cli's STORE_PATH for that
# process. Pairing this file with test_issue183_acceptance_evidence.py in ONE
# pytest process therefore fails `report["evidence_expired"] == 1` the same way
# the pre-existing test_evidence.py + test_issue183 pair fails at base 2bde591
# (both pairings proven 2026-10-13; neither exists under the repo's real
# harness, which is CI's per-file loop — .github/workflows/ci.yml runs
# `python "$f"` per file, a fresh interpreter each, under which every file here
# passes standalone). Same-process multi-file runs are not a supported harness.
#
# The frozen acceptance checks are unaffected either way: repro/_common.py
# clears and re-pins os.environ BEFORE it imports storelib.
schema = sync = write = state_expiry = None
ALLOWED_TYPES = FORWARD_COMPAT_SCHEMA_VERSION = None
STATE_EXPIRY_CLASSES = STATE_EXPIRY_ENV = STATE_EXPIRY_TTL_DAYS = None
SUPPORTED_SCHEMA_VERSION = None


def _import_storelib() -> None:
    """Import storelib under the sandbox env and bind it at module scope."""
    global schema, sync, write, state_expiry
    global ALLOWED_TYPES, FORWARD_COMPAT_SCHEMA_VERSION
    global STATE_EXPIRY_CLASSES, STATE_EXPIRY_ENV, STATE_EXPIRY_TTL_DAYS
    global SUPPORTED_SCHEMA_VERSION
    with patch.dict(os.environ, _IMPORT_VALUES, clear=False):
        from storelib import schema as _schema  # noqa: E402
        from storelib import sync as _sync  # noqa: E402
        from storelib import write as _write  # noqa: E402
        from storelib import state_expiry as _state_expiry  # noqa: E402
        from schema_meta import (  # noqa: E402
            ALLOWED_TYPES as _ALLOWED_TYPES,
            FORWARD_COMPAT_SCHEMA_VERSION as _FORWARD,
            STATE_EXPIRY_CLASSES as _CLASSES,
            STATE_EXPIRY_ENV as _ENV_MAP,
            STATE_EXPIRY_TTL_DAYS as _TTL,
            SUPPORTED_SCHEMA_VERSION as _SUPPORTED,
        )
    schema, sync, write, state_expiry = _schema, _sync, _write, _state_expiry
    ALLOWED_TYPES = _ALLOWED_TYPES
    FORWARD_COMPAT_SCHEMA_VERSION = _FORWARD
    STATE_EXPIRY_CLASSES = _CLASSES
    STATE_EXPIRY_ENV = _ENV_MAP
    STATE_EXPIRY_TTL_DAYS = _TTL
    SUPPORTED_SCHEMA_VERSION = _SUPPORTED

NOW = "2026-09-10T00:00:00Z"
NS = "project:state-expiry"
ENDPOINT_ID = "00000000-0000-4000-8000-000000000001"
SETTING_ID = "00000000-0000-4000-8000-000000000002"
METRIC_ID = "00000000-0000-4000-8000-000000000003"
FACT_ID = "00000000-0000-4000-8000-000000000004"
# Committed gold fixture (contract step 13): generated by
# python tests/fixtures/state/make_fixture.py. expected.json's exact bytes are
# the oracle; it is JSONL despite the .json suffix.
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "state"


class _StoreCase(unittest.TestCase):
    """A fresh isolated v15 store per test; the operator store is never opened."""

    @classmethod
    def setUpClass(cls) -> None:
        # Deferred storelib import (see the module-level NOTE): binds schema/
        # sync/write/state_expiry only once this class actually runs, so this
        # file never freezes STORE_PATH during pytest's collection phase.
        _import_storelib()

    def setUp(self) -> None:
        # storelib.schema freezes STORE_PATH at import; importlib.reload
        # re-executes the module body against the env pinned below and rebinds
        # the sys.modules entry IN PLACE. The in-place rebind is what matters:
        # a later `import storelib.cli` re-uses the same module object, so it
        # sees this reload's STORE_PATH rather than a stale one.
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-state-expiry-test-")
        self.root = Path(self.tmp.name)
        self.old_env = os.environ.copy()
        for key in _ROUTE_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update({
            "ZMEM_STORE": str(self.root / "store.sqlite"),
            "ZMEM_DATA": str(self.root),
            "ZMEM_MODELS_DIR": str(self.root / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
        })
        import importlib

        importlib.reload(schema)
        self.conn = sqlite3.connect(self.root / "store.sqlite")
        self.conn.row_factory = sqlite3.Row
        schema.init_db(self.conn)
        schema.migrate(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()
        # Re-free STORE_PATH off this test's sandbox and back onto whatever the
        # restored env resolves to, so the next module to import storelib (or
        # the next test class) does not inherit a dead temp path. Mirrors the
        # convention in tests/test_schema_forward_compat.py.
        import importlib

        importlib.reload(schema)

    # -- helpers ----------------------------------------------------------
    def side_row(self, memory_id: str):
        return self.conn.execute(
            "SELECT inject_until, expiry_class FROM memory_expiry WHERE memory_id=?",
            (memory_id,),
        ).fetchone()

    def add_state(self, content: str, *, cls: str = "endpoint",
                  inject_until: str | None = None, now: str = NOW,
                  mid: str | None = None) -> str:
        """add_memory a state row with deterministic ids for wire assertions."""
        if mid is not None:
            with patch.object(write, "uuid",
                              type("U", (), {"uuid4": staticmethod(lambda: mid)})()):
                res = write.add_memory(
                    self.conn, namespace=NS, type_="state", content=content,
                    signal="test", expiry_class=cls, inject_until=inject_until,
                    now=now)
        else:
            res = write.add_memory(
                self.conn, namespace=NS, type_="state", content=content,
                signal="test", expiry_class=cls, inject_until=inject_until,
                now=now)
        return str(res)


class StateSchemaTest(_StoreCase):
    """AC1: fresh v15 surface, the type, and the pure TTL/validation helpers."""

    def test_fresh_store_is_v15_with_memory_expiry(self):
        ver = self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()[0]
        self.assertEqual(ver, "15")
        self.assertEqual(SUPPORTED_SCHEMA_VERSION, 15)
        self.assertEqual(FORWARD_COMPAT_SCHEMA_VERSION, 15)
        self.assertIn("state", ALLOWED_TYPES)
        tabs = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        self.assertIn("memory_expiry", tabs)
        idx = [r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='memory_expiry' AND name NOT LIKE 'sqlite_%'")]
        self.assertEqual(idx, ["idx_memory_expiry_until"])
        cols = [(r[1], r[2]) for r in self.conn.execute(
            "PRAGMA table_info(memory_expiry)")]
        self.assertEqual(cols, [("memory_id", "TEXT"), ("inject_until", "TEXT"),
                                ("expiry_class", "TEXT")])

    def test_memory_table_is_unchanged_after_v15(self):
        # AC1's additive-only guard, absolute rather than relative: the v15
        # surface is a side table, so `memory` must keep exactly its 29 v14
        # columns (name, type, notnull, dflt_value, pk) for forward compat —
        # an older v14 client's SQL must stay fully valid on a v15 store.
        v14_memory_columns = [
            "id", "namespace", "type", "content", "tags", "source_ref",
            "source_hash", "confidence", "signal", "valid_from",
            "superseded_at", "ingestion_ts", "retrieval_count", "last_retrieved",
            "surfaced_count", "last_surfaced", "merged_from", "content_norm",
            "valid_until", "update_of", "taint", "trust_score", "applied_count",
            "violated_count", "embedding", "embedding_model", "embedded_at",
            "consolidated_at", "supersede_reason",
        ]
        rows = [tuple(r) for r in self.conn.execute(
            "PRAGMA table_info(memory)")]
        # (cid, name, type, notnull, dflt_value, pk)
        self.assertEqual([r[1] for r in rows], v14_memory_columns,
                         "v15 must not add, drop, or reorder a memory column")
        self.assertTrue(all(isinstance(r[5], int) for r in rows),
                        "pk positions must remain integers")
        # type / content are still the write-critical NOT NULL columns.
        notnull = {r[1] for r in rows if r[3] == 1}
        self.assertIn("type", notnull)
        self.assertIn("content", notnull)

    def test_ttl_defaults_and_env_overrides(self):
        self.assertEqual(STATE_EXPIRY_CLASSES, ("endpoint", "setting", "metric"))
        self.assertEqual(STATE_EXPIRY_TTL_DAYS,
                         {"endpoint": 30, "setting": 90, "metric": 14})
        self.assertEqual(state_expiry.state_expiry_days("endpoint"), 30)
        self.assertEqual(state_expiry.state_expiry_days("setting"), 90)
        self.assertEqual(state_expiry.state_expiry_days("metric"), 14)
        with patch.dict(os.environ, {"ZMEM_STATE_ENDPOINT_DAYS": "7"}):
            self.assertEqual(state_expiry.state_expiry_days("endpoint"), 7)
        # A missing override falls back to the default.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZMEM_STATE_ENDPOINT_DAYS", None)
            self.assertEqual(state_expiry.state_expiry_days("endpoint"), 30)

    def test_ttl_env_override_is_validated(self):
        for bad in ("0", "-3", "abc", "2.5", "  "):
            with patch.dict(os.environ, {"ZMETRIC": bad}, clear=False):
                os.environ["ZMEM_STATE_METRIC_DAYS"] = bad
                with self.assertRaisesRegex(
                        ValueError,
                        "invalid state expiry for class 'metric': "
                        "ZMEM_STATE_METRIC_DAYS must be a positive decimal integer"):
                    state_expiry.state_expiry_days("metric")

    def test_unknown_class_is_refused_before_any_env_read(self):
        # The class is validated first, so an unknown class never probes a var.
        # The message is the contract's exact direct-writer string #2
        # (issue #173 Execution step 10) — it deliberately does not echo the
        # offending value.
        with patch.dict(os.environ, {"ZMEM_STATE_BOGUS_DAYS": "1"}):
            with self.assertRaises(ValueError) as cm:
                state_expiry.state_expiry_days("bogus")
            self.assertEqual(
                str(cm.exception),
                "state expiry class must be one of endpoint, setting, metric")

    def test_inject_until_normalization_and_refusal(self):
        self.assertEqual(
            state_expiry.validate_inject_until("2026-11-12T09:00:00Z"),
            "2026-11-12T09:00:00Z")
        # +00:00 normalizes to Z.
        self.assertEqual(
            state_expiry.validate_inject_until("2026-11-12T09:00:00+00:00"),
            "2026-11-12T09:00:00Z")
        # Fractional seconds are preserved verbatim.
        self.assertEqual(
            state_expiry.validate_inject_until("2026-11-12T09:00:00.123456Z"),
            "2026-11-12T09:00:00.123456Z")
        for bad in ("2026-11-12", "2026-11-12T09:00:00",  # date-only, naive
                    "2026-11-12T09:00:00+02:00",          # non-UTC offset
                    "12/11/2026", "not-a-time"):
            with self.assertRaisesRegex(
                    ValueError, "inject_until must be UTC ISO-8601"):
                state_expiry.validate_inject_until(bad)

    def test_compute_inject_until_adds_ttl(self):
        self.assertEqual(
            state_expiry.compute_inject_until("endpoint", now=NOW),
            "2026-10-10T00:00:00Z")
        self.assertEqual(
            state_expiry.compute_inject_until("setting", now=NOW),
            "2026-12-09T00:00:00Z")
        self.assertEqual(
            state_expiry.compute_inject_until("metric", now=NOW),
            "2026-09-24T00:00:00Z")


class StateMigrationTest(_StoreCase):
    """AC1/AC6: v14->v15 preserves the memory table; a failed DDL rolls back."""

    def _force_v14_shape(self) -> None:
        """Return the (already-written) store to a bare v14 shape.

        The memory rows stay; only the v15 side table, its index and the
        version marker go, so the next ``migrate()`` must recreate them.
        """
        self.conn.execute("DROP INDEX IF EXISTS idx_memory_expiry_until")
        self.conn.execute("DROP TABLE IF EXISTS memory_expiry")
        self.conn.execute("UPDATE meta SET value='14' WHERE key='schema_version'")
        self.conn.commit()

    def test_v14_to_v15_preserves_memory_columns_and_counts(self):
        # Seed at v15 (so a state row + its side record both exist), then
        # rewind to a v14 shape and re-run the migration lane over real data.
        mid = self.add_state("row written before migration", mid=ENDPOINT_ID)
        self.conn.commit()
        self._force_v14_shape()
        before_cols = [(r[1], r[2], r[3], r[4]) for r in self.conn.execute(
            "PRAGMA table_info(memory)")]
        count_before = self.conn.execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0]

        schema.migrate(self.conn)

        self.assertEqual(self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "15")
        after_cols = [(r[1], r[2], r[3], r[4]) for r in self.conn.execute(
            "PRAGMA table_info(memory)")]
        self.assertEqual(before_cols, after_cols,
                         "the additive v15 migration must not touch memory columns")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0], count_before)
        # The migration recreated the additive surface (it is migration-only
        # DDL, deliberately absent from init_db, so a rewind drops it). The
        # pre-existing state row's side record is NOT resurrected — the side
        # table is write-path state, not derivable history; only NEW state
        # writes repopulate it.
        self.assertIsNotNone(self.conn.execute(
            "SELECT id FROM memory WHERE id=?", (mid,)).fetchone(),
            "the pre-existing memory row must survive the migration untouched")
        tabs = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("memory_expiry", tabs)
        idx = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='memory_expiry' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(idx, {"idx_memory_expiry_until"})
        # A second migrate is a no-op.
        schema.migrate(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "15")

    def test_failed_memory_expiry_ddl_rolls_back(self):
        # Force the store back to v14, then poison the side-table NAME with an
        # incompatible object so the v15 CREATE TABLE fails. Version, table set
        # and index set must all be unchanged afterwards.
        self._force_v14_shape()
        self.conn.execute("CREATE TABLE memory_expiry (poison INTEGER)")
        self.conn.commit()
        snapshot = lambda: sorted(  # noqa: E731
            (r[0], r[1]) for r in self.conn.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"))
        before = snapshot()
        with self.assertRaises(sqlite3.OperationalError):
            schema.migrate(self.conn)
        self.conn.rollback()
        self.assertEqual(snapshot(), before,
                         "a failed v15 DDL must leave the schema byte-identical")
        self.assertEqual(self.conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "14")


class StateWriteTest(_StoreCase):
    """AC2/AC3: writer defaults, the update transition matrix, exact errors."""

    def test_state_endpoint_default_expires_on_2026_10_10(self):
        mid = self.add_state("staging api base url", cls="endpoint")
        row = self.side_row(mid)
        self.assertIsNotNone(row)
        self.assertEqual(row["expiry_class"], "endpoint")
        self.assertEqual(row["inject_until"], "2026-10-10T00:00:00Z")

    def test_setting_and_metric_ttls_use_90_and_14_days(self):
        sid = self.add_state("build timeout setting", cls="setting")
        mid_ = self.add_state("request latency ms", cls="metric")
        self.assertEqual(self.side_row(sid)["inject_until"], "2026-12-09T00:00:00Z")
        self.assertEqual(self.side_row(mid_)["inject_until"], "2026-09-24T00:00:00Z")

    def test_bogus_class_and_nonstate_expiry_are_refused(self):
        before = self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
        # AC3 + Execution step 10: exact contract string, full equality (the
        # message is pinned verbatim, not merely "mentions the class").
        with self.assertRaises(ValueError) as cm:
            write.add_memory(self.conn, namespace=NS, type_="state",
                             content="bogus class row", signal="test",
                             expiry_class="bogus", now=NOW)
        self.assertEqual(
            str(cm.exception),
            "state expiry class must be one of endpoint, setting, metric")
        with self.assertRaisesRegex(
                ValueError, "non-state memories cannot set expiry_class or inject_until"):
            write.add_memory(self.conn, namespace=NS, type_="fact",
                             content="fact with expiry", signal="test",
                             expiry_class="endpoint", now=NOW)
        with self.assertRaisesRegex(
                ValueError, "non-state memories cannot set expiry_class or inject_until"):
            write.add_memory(self.conn, namespace=NS, type_="fact",
                             content="fact with inject_until", signal="test",
                             inject_until="2026-10-10T00:00:00Z", now=NOW)
        # Validation fires BEFORE the first INSERT: no partial row.
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0], before)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory_expiry").fetchone()[0], 0)

    def test_state_update_with_bogus_class_is_refused_before_tombstone(self):
        # Execution step 10: "a state-to-state update with a bad class ...
        # uses this contract" — the exact add-path message, raised before the
        # tombstone. Covers BOTH update branches: class-only (TTL path) and
        # class+explicit-inject_until (the branch that bypasses TTL and would
        # otherwise smuggle the unknown class straight into memory_expiry —
        # found live during Phase 4.5 review Q1).
        mid = self.add_state("bad-class update target", cls="endpoint")
        keep = dict(self.side_row(mid))
        for kwargs in (
            dict(expiry_class="bogus"),
            dict(expiry_class="bogus",
                 inject_until="2027-01-01T00:00:00Z"),
        ):
            with self.assertRaises(ValueError) as cm:
                write.update_memory(
                    self.conn, mid=mid, content="bad-class update target v2",
                    now="2026-09-11T00:00:00Z", **kwargs)
            self.assertEqual(
                str(cm.exception),
                "state expiry class must be one of endpoint, setting, metric")
            # Pre-tombstone: old row still live, side row byte-identical.
            old = self.conn.execute(
                "SELECT superseded_at FROM memory WHERE id=?", (mid,)).fetchone()
            self.assertIsNone(old["superseded_at"])
            self.assertEqual(dict(self.side_row(mid)), keep)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0], 1)

    def test_state_add_dedup_keeps_existing_expiry_and_side_row(self):
        # Reviewer Phase 4.5 Q2: a state add that dedups into an existing
        # keeper merges corroboration only (confidence/signal/tags/taint/
        # trust — the v12 precedent where feedback counters also stay
        # keeper-local) and never rewrites the keeper's semantics: type,
        # content AND expiry metadata all stay the keeper's. The incoming
        # class/timestamp are deliberately not adopted, so the AC7 invariants
        # hold — one side row, attached to the keeper, byte-unchanged.
        keeper = self.add_state("dedup target state row", cls="endpoint",
                                now=NOW)
        keep = dict(self.side_row(keeper))
        res = write.add_memory(
            self.conn, namespace=NS, type_="state",
            content="dedup target state row", signal="test",
            expiry_class="metric", inject_until="2027-01-01T00:00:00Z",
            now="2026-09-11T00:00:00Z")
        self.assertEqual(str(res), keeper)
        self.assertTrue(res.deduped)
        self.assertEqual(dict(self.side_row(keeper)), keep)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory_expiry").fetchone()[0], 1)

    def test_state_add_without_class_is_refused(self):
        with self.assertRaisesRegex(ValueError, "state memories require expiry_class"):
            write.add_memory(self.conn, namespace=NS, type_="state",
                             content="state without class", signal="test", now=NOW)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0], 0)

    def test_non_iso_inject_until_is_refused(self):
        with self.assertRaisesRegex(
                ValueError, "inject_until must be UTC ISO-8601"):
            write.add_memory(self.conn, namespace=NS, type_="state",
                             content="bad timestamp row", signal="test",
                             expiry_class="endpoint",
                             inject_until="2026-11-12 09:00:00", now=NOW)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory").fetchone()[0], 0)

    def test_explicit_inject_until_bypasses_ttl(self):
        mid = self.add_state("explicit expiry row", cls="metric",
                             inject_until="2027-01-01T00:00:00Z")
        self.assertEqual(self.side_row(mid)["inject_until"], "2027-01-01T00:00:00Z")

    def test_state_update_transitions_inherit_replace_and_cleanup(self):
        # state -> state, no expiry args: inherit class + timestamp unchanged.
        v1 = self.add_state("endpoint v1", cls="endpoint")
        orig = self.side_row(v1)
        res, created = write.update_memory(
            self.conn, mid=v1, content="endpoint v2", now="2026-09-11T00:00:00Z")
        v2 = str(res)
        self.assertTrue(created)
        self.assertIsNone(self.side_row(v1), "old row's side record must be gone")
        self.assertEqual(self.side_row(v2)["inject_until"], orig["inject_until"])
        self.assertEqual(self.side_row(v2)["expiry_class"], "endpoint")

        # state -> state, only expiry_class: recompute TTL from the new now.
        res, _ = write.update_memory(
            self.conn, mid=v2, content="endpoint v3", expiry_class="metric",
            now="2026-09-11T00:00:00Z")
        v3 = str(res)
        self.assertEqual(self.side_row(v3)["expiry_class"], "metric")
        self.assertEqual(self.side_row(v3)["inject_until"], "2026-09-25T00:00:00Z")

        # state -> state, only inject_until: keep the class, replace the time.
        res, _ = write.update_memory(
            self.conn, mid=v3, content="endpoint v4",
            inject_until="2027-06-01T00:00:00Z", now="2026-09-11T00:00:00Z")
        v4 = str(res)
        self.assertEqual(self.side_row(v4)["expiry_class"], "metric")
        self.assertEqual(self.side_row(v4)["inject_until"], "2027-06-01T00:00:00Z")

        # state -> non-state: the side record is dropped and none is created.
        res, _ = write.update_memory(
            self.conn, mid=v4, content="now a plain fact", type_="fact",
            now="2026-09-11T00:00:00Z")
        f = str(res)
        self.assertIsNone(self.side_row(v4))
        self.assertIsNone(self.side_row(f))

        # non-state -> state: requires a class and creates a fresh record.
        res, _ = write.update_memory(
            self.conn, mid=f, content="fact becomes state", type_="state",
            expiry_class="endpoint", now="2026-09-11T00:00:00Z")
        s = str(res)
        self.assertEqual(self.side_row(s)["expiry_class"], "endpoint")
        self.assertEqual(self.side_row(s)["inject_until"], "2026-10-11T00:00:00Z")

    def test_state_update_without_side_row_raises_contract_string(self):
        # AC2/AC7: state->state with BOTH expiry args omitted has nothing to
        # inherit when the old live row carries no memory_expiry record. The
        # contract names the exact message (issue #173 Execution step 10:
        # "the direct writer messages are exact"), and it must fire BEFORE the
        # tombstone — no replacement row, old row still live (reviewer round-1
        # finding 1).
        mid = self.add_state("orphan state row", cls="endpoint")
        self.conn.execute("DELETE FROM memory_expiry WHERE memory_id=?", (mid,))
        self.conn.commit()
        self.assertIsNone(self.side_row(mid))
        before = self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]

        with self.assertRaises(ValueError) as cm:
            write.update_memory(
                self.conn, mid=mid, content="orphan state row v2",
                now="2026-09-11T00:00:00Z")
        self.assertEqual(
            str(cm.exception),
            "state memories require an existing memory_expiry row")

        # Pre-tombstone: the memory count is unchanged and the old row was not
        # superseded (validation at write.py:1820 precedes the tombstone).
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0],
            before)
        old = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (mid,)).fetchone()
        self.assertIsNotNone(old)
        self.assertIsNone(old["superseded_at"])

    def test_one_transaction_timestamp_across_update(self):
        # AC7: superseded_at, valid_until and valid_from all share tx_now.
        mid = self.add_state("timestamped state row", cls="endpoint")
        t = "2026-09-12T00:00:00Z"
        res, _ = write.update_memory(
            self.conn, mid=mid, content="timestamped state row v2",
            expiry_class="endpoint", now=t)
        old = self.conn.execute(
            "SELECT superseded_at, valid_until FROM memory WHERE id=?", (mid,)
        ).fetchone()
        new = self.conn.execute(
            "SELECT valid_from FROM memory WHERE id=?", (str(res),)
        ).fetchone()
        self.assertEqual(old["superseded_at"], t)
        self.assertEqual(old["valid_until"], t)
        self.assertEqual(new["valid_from"], t)
        self.assertEqual(self.side_row(str(res))["inject_until"],
                         "2026-10-12T00:00:00Z")


class StateCleanupTest(_StoreCase):
    """AC7: every tombstone/absorb path drops the side row in-transaction."""

    def test_supersede_removes_the_side_row(self):
        a = self.add_state("superseded state row", cls="endpoint")
        self.assertIsNotNone(self.side_row(a))
        self.assertTrue(write.supersede_memory(self.conn, a, "cleanup probe"))
        self.assertIsNone(self.side_row(a),
                          "supersede_memory must drop the side row in-txn")

    def test_consolidate_absorb_removes_the_absorbed_side_row(self):
        import storelib.consolidate  # noqa: E402  (package attr is the function)
        consolidate = sys.modules["storelib.consolidate"]

        keep = self.add_state("keeper state row", cls="setting")
        absorb = self.add_state("absorbed state row", cls="metric")
        seed = self.conn.execute(
            "SELECT * FROM memory WHERE id=?", (keep,)).fetchone()
        nb = self.conn.execute(
            "SELECT * FROM memory WHERE id=?", (absorb,)).fetchone()
        self.conn.execute("BEGIN")
        consolidate._absorb_into_keeper(self.conn, seed, nb)
        # _absorb_into_keeper never commits/rollbacks on its own (its
        # docstring); commit so the assertions below observe the in-txn work.
        self.conn.commit()
        self.assertIsNone(self.side_row(absorb),
                          "consolidate absorb must drop the absorbed side row")
        self.assertIsNotNone(self.side_row(keep),
                             "the keeper's own side row must survive")

    def test_organize_stale_summary_removes_the_side_row(self):
        import storelib.organize  # noqa: E402  (package attr is the function)
        organize = sys.modules["storelib.organize"]

        # A stale summary is a live `organize:`-prefixed fact whose merged_from
        # shares a member with topic_key but whose id is not keep_id.
        member = self.add_state("topic member", cls="endpoint")
        stale = write.add_memory(
            self.conn, namespace=NS, type_="fact",
            content="organize stale summary row", signal="test",
            source_ref=organize.SUMMARY_SOURCE_REF_PREFIX + ":probe", now=NOW)
        stale = str(stale)
        self.conn.execute(
            "UPDATE memory SET merged_from=? WHERE source_ref LIKE ?",
            (member, organize.SUMMARY_SOURCE_REF_PREFIX + "%"))
        # Attach a side row directly (a fact normally has none; the guard must
        # still clear it defensively when it tombstones the summary).
        self.conn.execute(
            "INSERT OR REPLACE INTO memory_expiry VALUES (?,?,?)",
            (stale, "2027-01-01T00:00:00Z", "endpoint"))
        self.conn.commit()
        self.assertIsNotNone(self.side_row(stale))
        keep_id = "00000000-0000-4000-8000-00000000ffff"
        organize._supersede_stale_summaries(self.conn, member, keep_id)
        self.assertIsNone(
            self.side_row(stale),
            "organize's stale-summary tombstone must drop the side row in-txn")

    # AC7's sync-tombstone path is covered END TO END in
    # tests/test_jsonl_sync.py::TombstonePropagationTest::
    # test_imported_state_tombstone_deletes_expiry_row, which drives the real
    # store.py CLI (A live-sync, A supersede, tombstone import --allow-tombstones)
    # and asserts B's side row goes with the tombstone. This class previously
    # carried a same-body duplicate of test_supersede_removes_the_side_row under
    # a sync-suggesting name; it exercised no sync path, so it was removed rather
    # than left as misleading AC7 evidence.


class StateSyncTest(_StoreCase):
    """AC5/AC6: the memory_expiry JSONL wire round-trip and its rejections."""

    WIRE_KEYS = ["kind", "memory_id", "inject_until", "expiry_class"]

    def test_export_emits_expiry_record_immediately_after_parent(self):
        mid = self.add_state("api.example.com", cls="endpoint",
                             inject_until="2027-01-01T00:00:00Z",
                             mid=ENDPOINT_ID)
        self.conn.commit()
        out = self.root / "export.jsonl"
        self.assertEqual(sync.cmd_export_jsonl(self.conn, out=str(out)), 0)
        text = out.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n"), "JSONL must end in exactly one LF")
        self.assertFalse(text.endswith("\n\n"),
                         "JSONL must not end in a blank line")
        rows = [json.loads(l) for l in text.splitlines() if l.strip()]
        kinds = [r["kind"] for r in rows]
        self.assertIn("memory_expiry", kinds)
        i = kinds.index("memory_expiry")
        self.assertEqual(list(rows[i].keys()), self.WIRE_KEYS,
                         "wire keys must be the exact ordered 4-key shape")
        self.assertEqual(rows[i - 1]["kind"], "memory",
                         "the expiry record must follow its parent memory record")
        self.assertEqual(rows[i - 1]["id"], mid)
        self.assertEqual(rows[i]["memory_id"], mid)
        self.assertEqual(rows[i]["inject_until"], "2027-01-01T00:00:00Z")
        self.assertEqual(rows[i]["expiry_class"], "endpoint")

    def test_v14_export_without_expiry_emits_no_records(self):
        write.add_memory(self.conn, namespace=NS, type_="fact",
                         content="plain v14 fact", signal="test", now=NOW)
        self.conn.commit()
        out = self.root / "v14.jsonl"
        self.assertEqual(sync.cmd_export_jsonl(self.conn, out=str(out)), 0)
        rows = [json.loads(l) for l in
                out.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(all(r["kind"] != "memory_expiry" for r in rows),
                        "a v14 store has no side rows to export")

    def test_round_trip_preserves_class_and_timestamp(self):
        self.add_state("deploy-timeout", cls="setting",
                       inject_until="2026-12-09T00:00:00Z", mid=SETTING_ID)
        self.add_state("request-latency", cls="metric",
                       inject_until="2026-09-24T00:00:00Z", mid=METRIC_ID)
        self.conn.commit()
        out = self.root / "rt.jsonl"
        self.assertEqual(sync.cmd_export_jsonl(self.conn, out=str(out)), 0)

        other = sqlite3.connect(self.root / "other.sqlite")
        other.row_factory = sqlite3.Row
        schema.init_db(other)
        schema.migrate(other)
        try:
            self.assertEqual(
                sync.cmd_ingest_jsonl_strict(other, in_path=str(out), source_ref=None), 0)
            got = {(r["expiry_class"], r["inject_until"]) for r in other.execute(
                "SELECT expiry_class, inject_until FROM memory_expiry")}
            self.assertEqual(got, {("setting", "2026-12-09T00:00:00Z"),
                                   ("metric", "2026-09-24T00:00:00Z")})
        finally:
            other.close()

    def test_state_expiry_fixture_round_trips(self):
        # Contract step 13: tests/fixtures/state/expected.json is the gold
        # artifact; its EXACT bytes are the oracle. Ingest the committed
        # expiry.jsonl into a fresh store and re-export: the result must be
        # byte-identical to the committed expected.json.
        fixture = FIXTURE_DIR / "expiry.jsonl"
        gold = FIXTURE_DIR / "expected.json"
        self.assertTrue(fixture.exists() and gold.exists(),
                        "state fixture not generated — run "
                        "python tests/fixtures/state/make_fixture.py")

        rc = sync.cmd_ingest_jsonl_strict(
            self.conn, in_path=str(fixture), source_ref=None)
        self.assertEqual(rc, 0)

        out = self.root / "regen.jsonl"
        self.assertEqual(sync.cmd_export_jsonl(self.conn, out=str(out)), 0)
        self.assertEqual(
            out.read_bytes(), gold.read_bytes(),
            "the committed expected.json is the byte-exact oracle: a fresh "
            "store fed the committed fixture must re-export identical bytes")
        # And the side table landed all three (class, timestamp) pairs.
        got = {(r["expiry_class"], r["inject_until"]) for r in self.conn.execute(
            "SELECT expiry_class, inject_until FROM memory_expiry")}
        self.assertEqual(got, {("endpoint", "2026-10-10T00:00:00Z"),
                               ("setting", "2026-12-09T00:00:00Z"),
                               ("metric", "2026-09-24T00:00:00Z")})

    def test_identical_reimport_is_idempotent(self):
        self.add_state("idempotent row", cls="endpoint",
                       inject_until="2027-01-01T00:00:00Z", mid=ENDPOINT_ID)
        self.conn.commit()
        out = self.root / "idem.jsonl"
        self.assertEqual(sync.cmd_export_jsonl(self.conn, out=str(out)), 0)
        other = sqlite3.connect(self.root / "other.sqlite")
        other.row_factory = sqlite3.Row
        schema.init_db(other)
        schema.migrate(other)
        try:
            self.assertEqual(
                sync.cmd_ingest_jsonl_strict(other, in_path=str(out), source_ref=None), 0)
            n = other.execute("SELECT COUNT(*) FROM memory_expiry").fetchone()[0]
            self.assertEqual(n, 1)
            # Identical reimport: no duplicate row, still exit 0.
            self.assertEqual(
                sync.cmd_ingest_jsonl_strict(other, in_path=str(out), source_ref=None), 0)
            n2 = other.execute("SELECT COUNT(*) FROM memory_expiry").fetchone()[0]
            self.assertEqual(n2, 1, "identical reimport must not duplicate")
        finally:
            other.close()

    def test_v14_jsonl_without_expiry_imports_with_empty_side_table(self):
        self.add_state("state row only", cls="endpoint",
                       inject_until="2027-01-01T00:00:00Z", mid=ENDPOINT_ID)
        self.conn.commit()
        out = self.root / "full.jsonl"
        self.assertEqual(sync.cmd_export_jsonl(self.conn, out=str(out)), 0)
        # Strip the expiry records to synthesize a v14-shaped file.
        kept = [l for l in out.read_text(encoding="utf-8").splitlines()
                if l.strip() and json.loads(l)["kind"] != "memory_expiry"]
        v14 = self.root / "v14shape.jsonl"
        v14.write_text("\n".join(kept) + "\n", encoding="utf-8")
        other = sqlite3.connect(self.root / "other.sqlite")
        other.row_factory = sqlite3.Row
        schema.init_db(other)
        schema.migrate(other)
        try:
            self.assertEqual(
                sync.cmd_ingest_jsonl_strict(other, in_path=str(v14), source_ref=None), 0)
            n = other.execute("SELECT COUNT(*) FROM memory_expiry").fetchone()[0]
            self.assertEqual(n, 0, "a v14 file must import with zero side rows")
        finally:
            other.close()

    def test_rejects_bad_timestamp_unknown_class_and_bad_uuid(self):
        for obj, fragment in [
            ({"kind": "memory_expiry", "memory_id": ENDPOINT_ID,
              "inject_until": "2026-11-12 09:00:00", "expiry_class": "endpoint"},
             "memory_expiry inject_until must be UTC ISO-8601"),
            ({"kind": "memory_expiry", "memory_id": ENDPOINT_ID,
              "inject_until": "2026-11-12T09:00:00Z", "expiry_class": "bogus"},
             "memory_expiry expiry_class must be one of"),
            ({"kind": "memory_expiry", "memory_id": "not-a-uuid",
              "inject_until": "2026-11-12T09:00:00Z", "expiry_class": "endpoint"},
             "field 'memory_id' must be a 36-char UUID-shaped string"),
        ]:
            with self.assertRaisesRegex(ValueError, fragment.replace("(", r"\(").replace(")", r"\)")):
                sync._validate_expiry_record(obj)

    def test_rejects_missing_and_non_state_parent(self):
        # Missing parent.
        with self.assertRaisesRegex(
                ValueError, "memory_expiry parent must be a live state row"):
            sync._apply_expiry_record(
                self.conn,
                {"memory_id": ENDPOINT_ID, "inject_until": "2026-11-12T09:00:00Z",
                 "expiry_class": "endpoint"})
        # Non-state parent.
        fact = str(write.add_memory(self.conn, namespace=NS, type_="fact",
                                    content="fact parent", signal="test", now=NOW))
        self.conn.commit()
        with self.assertRaisesRegex(
                ValueError, "memory_expiry parent must be a live state row"):
            sync._apply_expiry_record(
                self.conn,
                {"memory_id": fact, "inject_until": "2026-11-12T09:00:00Z",
                 "expiry_class": "endpoint"})

    def test_conflicting_duplicate_is_refused_without_partial_commit(self):
        mid = self.add_state("conflict probe", cls="endpoint",
                             inject_until="2027-01-01T00:00:00Z", mid=ENDPOINT_ID)
        self.conn.commit()
        # Identical record: a no-op (idempotent).
        sync._apply_expiry_record(
            self.conn,
            {"memory_id": mid, "inject_until": "2027-01-01T00:00:00Z",
             "expiry_class": "endpoint"})
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM memory_expiry").fetchone()[0], 1)
        # Conflicting record: refused, and the original value is intact.
        with self.assertRaisesRegex(
                ValueError, "memory_expiry conflicts with existing record"):
            sync._apply_expiry_record(
                self.conn,
                {"memory_id": mid, "inject_until": "2027-02-01T00:00:00Z",
                 "expiry_class": "endpoint"})
        row = self.side_row(mid)
        self.assertEqual(row["inject_until"], "2027-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
