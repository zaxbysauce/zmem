"""Issue #98: the precision-gated cross-project hazard lane.

Pins the shipped implementation end to end:

* ``tests/fixtures/cross_project`` — the committed cases/expected pair is
  byte-identical to a fresh run of its generator (compact sorted-key JSON,
  one trailing LF), and the printed sha256 digests are stable.
* Reserved caps — a 6/6/6 foreign/current/global store delivers exactly the
  5 project / 2 cross / 3 global partition through the session-aware
  selector; the cross tier never consumes project or global slots, and an
  explicitly empty ops list (no hazard intersection) yields zero cross rows.
* Predicate filters — wrong signal, below-floor confidence, non-hazard ops,
  current-namespace-only, user:global-only, and superseded rows each admit
  zero cross rows, with a positive control proving the harness admits.
* Env matrix — ZMEM_CROSS_PROJECT unset arms pretool only; "0" kills every
  surface even with the explicit flag; "1" arms both; any other value warns
  exactly once and behaves as unset. The same matrix is replayed through the
  REAL hook subprocess (pretool delivers, user_prompt does not, "0" is
  enforced store-side, "1" arms user_prompt with store-side derived tokens
  forwarding, posttoolbatch behaves as pretool via its ops ring).
* Envelope and renderer — the selector envelope carries the full #158 key
  set, the rendered fence carries the exact ``[ns=<foreign>] [tier=cross]``
  bytes, the tier key rides ONLY cross rows, and delivery copies no row.
* Hazard override — ZMEM_CROSS_PROJECT_HAZARD_VERBS trims/case-folds/dedupes,
  drops unknown verbs with one warning, and falls back to the default set on
  an all-unknown override; a ``pop``-only override arms on "pop" but not
  "reset".

Every store-touching step pins ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR/
ZMEM_MODEL_AUTODOWNLOAD into a clean child env (ambient ZMEM_* and the
plugin-data vars stripped) — nothing here may touch the box store.

Run: python tests/test_cross_project_lane.py
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
BODY_PY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "cross_project"
GENERATE_PY = FIXTURE_DIR / "generate.py"
PYTHON = sys.executable

TS = "2026-09-10T00:00:00Z"
CURRENT_NS = "project:current"
FOREIGN_A = "project:foreign-a"
FOREIGN_B = "project:foreign-b"
GLOBAL_NS = "user:global"
QUERY = "git stash pop"
HAZARD_OPS = ["git", "stash", "pop"]

# Env vars no child process may inherit (the repo's _clean_env convention from
# tests/test_pretool_inject.py / tests/test_ops_tokens.py — plus the two
# issue #98 knobs so ambient operator settings cannot skew a matrix cell).
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", "ZMEM_SESSION",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_EMBED_PROFILE", "ZMEM_TEST_NOW", "ZMEM_AUTO_REKEY",
    "ZMEM_PENDING_SIDECAR", "ZMEM_DELIVER_WINDOW_S", "ZMEM_LEDGER_CAP",
    "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
    "ZMEM_INJECT_MARGIN",
)

# Module-top env pin BEFORE any storelib import (storelib freezes STORE_PATH
# at first import — the near-mutated-real-store hazard; see
# tests/test_zero_write_passive.py's module docstring).
_BOOT_TMP = tempfile.mkdtemp(prefix="zmem-crosslane-boot-")
os.environ["ZMEM_STORE"] = os.path.join(_BOOT_TMP, "store.sqlite")
os.environ["ZMEM_DATA"] = _BOOT_TMP
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_BOOT_TMP, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _k in ("ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
           "ZMEM_INJECT_MARGIN", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_TEST_NOW"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "storelib"))

from storelib import inject as inject_mod  # noqa: E402
from storelib import recall as recall_mod  # noqa: E402
from storelib import schema as schema_mod  # noqa: E402
from storelib import ops_tokens as ops_mod  # noqa: E402


def _clean_env(tmp: str, **extra: str) -> dict:
    """Child env pinned to a throwaway store, ambient ZMEM_* stripped."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
    })
    env.update(extra)
    return env


class CrossProjectLaneTest(unittest.TestCase):
    """The issue #98 lane: fixture digest, caps, predicates, matrix, envelope,
    hazard override."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-crosslane-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.conn = self._open_store(self.store)
        # Per-test save/pop of the two issue #98 knobs so an ambient value can
        # never leak into a non-matrix assertion.
        self._saved_cross = {
            key: os.environ.pop(key, None)
            for key in ("ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS")
        }

    def tearDown(self):
        try:
            self.conn.close()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key, value in self._saved_cross.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        recall_mod._CROSS_POLICY_WARNED = False

    # ---- shared helpers ---------------------------------------------------

    def _open_store(self, path: str) -> sqlite3.Connection:
        """A fresh schema-initialized store at ``path`` (raw connection with
        the row factory storelib readers require — the test_injection_recall
        direct-INSERT pattern, no store.py subprocess per seed)."""
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        schema_mod.init_db(conn)
        return conn

    def _seed(self, conn, row_id: str, namespace: str, content: str, *,
              signal: str = "test", confidence: float = 0.9,
              superseded_at: str | None = None) -> None:
        conn.execute(
            "INSERT INTO memory (id, namespace, type, content, tags, "
            "source_ref, source_hash, confidence, signal, valid_from, "
            "ingestion_ts, superseded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (row_id, namespace, "lesson", content, "", "", "", confidence,
             signal, TS, TS, superseded_at),
        )
        conn.commit()

    def _seed_caps_mix(self, conn) -> None:
        """6 qualifying foreign + 6 qualifying current + 6 qualifying global
        rows, every content matching the ``git stash pop`` query."""
        foreign_signals = ("test", "compile", "lint", "reviewer", "test", "lint")
        for i in range(6):
            ns = FOREIGN_A if i % 2 == 0 else FOREIGN_B
            self._seed(conn, f"cap-foreign-{i}", ns,
                       f"caps foreign case {i}: git stash pop on a foreign "
                       f"project needs git stash list first",
                       signal=foreign_signals[i])
        for i in range(6):
            self._seed(conn, f"cap-current-{i}", CURRENT_NS,
                       f"caps current case {i}: git stash pop while the "
                       f"suite runs flakes the tests")
        for i in range(6):
            self._seed(conn, f"cap-global-{i}", GLOBAL_NS,
                       f"caps global case {i}: git stash pop guidance "
                       f"applies across projects")

    def _selector(self, conn, data_dir: str, session: str,
                  ops_tokens: list | None) -> dict:
        envelope = inject_mod.select_and_budget_for_injection(
            conn, query=QUERY, namespace=CURRENT_NS, moment="pretool",
            session_id=session, lane="zcode", ops_tokens=ops_tokens,
            data_dir=data_dir,
        )
        self.assertIsInstance(envelope, dict)
        return envelope

    @staticmethod
    def _partition(rows: list) -> dict:
        """Delivered-slot partition excluding expansion rows (none exist in
        these fixtures, but the contract's exclusion is honored anyway)."""
        real = [r for r in rows
                if not r.get("link_relation") and not r.get("_graph_arrival_only")]
        part = {"project": [], "cross": [], "global": []}
        for r in real:
            if r.get("tier") == "cross":
                part["cross"].append(r)
            elif r["namespace"] == CURRENT_NS:
                part["project"].append(r)
            elif r["namespace"] == GLOBAL_NS:
                part["global"].append(r)
        return part

    @contextlib.contextmanager
    def _cross_env(self, value: str | None):
        """Set/unset ZMEM_CROSS_PROJECT around a block, resetting the
        one-shot warning owner (a module global on storelib.recall)."""
        recall_mod._CROSS_POLICY_WARNED = False
        saved = os.environ.pop("ZMEM_CROSS_PROJECT", None)
        try:
            if value is not None:
                os.environ["ZMEM_CROSS_PROJECT"] = value
            yield
        finally:
            if saved is None:
                os.environ.pop("ZMEM_CROSS_PROJECT", None)
            else:
                os.environ["ZMEM_CROSS_PROJECT"] = saved
            recall_mod._CROSS_POLICY_WARNED = False

    def _run_hook(self, tmp: str, mode: str, event: dict,
                  **extra_env: str) -> tuple[str, int, str]:
        """Run the REAL hook body against the seeded store in ``tmp``."""
        result = subprocess.run(
            [PYTHON, str(BODY_PY), str(STORE_PY), CURRENT_NS, "25000", mode],
            input=json.dumps(event), capture_output=True, text=True,
            env=_clean_env(tmp, **extra_env), timeout=120,
        )
        ctx = ""
        if result.stdout.strip():
            try:
                ctx = json.loads(result.stdout).get("additionalContext", "")
            except (TypeError, ValueError):
                ctx = ""
        return ctx, result.returncode, result.stderr

    def _seed_hook_store(self, tmp: str, *, write_ring_sid: str | None = None,
                         foreign_ns: str = FOREIGN_A) -> None:
        """init + one current row + one qualifying foreign row in ``tmp``."""
        conn = self._open_store(os.path.join(tmp, "store.sqlite"))
        try:
            self._seed(conn, "hook-current-1", CURRENT_NS,
                       "hook current: git stash pop while tests run flakes "
                       "the suite")
            self._seed(conn, "hook-foreign-1", foreign_ns,
                       "crosscanary: git stash pop on a foreign project "
                       "needs git stash list first")
        finally:
            conn.close()
        if write_ring_sid:
            self.assertTrue(
                ops_mod.append_ops_ring(tmp, write_ring_sid, "Bash", QUERY),
                "ring write failed — the posttoolbatch cell cannot run")

    # ---- A1: fixture digest ------------------------------------------------

    def test_fixture_digest(self):
        """A fresh generator run reproduces the committed cases.json and
        expected.json byte-for-byte, and the printed sha256 digests match the
        committed (CRLF-normalized) blobs."""
        out_dir = tempfile.mkdtemp(prefix="zmem-crosslane-fixture-")
        try:
            result = subprocess.run(
                [PYTHON, str(GENERATE_PY), "--out-dir", out_dir],
                cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            digests = [line for line in result.stdout.splitlines() if line]
            self.assertEqual(len(digests), 2,
                             "generate.py must print exactly two sha256 "
                             f"digests; got {result.stdout!r}")
            cases_digest, expected_digest = digests

            for name, digest in (("cases.json", cases_digest),
                                 ("expected.json", expected_digest)):
                committed = (FIXTURE_DIR / name).read_bytes()
                regenerated = (Path(out_dir) / name).read_bytes()
                # Windows checkouts can materialize CRLF — compare and hash
                # the LF-normalized blobs.
                self.assertEqual(
                    committed.replace(b"\r\n", b"\n"),
                    regenerated.replace(b"\r\n", b"\n"),
                    f"committed {name} drifted from a fresh generation")
                normalized = committed.replace(b"\r\n", b"\n")
                self.assertEqual(
                    hashlib.sha256(normalized).hexdigest(), digest,
                    f"printed digest for {name} must match the committed "
                    f"(CRLF-normalized) bytes")

            # The committed expectation stays coherent: cap counts sum to the
            # delivered ten, and both markers name an admitted source ns.
            expected = json.loads(
                (FIXTURE_DIR / "expected.json").read_text(encoding="utf-8"))
            self.assertEqual(sorted(expected["cap_counts"]),
                             ["cross", "global", "project"])
            self.assertEqual(sum(expected["cap_counts"].values()), 10)
            for marker, ns in zip(expected["tier_markers"],
                                  expected["admitted_source_namespaces"]):
                self.assertIn(f"[ns={ns}] [tier=cross]", marker)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    # ---- A2: reserved caps -------------------------------------------------

    def test_reserved_caps(self):
        """6/6/6 seeding delivers exactly 5 project / 2 cross / 3 global; the
        cross rows keep their foreign namespace and tier marker, no current
        row is displaced, and explicit empty ops suppress the tier."""
        self._seed_caps_mix(self.conn)
        envelope = self._selector(self.conn, self.tmp, "caps-a", HAZARD_OPS)
        self.assertEqual(envelope["reason"], "injected", envelope["reason"])
        part = self._partition(envelope["results"])
        self.assertEqual(len(part["project"]), 5,
                         f"project tier keeps its 5 slots: {part}")
        self.assertEqual(len(part["cross"]), 2,
                         f"cross tier is capped at 2 (CROSS_PROJECT_MAX): {part}")
        self.assertEqual(len(part["global"]), 3,
                         f"global tier keeps its 3 slots: {part}")
        for row in part["cross"]:
            self.assertEqual(row.get("tier"), "cross")
            self.assertIn(row["namespace"], (FOREIGN_A, FOREIGN_B))
        # All 5 project slots are CURRENT-project rows — a foreign row never
        # displaces one.
        self.assertTrue(all(r["namespace"] == CURRENT_NS and
                            r.get("tier") is None
                            for r in part["project"]))
        self.assertTrue(all(r["namespace"] == GLOBAL_NS and
                            r.get("tier") is None for r in part["global"]))
        # A second selector call with an EXPLICITLY empty ops list derives no
        # hazard intersection, so the tier stays closed (fresh session id so
        # the delivery ledger from the first call cannot skew the pool).
        envelope_empty = self._selector(self.conn, self.tmp, "caps-b", [])
        part_empty = self._partition(envelope_empty["results"])
        self.assertEqual(len(part_empty["cross"]), 0,
                         "ops_tokens=[] must yield zero cross rows")
        self.assertEqual(len(part_empty["project"]), 5)
        self.assertEqual(len(part_empty["global"]), 3)

    # ---- A3: predicate filters ----------------------------------------------

    def test_predicate_filters(self):
        """Each admission predicate negative yields zero cross rows; one
        positive control proves the harness admits."""

        def fresh_case(rows, ops_tokens):
            """Seed ``rows`` in a fresh store; return the delivered cross
            count through the real selector."""
            case_dir = tempfile.mkdtemp(prefix="zmem-crosslane-case-",
                                        dir=self.tmp)
            try:
                conn = self._open_store(os.path.join(case_dir, "store.sqlite"))
                try:
                    for args, kwargs in rows:
                        self._seed(conn, *args, **kwargs)
                    envelope = self._selector(conn, case_dir, "pred", ops_tokens)
                    return len(self._partition(envelope["results"])["cross"])
                finally:
                    conn.close()
            finally:
                shutil.rmtree(case_dir, ignore_errors=True)

        qualifying_foreign = ("pred-foreign-1", FOREIGN_A,
                              "predicate foreign: git stash pop needs git "
                              "stash list first")
        current = ("pred-current-1", CURRENT_NS,
                   "predicate current: git stash pop flakes the suite")
        negatives = (
            ("signal not in CROSS_PROJECT_SIGNALS",
             [(qualifying_foreign, {"signal": "none"}), (current, {})],
             HAZARD_OPS),
            ("confidence below the floor",
             [(qualifying_foreign, {"confidence": 0.1}), (current, {})],
             HAZARD_OPS),
            ("non-hazard ops tokens",
             [(qualifying_foreign, {}), (current, {})],
             ["git", "status"]),
            ("current-namespace row only (no foreign namespace)",
             [(current, {})], HAZARD_OPS),
            ("user:global row only",
             [(("pred-global-1", GLOBAL_NS,
                "predicate global: git stash pop guidance"), {})],
             HAZARD_OPS),
            ("superseded foreign row",
             [(qualifying_foreign, {"superseded_at": TS}), (current, {})],
             HAZARD_OPS),
        )
        for label, rows, ops in negatives:
            with self.subTest(label):
                self.assertEqual(
                    fresh_case(rows, ops), 0,
                    f"{label} must admit zero cross rows")
        # Positive control: the same harness with one qualifying foreign row
        # and hazard ops DOES admit — the negatives above are meaningful.
        self.assertGreaterEqual(
            fresh_case([(qualifying_foreign, {}), (current, {})], HAZARD_OPS),
            1, "positive control must admit at least one cross row")

    # ---- A4: env matrix ------------------------------------------------------

    def test_env_matrix(self):
        """ZMEM_CROSS_PROJECT truth table (function + admissions + the real
        hook subprocess) and the --bad-flag CLI guard."""
        self._seed(self.conn, "mx-foreign-1", FOREIGN_A,
                   "matrix foreign: git stash pop needs git stash list first")
        self._seed(self.conn, "mx-current-1", CURRENT_NS,
                   "matrix current: git stash pop flakes the suite")

        admissions_kwargs = dict(
            query=QUERY, current_namespace=CURRENT_NS,
            ops_tokens=HAZARD_OPS,
        )

        # unset -> pretool only.
        with self._cross_env(None):
            self.assertTrue(recall_mod.cross_project_surface_enabled("pretool"))
            self.assertFalse(
                recall_mod.cross_project_surface_enabled("user_prompt"))
            self.assertGreaterEqual(len(recall_mod.cross_project_admissions(
                self.conn, moment="pretool", **admissions_kwargs)), 1)
            self.assertEqual(len(recall_mod.cross_project_admissions(
                self.conn, moment="user_prompt", **admissions_kwargs)), 0)

        # "0" -> off everywhere, even with the explicit flag.
        with self._cross_env("0"):
            self.assertFalse(
                recall_mod.cross_project_surface_enabled("pretool"))
            self.assertFalse(recall_mod.cross_project_surface_enabled(
                "user_prompt", explicit=True))
            self.assertEqual(len(recall_mod.cross_project_admissions(
                self.conn, moment="pretool", **admissions_kwargs)), 0)

        # "1" -> on everywhere.
        with self._cross_env("1"):
            self.assertTrue(recall_mod.cross_project_surface_enabled("pretool"))
            self.assertTrue(
                recall_mod.cross_project_surface_enabled("user_prompt"))
            self.assertGreaterEqual(len(recall_mod.cross_project_admissions(
                self.conn, moment="user_prompt", **admissions_kwargs)), 1)

        # Any other non-empty value -> pretool only + EXACTLY one warning.
        with self._cross_env("yes"):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertTrue(
                    recall_mod.cross_project_surface_enabled("pretool"))
                self.assertFalse(
                    recall_mod.cross_project_surface_enabled("user_prompt"))
                # Repeat calls: the one-shot owner must hold at one line.
                recall_mod.cross_project_surface_enabled("user_prompt")
            warnings = [line for line in stderr.getvalue().splitlines()
                        if "[zmem] warning:" in line]
            self.assertEqual(
                len(warnings), 1,
                f"expected exactly one cross-policy warning, got {warnings!r}")

        # The CLI direct branches forward the explicit flag + moment/ops
        # context (source ratchet — the behavior is covered end to end by
        # the hook cells below).
        cli_source = (SCRIPTS_DIR / "storelib" / "cli.py").read_text(
            encoding="utf-8")
        for needle in ('"--include-cross-project"',
                       "dest=\"include_cross_project\"",
                       "explicit=args.include_cross_project",
                       "_cross_moment=args.moment",
                       "_cross_ops_tokens=list(args.ops_token) or None"):
            self.assertIn(needle, cli_source,
                          f"cli.py must keep the #158+#98 dispatch seam "
                          f"({needle!r} missing)")
        # R2 (final-critic): the explicit flag must reach the admission
        # re-gate, so a direct CLI call WITHOUT --moment still delivers (env
        # unset) and env=0 still kills it — the docstring's promise.
        self.assertIn("_cross_explicit=args.include_cross_project",
                      cli_source,
                      "cli.py must thread the explicit flag into the "
                      "admission re-gate")
        recall_source = (SCRIPTS_DIR / "storelib" / "recall.py").read_text(
            encoding="utf-8")
        self.assertIn("explicit=_cross_explicit", recall_source,
                      "recall.py admissions must honor the explicit flag")

    def test_explicit_flag_direct_cli(self):
        """--include-cross-project without --moment: env unset delivers the
        cross row through the direct path; ZMEM_CROSS_PROJECT=0 kills it."""
        import json as _json
        import subprocess as _sp

        def run_cli(store_py, **extra):
            env = _clean_env(cell, **extra)
            proc = _sp.run(
                [sys.executable, str(store_py), "recall",
                 "--query", QUERY, "--namespace", "project:current",
                 "--include-cross-project",
                 "--ops-token", "git", "--ops-token", "stash",
                 "--ops-token", "pop", "--json"],
                capture_output=True, text=True, env=env, timeout=120)
            return proc

        store_py = STORE_PY
        cell = tempfile.mkdtemp(prefix="zmem-crosslane-explicit-",
                                dir=self.tmp)
        self._seed_hook_store(cell)
        proc = run_cli(store_py)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = _json.loads(proc.stdout).get("results", [])
        self.assertIn("cross", [r.get("tier") for r in rows],
                      "explicit flag without --moment must deliver the "
                      "cross row (env unset)")
        proc = run_cli(store_py, ZMEM_CROSS_PROJECT="0")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rows = _json.loads(proc.stdout).get("results", [])
        self.assertNotIn("cross", [r.get("tier") for r in rows],
                         "env=0 must kill the tier even with the explicit "
                         "flag")

        # --- end-to-end through the REAL hook body -----------------------
        pretool_event = {"tool_name": "Bash",
                         "tool_input": {"command": QUERY}}
        user_prompt_event = {
            "prompt": "git stash pop the stash before switching branches"}

        def hook_cell(label, mode, event, *, cross_marker_expected,
                      ring_sid=None, **extra):
            with self.subTest(label):
                cell_tmp = tempfile.mkdtemp(prefix="zmem-crosslane-hook-",
                                            dir=self.tmp)
                try:
                    self._seed_hook_store(cell_tmp, write_ring_sid=ring_sid)
                    event_full = dict(event)
                    event_full["session_id"] = f"mx-{label}"
                    ctx, rc, stderr = self._run_hook(
                        cell_tmp, mode, event_full, **extra)
                    self.assertEqual(rc, 0, stderr)
                    self.assertIn("<<<ZMEM_UNTRUSTED_FENCE>>>", ctx)
                    self.assertIn("current", ctx)
                    marker = f"[ns={FOREIGN_A}] [tier=cross]"
                    if cross_marker_expected:
                        self.assertIn(
                            marker, ctx,
                            "cross row must ride the fence with its tier "
                            "marker on this surface")
                    else:
                        self.assertNotIn(
                            "[tier=cross]", ctx,
                            "this surface must stay closed to the cross tier")
                finally:
                    shutil.rmtree(cell_tmp, ignore_errors=True)

        hook_cell("pretool-unset", "pretool", pretool_event,
                  cross_marker_expected=True)
        hook_cell("user-prompt-unset", "user_prompt", user_prompt_event,
                  cross_marker_expected=False)
        hook_cell("pretool-env0", "pretool", pretool_event,
                  cross_marker_expected=False, ZMEM_CROSS_PROJECT="0")
        # user_prompt + env "1": the store-side selector derives --ops-token
        # values from the prompt itself (#98: derivation lives at the store
        # boundary; the #158 hook boundary keeps the hook a flag forwarder).
        # Delivery here proves the hazard gate armed on those store-derived
        # tokens — without them it cannot arm on this moment (the ring
        # derivation only runs on pretool).
        hook_cell("user-prompt-env1", "user_prompt", user_prompt_event,
                  cross_marker_expected=True, ZMEM_CROSS_PROJECT="1")
        # posttoolbatch maps to the pretool moment; its ops context arrives
        # via the per-session ring the PostToolUse hook wrote.
        hook_cell("posttoolbatch-unset", "posttoolbatch", pretool_event,
                  cross_marker_expected=True, ring_sid="mx-posttoolbatch-unset")

        # Unrecognized flags still exit 2 with argparse's own error line.
        bad = subprocess.run(
            [PYTHON, str(STORE_PY), "recall", "--query", "x", "--bad-flag"],
            env=_clean_env(tempfile.mkdtemp(prefix="zmem-crosslane-badflag-",
                                            dir=self.tmp)),
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(bad.returncode, 2)
        self.assertIn("error: unrecognized arguments: --bad-flag", bad.stderr)

    # ---- A5: envelope + renderer + no-copy ----------------------------------

    def test_envelope_and_marker(self):
        """The selector envelope carries the full required key set, the
        rendered fence carries the exact cross marker bytes, tier rides only
        cross rows, and delivery copies no row."""
        # Foreign rows in ONE namespace so both capped markers are
        # deterministic bytes.
        for i in range(6):
            self._seed(self.conn, f"env-foreign-{i}", FOREIGN_A,
                       f"envelope foreign case {i}: git stash pop needs git "
                       f"stash list first",
                       signal=("test", "compile", "lint")[i % 3])
        for i in range(6):
            self._seed(self.conn, f"env-current-{i}", CURRENT_NS,
                       f"envelope current case {i}: git stash pop flakes "
                       f"the suite")
        for i in range(6):
            self._seed(self.conn, f"env-global-{i}", GLOBAL_NS,
                       f"envelope global case {i}: git stash pop guidance "
                       f"applies across projects")

        def snapshot():
            return conn_execute_snapshot(self.conn)

        before = snapshot()
        envelope = self._selector(self.conn, self.tmp, "envelope", HAZARD_OPS)

        expected = json.loads(
            (FIXTURE_DIR / "expected.json").read_text(encoding="utf-8"))
        for key in expected["envelope_keys"]:
            self.assertIn(key, envelope,
                          f"selector envelope must carry {key!r}")
        self.assertIn("[ns=project:foreign-a] [tier=cross]",
                      envelope["rendered"])
        self.assertIn("<<<ZMEM_UNTRUSTED_FENCE>>>", envelope["rendered"])

        rows = envelope["results"]
        self.assertGreaterEqual(len(rows), 1)
        for row in rows:
            is_cross_ns = row["namespace"] == FOREIGN_A
            self.assertEqual(row.get("tier") == "cross", is_cross_ns,
                             f"tier must ride ONLY cross rows: {row['id']}")
        cross_rows = [r for r in rows if r.get("tier") == "cross"]
        self.assertEqual(len(cross_rows), 2)

        # No-copy: the store's rows are untouched by delivery apart from the
        # telemetry counters (surfaced_count/last_surfaced), which the
        # zero-write extension file pins separately.
        after = snapshot()
        self.assertEqual(before, after,
                         "delivery must not rewrite any memory row's "
                         "content/namespace/signal")

    # ---- A6: hazard override semantics ---------------------------------------

    def test_hazard_override_semantics(self):
        default = recall_mod._DEFAULT_HAZARD_VERBS
        cases = (
            # (env value, expected frozenset, warning substring). The
            # one-shot warning owner emits the FIRST cause: unknown verbs
            # are named for a mixed/all-unknown list, the no-usable-verbs
            # fallback fires only when nothing survived to dedupe (the
            # all-whitespace override).
            (" push , PULL,push,unknownverb",
             frozenset({"push", "pull"}), "unknownverb"),
            ("  , , ", default, "no usable verbs"),
            ("zzz,yyy", default, "zzz"),
        )
        for raw, expected_set, warning_needle in cases:
            with self.subTest(raw=raw):
                recall_mod._CROSS_POLICY_WARNED = False
                saved = os.environ.pop("ZMEM_CROSS_PROJECT_HAZARD_VERBS", None)
                try:
                    os.environ["ZMEM_CROSS_PROJECT_HAZARD_VERBS"] = raw
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        verbs = recall_mod.hazard_verbs()
                    self.assertEqual(verbs, expected_set)
                    lines = [line for line in stderr.getvalue().splitlines()
                             if "[zmem] warning:" in line]
                    self.assertEqual(len(lines), 1,
                                     f"exactly one hazard-override warning "
                                     f"expected, got {lines!r}")
                    self.assertIn(warning_needle, lines[0])
                finally:
                    if saved is None:
                        os.environ.pop("ZMEM_CROSS_PROJECT_HAZARD_VERBS",
                                       None)
                    else:
                        os.environ["ZMEM_CROSS_PROJECT_HAZARD_VERBS"] = saved
                    recall_mod._CROSS_POLICY_WARNED = False

        # A pop-only override arms admissions on "pop" but not "reset".
        self._seed(self.conn, "hz-foreign-1", FOREIGN_A,
                   "override foreign: git stash pop needs git stash list "
                   "first")
        recall_mod._CROSS_POLICY_WARNED = False
        saved = os.environ.pop("ZMEM_CROSS_PROJECT_HAZARD_VERBS", None)
        try:
            os.environ["ZMEM_CROSS_PROJECT_HAZARD_VERBS"] = "pop"
            self.assertEqual(recall_mod.hazard_verbs(), frozenset({"pop"}))
            common = dict(query=QUERY, moment="pretool",
                          current_namespace=CURRENT_NS)
            self.assertGreaterEqual(len(recall_mod.cross_project_admissions(
                self.conn, ops_tokens=["pop"], **common)), 1,
                "the overridden hazard verb 'pop' must arm the tier")
            self.assertEqual(len(recall_mod.cross_project_admissions(
                self.conn, ops_tokens=["reset"], **common)), 0,
                "a verb outside the override set must not arm the tier")
        finally:
            if saved is None:
                os.environ.pop("ZMEM_CROSS_PROJECT_HAZARD_VERBS", None)
            else:
                os.environ["ZMEM_CROSS_PROJECT_HAZARD_VERBS"] = saved
            recall_mod._CROSS_POLICY_WARNED = False


def conn_execute_snapshot(conn: sqlite3.Connection) -> list:
    """The row-content projection the no-copy assertions compare."""
    return conn.execute(
        "SELECT COUNT(*), id, namespace, content, signal FROM memory "
        "ORDER BY id"
    ).fetchall()


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2)
    finally:
        shutil.rmtree(_BOOT_TMP, ignore_errors=True)
