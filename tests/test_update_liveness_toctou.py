"""AC4 (#416-1): update_memory liveness TOCTOU — one live row, ONE live
successor (frozen acceptance check C4).

Deterministic interleave (no timing race, per the executed probe): writer
B's ``update_memory`` is paused AFTER its pre-lock liveness read by a
one-shot hook on ``writelib._validate_namespace`` (called between the
2298-2316 checks and the BEGIN IMMEDIATE); inside the hook writer A
completes a FULL update of the same row on a second connection.  B then
resumes with a stale view of the parent.

At base 471ea99 B commits anyway -> TWO live successors share one
``update_of`` parent, and B's unconditional tombstone UPDATE overwrites
A's tombstone -> leg RED.  The pin at head: B raises ValueError
mentioning ``superseded``; exactly ONE live successor remains; the
parent's (superseded_at, valid_until, supersede_reason) captured after A
are UNCHANGED after B's attempt (tombstone immutability — distinct `now`
values and distinct reasons make the overwrite observable).

Second test (PRESERVING-style pin, green at base too): a DIRECT
re-tombstone attempt on an already-superseded row raises — the refusal
message shape is pinned.

Isolation: env pinned to a per-run temp store BEFORE any storelib import;
sys.modules purged; fresh store per test via storelib._refresh_env_state;
no network; no models.  Runs under pytest and standalone (CI convention):
``python -X utf8 tests/test_update_liveness_toctou.py``
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
_TMP = tempfile.mkdtemp(prefix="zmem-c4-toctou-")
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
for _name in list(_SAVED_MODULES):
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
    """Restore the ambient env / module cache (pytest co-run citizenship;
    the tests/test_ops_namespace_gate_order.py discipline)."""
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


class UpdateLivenessToctouTest(unittest.TestCase):
    """Concurrent updates of one live row leave ONE live successor."""

    def setUp(self):
        # Fresh temp store per test: STORE_PATH is a frozen module global,
        # so re-point env then re-derive it before connecting.
        self.tmp = tempfile.mkdtemp(prefix="zmem-c4-leg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["ZMEM_STORE"] = os.path.join(self.tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = self.tmp
        storelib._refresh_env_state()
        self.store_path = os.path.join(self.tmp, "store.sqlite")

    def _connect(self, label):
        conn = storelib.connect()
        storelib.init_db(conn)
        storelib.migrate(conn)
        self.addCleanup(conn.close)
        return conn

    def _seed(self, conn) -> str:
        return str(_quiet(
            writelib.add_memory,
            conn, namespace="user:global", type_="fact",
            content="c4 live fact " + uuid.uuid4().hex,
            tags="c4", source_ref="c4", signal="test"))

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

    def test_leg1_interleave_single_live_successor(self):
        conn_b = self._connect("b")
        conn_a = self._connect("a")
        parent = self._seed(conn_b)

        original = writelib._validate_namespace
        state = {"fired": False, "tomb_after_a": None}

        def hooked(conn, ns):
            result = original(conn, ns)
            if not state["fired"]:
                state["fired"] = True
                # Writer A runs to completion while B is paused after its
                # pre-lock liveness read, before B's BEGIN IMMEDIATE.
                _quiet(writelib.update_memory, conn_a, mid=parent,
                       content="c4 writer A content " + uuid.uuid4().hex,
                       supersede_reason="updated", now=A_NOW)
                state["tomb_after_a"] = self._tombstone(conn_b, parent)
            return result

        writelib._validate_namespace = hooked
        b_error = None
        try:
            _quiet(writelib.update_memory, conn_b, mid=parent,
                   content="c4 writer B content " + uuid.uuid4().hex,
                   supersede_reason="explicit correction", now=B_NOW)
        except ValueError as exc:
            b_error = str(exc)
        finally:
            writelib._validate_namespace = original

        self.assertIsNotNone(
            b_error,
            "liveness TOCTOU: writer B committed while holding a stale view "
            "of the parent (writer A had already superseded it) — B must "
            "raise ValueError mentioning 'superseded'")
        self.assertIn("superseded", b_error.lower(),
                      f"writer B's refusal must mention superseded: "
                      f"{b_error!r}")
        tomb_final = self._tombstone(conn_b, parent)
        self.assertEqual(
            state["tomb_after_a"], tomb_final,
            "tombstone immutability: the parent's (superseded_at, "
            "valid_until, supersede_reason) captured after writer A must "
            "be UNCHANGED after B's refused attempt")
        live = self._live_successors(parent)
        self.assertEqual(
            len(live), 1,
            f"exactly ONE live successor must remain (found {live}) — "
            "the single-successor lineage is broken")

    def test_leg2_direct_retombstone_refused(self):
        conn = self._connect("main")
        parent = self._seed(conn)
        _quiet(writelib.update_memory, conn, mid=parent,
               content="c4 v2 " + uuid.uuid4().hex,
               supersede_reason="updated", now=A_NOW)
        with self.assertRaises(ValueError) as ctx:
            _quiet(writelib.update_memory, conn, mid=parent,
                   content="c4 v3 " + uuid.uuid4().hex,
                   supersede_reason="updated", now=B_NOW)
        self.assertIn("superseded", str(ctx.exception).lower(),
                      "the refusal must say the row is already superseded")
        self.assertEqual(len(self._live_successors(parent)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
