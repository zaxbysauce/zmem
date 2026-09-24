#!/usr/bin/env python
"""Issue #63, 8.6: cross-encoder rerank — explicit-recall-only; default
flipped ON at unset with the issue #126 release (owner-directed; opt out
with ZMEM_CROSS_ENCODER=0).

Run:  python tests/test_cross_encoder.py   (no pytest; house convention)

Pinned invariants:
- enablement parse is env-only; UNSET enables, any non-truthy SET value
  (canonically `0`) opts out;
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

import collections
import contextlib
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

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

    def test_default_on_and_explicit_opt_out(self):
        # Issue #126 default flip: UNSET means ON; a SET value keeps the
        # original truthy-membership parse, so "0"/"false"/"off"/garbage
        # (and set-but-empty) all opt out explicitly.
        os.environ.pop(ENABLE, None)
        from storelib.cross_encoder import enabled

        self.assertTrue(enabled(), "unset must enable (issue #126 flip)")
        for opt_out in ("", "0", "false", "off", "yes please"):
            os.environ[ENABLE] = opt_out
            self.assertFalse(enabled(), repr(opt_out))

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
            # (env_state, no_bump, no_hybrid, expected) — env_state is
            # "0" (explicit opt-out), "1" (truthy on), or None (unset, ON
            # since the issue #126 default flip).
            ("0", False, False, False),  # explicit opt-out wins everywhere
            ("1", False, False, True),  # explicit hybrid recall
            ("1", True, False, False),  # passive: hooks/prefetch/PreCompact
            ("1", False, True, False),  # search alias byte-stable contract
            ("1", True, True, False),
            ("0", True, True, False),
            (None, False, False, True),  # unset default-on (issue #126)
            (None, True, False, False),  # unset still never fires --no-bump
            (None, False, True, False),  # unset still never fires --no-hybrid
        ]
        for state, nb, nh, want in cases:
            os.environ.pop(ENABLE, None)
            if state is not None:
                os.environ[ENABLE] = state
            self.assertEqual(
                cli_allowed(no_bump=nb, no_hybrid=nh),
                want,
                f"env={state!r} no_bump={nb} no_hybrid={nh}",
            )

    def test_passive_matrix(self):
        # Issue #125 AC3: the passive lane of the single decision point.
        from storelib.cross_encoder import cli_allowed

        saved = {k: os.environ.get(k) for k in (ENABLE, "ZMEM_CROSS_ENCODER_PASSIVE")}

        def _restore_two():
            # Targeted restore ONLY -- a wholesale os.environ.clear() here
            # wiped USERPROFILE/PATH for every later test in the process
            # (doctor import then failed on Path.home()).
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.addCleanup(_restore_two)
        os.environ[ENABLE] = "1"
        os.environ.pop("ZMEM_CROSS_ENCODER_PASSIVE", None)
        self.assertFalse(cli_allowed(no_bump=True, no_hybrid=False, for_injection=True))
        os.environ["ZMEM_CROSS_ENCODER_PASSIVE"] = "1"
        self.assertTrue(cli_allowed(no_bump=True, no_hybrid=False, for_injection=True))
        self.assertFalse(
            cli_allowed(no_bump=False, no_hybrid=False, for_injection=True)
        )
        self.assertTrue(cli_allowed(no_bump=True, no_hybrid=True, for_injection=True))
        for bad in ("0", "true", "yes", "on", " 1", ""):
            os.environ["ZMEM_CROSS_ENCODER_PASSIVE"] = bad
            self.assertFalse(
                cli_allowed(no_bump=True, no_hybrid=False, for_injection=True)
            )
        matrix = [
            # (env_state, no_bump, no_hybrid, expected) — "0" rows are the
            # explicit opt-out; unset rows pin the issue #126 default-on.
            ("0", False, False, False),
            ("1", False, False, True),
            ("1", True, False, False),
            ("1", False, True, False),
            ("1", True, True, False),
            ("0", True, True, False),
            (None, False, False, True),
        ]
        os.environ["ZMEM_CROSS_ENCODER_PASSIVE"] = "1"
        for state, nb, nh, want in matrix:
            if state is not None:
                os.environ[ENABLE] = state
            else:
                os.environ.pop(ENABLE, None)
            self.assertEqual(
                cli_allowed(no_bump=nb, no_hybrid=nh, for_injection=False),
                want,
                f"explicit: env={state!r} no_bump={nb} no_hybrid={nh}",
            )
            self.assertEqual(
                cli_allowed(no_bump=nb, no_hybrid=nh),
                want,
                f"omitted: env={state!r} no_bump={nb} no_hybrid={nh}",
            )


def _seed_store(tmp: Path) -> Path:
    saved_store = os.environ.get("ZMEM_STORE")
    saved_profile_envs = {
        k: os.environ.get(k)
        for k in ("ZMEM_EMBED_PROFILE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD")
    }
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
    add_memory(
        conn, namespace="user:t", type_="fact", content=SEED_A_CONTENT, confidence=0.9
    )
    add_memory(
        conn, namespace="user:t", type_="fact", content=SEED_B_CONTENT, confidence=0.9
    )
    conn.close()
    return saved_store, saved_profile_envs


class CliRerankBehavior(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-ce-cli-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.saved = {}
        (self.saved["store"], self.saved["others"]) = _seed_store(Path(self.tmp))
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
        sys.argv = [
            "store.py",
            "recall",
            "--query",
            QUERY_TEXT,
            "--namespace",
            "user:t",
            "--json",
            *extra_args,
        ]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                from storelib.cli import main as cli_main

                try:
                    cli_main()
                except SystemExit as e:  # pragma: no cover
                    self.assertEqual(e.code or 0, 0)
        finally:
            sys.argv = old_argv
            from storelib.cross_encoder import set_scorer as reset

            reset(None)
        parsed = json.loads(out.getvalue())
        # v13 (issue #65, 10.8): read --json emits the envelope.
        rows = parsed["results"] if isinstance(parsed, dict) else parsed
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
            parsed = json.loads(json_text)
            # v13 (issue #65, 10.8): read --json emits the envelope.
            _rows = parsed["results"] if isinstance(parsed, dict) else parsed
            rows = {r["id"][:8]: r["content"] for r in _rows}
            content = rows[ids[0]]
            return (
                SEED_A_MARKER
                if f" {SEED_A_MARKER} " in f" {content} "
                else SEED_B_MARKER
            )

        natural_ids, _, natural_json = self._drive([])
        self.assertEqual(
            len(natural_ids),
            2,
            "both seeds are lexical matches; vector lane must never be "
            "required for coverage on sqlite_vec-absent runners",
        )
        natural_champ = marker_of_top(natural_ids, natural_json)
        loser = SEED_B_MARKER if natural_champ == SEED_A_MARKER else SEED_A_MARKER
        needle = (
            f"{QUERY_TEXT} beta" if loser == SEED_B_MARKER else f"{QUERY_TEXT} alpha"
        )

        ids_reranked, n_calls, rerank_json = self._drive_injected(
            lambda q, texts: [1.0 if t.startswith(needle) else 0.0 for t in texts]
        )
        self.assertEqual(n_calls, 1, "scorer invoked exactly once")
        self.assertEqual(
            marker_of_top(ids_reranked, rerank_json),
            loser,
            "injected preference must become the champion",
        )
        self.assertEqual(
            set(natural_ids), set(ids_reranked), "same membership after rerank"
        )

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
        sys.argv = [
            "store.py",
            "recall",
            "--query",
            QUERY_TEXT,
            "--namespace",
            "user:t",
            "--json",
        ]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                from storelib.cli import main as cli_main

                cli_main()  # must NOT exit non-zero / raise beyond here
        finally:
            sys.argv = old
            set_scorer(None)
        parsed = json.loads(out.getvalue())
        # v13 (issue #65, 10.8): read --json emits the envelope.
        rows = parsed["results"] if isinstance(parsed, dict) else parsed
        self.assertGreaterEqual(len(rows), 2)
        self.assertNotIn("_ce", rows[0])

    def test_transient_score_key_never_leaks(self):
        from storelib.cross_encoder import set_scorer

        set_scorer(lambda q, t: [float(len(x)) for x in t])
        out, err = io.StringIO(), io.StringIO()
        old = list(sys.argv)
        sys.argv = [
            "store.py",
            "recall",
            "--query",
            QUERY_TEXT,
            "--namespace",
            "user:t",
            "--json",
        ]
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
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
        "hooks/lib/zmem-session-start-payload.py",
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
        body = (REPO_ROOT / "hooks/lib/zmem-recall-body.py").read_text(encoding="utf-8")
        self.assertIn(
            "--no-bump", body, "the structural exclusion depends on this flag"
        )


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
        saved_env = {
            k: os.environ.get(k)
            for k in (
                "ZMEM_STORE",
                "ZMEM_DATA",
                "ZMEM_EMBED_PROFILE",
                "ZMEM_MODEL_AUTODOWNLOAD",
                "ZMEM_MODELS_DIR",
                ENABLE,
            )
        }
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
            add_memory(
                conn,
                namespace="user:t",
                type_="fact",
                content=SEED_A_CONTENT,
                confidence=0.9,
            )
            add_memory(
                conn,
                namespace="user:t",
                type_="fact",
                content=SEED_B_CONTENT,
                confidence=0.9,
            )
            conn.close()

            os.environ[ENABLE] = "1"
            canary = tmp / "canary.txt"  # nonexistent model path => no-op
            payload = json.dumps(
                {
                    "session_id": "ce-canary",
                    "prompt": QUERY_TEXT,
                    "cwd": str(tmp),
                }
            ).encode()

            env = dict(os.environ)
            env[MODEL_ENV] = str(canary)  # proves no scorer/model touched
            env["ZMEM_NAMESPACE"] = "user:t"
            hook = REPO_ROOT / "hooks" / "zmem-recall.sh"
            r = subprocess.run(
                [bash, str(hook)],
                input=payload,
                capture_output=True,
                env=env,
                timeout=90,
                cwd=str(REPO_ROOT),
            )
            self.assertEqual(
                r.returncode, 0, f"hook must fail-open: {r.stderr[-400:]!r}"
            )
            self.assertFalse(
                canary.exists(), "cross-encoder model/scorer ran under a hook"
            )
            blob = r.stdout.decode("utf-8", errors="replace")
            self.assertIn(
                SEED_A_CONTENT.split()[2],
                blob,
                "hook recall still surfaced the lexical candidate",
            )
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


PROFILE_SHA256 = "be30078bc29868074ddb09c8eefc8170594368fc82908d63988dd9e226c26993"
TOKENIZER_SHA256 = "caef352a8affbe14f5235b5487dd766f2133ddfb70c2d59bfca5734496051cd7"


class CrossEncoderProfileAndBudgetTests(unittest.TestCase):
    """Issue #125 AC1/AC2/AC4: profile registry, fixture digests, safe
    autodownload, and the 250 ms fail-open budget."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-ce-125-"))
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "ZMEM_STORE",
                "ZMEM_DATA",
                "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD",
                ENABLE,
                MODEL_ENV,
                "ZMEM_CROSS_ENCODER_MODEL_URL",
                "ZMEM_CROSS_ENCODER_PASSIVE",
                "ZMEM_CROSS_ENCODER_SHADOW",
                "ZMEM_CROSS_ENCODER_BUDGET_MS",
                "ZMEM_EMBED_PROFILE",
            )
        }
        os.environ["ZMEM_STORE"] = str(self.tmp / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(self.tmp)
        os.environ["ZMEM_MODELS_DIR"] = str(self.tmp / "missing-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        for k in (
            ENABLE,
            MODEL_ENV,
            "ZMEM_CROSS_ENCODER_MODEL_URL",
            "ZMEM_CROSS_ENCODER_PASSIVE",
            "ZMEM_CROSS_ENCODER_SHADOW",
            "ZMEM_CROSS_ENCODER_BUDGET_MS",
        ):
            os.environ.pop(k, None)
        sys.path.insert(0, str(SCRIPTS))
        for m in list(sys.modules):
            if m.startswith("storelib") or m in (
                "store",
                "embeddings",
                "cross_encoder_profiles",
            ):
                sys.modules.pop(m, None)

    def tearDown(self):
        from storelib.cross_encoder import set_scorer

        set_scorer(None)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_profile_has_nonempty_checksum(self):
        import cross_encoder_profiles as profiles

        entry = profiles.resolve_profile()
        self.assertEqual(profiles.DEFAULT_PROFILE, "mini-pair-scorer")
        digest = entry["sha256"]
        self.assertEqual(
            digest,
            PROFILE_SHA256,
            f"profile digest must be the exact lowercase pin, got {digest}",
        )
        self.assertEqual(entry["model_file"], "mini_pair_scorer.onnx")
        self.assertEqual(entry["tokenizer_file"], "tokenizer.json")
        self.assertEqual(entry["url"], "")
        # Unknown profiles refuse with the contract error string.
        with self.assertRaises(ValueError) as ctx:
            profiles.resolve_profile("no-such-profile")
        self.assertEqual(
            str(ctx.exception), "unknown cross-encoder profile: no-such-profile"
        )

    def test_fixture_hashes(self):
        model = (
            REPO_ROOT / "tests" / "fixtures" / "cross_encoder" / "mini_pair_scorer.onnx"
        )
        tok = REPO_ROOT / "tests" / "fixtures" / "cross_encoder" / "tokenizer.json"
        self.assertEqual(model.stat().st_size, 128)
        self.assertEqual(hashlib.sha256(model.read_bytes()).hexdigest(), PROFILE_SHA256)
        self.assertEqual(tok.stat().st_size, 899)
        self.assertEqual(hashlib.sha256(tok.read_bytes()).hexdigest(), TOKENIZER_SHA256)
        # tf-02: the expected-profile fixture is a real output authority -
        # its bytes must equal the canonical record for the shipped profile.
        self.assertEqual(EXPECTED_PROFILE_BYTES, _expected_profile_record())

    def test_autodownload_is_off_by_default(self):
        import cross_encoder_profiles as profiles
        from storelib import cross_encoder as ce

        dest = profiles.profile_model_path(
            Path(os.environ["ZMEM_MODELS_DIR"]), profiles.resolve_profile()
        )
        attempts = []
        real_urlopen = urllib.request.urlopen

        def _spy(*a, **k):
            attempts.append(a)
            return real_urlopen(*a, **k)

        with mock.patch("urllib.request.urlopen", _spy):
            for value in (None, "0", "yes", "true"):
                if value is None:
                    os.environ.pop("ZMEM_MODEL_AUTODOWNLOAD", None)
                else:
                    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = value
                ce.set_scorer(None)
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertIsNone(ce._local_scorer())
                self.assertEqual(
                    attempts, [], f"download attempted with " f"AUTODOWNLOAD={value!r}"
                )
                self.assertIn(
                    "[zmem] cross-encoder state=autodownload-disabled", err.getvalue()
                )
        self.assertFalse(dest.exists())

    def test_doctor_reports_unreadable_checksum_state(self):
        # Reviewer round 1, finding 1: a present-but-unreadable model must
        # surface as checksum_state="unreadable", never "missing".
        import doctor as doctor_module
        import builtins
        from unittest import mock

        models_dir = Path(os.environ["ZMEM_MODELS_DIR"])
        models_dir.mkdir(parents=True, exist_ok=True)
        model_file = models_dir / "mini_pair_scorer.onnx"
        model_file.write_bytes(b"present but locked")
        (models_dir / "tokenizer.json").write_text("[]", encoding="utf-8")
        real_open = builtins.open

        def _locked_open(file, *a, **k):
            if str(file) == str(model_file):
                raise PermissionError(13, "locked by probe")
            return real_open(file, *a, **k)

        with mock.patch("builtins.open", _locked_open):
            report = doctor_module.build_report(
                REPO_ROOT, REPO_ROOT, store_override=os.environ["ZMEM_STORE"]
            )
        ce = None
        for check in report.get("checks", []):
            details = check.get("details") or {}
            if "cross_encoder" in details:
                ce = details["cross_encoder"]
                break
        self.assertIsNotNone(ce)
        self.assertEqual(ce["checksum_state"], "unreadable")
        self.assertTrue(ce["model_path"].endswith("mini_pair_scorer.onnx"))

    def test_profile_path_refuses_digest_mismatch_before_load(self):
        # Reviewer round 1, finding 2: the load-time digest gate must be
        # discriminating. With LOADER FAKES installed, the mismatched
        # profile-path file WOULD construct a scorer — so a None result is
        # attributable to verify_profile_file alone, not to load failure.
        import types
        from unittest import mock
        import cross_encoder_profiles as profiles
        from storelib import cross_encoder as ce

        models_dir = Path(os.environ["ZMEM_MODELS_DIR"])
        models_dir.mkdir(parents=True, exist_ok=True)
        (models_dir / "mini_pair_scorer.onnx").write_bytes(b"loadable-by-fakes")
        (models_dir / "tokenizer.json").write_text("[]", encoding="utf-8")

        fake_ort = types.ModuleType("onnxruntime")

        class _FakeSession:
            instances = 0

            def __init__(self, model_bytes):
                _FakeSession.instances += 1
                assert isinstance(model_bytes, (bytes, bytearray))

            def run(self, _names, _feeds):
                return [[[0.0]]]

        fake_ort.InferenceSession = _FakeSession
        fake_tok = types.ModuleType("tokenizers")

        class _FakeTok:
            @staticmethod
            def from_file(_path):
                return _FakeTok()

            def enable_padding(self, **_k):
                pass

            def enable_truncation(self, **_k):
                pass

            def encode(self, _q, _t):
                class _Enc:
                    ids = [1]
                    attention_mask = [1]

                return _Enc()

        fake_tok.Tokenizer = _FakeTok
        ce.set_scorer(None)
        _FakeSession.instances = 0
        with mock.patch.dict(
            sys.modules, {"onnxruntime": fake_ort, "tokenizers": fake_tok}
        ):
            # Gate present: digest mismatch refuses BEFORE construction.
            self.assertIsNone(ce._local_scorer())
            self.assertEqual(
                _FakeSession.instances, 0, "session constructed despite digest mismatch"
            )
            # Gate (simulated) absent: the same file WOULD load — proving
            # the refusal above came from the digest gate.
            with mock.patch.object(profiles, "verify_profile_file", return_value=True):
                self.assertIsNotNone(ce._local_scorer())
                self.assertGreaterEqual(_FakeSession.instances, 1)
        ce.set_scorer(None)

    def test_checksum_mismatch_fails_open(self):
        import cross_encoder_profiles as profiles
        from storelib import cross_encoder as ce

        models_dir = Path(os.environ["ZMEM_MODELS_DIR"])
        models_dir.mkdir(parents=True, exist_ok=True)
        dest = profiles.profile_model_path(models_dir, profiles.resolve_profile())
        dest.write_bytes(b"not the pinned model bytes")
        tok = models_dir / "tokenizer.json"
        tok.write_bytes(
            (
                REPO_ROOT / "tests" / "fixtures" / "cross_encoder" / "tokenizer.json"
            ).read_bytes()
        )
        ce.set_scorer(None)
        self.assertIsNone(ce._local_scorer())
        # File stays (doctor reports mismatch; load refuses).
        self.assertTrue(dest.exists())

    def test_budget_exhaustion_preserves_order(self):
        from storelib import cross_encoder as ce

        rows = [
            {"id": "00000000-0000-4000-8000-000000000001", "content": "first"},
            {"id": "00000000-0000-4000-8000-000000000002", "content": "second"},
            {"id": "00000000-0000-4000-8000-000000000003", "content": "third"},
        ]
        samples = iter([0.000, 0.100, 0.251, 0.251, 0.251, 0.251, 0.251])

        def fake_clock():
            return next(samples)

        ce.set_scorer(lambda q, texts: [1.0 for _ in texts])
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                out = ce.maybe_rerank("query", rows, clock=fake_clock)
            self.assertEqual([r["id"] for r in out], [r["id"] for r in rows])
            self.assertNotIn("score", out[0])
            reason_lines = [
                line
                for line in err.getvalue().splitlines()
                if "cross-encoder reason=" in line
            ]
            self.assertEqual(len(reason_lines), 1)
            self.assertIn("reason=budget-exhausted", reason_lines[0])
        finally:
            ce.set_scorer(None)
        # CLI leg: budget exhaustion exits through the recall CLI with 0.
        os.environ[ENABLE] = "1"
        os.environ["ZMEM_CROSS_ENCODER_BUDGET_MS"] = "0"
        from storelib.schema import connect as _connect, _prepare_store
        from storelib.write import add_memory

        conn = _connect()
        _prepare_store(conn)
        # TWO query-matching rows: one row is the trivial early-exit that
        # by design emits no reason line (issue #125 A3).
        add_memory(
            conn,
            namespace="user:issue-125",
            type_="fact",
            content="budget exhaustion keeps recall healthy alpha",
            confidence=0.9,
        )
        add_memory(
            conn,
            namespace="user:issue-125",
            type_="fact",
            content="budget exhaustion keeps recall healthy bravo",
            confidence=0.9,
        )
        conn.close()
        out, err = io.StringIO(), io.StringIO()
        old_argv = list(sys.argv)
        sys.argv = [
            "store.py",
            "recall",
            "--query",
            "budget",
            "--namespace",
            "user:issue-125",
            "--json",
        ]
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                from storelib.cli import main as cli_main

                try:
                    cli_main()
                except SystemExit as e:
                    code = e.code or 0
        finally:
            sys.argv = old_argv
            ce.set_scorer(None)
        self.assertEqual(code, 0)
        self.assertIn("reason=budget-exhausted", err.getvalue())


FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "cross_encoder"
# Normalize the fixture read against CRLF materialization (house pattern):
# the committed blob is LF (pinned by digest), and .gitattributes now pins
# eol=lf for this path, but the normalization keeps the comparison exact
# even under a checkout config that ignores the attribute.
EXPECTED_SHADOW = (FIXTURE_DIR / "expected-shadow.jsonl").read_bytes()
EXPECTED_SHADOW = EXPECTED_SHADOW.replace(b"\r\n", b"\n")
EXPECTED_PROFILE_BYTES = (FIXTURE_DIR / "expected-profile.json").read_bytes()


def _expected_profile_record() -> bytes:
    # The canonical expected-profile.json line for the shipped profile
    # (LF-terminated; the byte contract the committed fixture must match).
    import json as _json
    import cross_encoder_profiles as _profiles

    entry = _profiles.resolve_profile()
    record = {
        "model_file": entry["model_file"],
        "model_sha256": entry["sha256"],
        "model_size": 128,
        "tokenizer_file": entry["tokenizer_file"],
        "tokenizer_sha256": TOKENIZER_SHA256,
        "tokenizer_size": 899,
    }
    line = _json.dumps(record, sort_keys=True, separators=(",", ":"))
    return (line + chr(10)).encode("utf-8")


SHADOW_IDS = [
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000002",
    "00000000-0000-4000-8000-000000000003",
]


class CrossEncoderShadowAndFinalSetTests(unittest.TestCase):
    """Issue #125 AC3/AC5: shadow-only passive evaluation, final-set-only
    scoring, and the production wiring through the session selector."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-ce-shadow-"))
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "ZMEM_STORE",
                "ZMEM_DATA",
                "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD",
                ENABLE,
                MODEL_ENV,
                "ZMEM_CROSS_ENCODER_MODEL_URL",
                "ZMEM_CROSS_ENCODER_PASSIVE",
                "ZMEM_CROSS_ENCODER_SHADOW",
                "ZMEM_CROSS_ENCODER_BUDGET_MS",
                "ZMEM_EMBED_PROFILE",
            )
        }
        os.environ["ZMEM_STORE"] = str(self.tmp / "store.sqlite")
        os.environ["ZMEM_DATA"] = str(self.tmp / "data")
        os.environ["ZMEM_MODELS_DIR"] = str(self.tmp / "missing-models")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_EMBED_PROFILE"] = "fake"
        for k in (
            ENABLE,
            MODEL_ENV,
            "ZMEM_CROSS_ENCODER_MODEL_URL",
            "ZMEM_CROSS_ENCODER_PASSIVE",
            "ZMEM_CROSS_ENCODER_SHADOW",
            "ZMEM_CROSS_ENCODER_BUDGET_MS",
        ):
            os.environ.pop(k, None)
        sys.path.insert(0, str(SCRIPTS))
        for m in list(sys.modules):
            if m.startswith("storelib") or m in (
                "store",
                "embeddings",
                "cross_encoder_profiles",
            ):
                sys.modules.pop(m, None)
        Path(os.environ["ZMEM_DATA"]).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        from storelib.cross_encoder import set_scorer

        set_scorer(None)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _shadow_rows(self):
        return [
            {"id": SHADOW_IDS[0], "content": "alpha row one"},
            {"id": SHADOW_IDS[1], "content": "bravo row two"},
            {"id": SHADOW_IDS[2], "content": "charlie row three"},
        ]

    def test_shadow_logs_deltas_without_reordering(self):
        from storelib import recall as recall_module
        from storelib.cross_encoder import set_scorer

        rows = self._shadow_rows()
        # id2 scores highest -> shadow order id2, id1, id3 (delta -1/+1/0).
        scores = {SHADOW_IDS[0]: 1.0, SHADOW_IDS[1]: 9.0, SHADOW_IDS[2]: 0.5}

        def fake(query, texts):
            out = []
            for t in texts:
                for rid, s in scores.items():
                    if row_content(rid) == t:
                        out.append(s)
            return out

        def row_content(rid):
            return next(r["content"] for r in rows if r["id"] == rid)

        set_scorer(fake)
        try:
            os.environ[ENABLE] = "1"
            os.environ["ZMEM_CROSS_ENCODER_PASSIVE"] = "1"
            os.environ["ZMEM_CROSS_ENCODER_SHADOW"] = "1"
            out = recall_module.rerank_final_injection_set(
                "issue 125 shadow query", rows
            )
            self.assertEqual(
                [r["id"] for r in out],
                SHADOW_IDS,
                "shadow mode must return the ORIGINAL order",
            )
            for r in out:
                self.assertNotIn("score", r)
            log_path = Path(os.environ["ZMEM_DATA"]) / "cross-encoder-shadow.jsonl"
            self.assertTrue(log_path.is_file())
            self.assertEqual(log_path.read_bytes(), EXPECTED_SHADOW)
            self.assertNotIn(b"logit", log_path.read_bytes())
            self.assertNotIn(b'"score"', log_path.read_bytes())
        finally:
            set_scorer(None)

    def test_passive_path_scores_only_final_rows(self):
        # AC3: the seam scores ONLY the final admitted set. The pipeline
        # (proven by test_production_passive_wiring_scores_final_set to pass
        # only its surfaced rows) hands the seam exactly the 10 admitted
        # rows; the 40-row deep pool is never handed over and must never be
        # evaluated. Shape mirrors the frozen C3 check.
        from storelib import recall as recall_module
        from storelib.cross_encoder import set_scorer

        admitted = [
            {
                "id": "00000000-0000-4000-8000-%010d" % (i + 1),
                "content": "issue 125 admitted candidate row %02d" % i,
            }
            for i in range(10)
        ]
        deep_pool = [
            {
                "id": "00000000-0000-4000-8000-%010d" % (i + 11),
                "content": "issue 125 deep pool row %02d" % i,
            }
            for i in range(40)
        ]
        scored_texts = []
        set_scorer(
            lambda q, texts: (
                scored_texts.extend(texts),
                [float(len(texts) - i) for i in range(len(texts))],
            )[1]
        )
        try:
            os.environ[ENABLE] = "1"
            os.environ["ZMEM_CROSS_ENCODER_PASSIVE"] = "1"
            os.environ["ZMEM_CROSS_ENCODER_SHADOW"] = "1"
            out = recall_module.rerank_final_injection_set(
                "issue 125 final set", admitted
            )
            self.assertEqual(
                collections.Counter(scored_texts),
                collections.Counter(r["content"] for r in admitted),
                "the scorer must see exactly the 10 final rows",
            )
            self.assertEqual(len(scored_texts), 10)
            pool_hits = [
                t for t in scored_texts if t in {r["content"] for r in deep_pool}
            ]
            self.assertEqual(pool_hits, [], "deep-pool rows must never be evaluated")
            self.assertEqual([r["id"] for r in out], [r["id"] for r in admitted])
            for r in out:
                self.assertNotIn("score", r)
        finally:
            set_scorer(None)

    def _seed(self):
        from storelib.schema import connect as _connect, _prepare_store
        from storelib.write import add_memory

        conn = _connect()
        _prepare_store(conn)
        admit = [
            "gamma receptor alignment note one",
            "gamma receptor alignment note two",
        ]
        lowconf = [
            "gamma receptor untrusted whisper one",
            "gamma receptor untrusted whisper two",
        ]
        for content in admit:
            add_memory(
                conn,
                namespace="user:issue-125",
                type_="fact",
                content=content,
                confidence=0.9,
            )
        for content in lowconf:
            add_memory(
                conn,
                namespace="user:issue-125",
                type_="fact",
                content=content,
                confidence=0.05,
            )
        conn.close()
        return admit, lowconf

    def _drive_ce_cli(self, argv_tail):
        out, err = io.StringIO(), io.StringIO()
        old_argv = list(sys.argv)
        sys.argv = ["store.py", *argv_tail]
        code = 0
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                from storelib.cli import main as cli_main

                try:
                    cli_main()
                except SystemExit as e:
                    code = e.code or 0
        finally:
            sys.argv = old_argv
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def _walk_contents(value):
        found = []
        if isinstance(value, dict):
            for k, v in value.items():
                if k == "content" and isinstance(v, str):
                    found.append(v)
                else:
                    found.extend(CrossEncoderShadowAndFinalSetTests._walk_contents(v))
        elif isinstance(value, list):
            for item in value:
                found.extend(CrossEncoderShadowAndFinalSetTests._walk_contents(item))
        return found

    def test_production_passive_wiring_scores_final_set(self):
        from storelib.cross_encoder import set_scorer

        admit, lowconf = self._seed()
        scored_texts = []
        set_scorer(
            lambda q, texts: (
                scored_texts.extend(texts),
                [float(len(texts) - i) for i in range(len(texts))],
            )[1]
        )
        try:
            os.environ[ENABLE] = "1"
            os.environ["ZMEM_CROSS_ENCODER_PASSIVE"] = "1"
            os.environ["ZMEM_CROSS_ENCODER_SHADOW"] = "1"
            code, out_text, err_text = self._drive_ce_cli(
                [
                    "recall",
                    "--query",
                    "gamma receptor",
                    "--namespace",
                    "user:issue-125",
                    "--limit",
                    "5",
                    "--include-global",
                    "--global-limit",
                    "3",
                    "--no-bump",
                    "--for-injection",
                    "--json",
                    "--session-id",
                    "sess-125-wiring",
                    "--moment",
                    "user_prompt",
                ]
            )
            self.assertEqual(code, 0, err_text)
            payload = json.loads(out_text)
            contents = self._walk_contents(payload)
            for row in admit:
                self.assertIn(
                    row, scored_texts, "production chain never scored an admitted row"
                )
                self.assertIn(row, contents)
            for row in lowconf:
                self.assertNotIn(
                    row, scored_texts, "gate-dropped row reached the scorer"
                )
            self.assertTrue(
                all(t in set(admit) | set(lowconf) for t in scored_texts),
                f"unexpected scored content: {scored_texts}",
            )
            self.assertEqual(
                err_text.count("reason=applied"),
                1,
                "exactly one applied terminal reason expected",
            )
        finally:
            set_scorer(None)

    def test_passive_lane_requires_double_opt_in(self):
        from storelib.cross_encoder import set_scorer

        admit, _lowconf = self._seed()
        scored_texts = []
        set_scorer(
            lambda q, texts: (scored_texts.extend(texts), [1.0 for _ in texts])[1]
        )
        try:
            os.environ[ENABLE] = "1"
            os.environ.pop("ZMEM_CROSS_ENCODER_PASSIVE", None)
            os.environ["ZMEM_CROSS_ENCODER_SHADOW"] = "1"
            code, out_text, err_text = self._drive_ce_cli(
                [
                    "recall",
                    "--query",
                    "gamma receptor",
                    "--namespace",
                    "user:issue-125",
                    "--limit",
                    "5",
                    "--include-global",
                    "--global-limit",
                    "3",
                    "--no-bump",
                    "--for-injection",
                    "--json",
                    "--session-id",
                    "sess-125-neg",
                    "--moment",
                    "user_prompt",
                ]
            )
            self.assertEqual(code, 0)
            json.loads(out_text)
            self.assertEqual(
                scored_texts, [], "PASSIVE unset must keep the scorer untouched"
            )
        finally:
            set_scorer(None)

    def test_passive_terminal_reasons(self):
        from storelib import recall as recall_module
        from storelib.cross_encoder import set_scorer

        rows = self._shadow_rows()
        os.environ[ENABLE] = "1"
        os.environ["ZMEM_CROSS_ENCODER_PASSIVE"] = "1"
        os.environ["ZMEM_CROSS_ENCODER_SHADOW"] = "1"
        # Applied: the reason surfaces on stderr exactly once.
        set_scorer(lambda q, texts: [1.0 for _ in texts])
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                recall_module.rerank_final_injection_set("query", rows)
            self.assertEqual(
                [
                    line
                    for line in err.getvalue().splitlines()
                    if "cross-encoder reason=" in line
                ].count("[zmem] cross-encoder reason=applied"),
                1,
            )
        finally:
            set_scorer(None)
        # Failed attempt: verbatim failure reason, original order.
        set_scorer(lambda q, texts: None)
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                out = recall_module.rerank_final_injection_set("query", rows)
            self.assertIn("reason=invalid-score", err.getvalue())
            self.assertEqual([r["id"] for r in out], SHADOW_IDS)
        finally:
            set_scorer(None)
        # Trivial early exit: no attempt, no reason line.
        set_scorer(lambda q, texts: [1.0 for _ in texts])
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                recall_module.rerank_final_injection_set("query", rows[:1])
            self.assertNotIn("cross-encoder reason=", err.getvalue())
        finally:
            set_scorer(None)

    def test_shadow_write_failure_emits_load_error(self):
        from storelib import recall as recall_module
        from storelib.cross_encoder import set_scorer

        rows = self._shadow_rows()
        os.environ[ENABLE] = "1"
        os.environ["ZMEM_CROSS_ENCODER_PASSIVE"] = "1"
        os.environ["ZMEM_CROSS_ENCODER_SHADOW"] = "1"
        # A directory where the log file would be written fails the append.
        (Path(os.environ["ZMEM_DATA"]) / "cross-encoder-shadow.jsonl").mkdir(
            parents=True
        )
        set_scorer(lambda q, texts: [1.0 for _ in texts])
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                out = recall_module.rerank_final_injection_set("query", rows)
            self.assertEqual(
                err.getvalue().count("reason=load-error"),
                1,
                "exactly one load-error terminal reason expected",
            )
            self.assertEqual([r["id"] for r in out], SHADOW_IDS)
        finally:
            set_scorer(None)

    def test_queryless_recent_selector_passes_rows(self):
        from storelib.cross_encoder import set_scorer

        admit, _lowconf = self._seed()
        scored_texts = []
        set_scorer(
            lambda q, texts: (scored_texts.extend(texts), [1.0 for _ in texts])[1]
        )
        try:
            os.environ[ENABLE] = "1"
            os.environ["ZMEM_CROSS_ENCODER_PASSIVE"] = "1"
            os.environ["ZMEM_CROSS_ENCODER_SHADOW"] = "1"
            code, out_text, err_text = self._drive_ce_cli(
                [
                    "recent",
                    "--namespace",
                    "user:issue-125",
                    "--limit",
                    "3",
                    "--include-global",
                    "--global-limit",
                    "2",
                    "--no-bump",
                    "--for-injection",
                    "--json",
                    "--session-id",
                    "sess-125-recent",
                    "--moment",
                    "session_start",
                ]
            )
            self.assertEqual(code, 0, err_text)
            payload = json.loads(out_text)
            contents = self._walk_contents(payload)
            for row in admit:
                self.assertIn(
                    row,
                    contents,
                    "query-less recent passive pull lost the "
                    "seeded rows (silent-envelope regression?)",
                )
        finally:
            set_scorer(None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
