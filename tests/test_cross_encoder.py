#!/usr/bin/env python
"""Issue #63, 8.6: cross-encoder rerank — explicit-recall-only, default off.

Run:  python tests/test_cross_encoder.py   (no pytest; house convention)

Pinned invariants:
- enablement parse is env-only and default OFF;
- `cli_allowed` refuses --no-bump (every passive hook surface) AND
  --no-hybrid (the search subcommand's byte-stable alias contract);
- through the REAL CLI dispatch: an injected scorer reorders an explicit
  recall exactly once per run, and is NEVER invoked for --no-hybrid or
  --no-bump runs;
- any scorer exception degrades to unchanged output (recall still succeeds);
- hooks structurally cannot reach it: no hook file references this module or
  its env var, and an END-TO-END real-bash UserPromptSubmit run with
  ZMEM_CROSS_ENCODER=1 leaves the scorer canary untouched;
- rerank results leak no transient scoring keys into JSON.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# Shared seed contract (issue #63 zax-review round B1 fix): BOTH candidates
# must be lexically retrievable by QUERY on a runner WITHOUT sqlite_vec —
# the old "zzz unrelated filler" row was vector-lane-only and collapsed CI to
# one result. Every consumer below imports these constants so wording can
# never drift between seeder and assertions again.
QUERY_TEXT = "candidate"
SEED_A_MARKER = "primary"
# A carries a duplicated query token so BM25's term-frequency component makes
# it the DETERMINISTIC natural champion (C-theater lesson: rank ties make
# reorder assertions meaningless); B is merely retrievable.
SEED_A_CONTENT = f"{QUERY_TEXT} alpha {QUERY_TEXT} {SEED_A_MARKER} facts"
SEED_B_MARKER = "bravo"
SEED_B_CONTENT = f"{QUERY_TEXT} beta {SEED_B_MARKER} secondary tail"

ENABLE = "ZMEM_CROSS_ENCODER"
MODEL_ENV = "ZMEM_CROSS_ENCODER_MODEL"


class EnabledMatrix(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get(ENABLE)
        self.addCleanup(self._restore)

    def _restore(self):
        if self.saved is None:
            os.environ.pop(ENABLE, None)
        else:
            os.environ[ENABLE] = self.saved

    def test_default_off_and_garbage_off(self):
        os.environ.pop(ENABLE, None)
        from storelib.cross_encoder import enabled
        self.assertFalse(enabled())
        for garbage in ("", "0", "false", "off", "yes please"):
            os.environ[ENABLE] = garbage
            self.assertFalse(enabled(), repr(garbage))

    def test_truthy_on(self):
        from storelib.cross_encoder import enabled
        for good in ("1", "true", "YES", "on"):
            os.environ[ENABLE] = good
            self.assertTrue(enabled(), repr(good))


class CliAllowedGate(unittest.TestCase):
    """The single dispatch decision point — exercised without I/O."""

    def test_matrix(self):
        from storelib.cross_encoder import cli_allowed
        cases = [
            # (env_on, no_bump, no_hybrid, expected)
            (False, False, False, False),
            (True, False, False, True),    # explicit hybrid recall
            (True, True, False, False),    # passive: hooks/prefetch/PreCompact
            (True, False, True, False),    # search alias byte-stable contract
            (True, True, True, False),
            (False, True, True, False),
        ]
        for on, nb, nh, want in cases:
            os.environ.pop(ENABLE, None)
            if on:
                os.environ[ENABLE] = "1"
            self.assertEqual(
                cli_allowed(no_bump=nb, no_hybrid=nh), want,
                f"env_on={on} no_bump={nb} no_hybrid={nh}",
            )


def _seed_store(tmp: Path) -> Path:
    saved_store = os.environ.get("ZMEM_STORE")
    saved_profile_envs = {k: os.environ.get(k) for k in
                          ("ZMEM_EMBED_PROFILE", "ZMEM_DATA",
                           "ZMEM_MODEL_AUTODOWNLOAD")}
    os.environ["ZMEM_STORE"] = str(tmp / "store.sqlite")
    os.environ["ZMEM_EMBED_PROFILE"] = "fake"
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    sys.path.insert(0, str(SCRIPTS))
    for m in list(sys.modules):
        if m.startswith("storelib") or m == "store":
            del sys.modules[m]
    from storelib.schema import connect, _prepare_store
    from storelib.write import add_memory
    conn = connect()
    _prepare_store(conn)
    add_memory(conn, namespace="user:t", type_="fact",
               content=SEED_A_CONTENT, confidence=0.9)
    add_memory(conn, namespace="user:t", type_="fact",
               content=SEED_B_CONTENT, confidence=0.9)
    conn.close()
    return saved_store, saved_profile_envs


class CliRerankBehavior(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-ce-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.saved = {}
        (self.saved["store"],
         self.saved["others"]) = _seed_store(Path(self.tmp))
        os.environ[ENABLE] = "1"

    def tearDown(self):
        if self.saved["store"] is None:
            os.environ.pop("ZMEM_STORE", None)
        else:
            os.environ["ZMEM_STORE"] = self.saved["store"]
        for k, v in self.saved["others"].items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop(ENABLE, None)

    def _drive(self, extra_args):
        """Uninjected drive: natural order (no model file -> prod CE no-op).
        Returns (ids, scorer_calls=0, json_text)."""
        return self._drive_injected(None, extra_args=list(extra_args or []))

    def _drive_injected(self, scorer, extra_args=None):
        """Drive the real CLI dispatch with an optional injected scorer.
        Returns (ids, scorer_call_count, json_text)."""
        extra_args = list(extra_args or [])
        from storelib.cross_encoder import set_scorer
        calls = {"n": 0}
        if scorer is None:
            set_scorer(None)
        else:
            def wrapped(query, texts):
                calls["n"] += 1
                return scorer(query, texts)
            set_scorer(wrapped)
        out, err = io.StringIO(), io.StringIO()
        old_argv = list(sys.argv)
        sys.argv = ["store.py", "recall", "--query", QUERY_TEXT,
                    "--namespace", "user:t", "--json", *extra_args]
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                from storelib.cli import main as cli_main
                try:
                    cli_main()
                except SystemExit as e:  # pragma: no cover
                    self.assertEqual(e.code or 0, 0)
        finally:
            sys.argv = old_argv
            from storelib.cross_encoder import set_scorer as reset
            reset(None)
        rows = json.loads(out.getvalue())
        return [r["id"][:8] for r in rows], calls["n"], out.getvalue()

    def test_explicit_recall_reranks_reorders_and_calls_once(self):
        """zax-review B1/P004: rerank must fire exactly once AND flip the
        champion to whichever candidate the injected scorer prefers.

        Engine-agnostic design: the first UNINJECTED drive fixes the natural
        order (the production cross-encoder with no configured model is an
        exact no-op), so no BM25/vector-weight assumption can make this flaky
        on CI or locally."""
        def marker_of_top(ids, json_text):
            # _drive truncates ids to 8 chars for assertions; match that here
            rows = {r["id"][:8]: r["content"] for r in json.loads(json_text)}
            content = rows[ids[0]]
            return (SEED_A_MARKER if f" {SEED_A_MARKER} " in
                    f" {content} " else SEED_B_MARKER)

        natural_ids, _, natural_json = self._drive([])
        self.assertEqual(
            len(natural_ids), 2,
            "both seeds are lexical matches; vector lane must never be "
            "required for coverage on sqlite_vec-absent runners")
        natural_champ = marker_of_top(natural_ids, natural_json)
        loser = (SEED_B_MARKER if natural_champ == SEED_A_MARKER
                 else SEED_A_MARKER)
        needle = (f"{QUERY_TEXT} beta"
                  if loser == SEED_B_MARKER else f"{QUERY_TEXT} alpha")

        ids_reranked, n_calls, rerank_json = self._drive_injected(
            lambda q, texts: [1.0 if t.startswith(needle) else 0.0
                              for t in texts])
        self.assertEqual(n_calls, 1, "scorer invoked exactly once")
        self.assertEqual(marker_of_top(ids_reranked, rerank_json), loser,
                         "injected preference must become the champion")
        self.assertEqual(set(natural_ids), set(ids_reranked),
                         "same membership after rerank")

    def test_no_hybrid_never_invokes(self):
        _, n, _ = self._drive(["--no-hybrid"])
        self.assertEqual(n, 0, "search alias must be unreachable")

    def test_no_bump_never_invokes(self):
        _, n, _ = self._drive(["--no-bump"])
        self.assertEqual(n, 0, "passive surfaces must be unreachable")

    def test_exception_degrades_to_success(self):
        from storelib.cross_encoder import set_scorer

        def boom(q, t):
            raise RuntimeError("model exploded")

        set_scorer(boom)
        out, err = io.StringIO(), io.StringIO()
        old = list(sys.argv)
        sys.argv = ["store.py", "recall", "--query", QUERY_TEXT,
                    "--namespace", "user:t", "--json"]
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                from storelib.cli import main as cli_main
                cli_main()  # must NOT exit non-zero / raise beyond here
        finally:
            sys.argv = old
            set_scorer(None)
        rows = json.loads(out.getvalue())
        self.assertGreaterEqual(len(rows), 2)
        self.assertNotIn("_ce", rows[0])

    def test_transient_score_key_never_leaks(self):
        from storelib.cross_encoder import set_scorer
        set_scorer(lambda q, t: [float(len(x)) for x in t])
        out, err = io.StringIO(), io.StringIO()
        old = list(sys.argv)
        sys.argv = ["store.py", "recall", "--query", QUERY_TEXT,
                    "--namespace", "user:t", "--json"]
        try:
            with contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                from storelib.cli import main as cli_main
                cli_main()
        finally:
            sys.argv = old
            set_scorer(None)
        blob = out.getvalue()
        self.assertNotIn('"_ce"', blob)


class HookStructuralExclusion(unittest.TestCase):
    SOURCE_FILES = [
        "hooks/zmem-recall.sh",
        "hooks/zmem-subagent-recall.sh",
        "hooks/zmem-precompact.sh",
        "hooks/zmem-session-start.sh",
        "hooks/lib/zmem-recall-body.py",
        "hermes-plugin/__init__.py",
    ]

    def test_no_hook_references_module_or_env(self):
        for rel in self.SOURCE_FILES:
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            self.assertNotIn("cross_encoder", text.lower(), rel)
            self.assertNotIn(ENABLE, text, rel)
            self.assertNotIn(MODEL_ENV, text, rel)

    def test_passive_surfaces_pin_no_bump_in_recall_body(self):
        body = (REPO_ROOT / "hooks/lib/zmem-recall-body.py").read_text(
            encoding="utf-8")
        self.assertIn("--no-bump", body,
                      "the structural exclusion depends on this flag")


class EndToEndHookCanary(unittest.TestCase):
    """REAL bash UserPromptSubmit hook, CE enabled in child env: the injected
    canary proves the scorer function never executes inside that flow."""

    def test_hook_run_leaves_canary_untouched(self):
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash unavailable")
        tmp = Path(tempfile.mkdtemp(prefix="zmem-ce-hook-"))
        self.addCleanup(shutil.rmtree, tmp, True)

        # Hermetic on BOTH axes the hook resolves: the data dir (hooks prefer
        # ZMEM_DATA over ZMEM_STORE) AND the hook's project namespace.
        saved_env = {k: os.environ.get(k) for k in (
            "ZMEM_STORE", "ZMEM_DATA", "ZMEM_EMBED_PROFILE",
            "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR", ENABLE)}
        try:
            data_dir = tmp / "data"
            data_dir.mkdir()
            os.environ["ZMEM_DATA"] = str(data_dir)
            os.environ.pop("ZMEM_STORE", None)
            os.environ["ZMEM_EMBED_PROFILE"] = "fake"
            os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
            os.environ["ZMEM_MODELS_DIR"] = str(tmp / "no-models")

            sys.path.insert(0, str(SCRIPTS))
            for m in list(sys.modules):
                if m.startswith("storelib") or m == "store":
                    del sys.modules[m]
            from storelib.schema import connect, _prepare_store
            from storelib.write import add_memory

            conn = connect()
            _prepare_store(conn)
            add_memory(conn, namespace="user:t", type_="fact",
                       content=SEED_A_CONTENT, confidence=0.9)
            add_memory(conn, namespace="user:t", type_="fact",
                       content=SEED_B_CONTENT, confidence=0.9)
            conn.close()

            os.environ[ENABLE] = "1"
            canary = tmp / "canary.txt"     # nonexistent model path => no-op
            payload = json.dumps({
                "session_id": "ce-canary", "prompt": QUERY_TEXT,
                "cwd": str(tmp),
            }).encode()

            env = dict(os.environ)
            env[MODEL_ENV] = str(canary)   # proves no scorer/model touched
            env["ZMEM_NAMESPACE"] = "user:t"
            hook = REPO_ROOT / "hooks" / "zmem-recall.sh"
            r = subprocess.run(
                [bash, str(hook)], input=payload, capture_output=True,
                env=env, timeout=90, cwd=str(REPO_ROOT),
            )
            self.assertEqual(r.returncode, 0,
                             f"hook must fail-open: {r.stderr[-400:]!r}")
            self.assertFalse(canary.exists(),
                             "cross-encoder model/scorer ran under a hook")
            blob = r.stdout.decode("utf-8", errors="replace")
            self.assertIn(SEED_A_CONTENT.split()[2], blob,
                          "hook recall still surfaced the lexical candidate")
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main(verbosity=2)
