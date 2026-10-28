"""Issue #363 AC2 — applied_ops replay idempotency for the op queue.

The op queue's exactly-once contract has two legs, and both must hold
WITHOUT inflating the keeper row's counters:

- replaying the SAME op_id must return the recorded verdict ("duplicate")
  and write nothing (this already holds at base — a preserving pin);
- re-applying the SAME CONTENT under a NEW op_id (the reconnect/retry
  shape: the queue replays after a crash, the hub has already folded the
  row) must land as a dedup FOLD receipt ("committed" + deduped=True per
  the current storelib/ops.py receipt contract) while the keeper's
  trust_score and retrieval_count stay EXACTLY as they were.  At base the
  fold path bumps retrieval_count +1 (write.py _merge_on_dedup) and applies
  the +0.05 corroboration trust delta (write.py adjust_trust on dedup), so
  this file is RED at base and GREEN once the applied-ops replay path
  stops touching the keeper's counters.

Isolation: setUpModule pins ZMEM_STORE/ZMEM_DATA (plus a nonexistent
ZMEM_MODELS_DIR and ZMEM_MODEL_AUTODOWNLOAD=0) to a per-run temp dir
BEFORE storelib is imported via importlib.util.spec_from_file_location
under its canonical name, so the import-time store-path resolution can
never observe the operator's store.  tearDownModule restores the env and
the previous sys.modules state and deletes the temp dir.  The canonical
store is never touched; no network; no model download.

Runs under pytest and standalone (CI convention):
``python -X utf8 tests/test_applied_ops_idempotency.py``
"""

from __future__ import annotations

import importlib.util
import io
import os
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

# Ambient values that would steer storelib at import or apply time.  All are
# popped, then the pinned scratch values are set (the tests/test_t09_op_queue_
# supp.py _PIN_KEYS discipline).
_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_URL",
    "ZMEM_REMOTE_URL", "ZMEM_OPQUEUE_MAX_BYTES", "ZMEM_OPQUEUE_MAX_AGE_DAYS",
)

_SAVED_ENV: dict = {}
_SAVED_MODULES: dict = {}
_TMP = ""
_PATH_INSERTED = False
storelib = None
ops = None
links = None


def setUpModule():  # noqa: N802 - unittest hook
    """Pin the scratch env BEFORE any storelib import, then import isolated."""
    global _SAVED_ENV, _SAVED_MODULES, _TMP, _PATH_INSERTED
    global storelib, ops, links
    _TMP = tempfile.mkdtemp(prefix="zmem-appliedops-363-")
    _SAVED_ENV = {key: os.environ.get(key) for key in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
        "ZMEM_DATA": _TMP,
        "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    _SAVED_MODULES = {
        key: value for key, value in list(sys.modules.items())
        if key == "storelib" or key.startswith("storelib.")
    }
    for key in _SAVED_MODULES:
        del sys.modules[key]
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
        _PATH_INSERTED = True
    pkg_dir = SCRIPTS_DIR / "storelib"
    spec = importlib.util.spec_from_file_location(
        "storelib", pkg_dir / "__init__.py",
        submodule_search_locations=[str(pkg_dir)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["storelib"] = module
    spec.loader.exec_module(module)
    storelib = module
    # The package __init__ does not import the ops submodule (and its
    # module __getattr__ refuses unknown names), so bind it explicitly.
    import importlib as _importlib
    ops = _importlib.import_module("storelib.ops")
    links = _importlib.import_module("storelib.links")


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore env + sys.modules exactly; the scratch dir is deleted."""
    import shutil
    global storelib, ops, links
    storelib = None
    ops = None
    links = None
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


class _Legacy:
    """The unscoped operator principal (the CLI's startup principal); same
    shape as tests/test_t09_op_queue_supp.py _Legacy so the capability gate
    allows the adds."""

    revoked = False
    capabilities = None
    principal_id = "legacy"
    scoped = False
    namespaces = None


class AppliedOpsReplayIdempotencyTest(unittest.TestCase):
    """Drive storelib/ops.py apply_ops on a real isolated storelib
    connection (the engine the queue flush and the CLI apply-ops both call)
    and pin the replay invariants on the keeper row."""

    maxDiff = None

    def _connect(self):
        conn = storelib.connect()
        storelib.init_db(conn)
        storelib.migrate(conn)  # applied_ops is a v19 table
        return conn

    def _apply(self, conn, op):
        envelope = dict(op)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            result = ops.apply_ops(
                conn, [envelope], principal="", principal_lookup=None,
                default_principal=_Legacy())
        return result

    def _row(self, conn, memory_id):
        return conn.execute(
            "SELECT trust_score, retrieval_count FROM memory WHERE id=?",
            (memory_id,)).fetchone()

    def _content_rows(self, conn, content):
        return conn.execute(
            "SELECT COUNT(*) FROM memory WHERE content=? AND superseded_at "
            "IS NULL", (content,)).fetchone()[0]

    def test_same_op_id_replay_applies_once(self):
        """A replayed op_id returns its recorded 'duplicate' verdict and
        writes nothing: row count, trust_score and retrieval_count are
        byte-for-byte the post-first-apply values.  Preserving pin — this
        holds at base (the applied_ops PRIMARY KEY is checked first) and
        must KEEP holding after the #363 change."""
        conn = self._connect()
        try:
            content = "363 same-op-id probe row (arm's-length check)"
            op = {"op_id": uuid.uuid4().hex, "op": "add",
                  "args": {"type": "fact", "content": content,
                           "namespace": "user:global"}}
            first = self._apply(conn, op)
            receipt = first["receipts"][0]
            self.assertEqual(receipt["status"], "committed", first)
            self.assertFalse(receipt["deduped"], first)
            memory_id = receipt["canonical_id"]
            after_first = self._row(conn, memory_id)
            self.assertEqual(self._content_rows(conn, content), 1)

            replay = self._apply(conn, op)
            replay_receipt = replay["receipts"][0]
            self.assertEqual(replay_receipt["status"], "duplicate", replay)
            self.assertEqual(replay_receipt["canonical_id"], memory_id,
                             replay)

            after_replay = self._row(conn, memory_id)
            self.assertEqual(self._content_rows(conn, content), 1,
                             "a replayed op_id must not add a row")
            self.assertAlmostEqual(after_replay["trust_score"],
                                   after_first["trust_score"], places=9,
                                   msg="trust_score moved on same-op_id replay")
            self.assertEqual(after_replay["retrieval_count"],
                             after_first["retrieval_count"],
                             "retrieval_count moved on same-op_id replay")
        finally:
            conn.close()

    def test_new_op_id_same_content_does_not_inflate_counters(self):
        """The reconnect retry shape: a NEW op_id carrying content the store
        already holds lands as a dedup FOLD receipt (committed + deduped=True
        — the current storelib/ops.py receipt contract) and must NOT inflate
        the keeper: exactly one live row, trust_score unchanged (no +0.05
        corroboration delta from a queue replay) and retrieval_count
        unchanged (no _merge_on_dedup bump).  RED at base — the fold path
        bumps both — GREEN once the applied-ops replay path applies the
        content exactly once with the keeper's counters untouched."""
        conn = self._connect()
        try:
            content = "363 new-op-id probe row (arm's-length check)"
            op_a = {"op_id": uuid.uuid4().hex, "op": "add",
                    "args": {"type": "fact", "content": content,
                             "namespace": "user:global"}}
            op_b = {"op_id": uuid.uuid4().hex, "op": "add",
                    "args": {"type": "fact", "content": content,
                             "namespace": "user:global"}}
            first = self._apply(conn, op_a)
            receipt = first["receipts"][0]
            self.assertEqual(receipt["status"], "committed", first)
            self.assertFalse(receipt["deduped"], first)
            memory_id = receipt["canonical_id"]
            self.assertEqual(self._content_rows(conn, content), 1)

            # Seed trust below the 1.0 clamp so the +0.05 corroboration
            # delta would be VISIBLE if the replay applied it (the plain
            # clamp at 1.0 would mask it).  adjust_trust is the engine's
            # own exported helper (storelib/links.py), clamped in SQL.
            links.adjust_trust(conn, memory_id, -0.20)
            seeded = self._row(conn, memory_id)
            self.assertAlmostEqual(seeded["trust_score"], 0.80, places=9)
            self.assertEqual(seeded["retrieval_count"], 0)

            second = self._apply(conn, op_b)
            second_receipt = second["receipts"][0]
            # Receipt statuses are the current contract (ops.py): a dedup
            # FOLD is committed + deduped=True, NOT "duplicate" (that word
            # is reserved for a replayed op_id).
            self.assertEqual(second_receipt["status"], "committed", second)
            self.assertTrue(second_receipt["deduped"], second)
            self.assertEqual(second_receipt["canonical_id"], memory_id,
                             second)

            self.assertEqual(
                self._content_rows(conn, content), 1,
                "re-applying the same content under a new op_id must fold "
                "into the existing keeper, never add a second row")
            after = self._row(conn, memory_id)
            self.assertAlmostEqual(
                after["trust_score"], seeded["trust_score"], places=9,
                msg="a queue replay of already-held content must not apply "
                    "the +0.05 trust corroboration delta to the keeper")
            self.assertEqual(
                after["retrieval_count"], seeded["retrieval_count"],
                "a queue replay of already-held content must not bump the "
                "keeper's retrieval_count")
        finally:
            conn.close()

    def test_replay_after_reconnect_shape(self):
        """A second connection to the same store file (the process-restart
        shape of a queue flush retry) still resolves the committed op_id
        from applied_ops: the replay is 'duplicate', the row set and the
        keeper's counters are unchanged.  Holds at base for the same-op_id
        leg and must keep holding after the #363 change."""
        conn = self._connect()
        content = "363 reconnect probe row (arm's-length check)"
        op = {"op_id": uuid.uuid4().hex, "op": "add",
              "args": {"type": "fact", "content": content,
                       "namespace": "user:global"}}
        first = self._apply(conn, op)
        receipt = first["receipts"][0]
        self.assertEqual(receipt["status"], "committed", first)
        memory_id = receipt["canonical_id"]
        before = self._row(conn, memory_id)
        conn.close()

        conn2 = self._connect()
        try:
            replay = self._apply(conn2, op)
            replay_receipt = replay["receipts"][0]
            self.assertEqual(replay_receipt["status"], "duplicate", replay)
            self.assertEqual(replay_receipt["canonical_id"], memory_id,
                             replay)
            after = self._row(conn2, memory_id)
            self.assertEqual(self._content_rows(conn2, content), 1)
            self.assertAlmostEqual(after["trust_score"],
                                   before["trust_score"], places=9,
                                   msg="trust_score moved across the "
                                       "reconnect replay")
            self.assertEqual(after["retrieval_count"],
                             before["retrieval_count"],
                             "retrieval_count moved across the reconnect "
                             "replay")
        finally:
            conn2.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
