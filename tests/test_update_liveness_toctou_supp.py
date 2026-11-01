"""SUPPLEMENT to AC4 (#416-1): update_memory's TOCTOU refusal must not
damage a transaction it does not own, and must fire under the pinned
variant too (trace 414-416 supplement).

Both legs reuse the frozen C4 interleave: writer B paused after its
pre-lock liveness read by a one-shot ``writelib._validate_namespace``
hook; writer A commits a full update of the same row inside the window.

- (a) OUTER-TRANSACTION leg: B's caller (the apply_ops pattern — one
  ``BEGIN IMMEDIATE`` for the batch, update_memory joining it) owns the
  transaction on B's connection.  The caller's BEGIN is opened inside the
  window — AFTER A's commit, BEFORE update_memory's begin-or-join
  decision — so B runs with ``started_tx=False``.  When the under-lock
  superseded check fires, update_memory must NOT roll back the caller's
  transaction: conn_b stays in_transaction, the caller's own rollback
  completes cleanly, A's tombstone is untouched, ONE live successor.
  (SQLite's single-writer lock means the caller's transaction can only be
  opened in that window — held earlier, writer A could never commit — so
  the window-open is the one deterministic composition of the shape.)
- (b) PINNED-VARIANT leg: the same interleave with
  ``expected_old_namespace`` armed to the row's real namespace.  B must
  still refuse — the under-lock superseded check fires BEFORE the pinned
  tombstone write ("already superseded", not the namespace-guard message)
  — ONE live successor, A's tombstone timestamps unchanged; B's own
  (self-owned) transaction IS rolled back.

Issue #420 supplement (frozen C8 parity) — the stale taint/namespace
derive legs.  The under-lock re-read must revalidate the fields the
successor DERIVES from the pre-lock snapshot, not just liveness; an
in-place (non-superseding) mutation committed on a separate connection
inside the same ``_validate_namespace`` window must refuse:

- (c) TAINT-RAISE leg: ``UPDATE memory SET taint='untrusted_web'`` in the
  window -> ValueError naming the drift ("changed under the write lock"
  + "taint"), NO successor minted, parent still live (now untrusted_web);
- (d) NAMESPACE-REKEY leg: ``UPDATE memory SET namespace='user:rekeyed'``
  in the window -> refusal naming namespace, no successor, parent live;
- (e) NO-DRIFT control: the same window shape with a value-preserving
  write (``SET taint=taint``) must NOT refuse — the drift check compares
  VALUES, so a no-op concurrent write cannot spuriously refuse and the
  update commits (one live successor, parent tombstoned).

Isolation follows the frozen C4 exactly.  Standalone:
``python -X utf8 tests/test_update_liveness_toctou_supp.py``
"""

from __future__ import annotations
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

A_NOW = "2030-01-01T00:00:00Z"
B_NOW = "2031-06-15T12:00:00Z"

# -- env pinned BEFORE any storelib import (and before the issue-#244 store
# isolation guard loads, which reads the ambient env at import time).
_TMP = tempfile.mkdtemp(prefix="zmem-c4s-toctou-")
os.environ.update({
    "ZMEM_HOME": str(REPO_ROOT),
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_BACKUP_DIR", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
           "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_TOKEN", "ZMEM_MCP_URL",
           "ZMEM_REMOTE_URL"):
    os.environ.pop(_k, None)
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

_SAVED_MODULES = {key: value for key, value in list(sys.modules.items())
                  if key == "storelib" or key.startswith("storelib.")}
for _name in _SAVED_MODULES:
    del sys.modules[_name]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
    _PATH_INSERTED = True
else:
    _PATH_INSERTED = False
_PINNED_KEYS = ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING",
                "ZMEM_BACKUP_DIR", "CLAUDE_PLUGIN_DATA",
                "ZCODE_PLUGIN_DATA", "ZMEM_MCP_TOKENS_FILE",
                "ZMEM_MCP_TOKEN", "ZMEM_MCP_URL", "ZMEM_REMOTE_URL")
_SAVED_ENV = {key: os.environ.get(key) for key in _PINNED_KEYS}

import storelib  # noqa: E402
from storelib import write as writelib  # noqa: E402


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


def _quiet(fn, *args, **kwargs):
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        return fn(*args, **kwargs)


class UpdateToctouSupplementTest(unittest.TestCase):
    """The refusal's blast radius under caller-owned and pinned variants."""

    def setUp(self):
        # Fresh temp store per test: STORE_PATH is a frozen module global,
        # so re-point env then re-derive it before connecting.
        self.tmp = tempfile.mkdtemp(prefix="zmem-c4s-leg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["ZMEM_STORE"] = os.path.join(self.tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = self.tmp
        storelib._refresh_env_state()
        self.store_path = os.path.join(self.tmp, "store.sqlite")

    def _connect(self):
        conn = storelib.connect()
        storelib.init_db(conn)
        storelib.migrate(conn)
        self.addCleanup(conn.close)
        return conn

    def _seed(self, conn) -> str:
        return str(_quiet(
            writelib.add_memory,
            conn, namespace="user:global", type_="fact",
            content="c4s live fact " + uuid.uuid4().hex,
            tags="c4s", source_ref="c4s", signal="test"))

    def _tombstone(self, conn, mid):
        row = conn.execute(
            "SELECT superseded_at, valid_until, supersede_reason "
            "FROM memory WHERE id=?", (mid,)).fetchone()
        return tuple(row)

    def _live_successors(self, mid):
        conn = sqlite3.connect(self.store_path)
        try:
            return [r[0] for r in conn.execute(
                "SELECT id FROM memory WHERE update_of=? "
                "AND superseded_at IS NULL", (mid,))]
        finally:
            conn.close()

    def _run_b(self, conn_b, parent, *, expected_ns=None,
               in_hook=None):
        """The frozen C4 interleave: B paused after its pre-lock liveness
        read by a one-shot _validate_namespace hook; writer A commits a
        full update of the same row inside the window.  ``in_hook`` runs
        after A's commit (leg a opens the caller's transaction there)."""
        conn_a = self._connect()
        original = writelib._validate_namespace
        state = {"fired": False, "tomb_after_a": None}

        def hooked(conn, ns):
            result = original(conn, ns)
            if not state["fired"]:
                state["fired"] = True
                _quiet(writelib.update_memory, conn_a, mid=parent,
                       content="c4s writer A content " + uuid.uuid4().hex,
                       supersede_reason="updated", now=A_NOW)
                state["tomb_after_a"] = self._tombstone(conn_a, parent)
                if in_hook is not None:
                    in_hook()
            return result

        kwargs = dict(mid=parent,
                      content="c4s writer B content " + uuid.uuid4().hex,
                      supersede_reason="explicit correction", now=B_NOW)
        if expected_ns is not None:
            kwargs["expected_old_namespace"] = expected_ns
        writelib._validate_namespace = hooked
        b_error = None
        try:
            _quiet(writelib.update_memory, conn_b, **kwargs)
        except ValueError as exc:
            b_error = str(exc)
        finally:
            writelib._validate_namespace = original
        return b_error, state

    def test_leg_a_caller_owned_transaction_survives_refusal(self):
        conn_b = self._connect()
        parent = self._seed(conn_b)

        def open_caller_tx():
            # B's CALLER owns the outer BEGIN IMMEDIATE (the apply_ops
            # batch pattern) — opened in the window, after A's commit, so
            # B's update joins a transaction it does NOT own.
            conn_b.execute("BEGIN IMMEDIATE")

        b_error, state = self._run_b(conn_b, parent, in_hook=open_caller_tx)
        self.assertIsNotNone(
            b_error,
            "leg a: writer B must refuse on the stale view (writer A "
            "superseded the row in the read-to-lock window)")
        self.assertIn("superseded", b_error.lower(),
                      f"leg a: refusal must mention superseded: {b_error!r}")
        self.assertTrue(
            conn_b.in_transaction,
            "leg a: update_memory rolled back a transaction it does NOT "
            "own — the caller's outer BEGIN IMMEDIATE must still be open "
            "after the refusal")
        conn_b.rollback()  # the caller's own rollback must succeed cleanly
        self.assertFalse(
            conn_b.in_transaction,
            "leg a: the caller's rollback must complete cleanly")
        self.assertEqual(
            state["tomb_after_a"], self._tombstone(conn_b, parent),
            "leg a: A's tombstone triple must be unchanged by B's refused "
            "attempt")
        live = self._live_successors(parent)
        self.assertEqual(
            len(live), 1,
            f"leg a: exactly ONE live successor must remain (found {live})")

    def test_leg_b_pinned_namespace_variant_still_refuses(self):
        conn_b = self._connect()
        parent = self._seed(conn_b)
        b_error, state = self._run_b(conn_b, parent,
                                     expected_ns="user:global")
        self.assertIsNotNone(
            b_error, "leg b: the pinned variant must refuse too")
        self.assertIn(
            "already superseded", b_error.lower(),
            f"leg b: the UNDER-LOCK superseded check must fire BEFORE the "
            f"pinned tombstone write (got {b_error!r})")
        self.assertFalse(
            conn_b.in_transaction,
            "leg b: B owned its transaction here — the refusal must roll "
            "it back (contrast with leg a's caller-owned transaction)")
        self.assertEqual(
            state["tomb_after_a"], self._tombstone(conn_b, parent),
            "leg b: A's tombstone timestamps must be unchanged (a B "
            "re-tombstone would move them to B_NOW and the 'explicit "
            "correction' reason)")
        live = self._live_successors(parent)
        self.assertEqual(
            len(live), 1,
            f"leg b: exactly ONE live successor must remain (found {live})")

    def _interleave_inplace(self, conn_b, mid, mutate_sql, mutate_params=()):
        """The frozen C8 interleave (issue #420): B paused between its
        pre-lock ``SELECT *`` and the ``BEGIN IMMEDIATE`` by the one-shot
        _validate_namespace hook; a racing writer commits an IN-PLACE
        (non-superseding) mutation on its OWN connection inside the window.
        Returns the ValueError message (None when update_memory returned
        normally)."""
        conn_a = self._connect()  # the racing writer's own connection
        original = writelib._validate_namespace
        state = {"fired": False}

        def hooked(conn, ns):
            result = original(conn, ns)
            if not state["fired"]:
                state["fired"] = True
                conn_a.execute(mutate_sql, mutate_params)
                conn_a.commit()
            return result

        error = None
        writelib._validate_namespace = hooked
        try:
            _quiet(writelib.update_memory, conn_b, mid=mid,
                   content="c4s replacement " + uuid.uuid4().hex,
                   supersede_reason="updated", now=B_NOW)
        except ValueError as exc:
            error = str(exc)
        finally:
            writelib._validate_namespace = original
            conn_a.close()
        return error

    def _row(self, mid):
        conn = sqlite3.connect(self.store_path)
        conn.row_factory = sqlite3.Row
        try:
            return conn.execute(
                "SELECT taint, namespace, superseded_at FROM memory "
                "WHERE id=?", (mid,)).fetchone()
        finally:
            conn.close()

    def test_leg_c_taint_raise_in_window_refuses_stale_derive(self):
        conn_b = self._connect()
        parent = self._seed(conn_b)
        error = self._interleave_inplace(
            conn_b, parent,
            "UPDATE memory SET taint='untrusted_web' WHERE id=?", (parent,))
        row = self._row(parent)
        self.assertEqual(
            row["taint"], "untrusted_web",
            f"leg c: the racing writer's taint raise must land on the "
            f"parent (found {tuple(row)!r})")
        self.assertIsNotNone(
            error,
            "leg c: update_memory must refuse a successor whose taint "
            "lineage derives from the pre-lock view (the row was raised to "
            "untrusted_web in the read-to-lock window)")
        self.assertIn(
            "changed under the write lock", error,
            f"leg c: the refusal must name the drift: {error!r}")
        self.assertIn("taint", error.lower(),
                      f"leg c: the refusal must name taint: {error!r}")
        self.assertEqual(
            self._live_successors(parent), [],
            "leg c: a refused update must mint NO successor")
        self.assertIsNone(
            row["superseded_at"],
            f"leg c: the parent must stay live (found {tuple(row)!r})")

    def test_leg_d_namespace_rekey_in_window_refuses_stale_derive(self):
        conn_b = self._connect()
        parent = self._seed(conn_b)
        error = self._interleave_inplace(
            conn_b, parent,
            "UPDATE memory SET namespace='user:rekeyed' WHERE id=?",
            (parent,))
        row = self._row(parent)
        self.assertEqual(
            row["namespace"], "user:rekeyed",
            f"leg d: the racing writer's rekey must land on the parent "
            f"(found {tuple(row)!r})")
        self.assertIsNotNone(
            error,
            "leg d: update_memory must refuse a successor minted into the "
            "pre-rekey namespace derived from the pre-lock view")
        self.assertIn(
            "changed under the write lock", error,
            f"leg d: the refusal must name the drift: {error!r}")
        self.assertIn("namespace", error.lower(),
                      f"leg d: the refusal must name namespace: {error!r}")
        self.assertEqual(
            self._live_successors(parent), [],
            "leg d: a refused update must mint NO successor")
        self.assertIsNone(
            row["superseded_at"],
            f"leg d: the parent must stay live (found {tuple(row)!r})")

    def test_leg_e_no_drift_control_still_commits(self):
        conn_b = self._connect()
        parent = self._seed(conn_b)
        error = self._interleave_inplace(
            conn_b, parent,
            "UPDATE memory SET taint=taint WHERE id=?", (parent,))
        self.assertIsNone(
            error,
            f"leg e (no-drift control): a value-preserving concurrent write "
            f"must NOT refuse (the drift check compares VALUES, not write "
            f"activity): {error!r}")
        row = self._row(parent)
        self.assertIsNotNone(
            row["superseded_at"],
            f"leg e: the parent must be tombstoned by the successful "
            f"update (found {tuple(row)!r})")
        live = self._live_successors(parent)
        self.assertEqual(
            len(live), 1,
            f"leg e: the successful update must mint exactly ONE live "
            f"successor (found {live})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
