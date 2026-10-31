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

Issue #420 supplement (frozen C11 parity) — LEGACY single-token mode's
target-scope class: a SCOPED legacy identity (the ``--namespace-scope``
passthrough the hub forwards for single-token callers the engine has no
map entry for — cli.py builds exactly this ``LegacyPrincipal`` with
``scoped=True`` + an allow-list) must get the SAME in-transaction
enforcement map-mode principals get, on the ENGINE's own target
resolution (``id_or_dep``-first) and the ``id`` alias alike: foreign
by-id mutation targets refuse per-op (``namespace_not_allowed``) with no
successor and a live parent, destination rekeys refuse, in-scope targets
still commit, and mixed batches keep per-op receipts.  The class also
pins the CLI flag surface: ``--namespace-scope`` refuses ``--principal``
and malformed lists with exit 2.

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
import subprocess
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

    def test_hub_forwarding_condition_structural(self):
        """F1 (reviews PRR-001/F1): the MCP apply-ops handler forwards
        --principal for ANY map principal (mode-aware), and the old
        capabilities-gated forward is GONE from the handler. Structural
        pin (repo convention: t09-s2); the engine leg is behaviorally
        pinned by the frozen C1 + the executed probe recorded in the
        review artifacts — this leg cannot drive the hub tool itself
        (the SDK route is not importable here), which is its disclosed
        limit."""
        src = (REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py")
        text = src.read_text(encoding="utf-8")
        start = text.index("async def apply_ops")
        end = text.index("async def ", start + 10)
        segment = text[start:end]
        self.assertGreaterEqual(
            segment.count("if principal_map is not None and getattr("), 2,
            "the map-mode forward condition must appear in BOTH routing "
            "branches of the apply_ops handler")
        self.assertNotIn(
            'getattr(resolved, "capabilities", None) is not None',
            segment,
            "the old capabilities-gated forward must be absent from the "
            "handler")

    def test_map_principal_target_row_foreign_namespace_refused(self):
        """F7/TC-06: the TARGET-ROW scope gate (mutations refuse when the
        target row lives outside the allow-list) composed with a
        MAP-DERIVED scoped principal via the REAL map loader (the frozen
        t09 pin uses a hand-built class; the frozen C1 covers the
        destination-namespace half)."""
        lookup = self._install([
            _entry("p-seed-unscoped"),
            _entry("p-tc6", namespaces=["project:allowed"],
                   capabilities=["mutate"]),
        ])
        seed = self._add(namespace="user:global", content="tc6 seed")
        seed_receipt = self._apply([seed], "p-seed-unscoped",
                                   lookup)["receipts"][0]
        self.assertEqual(seed_receipt["status"], "committed",
                         f"TC-06 seed must commit: {seed_receipt}")
        seed_id = (seed_receipt.get("canonical_id")
                   or seed_receipt.get("new_id"))
        op = {"op_id": uuid.uuid4().hex, "op": "update",
              "args": {"id_or_dep": seed_id,
                       "content": "tc6 update attempt "
                                  + uuid.uuid4().hex}}
        receipt = self._apply([op], "p-tc6", lookup)["receipts"][0]
        self.assertEqual(receipt["status"], "rejected", receipt)
        self.assertEqual(
            receipt.get("error"), "namespace_not_allowed",
            f"TC-06: the map-derived scoped principal's update of a "
            f"foreign-namespace TARGET row must be "
            f"namespace_not_allowed, got {receipt}")

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


FOREIGN_NS = "project:outside"


class LegacyScopedTargetScopeTest(unittest.TestCase):
    """Issue #420 (frozen C11 parity): a SCOPED LEGACY identity — the
    engine-side representation of a scoped single-token caller (the hub
    forwards ``--namespace-scope`` and cli.py builds this exact
    ``LegacyPrincipal`` with ``scoped=True`` + the allow-list) — enforces
    the by-id TARGET scope on the engine's own resolution: foreign by-id
    mutation targets are per-op ``namespace_not_allowed`` refusals with no
    successor and a live parent, destination rekeys refuse, in-scope
    targets commit, and mixed batches keep per-op receipts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-c1s-legacy-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        os.environ["ZMEM_STORE"] = os.path.join(self.tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = self.tmp
        storelib._refresh_env_state()
        self.conn = storelib.connect()
        storelib.init_db(self.conn)
        storelib.migrate(self.conn)
        self.addCleanup(self.conn.close)
        # The scoped legacy identity, built exactly like the fixed cli.py
        # builds it for --namespace-scope (LegacyPrincipal narrowed to an
        # allow-list) — never a hand-rolled duck type.
        self.scoped = ops.LegacyPrincipal()
        self.scoped.namespaces = frozenset({"user:global"})
        self.scoped.scoped = True

    def _apply(self, ops_list, default_principal):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            return ops.apply_ops(self.conn, [dict(o) for o in ops_list],
                                 principal="", principal_lookup=None,
                                 default_principal=default_principal)

    def _apply_scoped(self, ops_list):
        return self._apply(ops_list, self.scoped)

    def _seed(self, namespace, content="c1s target"):
        """Seed through the engine's own path under the UNSCOPED legacy
        identity (the operator route — every namespace is in scope)."""
        op = {"op_id": uuid.uuid4().hex, "op": "add",
              "args": {"type": "fact",
                       "content": f"{content} {uuid.uuid4().hex}",
                       "namespace": namespace, "tags": "c1s",
                       "source": "c1s"}}
        receipt = self._apply([op], ops.LegacyPrincipal())["receipts"][0]
        self.assertEqual(receipt["status"], "committed",
                         f"seed must commit: {receipt}")
        return receipt["canonical_id"]

    def _successors(self, mid):
        return [row[0] for row in self.conn.execute(
            "SELECT id FROM memory WHERE update_of=? "
            "AND superseded_at IS NULL", (mid,))]

    def _assert_parent_live(self, mid, leg):
        row = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?", (mid,)).fetchone()
        self.assertIsNotNone(row, f"{leg}: the parent row must still exist")
        self.assertIsNone(row[0], f"{leg}: the parent must stay live "
                                  f"(superseded_at={row[0]!r})")
        self.assertEqual(self._successors(mid), [],
                         f"{leg}: a refused op must mint NO successor")

    def test_legacy_a_foreign_by_id_update_via_id_refused(self):
        """(a) the ``id`` alias (not the engine's primary selector) naming
        a foreign-namespace target refuses per-op."""
        target = self._seed(FOREIGN_NS)
        op = {"op_id": uuid.uuid4().hex, "op": "update",
              "args": {"id": target,
                       "content": f"c1s pwned {uuid.uuid4().hex}"}}
        receipt = self._apply_scoped([op])["receipts"][0]
        self.assertEqual(receipt["status"], "rejected",
                         f"leg a: {receipt}")
        self.assertEqual(receipt.get("error"), "namespace_not_allowed",
                         f"leg a: the refusal must be namespace_not_allowed "
                         f"(got {receipt})")
        self._assert_parent_live(target, "leg a")

    def test_legacy_b_foreign_by_id_via_id_or_dep_all_kinds_refused(self):
        """(b) the PRIMARY selector ``id_or_dep`` refuses for update AND
        supersede AND invalidate — each per-op, no successor, no
        tombstone."""
        kinds = (
            ("update", lambda mid: {"id_or_dep": mid,
                                    "content": f"c1s up {uuid.uuid4().hex}"}),
            ("supersede", lambda mid: {"id_or_dep": mid,
                                       "reason": "c1s supersede"}),
            ("invalidate", lambda mid: {"id_or_dep": mid,
                                        "reason": "c1s retired"}),
        )
        for kind, make_args in kinds:
            target = self._seed(FOREIGN_NS)
            op = {"op_id": uuid.uuid4().hex, "op": kind,
                  "args": make_args(target)}
            receipt = self._apply_scoped([op])["receipts"][0]
            self.assertEqual(
                receipt["status"], "rejected",
                f"leg b ({kind}): a foreign by-id {kind} must be a per-op "
                f"refusal (got {receipt})")
            self.assertEqual(
                receipt.get("error"), "namespace_not_allowed",
                f"leg b ({kind}): the refusal must be "
                f"namespace_not_allowed (got {receipt})")
            self._assert_parent_live(target, f"leg b ({kind})")

    def test_legacy_c_in_scope_by_id_update_commits(self):
        """(c) GREEN CONTROL: a within-scope by-id update still commits —
        the target gate narrows, it does not block the token's own
        namespace."""
        target = self._seed("user:global")
        op = {"op_id": uuid.uuid4().hex, "op": "update",
              "args": {"id": target,
                       "content": f"c1s in-scope up {uuid.uuid4().hex}"}}
        receipt = self._apply_scoped([op])["receipts"][0]
        self.assertEqual(receipt["status"], "committed",
                         f"leg c (green control): {receipt}")
        parent = self.conn.execute(
            "SELECT superseded_at FROM memory WHERE id=?",
            (target,)).fetchone()
        self.assertIsNotNone(parent[0],
                             "leg c: the successful update must tombstone "
                             "the parent")
        self.assertEqual(len(self._successors(target)), 1,
                         "leg c: the successful update must mint exactly "
                         "ONE live successor")

    def test_legacy_d_destination_rekey_refused(self):
        """(d) an update naming an IN-SCOPE target with a FOREIGN
        destination namespace (a rekey) refuses per-op."""
        target = self._seed("user:global")
        op = {"op_id": uuid.uuid4().hex, "op": "update",
              "args": {"id_or_dep": target, "namespace": FOREIGN_NS,
                       "content": f"c1s rekey {uuid.uuid4().hex}"}}
        receipt = self._apply_scoped([op])["receipts"][0]
        self.assertEqual(receipt["status"], "rejected",
                         f"leg d: {receipt}")
        self.assertEqual(receipt.get("error"), "namespace_not_allowed",
                         f"leg d: the destination rekey must be "
                         f"namespace_not_allowed (got {receipt})")
        self._assert_parent_live(target, "leg d")

    def test_legacy_e_mixed_batch_keeps_per_op_receipts(self):
        """(e) a mixed batch (one in-scope add + one foreign by-id update):
        the add COMMITS, the update REJECTS — the refusal is per-op, never
        whole-batch and never fail-open."""
        foreign = self._seed(FOREIGN_NS)
        batch = [
            {"op_id": uuid.uuid4().hex, "op": "add",
             "args": {"type": "fact",
                      "content": f"c1s mixed add {uuid.uuid4().hex}",
                      "namespace": "user:global"}},
            {"op_id": uuid.uuid4().hex, "op": "update",
             "args": {"id": foreign,
                      "content": f"c1s mixed pwn {uuid.uuid4().hex}"}},
        ]
        receipts = self._apply_scoped(batch)["receipts"]
        self.assertEqual(receipts[0]["status"], "committed",
                         f"leg e: the in-scope add must commit (got "
                         f"{receipts[0]})")
        self.assertEqual(receipts[1]["status"], "rejected",
                         f"leg e: the foreign by-id update must reject "
                         f"(got {receipts[1]})")
        self.assertEqual(receipts[1].get("error"), "namespace_not_allowed",
                         receipts[1])
        self._assert_parent_live(foreign, "leg e")

    def _run_cli_apply_ops(self, extra_args):
        ops_path = Path(self.tmp) / "cli-ops.jsonl"
        ops_path.write_text(json.dumps({
            "op_id": uuid.uuid4().hex, "op": "add",
            "args": {"type": "fact", "content": "c1s cli flag probe",
                     "namespace": "user:global"}}) + "\n", encoding="utf-8")
        argv = [sys.executable,
                str(REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"),
                "apply-ops", "--in", str(ops_path), "--json",
                *extra_args]
        return subprocess.run(argv, env=dict(os.environ),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=180)

    def test_cli_namespace_scope_with_principal_refused(self):
        """CLI: ``--namespace-scope`` combined with ``--principal`` refuses
        the whole batch (one identity mechanism per call) with exit 2."""
        run = self._run_cli_apply_ops(
            ["--principal", "p-x", "--namespace-scope", "user:global"])
        self.assertEqual(
            run.returncode, 2,
            f"the flag combination must exit 2 (rc={run.returncode}, "
            f"stdout={run.stdout[:200]!r}, stderr={run.stderr[:200]!r})")
        self.assertIn("--namespace-scope", run.stderr)
        self.assertIn("cannot be combined with --principal", run.stderr)

    def test_cli_namespace_scope_malformed_list_refused(self):
        """CLI: a malformed ``--namespace-scope`` list (an empty item, e.g.
        ``a,,b``) refuses with exit 2."""
        run = self._run_cli_apply_ops(["--namespace-scope", "a,,b"])
        self.assertEqual(
            run.returncode, 2,
            f"a malformed scope list must exit 2 (rc={run.returncode}, "
            f"stdout={run.stdout[:200]!r}, stderr={run.stderr[:200]!r})")
        self.assertIn("--namespace-scope", run.stderr)
        self.assertIn("non-empty namespaces", run.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
