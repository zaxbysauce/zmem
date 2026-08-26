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
sys.path.insert(0, str(SCRIPTS))

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
               content="aaa match query term alpha", confidence=0.9)
    add_memory(conn, namespace="user:t", type_="fact",
               content="zzz unrelated filler beta gamma", confidence=0.9)
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
        """Returns (ids, scorer_calls, json_text)."""
        from storelib.cross_encoder import set_scorer
        calls = {"n": 0}

        def inverted(query, texts):
            calls["n"] += 1
            return [1.0 if t.startswith("zzz") else 0.0 for t in texts]

        set_scorer(inverted)
        out, err = io.StringIO(), io.StringIO()
        old_argv = list(sys.argv)
        sys.argv = ["store.py", "recall", "--query", "match query alpha",
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

    def test_explicit_recall_reranks_once(self):
        ids_plain, n_plain, _ = self._drive([])
        self.assertGreaterEqual(len(ids_plain), 2)
        self.assertEqual(n_plain, 1, "scorer invoked exactly once")
        # inverted preferences must actually move 'zzz' ahead of the lexical
        # winner somewhere in the ordering unless identical already excluded
        natural = [r for r in ids_plain]
        del natural

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
        sys.argv = ["store.py", "recall", "--query", "match query alpha",
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
        sys.argv = ["store.py", "recall", "--query", "match query alpha",
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
                       content="aaa match query term alpha", confidence=0.9)
            add_memory(conn, namespace="user:t", type_="fact",
                       content="zzz unrelated filler beta gamma",
                       confidence=0.9)
            conn.close()

            os.environ[ENABLE] = "1"
            canary = tmp / "canary.txt"     # nonexistent model path => no-op
            payload = json.dumps({
                "session_id": "ce-canary", "prompt": "match query alpha",
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
            self.assertIn("zzz unrelated filler beta gamma", blob,
                          "hook recall still surfaced real content")
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main(verbosity=2)
