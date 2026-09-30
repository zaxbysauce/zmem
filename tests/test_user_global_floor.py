"""Issue #235: a user:global relevance floor on the passive injection lane.

The selector threads a per-moment floor into the shared kwargs dict both
recall entry points consume; on gated moments (``pretool``/``subagent`` —
hooks' posttoolbatch maps to ``pretool`` store-side) a ``user:global``
candidate whose MAX measured relevance lane sits below the floor is
withheld before the merge and its slot is returned empty rather than
backfilled. The envelope reports the withhold as ``global_withheld``.

Explicit query-less rule (issue #235 comment): a global candidate with NO
measured lane keeps the relevance gate's ``not measured`` exemption — the
floor never withholds it. Claude Code SubagentStart is query-less until
issue #253 restores task-keyed recall; emptying that surface's global tier
would contradict the issue's own AC2.

Every CLI check drives the exact argv shape hooks/lib/zmem-recall-body.py
builds, against a throwaway store seeded with a discriminating triple:

  proj-payments   project:ugfloor  lex 1.0     (operation-relevant project)
  global-runbook  user:global      lex 1.0     (relevant global — must ride)
  global-summit   user:global      lex ~0.34   (tangential global — generic
                                                token overlap only; above
                                                the generic lex floor 0.30,
                                                below the tier floor 0.5)

Run: python tests/test_user_global_floor.py   (no pytest required)
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import contextlib
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
INJECT_PY = SCRIPTS_DIR / "storelib" / "inject.py"
PYTHON = sys.executable

NS = "project:ugfloor"
NS_GLOBAL = "user:global"
QUERY = "database migration rollback payments"

ROWS = [
    ("proj-payments", NS,
     "database migration rollback procedure for the payments service"),
    ("global-runbook", NS_GLOBAL,
     "payments database migration runbook global lesson verify rollback plan"),
    ("global-summit", NS_GLOBAL,
     "database migration summit 2025 conference notes database migration talks"),
]

# Ambient knobs that must not leak into a fixture subprocess; the floor env
# itself is popped so every default-floor test exercises the shipped 0.5.
_POP_ENV = (
    "ZMEM_TEST_NOW", "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_EMBED_PROFILE",
    "ZMEM_SESSION", "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
    "ZMEM_INJECT_FLOOR_USER_GLOBAL", "ZMEM_QUERY_CONTEXT",
)

# Child-process seeding: storelib is imported only in the child (this test
# process never imports it for CLI checks), so STORE_PATH freezing can never
# cross test methods. Every child line is a complete physical line.
_SEED_CHILD = (
    "import json, sqlite3, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from storelib.schema import init_db, migrate\n"
    "conn = sqlite3.connect(sys.argv[2])\n"
    "conn.row_factory = sqlite3.Row\n"
    "init_db(conn)\n"
    "migrate(conn)\n"
    "rows = json.loads(sys.argv[3])\n"
    "sql = (\"INSERT INTO memory (id, namespace, type, content, tags, \"\n"
    "       \"source_ref, source_hash, confidence, signal, valid_from, \"\n"
    "       \"ingestion_ts) VALUES (?, ?, 'fact', ?, '', 'session:ugfloor', \"\n"
    "       \"'', 0.9, 'test', ?, ?)\")\n"
    "for index, (mid, ns, content) in enumerate(rows):\n"
    "    ts = '2026-01-01T00:00:0%d Z'.replace(' ', '') % index\n"
    "    conn.execute(sql, (mid, ns, content, ts, ts))\n"
    "conn.commit()\n"
    "conn.close()\n"
)


class _FloorBase(unittest.TestCase):
    """Throwaway store + data dir; every check drives the real CLI."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-ugfloor-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.data_dir = self.tmp
        self.env = dict(os.environ)
        self.env["ZMEM_STORE"] = self.store
        self.env["ZMEM_DATA"] = self.data_dir
        self.env["ZMEM_INJECT"] = "1"
        self.env["ZMEM_CAPTURE_MODE"] = "manual"
        self.env["ZMEM_AUTO_REKEY"] = "0"
        self.env["ZMEM_MODELS_DIR"] = os.path.join(self.tmp, "models-missing")
        self.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        self.env["PYTHONUTF8"] = "1"
        for key in _POP_ENV:
            self.env.pop(key, None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed(self):
        proc = subprocess.run(
            [PYTHON, "-c", _SEED_CHILD, str(SCRIPTS_DIR), self.store,
             json.dumps(ROWS)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])

    def _recall(self, moment, query=None, extra_env=None):
        """One hook-shaped CLI pull with a fresh session id.

        Query-less pulls drive the ``recent`` subcommand (recall requires
        --query); that is exactly the lane hooks/lib/zmem-recall-body.py
        selects when the event carries no prompt.
        """
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        command = "recall" if query is not None else "recent"
        argv = [PYTHON, str(STORE_PY), command, "--namespace", NS]
        if query is not None:
            argv += ["--query", query]
        argv += [
            "--limit", "5", "--include-global", "--global-limit", "3",
            "--no-bump", "--for-injection", "--json",
            f"--session-id=ugfloor-{uuid.uuid4().hex[:12]}",
            "--moment", moment, "--lane", "claude",
        ]
        proc = subprocess.run(argv, capture_output=True, text=True, env=env,
                              cwd=str(REPO_ROOT), timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        return json.loads(proc.stdout.strip())

    @staticmethod
    def _ids(envelope):
        return [row.get("id") for row in envelope.get("results", [])]


class GatedMomentWithholdTest(_FloorBase):
    """AC1: below-floor global withheld on pretool/subagent; reported."""

    def setUp(self):
        super().setUp()
        self._seed()

    def test_user_global_floor_withholds_low_relevance_rows(self):
        envelope = self._recall("pretool", query=QUERY)
        delivered = self._ids(envelope)
        # The tangential global row is withheld; its slot is not backfilled
        # (only ONE user:global row rides the fence).
        self.assertNotIn("global-summit", delivered)
        self.assertIn("global-runbook", delivered)
        self.assertIn("proj-payments", delivered)
        self.assertEqual(
            sum(1 for row in envelope["results"]
                if row.get("namespace") == NS_GLOBAL), 1)
        # Telemetry names the withheld slot.
        self.assertGreaterEqual(int(envelope.get("global_withheld", 0)), 1)
        # The rendered fence (the hook-path integration shape) omits the
        # tangential content and keeps the relevant global content.
        rendered = envelope.get("rendered") or ""
        self.assertNotIn("summit", rendered.lower())
        self.assertIn("runbook", rendered.lower())

    def test_high_relevance_global_rows_still_surface(self):
        envelope = self._recall("pretool", query=QUERY)
        self.assertIn("global-runbook", self._ids(envelope))

    def test_subagent_query_bearing_pull_withholds_too(self):
        envelope = self._recall("subagent", query=QUERY)
        self.assertNotIn("global-summit", self._ids(envelope))
        self.assertIn("global-runbook", self._ids(envelope))
        self.assertGreaterEqual(int(envelope.get("global_withheld", 0)), 1)


class UngatedMomentCompositionTest(_FloorBase):
    """AC1's other half: ungated moments keep current composition."""

    def setUp(self):
        super().setUp()
        self._seed()

    def test_session_start_and_user_prompt_global_behavior_unchanged(self):
        for moment, query in (("session_start", None),
                              ("user_prompt", QUERY)):
            envelope = self._recall(moment, query=query)
            delivered = self._ids(envelope)
            self.assertIn("global-summit", delivered,
                          f"{moment}: composition must be unchanged")
            self.assertIn("global-runbook", delivered)
            self.assertNotIn("global_withheld", envelope,
                             f"{moment}: ungated — no withhold telemetry")


class QuerylessExemptionTest(_FloorBase):
    """The explicit query-less rule from the issue comment."""

    def setUp(self):
        super().setUp()
        self._seed()

    def test_queryless_subagent_pulls_keep_not_measured_exemption(self):
        envelope = self._recall("subagent", query=None)
        delivered = self._ids(envelope)
        # Query-less recent lane: global rows carry no measured lanes, so
        # the not-measured exemption holds even on a gated moment (issue
        # #253 will restore task-keyed queries and make the floor bite).
        self.assertIn("global-summit", delivered)
        self.assertIn("global-runbook", delivered)
        self.assertNotIn("global_withheld", envelope)


class FloorEnvTuningTest(_FloorBase):
    """ZMEM_INJECT_FLOOR_USER_GLOBAL tightens or disables the floor."""

    def setUp(self):
        super().setUp()
        self._seed()

    def test_floor_env_override_and_disable(self):
        # Disabled at 0: the tangential row rides again (honest disable).
        envelope = self._recall("pretool", query=QUERY,
                                extra_env={"ZMEM_INJECT_FLOOR_USER_GLOBAL":
                                           "0"})
        self.assertIn("global-summit", self._ids(envelope))
        # Tightened past every measured lane (relevance values are <= 1.0,
        # the runbook's lex lane is exactly 1.0): both global rows withheld,
        # the project row still rides, and the counter names both.
        envelope = self._recall("pretool", query=QUERY,
                                extra_env={"ZMEM_INJECT_FLOOR_USER_GLOBAL":
                                           "1.5"})
        delivered = self._ids(envelope)
        self.assertNotIn("global-summit", delivered)
        self.assertNotIn("global-runbook", delivered)
        self.assertIn("proj-payments", delivered)
        self.assertEqual(int(envelope.get("global_withheld", 0)), 2)

    def test_row_exactly_at_the_floor_is_delivered(self):
        """Boundary pin: the shipped operator is >= (review nit 1).

        The floor is set to EXACTLY the tangential row's measured lex lane
        (read from the first envelope's candidate_lanes, so the pin holds
        even if the FTS-derived value drifts): a row AT the floor delivers.
        Flipping the comparison to a strict > would withhold it and turn
        this RED.
        """
        # Read the lane value from a floor-DISABLED pull: a withheld row
        # never reaches the merge, so its lane is absent from the envelope
        # of a withholding run.
        envelope = self._recall("pretool", query=QUERY,
                                extra_env={"ZMEM_INJECT_FLOOR_USER_GLOBAL":
                                           "0"})
        lanes = envelope.get("candidate_lanes") or {}
        summit = lanes.get("global-summit") or {}
        lane_value = summit.get("lex")
        self.assertIsNotNone(
            lane_value, "fixture must measure a lex lane for global-summit")
        exact = repr(float(lane_value))
        at_floor = self._recall(
            "pretool", query=QUERY,
            extra_env={"ZMEM_INJECT_FLOOR_USER_GLOBAL": exact})
        self.assertIn("global-summit", self._ids(at_floor),
                      f"row at exactly the floor ({exact}) must deliver")
        self.assertNotIn("global_withheld", at_floor)


class SelectorSymmetryTest(unittest.TestCase):
    """AC3: the selector threads tier params to BOTH recall functions.

    Fresh in-process load of storelib.inject with storelib.recall's two
    entry points stubbed; the kwargs each stub received must carry the same
    ``_user_global_floor``. This is the PR #225 fail-open hazard class: an
    asymmetric kwarg raises TypeError inside the selector's blanket except
    and silently empties the passive lane.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-ugfloor-sym-")
        # Snapshot for teardown: the selector's LAZY imports (storelib.recall,
        # delivery_ledger, ops_tokens) resolve at CALL time, so sys.path and
        # sys.modules must stay populated through the test body; teardown
        # restores the pre-test world.
        self._saved_modules = {k: v for k, v in sys.modules.items()
                               if k == "storelib" or k.startswith("storelib.")}
        self._had_scripts_dir = str(SCRIPTS_DIR) in sys.path

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
        for key in list(sys.modules):
            if key == "storelib" or key.startswith("storelib."):
                sys.modules.pop(key, None)
        sys.modules.update(self._saved_modules)
        if not self._had_scripts_dir and str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    def _load_selector(self):
        """Fresh-load storelib.inject; return (inject_mod, recall_mod).

        ``select_and_budget_for_injection`` imports ``storelib.recall``
        lazily INSIDE the function body, so the stubs must patch the real
        ``storelib.recall`` module's attributes, not a module-level alias —
        and the import machinery must stay live for the call itself.
        """
        for key in list(sys.modules):
            if key == "storelib" or key.startswith("storelib."):
                sys.modules.pop(key, None)
        env = dict(os.environ)
        for key in _POP_ENV:
            env.pop(key, None)
        env["ZMEM_STORE"] = os.path.join(self.tmp, "store.sqlite")
        env["ZMEM_DATA"] = self.tmp
        with mock.patch.dict(os.environ, env, clear=True):
            if str(SCRIPTS_DIR) not in sys.path:
                sys.path.insert(0, str(SCRIPTS_DIR))
            import storelib.recall as recall_mod  # noqa: F401
            spec = importlib.util.spec_from_file_location(
                "storelib.inject_sym", str(INJECT_PY))
            module = importlib.util.module_from_spec(spec)
            with contextlib.redirect_stdout(io.StringIO()):
                spec.loader.exec_module(module)
            return module, recall_mod

    @staticmethod
    def _stub(capture_sink):
        def _stub_impl(conn, **kwargs):
            capture_sink.append(kwargs)
            capture = kwargs.get("_capture")
            if capture is not None:
                capture.update({"results": [], "reason": "empty-pool",
                                "candidate_ids": []})
            return []
        return _stub_impl

    def _selector_env(self):
        env = dict(os.environ)
        for key in _POP_ENV:
            env.pop(key, None)
        env["ZMEM_STORE"] = os.path.join(self.tmp, "store.sqlite")
        env["ZMEM_DATA"] = self.tmp
        return env

    def test_selector_tier_forwarding_is_symmetric(self):
        inject_mod, recall_mod = self._load_selector()
        recorded = {"recall": [], "recent": []}

        conn = sqlite3.connect(":memory:")
        with mock.patch.dict(os.environ, self._selector_env(), clear=True):
            with mock.patch.object(recall_mod, "recall_memory",
                                   side_effect=self._stub(
                                       recorded["recall"])), \
                 mock.patch.object(recall_mod, "recent_memory",
                                   side_effect=self._stub(
                                       recorded["recent"])):
                for query, lane in ((QUERY, "recall"), ("", "recent")):
                    with contextlib.redirect_stdout(io.StringIO()):
                        inject_mod.select_and_budget_for_injection(
                            conn, query=query, namespace=NS,
                            moment="pretool",
                            session_id=f"sym-{uuid.uuid4().hex[:8]}",
                            data_dir=self.tmp, user_global_floor=0.5)
                    self.assertTrue(recorded[lane],
                                    f"{lane} path never dispatched")
                    got = recorded[lane][-1]
                    self.assertIn("_user_global_floor", got)
                    self.assertEqual(got["_user_global_floor"], 0.5)
        # Both paths received the identical tier parameter value.
        self.assertEqual(
            {r["_user_global_floor"] for r in recorded["recall"]}, {0.5})
        self.assertEqual(
            {r["_user_global_floor"] for r in recorded["recent"]}, {0.5})

    def test_selector_floor_off_on_ungated_moments(self):
        inject_mod, recall_mod = self._load_selector()
        recorded = {"recent": []}

        conn = sqlite3.connect(":memory:")
        with mock.patch.dict(os.environ, self._selector_env(), clear=True):
            with mock.patch.object(recall_mod, "recent_memory",
                                   side_effect=self._stub(
                                       recorded["recent"])):
                with contextlib.redirect_stdout(io.StringIO()):
                    inject_mod.select_and_budget_for_injection(
                        conn, query="", namespace=NS,
                        moment="session_start",
                        session_id=f"sym-{uuid.uuid4().hex[:8]}",
                        data_dir=self.tmp)
        self.assertTrue(recorded["recent"])
        # Ungated moment: the threaded floor is None (no enforcement).
        self.assertIsNone(recorded["recent"][-1].get("_user_global_floor"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
