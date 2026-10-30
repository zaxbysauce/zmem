"""SUPPLEMENT to AC1 (#414-1): the auth-parity MATRIX of the scoped-principal
rebuild in ``ops._PrincipalIndex.__call__`` (trace 414-416 supplement).

Every leg drives the REAL ``principal_lookup_from_env`` (temp
``ZMEM_MCP_TOKENS_FILE``) and ``storelib.ops.apply_ops`` in-process:

- (a) ABSENT ``namespaces`` key -> UNSCOPED legacy entry: an omitted-
  namespace add commits into ``user:global`` (legacy behavior STAYS);
- (b) JSON ``null`` namespaces -> identical to absent;
- (c) ``namespaces: []`` -> lookup yields NOTHING -> apply_ops fails CLOSED
  for EVERY kind (add/capture turn ``unknown_principal``; the mutating
  kinds stay ``capability_denied`` — the capability gate runs first —
  either way nothing commits);
- (d) ``namespaces: "project:allowed"`` (wrong TYPE) -> same fail-closed;
- (e) ``capabilities: []`` with a valid scope -> ``capability_denied`` for
  the mutating kinds, namespace gate still armed;
- (f) GREEN CONTROL: a scoped principal's EXPLICIT in-scope add commits
  inside ``project:allowed``.

The principal is NEVER a hand-built class.  Isolation follows the frozen
C1 exactly.  Standalone: ``python -X utf8 tests/\
test_ops_scoped_principal_parity_supp.py``
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

# -- env pinned BEFORE any storelib import AND before the issue-#244 store
# isolation guard loads (the guard reads the ambient env at import time).
_TMP = tempfile.mkdtemp(prefix="zmem-c1s-scoped-")
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


def _entry(principal: str, **overrides) -> dict:
    entry = {"principal": principal, "host": "host-supp",
             "harness": "harness-supp", "capabilities": ["mutate"],
             "revoked": False}
    entry.update(overrides)
    return entry


def _write_map(tmp: str, entries: list[dict]) -> str:
    path = Path(tmp) / "tokens-supp.json"
    path.write_text(json.dumps({"principals": entries}),
                    encoding="utf-8")
    return str(path)


class ScopedPrincipalMatrixTest(unittest.TestCase):
    """The token-map discipline of the scoped-principal rebuild."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-c1s-leg-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["ZMEM_STORE"] = os.path.join(self.tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = self.tmp
        storelib._refresh_env_state()
        self.conn = storelib.connect()
        storelib.init_db(self.conn)
        storelib.migrate(self.conn)
        self.addCleanup(self.conn.close)
        self.default_principal = ops.LegacyPrincipal()

    def _install(self, entries):
        os.environ["ZMEM_MCP_TOKENS_FILE"] = _write_map(self.tmp, entries)
        lookup = ops.principal_lookup_from_env()
        self.assertIsNotNone(lookup, "tokens map must load")
        return lookup

    def _apply(self, ops_list, principal, lookup):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            return ops.apply_ops(self.conn, [dict(o) for o in ops_list],
                                 principal=principal, principal_lookup=lookup,
                                 default_principal=self.default_principal)

    def _add(self, *, namespace=None, content="c1s"):
        args = {"type": "fact", "content": f"{content} {uuid.uuid4().hex}",
                "tags": "c1s", "source": "c1s"}
        if namespace is not None:
            args["namespace"] = namespace
        return {"op_id": uuid.uuid4().hex, "op": "add", "args": args}

    def _row_ns(self, receipt) -> str:
        row_id = receipt.get("canonical_id") or receipt.get("new_id")
        return self.conn.execute(
            "SELECT namespace FROM memory WHERE id=?", (row_id,)
        ).fetchone()["namespace"]

    def test_leg_a_absent_namespaces_stays_unscoped_legacy(self):
        lookup = self._install([_entry("p-unscoped")])
        receipt = self._apply([self._add()], "p-unscoped", lookup)["receipts"][0]
        self.assertEqual(receipt["status"], "committed", f"leg a: {receipt}")
        self.assertFalse(getattr(lookup("p-unscoped"), "scoped", False),
                         "leg a: an entry with NO namespaces key must "
                         "rebuild as an UNSCOPED legacy principal")
        self.assertEqual(self._row_ns(receipt), "user:global",
                         "leg a: an unscoped legacy entry's omitted-"
                         "namespace add must keep landing user:global")

    def test_leg_b_null_namespaces_same_as_absent(self):
        lookup = self._install([_entry("p-null", namespaces=None)])
        receipt = self._apply([self._add()], "p-null", lookup)["receipts"][0]
        self.assertEqual(receipt["status"], "committed", f"leg b: {receipt}")
        self.assertEqual(self._row_ns(receipt), "user:global",
                         "leg b: JSON null namespaces must behave exactly "
                         "like an absent key (unscoped legacy entry)")

    def test_leg_c_empty_namespaces_list_fails_closed_every_kind(self):
        lookup = self._install([_entry("p-empty", namespaces=[])])
        seed = self._apply([self._add(namespace="project:allowed")],
                           "", lookup)["receipts"][0]
        self.assertEqual(seed["status"], "committed")
        target = seed.get("canonical_id")
        batch = [
            self._add(content="c1s add"),
            {"op_id": uuid.uuid4().hex, "op": "capture",
             "args": {"type": "fact", "content": f"c1s cap {uuid.uuid4().hex}",
                      "tags": "c1s", "source": "c1s"}},
            {"op_id": uuid.uuid4().hex, "op": "update",
             "args": {"id_or_dep": target, "content": "c1s up",
                      "namespace": "user:global"}},
            {"op_id": uuid.uuid4().hex, "op": "supersede",
             "args": {"id_or_dep": target, "reason": "explicit correction"}},
            {"op_id": uuid.uuid4().hex, "op": "invalidate",
             "args": {"id_or_dep": target, "reason": "fact retired"}},
        ]
        receipts = self._apply(batch, "p-empty", lookup)["receipts"]
        for receipt in receipts:
            self.assertEqual(receipt["status"], "rejected",
                             f"leg c: namespaces:[] must fail closed for "
                             f"EVERY kind (got {receipt})")
            if receipt["op"] in ("add", "capture"):
                self.assertEqual(
                    receipt.get("error"), "unknown_principal",
                    f"leg c: the previously fail-open kinds must refuse "
                    f"with unknown_principal (got {receipt})")
            else:
                self.assertIn(receipt.get("error"),
                              ("unknown_principal", "capability_denied"),
                              "leg c: the mutating kinds must stay denied "
                              "(the capability gate precedes the "
                              "unknown-principal gate)")
        live = self.conn.execute(
            "SELECT COUNT(*) FROM memory WHERE superseded_at IS NULL"
        ).fetchone()[0]
        self.assertEqual(live, 1, "leg c: nothing but the seed row exists")

    def test_leg_d_string_namespaces_fails_closed(self):
        lookup = self._install([_entry("p-string",
                                       namespaces="project:allowed")])
        self.assertIsNone(lookup("p-string"),
                           "leg d: a WRONG-TYPE namespaces value (a string) "
                           "must resolve to NOTHING, never to an unscoped "
                           "principal")
        receipt = self._apply([self._add(namespace="project:allowed")],
                              "p-string", lookup)["receipts"][0]
        self.assertEqual(receipt.get("status"), "rejected",
                         f"leg d: {receipt}")
        self.assertIn(receipt.get("error"),
                      ("unknown_principal", "capability_denied"),
                      "leg d: wrong refusal class")

    def test_leg_e_empty_capabilities_denies_mutate_keeps_scope(self):
        lookup = self._install([_entry("p-caps", capabilities=[],
                                       namespaces=["project:allowed"])])
        in_scope = self._apply([self._add(namespace="project:allowed")],
                               "p-caps", lookup)["receipts"][0]
        self.assertEqual(in_scope["status"], "committed",
                         f"leg e in-scope add: {in_scope}")
        self.assertEqual(self._row_ns(in_scope), "project:allowed")
        out_of_scope = self._apply([self._add(namespace="user:global")],
                                   "p-caps", lookup)["receipts"][0]
        self.assertEqual(out_of_scope.get("status"), "rejected",
                         f"leg e: the namespace gate must stay armed under "
                         f"an empty capability set (got {out_of_scope})")
        self.assertEqual(out_of_scope.get("error"), "namespace_not_allowed")
        target = in_scope.get("canonical_id")
        update = {"op_id": uuid.uuid4().hex, "op": "update",
                  "args": {"id_or_dep": target,
                           "content": f"c1s up {uuid.uuid4().hex}"}}
        mutating = self._apply([update], "p-caps", lookup)["receipts"][0]
        self.assertEqual(mutating.get("status"), "rejected",
                         f"leg e: a mutating kind under capabilities:[] "
                         f"must be denied (got {mutating})")
        self.assertEqual(mutating.get("error"), "capability_denied")

    def test_leg_f_scoped_explicit_allowed_namespace_commits(self):
        lookup = self._install([_entry("p-scoped",
                                       namespaces=["project:allowed"])])
        receipt = self._apply([self._add(namespace="project:allowed")],
                              "p-scoped", lookup)["receipts"][0]
        self.assertEqual(receipt["status"], "committed",
                         f"leg f (green control): {receipt}")
        self.assertEqual(self._row_ns(receipt), "project:allowed",
                         "leg f: the committed row must land inside the "
                         "explicitly allowed namespace")


if __name__ == "__main__":
    unittest.main(verbosity=2)
