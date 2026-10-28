"""PR #409 feedback (copilot FB-COP-1): the namespace gate precedes the
applied-ops replay guard.

At the first shipped head of the #363 replay guard the guard ran BEFORE
_namespace_allowed for add/capture ops, so a SCOPED principal whose
allow-list excluded the namespace received a success receipt
(``committed`` + ``deduped=True`` + the live row's canonical id) for a
byte-identical replay instead of ``namespace_not_allowed`` — a spurious
success on an unauthorized write path and a cross-namespace
content-existence oracle.  Reproduced by an executed probe on head
ea2fbf3d; this file pins the corrected order:

- an add to a namespace OUTSIDE a scoped principal's allow-list is
  ``namespace_not_allowed`` even when identical content is already live;
- the same replay under a principal whose allow-list INCLUDES the
  namespace still folds (``committed`` + ``deduped=True``) — the #363 AC2
  idempotency contract is unchanged for authorized callers
  (tests/test_applied_ops_idempotency.py owns that pin in depth).

Isolation follows tests/test_applied_ops_idempotency.py: env pinned to a
per-run temp store BEFORE storelib is spec-loaded; the operator store is
never touched; no network; no model download.

Runs under pytest and standalone (CI convention):
``python -X utf8 tests/test_ops_namespace_gate_order.py``
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


def setUpModule():  # noqa: N802 - unittest hook
    global _SAVED_ENV, _SAVED_MODULES, _TMP, _PATH_INSERTED
    global storelib, ops
    _TMP = tempfile.mkdtemp(prefix="zmem-gate-order-363-")
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
    import importlib as _importlib
    ops = _importlib.import_module("storelib.ops")


def tearDownModule():  # noqa: N802 - unittest hook
    import shutil
    global storelib, ops
    storelib = None
    ops = None
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
    """Unscoped operator principal (CLI startup semantics)."""

    revoked = False
    capabilities = None
    principal_id = "legacy"
    scoped = False
    namespaces = None


class _ScopedPeer:
    """Scoped principal allowed ONLY user:global — project:demo is not."""

    revoked = False
    capabilities = None
    principal_id = "scoped-peer"
    scoped = True
    namespaces = frozenset({"user:global"})


class NamespaceGateOrderTest(unittest.TestCase):
    """The gate fires before the replay guard for add/capture ops."""

    def _connect(self):
        conn = storelib.connect()
        storelib.init_db(conn)
        storelib.migrate(conn)
        return conn

    def _apply(self, conn, op, principal):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            return ops.apply_ops(conn, [dict(op)], principal="",
                                 principal_lookup=None,
                                 default_principal=principal)

    def test_replay_into_disallowed_namespace_is_refused(self):
        conn = self._connect()
        content = "gate-order " + uuid.uuid4().hex
        seed = {"op_id": uuid.uuid4().hex, "op": "add",
                "args": {"namespace": "project:demo", "content": content,
                         "tags": "probe", "source": "probe"}}
        replay = {"op_id": uuid.uuid4().hex, "op": "add",
                  "args": {"namespace": "project:demo", "content": content,
                           "tags": "probe", "source": "probe"}}
        first = self._apply(conn, seed, _Legacy())
        self.assertEqual(first["receipts"][0]["status"], "committed")
        keeper_id = first["receipts"][0]["canonical_id"]

        result = self._apply(conn, replay, _ScopedPeer())
        receipt = result["receipts"][0]
        self.assertEqual(
            receipt["status"], "rejected",
            "a namespace-gated replay must be refused, not folded: "
            f"{receipt}")
        self.assertEqual(receipt.get("error"), "namespace_not_allowed")
        # The refusal must neither claim a fold nor disclose the live
        # row's canonical id.
        self.assertFalse(receipt.get("deduped"))
        self.assertNotEqual(receipt.get("canonical_id"), keeper_id)

    def test_authorized_replay_still_folds(self):
        conn = self._connect()
        content = "gate-order-authorized " + uuid.uuid4().hex
        seed = {"op_id": uuid.uuid4().hex, "op": "add",
                "args": {"namespace": "project:demo", "content": content,
                         "tags": "probe", "source": "probe"}}
        replay = {"op_id": uuid.uuid4().hex, "op": "add",
                  "args": {"namespace": "project:demo", "content": content,
                           "tags": "probe", "source": "probe"}}
        self._apply(conn, seed, _Legacy())
        result = self._apply(conn, replay, _Legacy())
        receipt = result["receipts"][0]
        self.assertEqual(receipt["status"], "committed")
        self.assertTrue(receipt.get("deduped"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
