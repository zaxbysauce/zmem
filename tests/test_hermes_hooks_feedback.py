"""Post-approval feedback-round tests for issue #163 (swarm-pr-review
PRR findings).  Lives in a NEW file by convention: the frozen acceptance
checks replay tests/test_hermes_hooks.py, so review-driven additions land
here.  Covers the PRR-001/002 real-wire envelope regression (int
``budget_admission`` + store-optional keys), PRR-011 interval arming,
PRR-012 source-ref-exists truth paths, PRR-013 nudge literal pin,
PRR-015/PRR-M04 ops-append contract branches, PRR-022 dash-token guard,
and the TC-03 first-arm-wins rule."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# NOTE: no module-level os.environ pinning here.  This module may share a
# process with tests/test_hermes_hooks.py (which pins its own scratch at
# import); re-pinning here would leak into that module's subprocess
# children.  Isolation is per-subprocess: every real-CLI call below builds
# an explicit env from its own scratch dir.
_FB_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-163-fb-"))


def _fb_env(**extra):
    env = dict(os.environ)
    env["ZMEM_STORE"] = str(_FB_SCRATCH / "store.sqlite")
    env["ZMEM_DATA"] = str(_FB_SCRATCH)
    env["ZMEM_MODELS_DIR"] = str(_FB_SCRATCH / "missing-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.update(extra)
    return env

_HOOKS_ENV_KEYS = ("ZMEM_QUERY_CONTEXT", "ZMEM_INJECT",
                   "ZMEM_CONVENTION_INTERVAL")

_STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
_HOOKS_DIR = REPO_ROOT / "hermes-plugin" / "hooks"


def _load_transport():
    spec = importlib.util.spec_from_file_location(
        "zmem_transport_163fb", REPO_ROOT / "hermes-plugin" / "transport.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_transport_163fb"] = module
    spec.loader.exec_module(module)
    return module


def _load_fake_executor():
    spec = importlib.util.spec_from_file_location(
        "zmem_fake_executor_163fb",
        REPO_ROOT / "tests" / "support" / "fake_executor.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_fake_executor_163fb"] = module
    spec.loader.exec_module(module)
    return module


def _load_provider(name="zmem_hermes_163fb"):
    agent = types.ModuleType("agent")
    mp = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    mp.MemoryProvider = MemoryProvider
    agent.memory_provider = mp
    sys.modules.setdefault("agent", agent)
    sys.modules.setdefault("agent.memory_provider", mp)
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sync_deadline(transport):
    deadline = transport.DeadlineExecutor()

    def _run(fn, deadline_s):
        return fn()

    deadline.run = _run
    return deadline


def _make_provider(mod, transport, *, deadline_mode="sync"):
    fake_executor = _load_fake_executor()
    scheduler = fake_executor.FakeExecutor()
    original_submit = scheduler.submit

    def _submit(fn, delay_s=None):
        return original_submit(fn, 0.0 if delay_s is None else delay_s)

    scheduler.submit = _submit
    deadline = _sync_deadline(transport)
    if deadline_mode == "timeout":
        deadline.run = lambda fn, deadline_s: None
    provider = mod.ZmemMemoryProvider(scheduler=scheduler, deadline=deadline)
    return provider, deadline


def _store(*args):
    return subprocess.run(
        [sys.executable, str(_STORE_PY), *args],
        capture_output=True, text=True, encoding="utf-8",
        env=_fb_env())


def _seed_row(namespace, row_type, content, source_ref):
    r = _store("add", "--namespace", namespace, "--type", row_type,
               "--content", content, "--signal", "test",
               "--source-ref", source_ref)
    assert r.returncode == 0, r.stderr


def _ring_stem(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


class RealWireEnvelopeTest(unittest.TestCase):
    """PRR-001/PRR-002 regression pin: the provider validator must accept
    the envelope the REAL store emits — int ``budget_admission`` and the
    store-optional keys (effective_ops et al).  The round-1 tests passed
    only because a fake store fabricated a dict wire shape."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _HOOKS_ENV_KEYS}
        for key in _HOOKS_ENV_KEYS:
            os.environ.pop(key, None)
        self._transport = _load_transport()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _provider(self, mod, deadline_mode="sync"):
        provider, deadline = _make_provider(mod, self._transport,
                                            deadline_mode=deadline_mode)
        return provider, deadline

    def test_validator_accepts_int_budget_admission(self):
        mod = _load_provider("zmem163fb_int_admission")
        provider, _deadline = self._provider(mod)
        envelope = {
            "results": [], "count": 0, "omitted": 0, "reason": "injected",
            "excluded": [], "candidate_ids": [], "tokens_used": 0,
            "tokens_budget": 1500, "budget_dropped": 0,
            "budget_admission": 937, "budget_truncated": 0,
            "budget_dropped_protected": 0, "arms": {},
            "rendered": "fence text",
        }
        self.assertTrue(provider._provider_envelope_is_valid(envelope))

    def test_validator_accepts_store_optional_keys(self):
        mod = _load_provider("zmem163fb_optional_keys")
        provider, _deadline = self._provider(mod)
        envelope = {
            "results": [], "count": 1, "omitted": 0, "reason": "injected",
            "excluded": [], "candidate_ids": [], "tokens_used": 10,
            "tokens_budget": 1500, "budget_dropped": 0,
            "budget_admission": 42, "budget_truncated": 0,
            "budget_dropped_protected": 0, "arms": {},
            "rendered": "fence text",
            "effective_ops": ["git", "status"],
            "secret_withheld": 1, "global_withheld": 2,
            "budget_note": "trimmed", "injection_risk": 0.1,
            "candidate_lanes": ["hermes-provider"],
        }
        self.assertTrue(provider._provider_envelope_is_valid(envelope))

    def test_validator_rejects_negative_and_bool_numbers(self):
        mod = _load_provider("zmem163fb_bad_numbers")
        provider, _deadline = self._provider(mod)
        base = {
            "results": [], "count": 0, "omitted": 0, "reason": "injected",
            "excluded": [], "candidate_ids": [], "tokens_used": 0,
            "tokens_budget": 1500, "budget_dropped": 0,
            "budget_admission": 0, "budget_truncated": 0,
            "budget_dropped_protected": 0, "arms": {}, "rendered": "f",
        }
        for field in ("count", "tokens_used", "budget_admission"):
            bad = dict(base)
            bad[field] = -1
            self.assertFalse(provider._provider_envelope_is_valid(bad),
                             field)
            bad = dict(base)
            bad[field] = True
            self.assertFalse(provider._provider_envelope_is_valid(bad),
                             field)

    def test_pre_llm_delivers_real_wire_envelope(self):
        """End-to-end through LocalSubprocess: the fake store emits the REAL
        wire shape (int admission + effective_ops) and pre_llm_call must
        deliver context — the exact probe that was {} before the fix."""
        mod = _load_provider("zmem163fb_realwire")
        provider, deadline = _make_provider(mod, self._transport)
        provider.post_tool_call(session_id="session-163fb",
                                tool_name="bash",
                                args={"command": "git status"},
                                status="ok")
        provider._scheduler.advance(0.0)
        envelope = {
            "results": [], "count": 0, "omitted": 0, "reason": "injected",
            "excluded": [], "candidate_ids": [], "tokens_used": 0,
            "tokens_budget": 1500, "budget_dropped": 0,
            "budget_admission": 937, "budget_truncated": 0,
            "budget_dropped_protected": 0, "arms": {},
            "rendered": "fence text",
            "effective_ops": ["git", "status"],
        }
        tmp = Path(tempfile.mkdtemp(prefix="zmem163fb-store-"))
        script = ("import json, sys\nprint(json.dumps("
                  + repr(envelope) + "))\n")
        (tmp / "store.py").write_text(script, encoding="utf-8")
        provider._transport = self._transport.LocalSubprocess(
            store_py=str(tmp / "store.py"), executor=deadline, deadline_s=6.0)
        result = provider.pre_llm_call(session_id="session-163fb",
                                       user_message="hello")
        self.assertEqual(result, {"context": "fence text"})


class IntervalArmingTest(unittest.TestCase):
    """PRR-011: the convention nudge arms on tool_count % interval, and
    PRR-003n/TC-03: first-arm-wins."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _HOOKS_ENV_KEYS}
        for key in _HOOKS_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["ZMEM_NAMESPACE"] = "user:global"
        self._transport = _load_transport()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_convention_nudge_arms_at_interval(self):
        mod = _load_provider("zmem163fb_interval")
        provider, _deadline = _make_provider(mod, self._transport)
        os.environ["ZMEM_CONVENTION_INTERVAL"] = "2"
        for i in range(2):
            self.assertEqual(provider.post_tool_call(
                session_id="sess-int", tool_name="bash",
                args={"command": "git status"}, status="ok"), {})
        with provider._callback_state_lock:
            self.assertEqual(
                provider._callback_state["sess-int"]["pending_nudge"],
                mod._provider_convention_nudge("sess-int"))

    def test_failure_nudge_arms_on_error_status(self):
        mod = _load_provider("zmem163fb_failure")
        provider, _deadline = _make_provider(mod, self._transport)
        self.assertEqual(provider.post_tool_call(
            session_id="sess-fail", tool_name="bash",
            args={"command": "git push"}, status="error"), {})
        with provider._callback_state_lock:
            self.assertEqual(
                provider._callback_state["sess-fail"]["pending_nudge"],
                mod._provider_failure_nudge("sess-fail"))

    def test_first_arm_wins(self):
        mod = _load_provider("zmem163fb_firstarm")
        provider, _deadline = _make_provider(mod, self._transport)
        provider._set_pending_nudge_for_test("sess-arm",
                                             "existing nudge text")
        self.assertEqual(provider.post_tool_call(
            session_id="sess-arm", tool_name="bash",
            args={"command": "git push"}, status="error"), {})
        with provider._callback_state_lock:
            self.assertEqual(
                provider._callback_state["sess-arm"]["pending_nudge"],
                "existing nudge text")


class VerifyNudgeLiteralTest(unittest.TestCase):
    """PRR-013: the provider verify nudge is a BYTE-FOR-BYTE copy of the
    compatibility hook's canonical text — pinned against a literal, not
    against the same function the implementation calls."""

    LITERAL = (
        "ZMem reflect-before-stop: you're about to finish a coding turn. "
        "Before you do, consider whether you discovered anything worth "
        "capturing for future sessions \u2014 a gotcha, a convention, a "
        "corrected assumption. If so, capture it now by calling the "
        "zmem_add tool:\n"
        "  zmem_add with type=\"lesson\", content=\"<the lesson>\", "
        "signal=\"<test|reviewer|user|none>\", source_ref=\"session:SID\"\n"
        "If nothing generalizable, finish the turn."
    )

    def test_provider_verify_nudge_matches_compat_literal(self):
        mod = _load_provider("zmem163fb_literal")
        self.assertEqual(mod._provider_verify_nudge("SID"),
                         self.LITERAL.replace("SID", "SID"))

    def test_compat_hook_source_still_holds_the_literal(self):
        """Read the compatibility hook source and assert the pinned
        sentence fragments are still present — catches silent divergence
        in either direction."""
        source = (_HOOKS_DIR / "zmem-hermes-verify.py").read_text(
            encoding="utf-8")
        for fragment in (
                "ZMem reflect-before-stop: you're about to finish a coding "
                "turn. ",
                "capturing for future sessions",
                "If nothing generalizable, finish the turn."):
            self.assertIn(fragment, source)


class SourceRefExistsTest(unittest.TestCase):
    """PRR-012: the lesson-only / superseded filters of source-ref-exists
    are pinned with REAL store rows through the real CLI."""

    @classmethod
    def setUpClass(cls):
        cls.ns = "project:fb163"
        r = _store("init")
        assert r.returncode == 0, r.stderr
        _seed_row(cls.ns, "lesson", "fb live lesson",
                  "session:fb-live")
        _seed_row(cls.ns, "lesson", "fb superseded lesson",
                  "session:fb-dead")
        listing = _store("list", "--namespace", cls.ns, "--limit", "100")
        assert listing.returncode == 0, listing.stderr
        match = next(re.search(r"\[([0-9a-f-]{36})\]", line)
                     for line in listing.stdout.splitlines()
                     if "fb superseded lesson" in line)
        dead_id = match.group(1)
        sup = _store("supersede", "--id", dead_id,
                     "--reason", "fb test supersede")
        assert sup.returncode == 0, sup.stderr
        _seed_row(cls.ns, "convention", "fb convention row",
                  "session:fb-conv")

    def _probe(self, source_ref):
        r = _store("source-ref-exists", "--source-ref", source_ref)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_live_lesson_exists(self):
        self.assertEqual(self._probe("session:fb-live"),
                         {"exists": True})

    def test_convention_row_does_not_count(self):
        self.assertEqual(self._probe("session:fb-conv"),
                         {"exists": False})

    def test_superseded_lesson_does_not_count(self):
        rows = json.loads(_store(
            "list", "--namespace", self.ns, "--json").stdout or "{}")
        self.assertIsInstance(rows, (dict, list))  # list surface usable
        self.assertEqual(self._probe("session:fb-dead"),
                         {"exists": False})

    def test_unknown_ref_answers_false(self):
        self.assertEqual(self._probe("session:fb-never"),
                         {"exists": False})


class OpsAppendContractTest(unittest.TestCase):
    """PRR-015/M-04: the reshaped ops-append contract branches — the
    declared no-token no-op, the {"ok":false} failure stdout, the
    --session/--op aliases, and the required --namespace."""

    def test_no_token_operation_is_successful_noop(self):
        scratch = tempfile.mkdtemp(prefix="zmem163fb-noop-")
        env = _fb_env()
        r = subprocess.run(
            [sys.executable, str(_STORE_PY), "ops-append",
             "--namespace", "project:fb", "--session-id", "noop-sess",
             "--tool", "bash", "--operation", "x", "--json"],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '{"ok":true}\n')
        ring = Path(scratch, "ops", _ring_stem("noop-sess") + ".log")
        self.assertFalse(ring.exists(), "writer must not run for no tokens")

    def test_old_session_op_aliases_still_parse(self):
        scratch = tempfile.mkdtemp(prefix="zmem163fb-alias-")
        env = _fb_env()
        r = subprocess.run(
            [sys.executable, str(_STORE_PY), "ops-append",
             "--namespace", "project:fb", "--session", "alias-sess",
             "--tool", "Bash", "--op", "git status", "--json"],
            capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, '{"ok":true}\n')

    def test_missing_namespace_is_argparse_exit_2(self):
        r = subprocess.run(
            [sys.executable, str(_STORE_PY), "ops-append",
             "--session-id", "s", "--tool", "bash",
             "--operation", "git status", "--json"],
            capture_output=True, text=True, env=_fb_env())
        self.assertEqual(r.returncode, 2)
        self.assertEqual(r.stdout, "")

    def test_dash_prefixed_operation_token_is_dropped(self):
        """PRR-022: a non-runner event whose basename starts with '-' must
        not emit a dash-prefixed token (argv option injection downstream)."""
        scripts = str(REPO_ROOT / "skills" / "memory" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import storelib.ops_tokens as ops_tokens
        self.assertEqual(ops_tokens.derive_ops_tokens("edit notes -x.txt"),
                         [])
        provider_mod = _load_provider("zmem163fb_dash")
        self.assertEqual(
            provider_mod._derive_provider_ops_tokens("edit notes -x.txt"),
            [])


class TornTailRepairTest(unittest.TestCase):
    """PRR-007: a torn trailing line (crash mid-write) must be repaired on
    disk BEFORE the next append — the append must never fuse onto the torn
    bytes (review round: the first fix ran the repair after the append,
    where the flag was unreachable and the fusion had already happened)."""

    def test_torn_tail_repaired_before_append(self):
        scripts = str(REPO_ROOT / "skills" / "memory" / "scripts")
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        import storelib.ops_tokens as ops_tokens
        tmp = tempfile.mkdtemp(prefix="zmem163fb-torn-")
        try:
            ring = Path(ops_tokens._ring_path(tmp, "torn-repair"))
            ring.parent.mkdir(parents=True)
            good = json.dumps({"ts": 1, "tool": "bash",
                               "ops": "git status"})
            with open(ring, "ab") as f:
                f.write((good + "\n").encode("utf-8"))
                f.write(b'{"ts": 2, "tool": "ba')  # torn, no newline
            self.assertTrue(ops_tokens.append_ops_ring(
                tmp, "torn-repair", "Bash", "git push"))
            raw = ring.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            lines = raw.decode("utf-8").splitlines()
            self.assertEqual(len(lines), 2)
            parsed = [json.loads(line) for line in lines]  # all parse
            self.assertEqual(parsed[-1]["ops"], "git push")
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class NudgePreservationTest(unittest.TestCase):
    """TC-18 / PRR-005 conformance pin: an invalid or empty-render
    delivery leaves the armed pending nudge intact (preserve-for-next-call
    is the issue contract; starvation is accepted per that contract)."""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _HOOKS_ENV_KEYS}
        for key in _HOOKS_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["ZMEM_NAMESPACE"] = "user:global"
        self._transport = _load_transport()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_empty_render_preserves_pending_nudge(self):
        mod = _load_provider("zmem163fb_preserve")
        provider, deadline = _make_provider(mod, self._transport)
        provider.post_tool_call(session_id="sess-pres", tool_name="bash",
                                args={"command": "git status"}, status="ok")
        provider._scheduler.advance(0.0)
        provider._set_pending_nudge_for_test("sess-pres", "nudge text")
        envelope = {"results": [], "count": 0, "omitted": 0,
                    "reason": "empty-pool", "excluded": [],
                    "candidate_ids": [], "tokens_used": 0,
                    "tokens_budget": 1500, "budget_dropped": 0,
                    "budget_admission": 0, "budget_truncated": 0,
                    "budget_dropped_protected": 0, "arms": {},
                    "rendered": ""}
        tmp = Path(tempfile.mkdtemp(prefix="zmem163fb-preserve-"))
        (tmp / "store.py").write_text(
            "import json, sys\nprint(json.dumps(" + repr(envelope) + "))\n",
            encoding="utf-8")
        provider._transport = self._transport.LocalSubprocess(
            store_py=str(tmp / "store.py"), executor=deadline, deadline_s=6.0)
        self.assertEqual(provider.pre_llm_call(session_id="sess-pres",
                                               user_message="hello"), {})
        with provider._callback_state_lock:
            self.assertEqual(
                provider._callback_state["sess-pres"]["pending_nudge"],
                "nudge text")


if __name__ == "__main__":
    unittest.main(verbosity=2)
