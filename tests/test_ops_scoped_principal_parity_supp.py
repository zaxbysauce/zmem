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

    def _run_cli_apply_ops(self, extra_args, ops=None):
        ops_path = Path(self.tmp) / "cli-ops.jsonl"
        if ops is None:
            ops = [{"op_id": uuid.uuid4().hex, "op": "add",
                    "args": {"type": "fact", "content": "c1s cli flag probe",
                             "namespace": "user:global"}}]
        ops_path.write_text(
            "".join(json.dumps(o) + "\n" for o in ops), encoding="utf-8")
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
        """CLI: a malformed ``--namespace-scope`` entry (``a,,b`` is not a
        grammar-valid namespace) refuses with exit 2 (the transport is
        grammar-validated per item since review PRR-001)."""
        run = self._run_cli_apply_ops(["--namespace-scope", "a,,b"])
        self.assertEqual(
            run.returncode, 2,
            f"a malformed scope list must exit 2 (rc={run.returncode}, "
            f"stdout={run.stdout[:200]!r}, stderr={run.stderr[:200]!r})")
        self.assertIn("--namespace-scope", run.stderr)
        self.assertIn("not valid namespaces", run.stderr)

    def test_cli_namespace_scope_empty_value_refused(self):
        """CLI (review PRR-005): an EXPLICIT empty ``--namespace-scope``
        value refuses with exit 2 — flag-absent (argparse default None)
        stays the unscoped legacy identity, but an explicit empty value
        must never silently mean unscoped."""
        run = self._run_cli_apply_ops(["--namespace-scope", ""])
        self.assertEqual(
            run.returncode, 2,
            f"an empty scope value must exit 2 (rc={run.returncode}, "
            f"stdout={run.stdout[:200]!r}, stderr={run.stderr[:200]!r})")
        self.assertIn("--namespace-scope", run.stderr)

    def test_cli_namespace_scope_valid_list_enforces(self):
        """CLI (cubic 4434387879): the VALID scope path through the REAL
        store.py subprocess — an in-scope add commits, an out-of-scope add
        is refused with namespace_not_allowed (refusal-only coverage made
        this impossible before)."""
        ok = self._run_cli_apply_ops(
            ["--namespace-scope", "user:global"],
            ops=[{"op_id": uuid.uuid4().hex, "op": "add",
                  "args": {"type": "fact",
                           "content": f"cli valid scope {uuid.uuid4().hex}",
                           "namespace": "user:global"}}])
        self.assertEqual(ok.returncode, 0,
                         f"in-scope add must commit (rc={ok.returncode}, "
                         f"stderr={ok.stderr[:200]!r})")
        receipts = json.loads(ok.stdout)["receipts"]
        self.assertEqual(receipts[0]["status"], "committed",
                         f"in-scope add: {receipts}")
        bad = self._run_cli_apply_ops(
            ["--namespace-scope", "user:global"],
            ops=[{"op_id": uuid.uuid4().hex, "op": "add",
                  "args": {"type": "fact",
                           "content": f"cli out of scope {uuid.uuid4().hex}",
                           "namespace": FOREIGN_NS}}])
        self.assertEqual(bad.returncode, 0,
                         "per-op refusals still exit 0 with receipts")
        receipts = json.loads(bad.stdout)["receipts"]
        self.assertEqual(receipts[0]["status"], "rejected",
                         f"out-of-scope add: {receipts}")
        self.assertEqual(receipts[0].get("error"), "namespace_not_allowed",
                         f"out-of-scope add: {receipts}")


class _HubServerMixin:
    """Shared machinery for the REAL-hub legs: a scoped single-token file,
    a scratch store, a fresh mcp_server import, and a seeded foreign row
    (the hub tool drives REAL store.py subprocesses, so seeding goes
    through the subprocess too — the C11 frozen-probe pattern)."""

    TOKEN = "hub-scope-test-token"
    # Legacy single-token mode's FIXED principal id (auth.py from_token_
    # config): the self-assertion a caller passes is this id, not the raw
    # token string.
    TOKEN_ID = "zmem-operator"
    NAMESPACES = ["user:global"]

    @classmethod
    def _build_hub(cls):
        import importlib.util
        if importlib.util.find_spec("mcp") is None:
            raise unittest.SkipTest(
                "the mcp package is required for the real-hub apply_ops "
                "legs (install hermes-plugin/server/requirements.txt)")
        cls.tmp = tempfile.mkdtemp(prefix="zmem-hub-scope-")
        cls.token_file = os.path.join(cls.tmp, "token.json")
        Path(cls.token_file).write_text(json.dumps({
            "token": cls.TOKEN,
            "namespaces": cls.NAMESPACES,
        }), encoding="utf-8")
        cls._env_restore = {
            k: os.environ.get(k) for k in (
                "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
                "ZMEM_MCP_TOKEN", "ZMEM_MCP_URL", "ZMEM_REMOTE_URL",
                "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME",
                "ZMEM_MODEL_AUTODOWNLOAD")
        }
        os.environ.update({
            "ZMEM_MCP_TOKEN_FILE": cls.token_file,
            "ZMEM_STORE": os.path.join(cls.tmp, "store.sqlite"),
            "ZMEM_DATA": cls.tmp,
            "ZMEM_HOME": str(REPO_ROOT),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
        })
        for k in ("ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKENS_FILE", "ZMEM_MCP_URL",
                  "ZMEM_REMOTE_URL"):
            os.environ.pop(k, None)
        # Fresh import: the hub reads auth env at import/build time (the
        # C11 probe purge list).
        for name in [k for k in list(sys.modules)
                     if k == "mcp_server" or k.startswith("mcp_server.")
                     or k in ("auth", "host", "redaction", "fleet")
                     or k.startswith("fleet.")]:
            del sys.modules[name]
        server_dir = str(REPO_ROOT / "hermes-plugin" / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        import mcp_server  # noqa: E402
        cls.mcp_server = mcp_server
        cls.addClassCleanup(cls._teardown_hub)

    @classmethod
    def _teardown_hub(cls):
        for k, v in cls._env_restore.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @classmethod
    def _seed_via_subprocess(cls, namespace, content="hub scope seed"):
        """Seed through the real store.py subprocess (the hub's own write
        route), returning the canonical row id (``add --json`` emits
        ``{"ok": true, "id": ...}``)."""
        out = subprocess.run(
            [sys.executable, "-X", "utf8", str(SCRIPTS_DIR / "store.py"),
             "add", "--namespace", namespace,
             "--content", f"{content} {uuid.uuid4().hex}",
             "--type", "fact", "--json"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=180,
            input="")
        if out.returncode != 0:
            raise AssertionError(f"seed failed: {out.stderr[:300]!r}")
        payload = json.loads(out.stdout)
        return payload["id"]

    @staticmethod
    def _unwrap(result):
        """FastMCP tool results unwrap (the C11 probe pattern)."""
        if hasattr(result, "content") and result.content:
            item = result.content[0]
            text = getattr(item, "text", None)
            if text is not None:
                return json.loads(text)
        if isinstance(result, dict):
            return result
        return json.loads(str(result))

    @classmethod
    def _call_apply_ops(cls, server, ops_list, principal=""):
        import asyncio
        result = asyncio.run(server._tool_manager.call_tool(
            "apply_ops",
            {"ops": [dict(o) for o in ops_list], "principal": principal},
            context=None))
        return cls._unwrap(result)


class HubScopePassthroughTest(_HubServerMixin, unittest.TestCase):
    """Review PRR-002 (the CI-executed kill for the hub forwarding branch):
    the scoped-legacy scope passthrough is driven through the REAL hub —
    build_server + the real apply_ops tool + REAL store.py subprocesses.
    Deleting the forwarding branch (mcp_server.py ``elif principal_map is
    None and getattr(resolved, "scoped", False)``) makes these legs RED;
    the shipped engine-level legs cannot (they hand-build the principal),
    which is exactly the coverage gap the review proved with a green-under-
    mutation experiment."""

    @classmethod
    def setUpClass(cls):
        cls._build_hub()

    def test_hub_a_foreign_by_id_refused_through_real_hub(self):
        """The kill leg: a scoped legacy caller's by-id update naming a
        foreign-namespace target is refused per-op THROUGH THE HUB (hub
        forward -> CLI parse -> scoped engine, the full production path)."""
        server = self.mcp_server.build_server(host="127.0.0.1", port=0,
                                              use_tls=False)
        target = self._seed_via_subprocess(FOREIGN_NS)
        result = self._call_apply_ops(server, [{
            "op_id": uuid.uuid4().hex, "op": "update",
            "args": {"id": target,
                     "content": f"hub pwned {uuid.uuid4().hex}"}},
        ])
        self.assertTrue(result.get("ok"), f"hub envelope: {result}")
        receipt = result["receipts"][0]
        self.assertEqual(receipt["status"], "rejected",
                         f"hub foreign by-id update: {receipt}")
        self.assertEqual(receipt.get("error"), "namespace_not_allowed",
                         f"hub foreign by-id update: {receipt}")

    def test_hub_b_flag_reaches_store_argv(self):
        """The argv-reach leg: ``--namespace-scope`` (newline-joined
        allow-list) reaches the store subprocess argv. Pairs with the kill
        leg — a deleted forwarding branch drops the flag from the captured
        argv and both legs fail."""
        captured = {}
        real_run = self.mcp_server._run_store_async

        async def spy(args, **kwargs):
            captured["argv"] = list(args)
            return await real_run(args, **kwargs)

        self.mcp_server._run_store_async = spy
        try:
            server = self.mcp_server.build_server(host="127.0.0.1", port=0,
                                                  use_tls=False)
            result = self._call_apply_ops(server, [{
                "op_id": uuid.uuid4().hex, "op": "add",
                "args": {"type": "fact",
                         "content": f"hub argv probe {uuid.uuid4().hex}",
                         "namespace": "user:global"}},
            ])
            self.assertTrue(result.get("ok"), f"hub envelope: {result}")
        finally:
            self.mcp_server._run_store_async = real_run
        argv = captured.get("argv") or []
        self.assertIn("--namespace-scope", argv,
                      f"the scope flag must reach the store argv: {argv}")
        flag_value = argv[argv.index("--namespace-scope") + 1]
        self.assertEqual(flag_value, "user:global",
                         f"the allow-list must round-trip: {flag_value!r}")

    def test_hub_c_self_assertion_routes_through_scope(self):
        """Cubic 4434387867: an AUTHENTICATED legacy caller passing the
        tool-level principal naming its OWN id used to reach the CLI as
        ``--principal <token-id>`` and every op was denied (unknown_
        principal — no map entry). The self-assertion is now dropped and
        the scoped caller routes through the scope passthrough: a foreign
        by-id op is refused with the per-op scope error, and an in-scope
        add commits. The authenticated request is simulated at the same
        seam the SDK uses (auth_context.get_access_token)."""
        from unittest import mock
        server = self.mcp_server.build_server(host="127.0.0.1", port=0,
                                              use_tls=False)
        foreign = self._seed_via_subprocess(FOREIGN_NS)
        import mcp.server.auth.middleware.auth_context as _auth_ctx
        with mock.patch.object(_auth_ctx, "get_access_token",
                               lambda: object()):
            result = self._call_apply_ops(server, [{
                "op_id": uuid.uuid4().hex, "op": "update",
                "args": {"id": foreign,
                         "content": f"self-assert pwned {uuid.uuid4().hex}"}},
            ], principal=self.TOKEN_ID)
            receipt = result["receipts"][0]
            self.assertEqual(receipt["status"], "rejected",
                             f"self-asserted foreign update: {result}")
            self.assertEqual(receipt.get("error"), "namespace_not_allowed",
                             f"self-asserted foreign update: {result}")
            ok = self._call_apply_ops(server, [{
                "op_id": uuid.uuid4().hex, "op": "add",
                "args": {"type": "fact",
                         "content": f"self-assert in scope {uuid.uuid4().hex}",
                         "namespace": "user:global"}},
            ], principal=self.TOKEN_ID)
        self.assertTrue(ok.get("ok"), f"self-asserted in-scope add: {ok}")
        self.assertEqual(ok["receipts"][0]["status"], "committed",
                         f"self-asserted in-scope add: {ok}")


class CommaScopeRoundTripTest(_HubServerMixin, unittest.TestCase):
    """Review PRR-001 (the kill leg for the transport fix): a token whose
    allow-list contains a COMMA-bearing namespace — grammar-valid
    (``project:.+`` admits commas) — must round-trip the hub->CLI transport
    EXACTLY. At the comma separator the engine enforced the split set
    {project:a, b}: the never-granted fragment was ACCEPTED and the granted
    namespace REFUSED. Under the newline separator both arms hold."""

    TOKEN = "hub-comma-scope-token"
    NAMESPACES = ["project:a,b"]

    @classmethod
    def setUpClass(cls):
        cls._build_hub()
        cls.TOKEN_ID = cls.TOKEN

    def test_granted_comma_namespace_commits(self):
        """Fail-closed arm closed: an op naming the granted comma-bearing
        namespace commits (at the comma separator it was refused)."""
        server = self.mcp_server.build_server(host="127.0.0.1", port=0,
                                              use_tls=False)
        result = self._call_apply_ops(server, [{
            "op_id": uuid.uuid4().hex, "op": "add",
            "args": {"type": "fact",
                     "content": f"comma granted {uuid.uuid4().hex}",
                     "namespace": "project:a,b"}},
        ])
        self.assertTrue(result.get("ok"), f"hub envelope: {result}")
        receipt = result["receipts"][0]
        self.assertEqual(receipt["status"], "committed",
                         f"the granted comma namespace must commit: "
                         f"{receipt}")

    def test_never_granted_fragment_refused(self):
        """Widen arm closed: an op naming the never-granted fragment
        ``project:a`` (a DIFFERENT namespace the split used to admit) is
        refused with namespace_not_allowed."""
        server = self.mcp_server.build_server(host="127.0.0.1", port=0,
                                              use_tls=False)
        target = self._seed_via_subprocess("project:a")
        result = self._call_apply_ops(server, [{
            "op_id": uuid.uuid4().hex, "op": "update",
            "args": {"id": target,
                     "content": f"comma fragment pwned {uuid.uuid4().hex}"}},
        ])
        receipt = result["receipts"][0]
        self.assertEqual(receipt["status"], "rejected",
                         f"the fragment namespace must be refused: {result}")
        self.assertEqual(receipt.get("error"), "namespace_not_allowed",
                         f"the fragment namespace must be refused: {result}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
