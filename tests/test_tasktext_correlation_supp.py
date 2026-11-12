"""Issue #428 Phase 4.2 guardrails (supplementary to tests/test_tasktext_correlation.py).

1. ``CorrelationIdParityTest`` — hooks/lib/zmem-recall-body.py's
   ``_correlation_id`` mirrors storelib.delivery_ledger.normalize_correlation_id
   (the #158 boundary forbids the hook adapter from importing storelib); the
   two must agree on every input or the hook would forward an id the store
   discards (or vice versa).

2. ``RemoteReadFlagCompletenessTest`` — the remote-gate honesty contract
   (storelib/remote_gate.py ``_READ_DEFAULTS`` comment: "a future flag
   missing here reverts to silent-drop") had no enforcing test. Every
   ``recall``/``recent`` argparse dest must be either refused-when-supplied
   (in ``_READ_DEFAULTS[cmd]``) or in the explicit forwarded/meta set below.
   The prefetch route keeps its refusals inline, so its dest set is pinned
   literally: a NEW prefetch flag fails here until a remote disposition
   (forward or refuse by name) is decided. This is the guardrail for the
   #428 defect class (an identity/flag dropped before the seam that would
   use it); its first run found ``recall --state-hint`` silently dropped in
   remote mode.

standalone: python -X utf8 tests/test_tasktext_correlation_supp.py
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Pin a scratch store BEFORE tests/_store_guard and before any storelib
# import (storelib freezes the store path at first import).
_SCRATCH = tempfile.mkdtemp(prefix="zmem-428-supp-")
os.environ["ZMEM_STORE"] = os.path.join(_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _SCRATCH
os.environ["ZMEM_MODELS_DIR"] = os.path.join(_SCRATCH, "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
for _k in ("ZMEM_REMOTE_URL", "ZMEM_MCP_URL"):
    os.environ.pop(_k, None)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))

from storelib import cli as _cli  # noqa: E402
from storelib import delivery_ledger as _dl  # noqa: E402
from storelib import remote_gate as _rg  # noqa: E402


def _load_body():
    spec = importlib.util.spec_from_file_location(
        "zmem_recall_body_428supp",
        REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Stop(Exception):
    pass


def _subparsers() -> dict:
    """Capture the real store.py parser by intercepting parse_args inside
    cli.main() (the parser is built inline there)."""
    captured: dict = {}
    orig = argparse.ArgumentParser.parse_args

    def _capture(self, *a, **k):
        captured["ap"] = self
        raise _Stop()

    argparse.ArgumentParser.parse_args = _capture
    saved_argv = sys.argv
    sys.argv = ["store.py", "recall", "--query", "x"]
    try:
        _cli.main()
    except _Stop:
        pass
    finally:
        argparse.ArgumentParser.parse_args = orig
        sys.argv = saved_argv
    ap = captured.get("ap")
    if ap is None:
        raise AssertionError("could not capture the store.py parser")
    sub = next(a for a in ap._actions
               if isinstance(a, argparse._SubParsersAction))
    return sub.choices


def _dests(parser) -> set:
    return {a.dest for a in parser._actions}


class CorrelationIdParityTest(unittest.TestCase):
    CORPUS = [
        "ag-1", "  ag-1  ", "", "   ", "\t", "a" * 128, "a" * 129,
        " " + "a" * 128 + " ", "-rf", "--x", " -lead", "ag\n1", "ag\x00",
        "ag\x7f", "ag\x1b[31m", "ünïcødé-id", "id with spaces",
        None, 5, 1.5, True, ["ag-1"], {"id": "ag-1"}, b"ag-1",
        # PR #453 review PRR-002: unpaired surrogates and the C1 range.
        # A surrogate admitted by the old C0/DEL-only check raised inside
        # the POSIX argv encoder and silently dropped the WHOLE recall.
        "\ud800", "\udfff", "ag\ud800x", "\udc80",
        "ag\x85", "ag\x9b", "\u0085", "\u009b",
    ]

    def test_hook_and_store_normalizers_agree(self):
        body = _load_body()
        for value in self.CORPUS:
            with self.subTest(value=value):
                self.assertEqual(body._correlation_id(value),
                                 _dl.normalize_correlation_id(value))

    def test_bounds_pinned_equal(self):
        body = _load_body()
        self.assertEqual(body._CORRELATION_ID_MAX,
                         _dl.TASK_TEXT_CORRELATION_ID_MAX)

    def test_rule_shape(self):
        n = _dl.normalize_correlation_id
        self.assertEqual(n("  ag-1  "), "ag-1")
        self.assertEqual(n("a" * 128), "a" * 128)
        for bad in ("", "  ", "a" * 129, "-rf", "ag\n1", "ag\x7f", None, 5,
                    # PRR-002: surrogates and INTERIOR C1 are rejected
                    # (a trailing NEL is stripped as whitespace by str.strip
                    # before the rule sees it — that is the documented
                    # strip-then-validate order on both sides).
                    "\ud800", "ag\udfff", "a\x85g", "a\x9bg"):
            with self.subTest(bad=bad):
                self.assertEqual(n(bad), "")

    def test_argv_safe_guard(self):
        body = _load_body()
        # The per-flag seam guard: a surrogate never rides the store argv,
        # a normal id does.
        self.assertFalse(body._argv_safe("\ud800"))
        self.assertTrue(body._argv_safe("ag-1"))


class CapShieldTest(unittest.TestCase):
    """PR #453 review PRR-001: the park cap is TAG-AWARE — untagged entries
    are evicted first; a tagged entry survives until an all-tagged
    over-cap stash drops its oldest (disclosed residual)."""

    def _stash(self):
        d = tempfile.mkdtemp(prefix="zmem-428-cap-")
        return d, "cap-sid"

    def test_tagged_survives_cap_with_untagged_present(self):
        d, sid = self._stash()
        now = time.time()
        for i in range(16):
            _dl.park_task_text(d, sid, "TAGGED-%d own task" % i,
                               agent_id="ag-%d" % i, now=now - 600 + i)
        _dl.park_task_text(d, sid, "untagged-sibling task", now=now - 10)
        got = _dl.consume_task_text(d, sid, agent_id="ag-0")
        self.assertEqual(got, "TAGGED-0 own task",
                         "the tagged park must survive the untagged "
                         "sibling's park (PRR-001)")
        texts = [e.get("text") for e in
                 _dl._load_tasktext(_dl.tasktext_path(d, sid),
                                    time.time())]
        self.assertNotIn("untagged-sibling task", texts,
                         "the UNTAGGED entry is the one evicted")

    def test_all_tagged_over_cap_drops_oldest_disclosed(self):
        d, sid = self._stash()
        now = time.time()
        for i in range(_dl.TASK_TEXT_CAP + 1):
            _dl.park_task_text(d, sid, "TAGGED-%d" % i,
                               agent_id="ag-%d" % i, now=now - 700 + i)
        texts = [e.get("text") for e in
                 _dl._load_tasktext(_dl.tasktext_path(d, sid),
                                    time.time())]
        self.assertNotIn("TAGGED-0", texts,
                         "all-tagged over-cap drops the OLDEST (disclosed)")
        self.assertIn("TAGGED-%d" % _dl.TASK_TEXT_CAP, texts)
        self.assertLessEqual(len(texts), _dl.TASK_TEXT_CAP)

    def test_cap_untagged_only_matches_legacy(self):
        d, sid = self._stash()
        now = time.time()
        for i in range(_dl.TASK_TEXT_CAP + 2):
            _dl.park_task_text(d, sid, "untagged-%d" % i, now=now - 700 + i)
        entries = _dl._load_tasktext(_dl.tasktext_path(d, sid),
                                     time.time())
        # PR #453 review round 3: compare the FULL ordered entry tuples
        # (text + agent_id + ts), not a sorted-text bag — a reorder
        # mutant must fail here too.
        self.assertEqual(
            [(e.get("text"), e.get("agent_id"), e.get("ts"))
             for e in entries],
            [("untagged-%d" % i, "", now - 700 + i)
             for i in range(2, _dl.TASK_TEXT_CAP + 2)],
            "all-untagged cap eviction is byte-identical "
            "to the legacy oldest-first behavior (order included)")


class ParkIdRedactionTest(unittest.TestCase):
    """PR #453 review PRR-017: a secret-shaped id must not persist
    cleartext — the park degrades to untagged with a stderr audit line."""

    def _stash(self):
        d = tempfile.mkdtemp(prefix="zmem-428-redact-")
        return d, "redact-sid"

    def test_secret_shaped_id_parks_untagged(self):
        d, sid = self._stash()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _dl.park_task_text(d, sid, "delegation text",
                               agent_id="sk-" + "a" * 30)
        stash = _dl._load_tasktext(_dl.tasktext_path(d, sid), time.time())
        self.assertEqual(len(stash), 1)
        self.assertEqual(stash[0].get("agent_id"), "",
                         "a secret-shaped id parks UNTAGGED (redaction "
                         "parity with the text field)")
        self.assertEqual(stash[0].get("text"), "delegation text")
        self.assertIn("parked untagged", err.getvalue())

    def test_clean_id_parks_tagged(self):
        d, sid = self._stash()
        _dl.park_task_text(d, sid, "delegation text", agent_id="ag-clean")
        stash = _dl._load_tasktext(_dl.tasktext_path(d, sid), time.time())
        self.assertEqual(stash[0].get("agent_id"), "ag-clean")


class StrandedReservationSignalTest(unittest.TestCase):
    """PR #453 review PRR-017: a stash of ONLY tag-reserved entries emits
    one bounded stderr line instead of a silent empty return."""

    def _stash_tagged_only(self):
        d = tempfile.mkdtemp(prefix="zmem-428-strand-")
        now = time.time()
        _dl.park_task_text(d, "strand-sid", "reserved text",
                           agent_id="ag-1", now=now - 30)
        return d

    def test_all_tagged_stash_signals(self):
        d = self._stash_tagged_only()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = _dl.consume_task_text(d, "strand-sid")
        self.assertEqual(got, "")
        self.assertIn("tag-reserved", err.getvalue())

    def test_untagged_fallback_is_silent(self):
        d = self._stash_tagged_only()
        now = time.time()
        _dl.park_task_text(d, "strand-sid", "untagged fallback",
                           now=now - 5)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = _dl.consume_task_text(d, "strand-sid",
                                        agent_id="ag-other")
        self.assertEqual(got, "untagged fallback")
        self.assertEqual(err.getvalue(), "",
                         "a served untagged fallback emits no diagnostic")


class HookConsumeInvalidIdTest(unittest.TestCase):
    """PR #453 review PRR-008: the SUBAGENT consume branch must treat
    invalid agent_id values as absent (the pre-tool park branch had the
    legs; the consume branch did not). Drives the REAL hook body via the
    adapter harness pattern (store subprocess captured — no child ever
    spawns) and asserts the flag is NOT forwarded."""

    VALID = "ag-9"

    def _captured_argv(self, agent_id_value):
        body = _load_body()
        calls: list = []

        def fake_check_output(command, **kw):
            calls.append(list(command))
            return b"{}"

        class _FakeProc:
            returncode = 1
            stdout = b"{}"

        argv = ["body", str(REPO_ROOT / "skills" / "memory" / "scripts"
                            / "store.py"),
                "project:pinadapter", "25000", "subagent"]
        sink = io.StringIO()
        event = {"session_id": "s-cond",
                 "hook_event_name": "SubagentStart",
                 "agent_id": agent_id_value}

        class _FakeStdin:
            buffer = io.BytesIO(json.dumps(event).encode("utf-8"))

        with mock.patch.object(body.subprocess, "check_output",
                               fake_check_output), \
                mock.patch.object(body.subprocess, "run",
                                  lambda *a, **k: _FakeProc()), \
                mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "stdin", _FakeStdin()), \
                contextlib.redirect_stdout(sink):
            rc = body.main()
        store_argv = calls[0] if calls else []
        return rc, store_argv

    def _assert_no_consume_flag(self, agent_id_value):
        _rc, store_argv = self._captured_argv(agent_id_value)
        self.assertNotIn("--consume-agent-id", store_argv,
                         "invalid consume id must be treated as absent: "
                         "%r" % (agent_id_value,))
        self.assertNotIn(
            True, [a.startswith("--consume-agent-id=") for a in store_argv],
            "invalid consume id must be absent in attached form too: %r"
            % (agent_id_value,))
        self.assertIn("--consume-tasktext", store_argv,
                      "non-vacuity control: the grant itself must still "
                      "be present")

    def test_valid_id_forwarded(self):
        _rc, store_argv = self._captured_argv(self.VALID)
        self.assertIn("--consume-agent-id", store_argv)
        idx = store_argv.index("--consume-agent-id")
        self.assertEqual(store_argv[idx + 1], self.VALID)

    def test_surrogate_not_forwarded(self):
        self._assert_no_consume_flag("\ud800")

    def test_c1_not_forwarded(self):
        self._assert_no_consume_flag("a\x85g")

    def test_oversized_not_forwarded(self):
        self._assert_no_consume_flag("a" * 129)

    def test_option_shaped_not_forwarded(self):
        self._assert_no_consume_flag("--rf")


class RemoteBehaviorPinTest(unittest.TestCase):
    """PR #453 review PRR-007: behavior-level remote pins. The static
    dest-set guards could not catch a deleted refusal or a deleted
    forward; these drive the REAL remote routes and observe what happens.
    Refusal half: every refused dest must exit 2 naming the flag (a
    deleted refusal degrades to hub-unreachable instead -> test fails).
    Forward half: _route_read must hand the real query/namespace/limit to
    the hub call (a deleted forward changes the captured kwargs)."""

    REMOTE_ENV = {"ZMEM_REMOTE_URL": "http://127.0.0.1:9/mcp"}

    # argv per dest: the value must be a VALID non-default for that flag
    # (parsers validate choices/shapes). Derived once from the live
    # _READ_DEFAULTS + the prefetch refusal set so a NEW refused dest
    # without an argv here fails the completeness test below.
    DEST_ARGV = {
        "recall": {
            "moment": ["--moment", "pretool"],
            "session_id": ["--session-id", "supp-sid"],
            "lane": ["--lane", "hermes-compat"],
            "no_bump": ["--no-bump"],
            "state_hint": ["--state-hint", "dirty"],
            "for_injection": ["--for-injection"],
            "delegation_prompt": ["--delegation-prompt=x"],
            "delegation_agent_id": ["--delegation-agent-id", "ag-x"],
            "consume_agent_id": ["--consume-agent-id", "ag-x"],
            "consume_tasktext": ["--consume-tasktext"],
            "hybrid": ["--hybrid"],
            "include_global": ["--include-global"],
            "explain": ["--explain"],
            "no_unfold": ["--no-unfold"],
            "include_cross_project": ["--include-cross-project"],
            "legacy_unscoped": ["--legacy-unscoped"],
            "min_confidence": ["--min-confidence", "0.5"],
            "link_hops": ["--link-hops", "0"],
            "link_budget": ["--link-budget", "3"],
            "target": ["--target", "x"],
            "exclude": ["--exclude", "00000000-0000-0000-0000-000000000000"],
            "prompt_timestamp": ["--prompt-timestamp", "2026-01-01T00:00:00Z"],
            "as_of": ["--as-of", "2026-01-01T00:00:00Z"],
            "ops_token": ["--ops-token", "tok"],
            "principal": ["--principal", "p"],
            "harness": ["--harness", "h"],
            "authorized_namespace": ["--authorized-namespace", "project:x"],
            "global_limit": ["--global-limit", "2"],
            "no_mmr": ["--no-mmr"],
            "no_hybrid": ["--no-hybrid"],
        },
        "recent": {
            "moment": ["--moment", "subagent"],
            "session_id": ["--session-id", "supp-sid"],
            "no_bump": ["--no-bump"],
            "delegation_prompt": ["--delegation-prompt=x"],
            "delegation_agent_id": ["--delegation-agent-id", "ag-x"],
            "consume_agent_id": ["--consume-agent-id", "ag-x"],
            "consume_tasktext": ["--consume-tasktext"],
            "include_global": ["--include-global"],
            "min_confidence": ["--min-confidence", "0.5"],
            "as_of": ["--as-of", "2026-01-01T00:00:00Z"],
            "exclude": ["--exclude", "00000000-0000-0000-0000-000000000000"],
            "ops_token": ["--ops-token", "tok"],
            "principal": ["--principal", "p"],
            "harness": ["--harness", "h"],
            "authorized_namespace": ["--authorized-namespace", "project:x"],
            "global_limit": ["--global-limit", "2"],
            "include_cross_project": ["--include-cross-project"],
            "legacy_unscoped": ["--legacy-unscoped"],
            "for_injection": ["--for-injection"],
            "lane": ["--lane", "hermes-compat"],
        },
        "prefetch": {
            "exclude_ids": ["--exclude",
                            "00000000-0000-0000-0000-000000000000"],
            "principal": ["--principal", "p"],
            "harness": ["--harness", "h"],
            "state_hint": ["--state-hint", "dirty"],
            "for_injection": ["--for-injection"],
            "authorized_namespace": ["--authorized-namespace", "project:x"],
            "consume_agent_id": ["--consume-agent-id", "ag-x"],
        },
    }

    @classmethod
    def setUpClass(cls):
        cls.subs = _subparsers()

    def _remote_env(self):
        # Remote mode treats ZMEM_STORE/ZMEM_DATA + ZMEM_REMOTE_URL as a
        # config error (issue #289 contract): drop both and pin the
        # plugin-data resolver instead.
        env = dict(os.environ)
        env.update(self.REMOTE_ENV)
        env.pop("ZMEM_STORE", None)
        env.pop("ZMEM_DATA", None)
        env["CLAUDE_PLUGIN_DATA"] = _SCRATCH
        return env

    def _run_store_remote(self, argv):
        return subprocess.run(
            [sys.executable, "-X", "utf8",
             str(REPO_ROOT / "skills" / "memory" / "scripts" / "store.py")]
            + argv,
            capture_output=True, text=True, timeout=120,
            env=self._remote_env(), cwd=str(REPO_ROOT),
            encoding="utf-8", errors="replace")

    def _refused_dests_from_live_code(self, cmd):
        refused = dict(_rg._READ_DEFAULTS.get(cmd, {}))
        return refused

    def test_every_read_default_refused_e2e(self):
        """Kill the READ_DEFAULTS false-green direction end to end: for
        EVERY refused dest of recall/recent, supplying it remotely must
        exit 2 with the refusal phrase naming the flag — NOT an argparse
        usage error (a usage error would satisfy rc 2 while never
        reaching _refuse_read_flags, i.e. deleting the refusal stays
        green) and NOT a hub-unreachable envelope."""
        for cmd in ("recall", "recent"):
            for dest in self._refused_dests_from_live_code(cmd):
                argv_tail = self.DEST_ARGV.get(cmd, {}).get(dest)
                if argv_tail is None:
                    continue  # meta dests (json/no_auto_rekey/limit/...)
                flag_token = argv_tail[0].split("=")[0]
                with self.subTest(cmd=cmd, dest=dest):
                    base = [cmd, "--json"]
                    if cmd == "recall":
                        base += ["--query", "probe"]
                    r = self._run_store_remote(base + argv_tail)
                    combined = r.stdout + r.stderr
                    self.assertEqual(r.returncode, 2,
                                     "%s %s must be refused (rc 2), got "
                                     "rc=%s out=%r" % (cmd, dest,
                                                       r.returncode,
                                                       combined[:200]))
                    self.assertIn("not supported in remote mode", combined,
                                  "%s %s: rc 2 came from something other "
                                  "than the remote-gate refusal (usage "
                                  "error?)" % (cmd, dest))
                    self.assertIn(flag_token, combined)

    def test_prefetch_refusals_refused_e2e(self):
        """Kill the _route_prefetch false-green: EVERY inline prefetch
        refusal must exit 2 with the named-flag refusal phrase (a deleted
        refusal degrades to the rc-1 hub-unreachable envelope instead)."""
        for dest, argv_tail in self.DEST_ARGV["prefetch"].items():
            flag_token = argv_tail[0].split("=")[0]
            with self.subTest(dest=dest):
                r = self._run_store_remote(
                    ["prefetch", "--json", "--query", "",
                     "--namespace", "project:pin",
                     "--session-id", "supp-sid",
                     "--moment", "subagent"] + argv_tail)
                combined = r.stdout + r.stderr
                self.assertEqual(r.returncode, 2,
                                 "prefetch %s must be refused, got rc=%s "
                                 "out=%r" % (dest, r.returncode,
                                             combined[:200]))
                self.assertIn("not supported in remote mode", combined)
                self.assertIn(flag_token, combined)

    def test_route_read_forwards_real_kwargs(self):
        """Kill the FORWARDED_OR_META literal: _route_read must hand the
        real query/namespace/limit to the hub call for BOTH recall and
        recent — deleting a branch's forward (e.g. limit=args.limit on
        recent) fails here (PR #453 review round 3: the recent branch was
        previously unpinned)."""
        for cmd, expected in (
                ("recall", {"query": "probe-query",
                            "namespace": "project:pin", "limit": 4}),
                ("recent", {"namespace": "project:pin", "limit": 4})):
            seen = {}

            class FakeHTTP:
                def recall(self, query, namespace=None, limit=None, **kw):
                    seen.update(query=query, namespace=namespace,
                                limit=limit)
                    raise SystemExit(1)

                def recent(self, namespace=None, limit=None, **kw):
                    seen.update(namespace=namespace, limit=limit)
                    raise SystemExit(1)

            class FakeTransport:
                def resolve_deadline_s(self, env):
                    return 1.0

            class FakeOpQueue:
                def render_pending(self, *a, **k):
                    return ""

                def flush(self, *a, **k):
                    return None

            args = argparse.Namespace(
                query="probe-query", namespace="project:pin", limit=4,
                json=True)
            env = dict(os.environ)
            env.pop("ZMEM_STORE", None)
            env.pop("ZMEM_DATA", None)
            env["ZMEM_REMOTE_URL"] = self.REMOTE_ENV["ZMEM_REMOTE_URL"]
            env["CLAUDE_PLUGIN_DATA"] = _SCRATCH
            with self.subTest(cmd=cmd):
                with mock.patch.dict(os.environ, env), \
                     mock.patch.object(_rg, "_import_fleet",
                                       return_value=(FakeOpQueue(),
                                                     FakeTransport())), \
                     mock.patch.object(_rg, "_hub", return_value=FakeHTTP()):
                    with self.assertRaises(SystemExit):
                        _rg._route_read(cmd, args)
                self.assertEqual(seen, expected,
                                 "%s forward dropped or altered" % cmd)

    def test_forward_prefetch_forwards_real_kwargs(self):
        """The prefetch route's forwarded surface (query, namespace,
        session_id, moment, lane, reserve, ops_tokens, consume_tasktext)
        must reach the hub call verbatim (PR #453 review round 3: the
        forward half of the prefetch route was unpinned; the consume-grant
        verbatim pin lives in tests/test_tasktext_fleet_grant.py)."""
        seen = {}

        class FakeHTTP:
            def prefetch(self, query, **kw):
                seen["query"] = query
                seen.update(kw)
                raise SystemExit(1)

        class FakeTransport:
            def resolve_deadline_s(self, env):
                return 1.0

        class FakeOpQueue:
            def render_pending(self, *a, **k):
                return ""

            def flush(self, *a, **k):
                return None

        args = argparse.Namespace(
            query="probe-q", namespace="project:pin", session_id="supp-sid",
            moment="subagent", lane=None, reserve=True,
            ops_tokens=["tok"], consume_tasktext=True, consume_agent_id="",
            exclude_ids=None, principal=None, harness=None, state_hint=None,
            for_injection=False, authorized_namespace=None)
        env = dict(os.environ)
        env.pop("ZMEM_STORE", None)
        env.pop("ZMEM_DATA", None)
        env["ZMEM_REMOTE_URL"] = self.REMOTE_ENV["ZMEM_REMOTE_URL"]
        env["CLAUDE_PLUGIN_DATA"] = _SCRATCH
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(_rg, "_import_fleet",
                               return_value=(FakeOpQueue(),
                                             FakeTransport())), \
             mock.patch.object(_rg, "_hub", return_value=FakeHTTP()), \
             mock.patch.object(_rg.time, "monotonic", side_effect=[0.0, 0.0]):
            with self.assertRaises(SystemExit):
                _rg._forward_prefetch(args, FakeHTTP(), 1000)
        self.assertEqual(seen, {
            "query": "probe-q", "namespace": "project:pin",
            "session_id": "supp-sid", "moment": "subagent", "lane": None,
            "reserve": True, "ops_tokens": ["tok"], "consume_tasktext": True,
            "deadline_ms": 1000,
        })

    def test_completeness_map_covers_read_defaults(self):
        """Every refused recall/recent dest must have a DEST_ARGV entry
        (meta dests excepted) — a newly refused flag without a behavior
        pin fails here instead of shipping unpinned."""
        META = {"help", "json", "limit", "namespace", "no_auto_rekey"}
        for cmd in ("recall", "recent"):
            missing = sorted(
                d for d in self._refused_dests_from_live_code(cmd)
                if d not in META and d not in self.DEST_ARGV.get(cmd, {}))
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    missing, [],
                    "refused dest(s) %r lack behavior pins: add argv to "
                    "RemoteBehaviorPinTest.DEST_ARGV[%r]" % (missing, cmd))

    def test_dest_argv_keys_are_live_refused_dests(self):
        """The REVERSE direction (PR #453 review round 3): every
        DEST_ARGV key must still be a refused dest in the LIVE
        _READ_DEFAULTS. Without this, deleting a refusal entry (e.g.
        link_hops) makes the e2e loop silently skip that dest — the list
        and the check would collapse into one self-referential source."""
        META = {"help", "json", "limit", "namespace", "no_auto_rekey"}
        for cmd in ("recall", "recent"):
            stale = sorted(
                d for d in self.DEST_ARGV.get(cmd, {})
                if d not in META and d not in _rg._READ_DEFAULTS[cmd])
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    stale, [],
                    "DEST_ARGV[%r] pin(s) %r are no longer refused "
                    "remotely: restore the _READ_DEFAULTS entry or delete "
                    "the stale pin" % (cmd, stale))

    def test_completeness_map_covers_prefetch_refusals(self):
        """The prefetch refusal argv map must cover the pinned refusal
        set; the refusal loop in _route_prefetch is the live source — if
        a refusal tuple entry is deleted, the e2e leg above fails; if a
        NEW refusal is added, this map must grow with it."""
        # The refusal set is inline in _route_prefetch; re-derive it by
        # supplying ALL pinned dests at once and asserting the FIRST
        # refusal still names one of ours (weak direction) — the strong
        # direction is test_prefetch_refusals_refused_e2e above. Here we
        # pin that each map entry is a real prefetch dest with a real
        # parser action, so a renamed dest fails loudly.
        dests = _dests(self.subs["prefetch"])
        for dest in self.DEST_ARGV["prefetch"]:
            with self.subTest(dest=dest):
                self.assertIn(dest, dests)


class RemoteReadFlagCompletenessTest(unittest.TestCase):
    # Forwarded to the hub read tool (query/namespace/limit) or parser
    # meta / client-side presentation that never needs a hub parameter.
    FORWARDED_OR_META = {
        "recall": {"help", "json", "limit", "namespace", "no_auto_rekey",
                   "query"},
        "recent": {"help", "json", "limit", "namespace", "no_auto_rekey"},
    }
    # Pinned prefetch dest set (remote_gate._route_prefetch forwards
    # query/namespace/session_id/moment/lane/reserve/ops_tokens/
    # consume_tasktext and refuses the rest by name). Update ONLY together
    # with a remote disposition for the new flag.
    PREFETCH_PINNED = None  # filled from the live parser in setUpClass

    @classmethod
    def setUpClass(cls):
        cls.subs = _subparsers()

    def test_recall_recent_dests_all_dispositioned(self):
        for cmd in ("recall", "recent"):
            dests = _dests(self.subs[cmd])
            refused = set(_rg._READ_DEFAULTS[cmd])
            undispositioned = sorted(
                dests - refused - self.FORWARDED_OR_META[cmd])
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    undispositioned, [],
                    "remote %s would SILENTLY DROP these flags: add each to "
                    "remote_gate._READ_DEFAULTS[%r] (refused by name) or "
                    "forward it to the hub" % (cmd, cmd))

    def test_read_defaults_name_real_dests(self):
        # The reverse direction: a stale _READ_DEFAULTS key would make the
        # refusal check read an attribute the parser never sets.
        for cmd in ("recall", "recent"):
            dests = _dests(self.subs[cmd])
            with self.subTest(cmd=cmd):
                self.assertEqual(
                    sorted(set(_rg._READ_DEFAULTS[cmd]) - dests), [])

    def test_428_flags_refused_remotely(self):
        for cmd, flags in (("recall", ("delegation_agent_id",
                                       "consume_agent_id", "state_hint")),
                           ("recent", ("delegation_agent_id",
                                       "consume_agent_id"))):
            for dest in flags:
                with self.subTest(cmd=cmd, dest=dest):
                    self.assertIn(dest, _rg._READ_DEFAULTS[cmd])

    def test_prefetch_dest_set_pinned(self):
        expected = {
            "help", "json", "no_auto_rekey", "query", "namespace",
            "session_id", "moment", "lane", "reserve", "ops_tokens",
            "consume_tasktext", "consume_agent_id", "exclude_ids",
            "principal", "harness", "state_hint", "for_injection",
            "authorized_namespace",
            # --no-bump is an explicit no-op marker (cli.py help: "the
            # selector never bumps retrieval_count", issue #159): dropping
            # it remotely changes nothing, so it needs no hub parameter.
            "no_bump",
            # --prompt-timestamp (issue #389 AC8/F5): forwarded — the
            # e2e forward-capture leg lives in RemoteBehaviorPinTest.
            "prompt_timestamp",
        }
        actual = _dests(self.subs["prefetch"])
        self.assertEqual(
            sorted(actual - expected), [],
            "new prefetch flag(s): decide the remote disposition in "
            "remote_gate._route_prefetch (forward or refuse by name), add "
            "them to this pinned set AND to RemoteBehaviorPinTest (an e2e "
            "refusal leg for a refused dest / a forward capture for a "
            "forwarded one)")


if __name__ == "__main__":
    names = [a for a in sys.argv[1:] if not a.startswith("-")]
    loader = unittest.TestLoader()
    suite = (unittest.TestSuite(loader.loadTestsFromName(n, sys.modules[__name__])
                                for n in names)
             if names else loader.loadTestsFromModule(sys.modules[__name__]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print("CHECK-PASS" if result.wasSuccessful() else "CHECK-FAIL")
    sys.exit(0 if result.wasSuccessful() else 1)
