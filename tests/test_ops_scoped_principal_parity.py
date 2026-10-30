"""AC1 (#414-1): apply-ops must honor scoped principals from the REAL
tokens-file map (issue #414 item 1, frozen acceptance check C1).

The CLI applier resolves principals through ``ops.principal_lookup_from_env``
(``ZMEM_MCP_TOKENS_FILE``).  At base 471ea99 the rebuilt principal DROPS the
map entry's ``namespaces`` (a scoped entry is rebuilt as an unscoped
LegacyPrincipal), so:

- leg A: a scoped principal's add with an OMITTED namespace commits into
  ``user:global`` (outside its allow-list) instead of being refused with
  ``namespace_not_allowed``;
- leg B: a scoped principal's update re-keys a ``project:allowed`` row into
  ``user:global`` — a destination foreign to the principal (the dedicated
  update tool refuses this);
- leg C: a NON-EMPTY principal id that resolves to NOTHING fails OPEN for
  add (add/capture are not in ``_MUTATING_KINDS``, and an unresolved
  principal reads as unscoped).

The principal is NEVER a hand-built class: the map file is written to the
pinned temp dir and resolved via the real ``principal_lookup_from_env()``.
Isolation follows tests/test_ops_namespace_gate_order.py (env pinned to a
temp store BEFORE any storelib import; sys.modules purged; spec-load; the
operator store is never touched; no network; no model download).

Runs under pytest and standalone (CI convention):
``python -X utf8 tests/test_ops_scoped_principal_parity.py``
"""

from __future__ import annotations
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

ALLOWED = frozenset({"project:allowed"})

# -- env pinned BEFORE any storelib import AND before the issue-#244 store
# isolation guard loads (the guard reads the ambient env at import time; the
# pinned scratch store keeps it silent on a developer machine).
_TMP = tempfile.mkdtemp(prefix="zmem-c1-scoped-")
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
                "ZCODE_PLUGIN_DATA", "ZMEM_MCP_TOKENS_FILE",
                "ZMEM_MCP_TOKEN", "ZMEM_MCP_URL", "ZMEM_REMOTE_URL")
_SAVED_ENV = {key: os.environ.get(key) for key in _PINNED_KEYS}

_pkg_dir = SCRIPTS_DIR / "storelib"
_spec = importlib.util.spec_from_file_location(
    "storelib", _pkg_dir / "__init__.py",
    submodule_search_locations=[str(_pkg_dir)])
storelib = importlib.util.module_from_spec(_spec)
sys.modules["storelib"] = storelib
_spec.loader.exec_module(storelib)
import storelib.ops as ops  # noqa: E402


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


def _write_tokens_map(tmp: str) -> str:
    """The real CLI/hub principal map: ONE scoped principal allowed only
    project:allowed with the mutate capability."""
    path = Path(tmp) / "tokens.json"
    path.write_text(json.dumps({"principals": [{
        "principal": "p-scoped", "host": "host-a", "harness": "harness-a",
        "namespaces": ["project:allowed"], "capabilities": ["mutate"],
        "revoked": False,
    }]}), encoding="utf-8")
    return str(path)


class ScopedPrincipalParityTest(unittest.TestCase):
    """apply_ops must not widen a scoped principal into an unscoped one."""

    def setUp(self):
        # Fresh temp store per test: STORE_PATH is a frozen module global,
        # so re-point env then re-derive it (the store.py shim's own load
        # step) before connecting.
        self.tmp = tempfile.mkdtemp(prefix="zmem-c1-leg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["ZMEM_STORE"] = os.path.join(self.tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = self.tmp
        os.environ["ZMEM_MCP_TOKENS_FILE"] = _write_tokens_map(self.tmp)
        storelib._refresh_env_state()
        self.conn = storelib.connect()
        storelib.init_db(self.conn)
        storelib.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.lookup = ops.principal_lookup_from_env()
        self.assertIsNotNone(self.lookup, "tokens map must load")
        self.default_principal = ops.LegacyPrincipal()

    def _apply(self, op, principal):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            return ops.apply_ops(self.conn, [dict(op)], principal=principal,
                                 principal_lookup=self.lookup,
                                 default_principal=self.default_principal)

    def _row(self, row_id):
        return self.conn.execute(
            "SELECT namespace, superseded_at FROM memory WHERE id=?",
            (row_id,)).fetchone()

    def test_leg_a_omitted_namespace_add_stays_in_scope(self):
        op = {"op_id": uuid.uuid4().hex, "op": "add",
              "args": {"type": "fact", "content": "c1 leg a " + uuid.uuid4().hex,
                       "tags": "c1", "source": "c1"}}
        receipt = self._apply(op, "p-scoped")["receipts"][0]
        if receipt["status"] == "committed":
            row_id = receipt.get("canonical_id") or receipt.get("new_id")
            row = self._row(row_id)
            self.assertIn(
                row["namespace"], ALLOWED,
                "leg A: a scoped principal's omitted-namespace add committed "
                f"into {row['namespace']!r} (outside the allow-list "
                f"{sorted(ALLOWED)}); it must be rejected with "
                "namespace_not_allowed or land inside the allow-list")
        else:
            self.assertEqual(receipt["status"], "rejected",
                             f"leg A: unexpected receipt {receipt}")
            self.assertEqual(
                receipt.get("error"), "namespace_not_allowed",
                f"leg A: wrong refusal code {receipt}")

    def test_leg_b_update_rekey_into_foreign_namespace_refused(self):
        seed = {"op_id": uuid.uuid4().hex, "op": "add",
                "args": {"type": "fact", "namespace": "project:allowed",
                         "content": "c1 leg b seed " + uuid.uuid4().hex,
                         "tags": "c1", "source": "c1"}}
        seed_receipt = self._apply(seed, "p-scoped")["receipts"][0]
        self.assertEqual(seed_receipt["status"], "committed",
                         f"leg B seed must commit: {seed_receipt}")
        seed_id = (seed_receipt.get("canonical_id")
                   or seed_receipt.get("new_id"))
        update = {"op_id": uuid.uuid4().hex, "op": "update",
                  "args": {"id_or_dep": seed_id, "namespace": "user:global",
                           "content": "c1 leg b v2 " + uuid.uuid4().hex}}
        receipt = self._apply(update, "p-scoped")["receipts"][0]
        self.assertNotEqual(
            receipt["status"], "committed",
            "leg B: a scoped principal re-keyed a project:allowed row into "
            "user:global (a namespace foreign to the principal) — the "
            "dedicated update tool refuses this and apply-ops must too: "
            f"{receipt}")

    def test_leg_c_unresolved_principal_add_refused(self):
        op = {"op_id": uuid.uuid4().hex, "op": "add",
              "args": {"type": "fact", "namespace": "project:allowed",
                       "content": "c1 leg c " + uuid.uuid4().hex,
                       "tags": "c1", "source": "c1"}}
        receipt = self._apply(op, "ghost-unknown")["receipts"][0]
        self.assertNotEqual(
            receipt["status"], "committed",
            "leg C: a non-empty principal id resolving to NOTHING failed "
            f"OPEN for add (receipt {receipt}); it must be rejected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
