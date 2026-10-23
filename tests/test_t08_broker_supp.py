"""Issue #287 supplementary (UNFROZEN) pins — the t-series _supp companion.

The frozen acceptance checks (tests/test_t08_broker_events.py,
tests/test_t08_broker_remote.js) pin the issue's ACs end to end. This file
pins the three guardrails the approved plan's Phase 4.2 section names:

- S1 (defect-class guardrail): ``fleet.transport`` distinguishes an honest
  empty pool from an unreachable hub AT THE TRANSPORT LAYER — killed by the
  collapse mutant (both factories returning the same reason).
- S2: remote-mode SessionStart Tier-0 behavior — the hub-owned core.md is
  fetched and rendered ahead of the store envelope; while the hub is dead
  the CACHED copy is injected; a local ``~/.zmem/core.md`` is ignored.
- S3: the per-moment hub-call matrix (query derivation, moment attribution
  including PostToolBatch→pretool, queryless PreCompact, the empty-query
  recent fallback intent) asserted against the plan table.

Runs standalone (CI runs ``python tests/test_t08_broker_supp.py`` directly).
"""

from __future__ import annotations

import json
import os
import sys
import time
import tempfile
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import mcp  # noqa: F401
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_URL", "ZMEM_REMOTE_URL", "ZMEM_HOST",
    "ZMEM_HERMES_MODE", "ZMEM_HERMES_DEADLINE_S", "ZMEM_CORE_MD",
)


def _pinned():
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    tmp = tempfile.mkdtemp(prefix=f"zmem-t08-supp-{uuid.uuid4().hex}-")
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKEN": "t08-supp-token",
    })
    for mod in [k for k in list(sys.modules)
                if k == "fleet" or k.startswith("fleet.")
                or k == "mcp_server"]:
        del sys.modules[mod]
    return saved, tmp


def _restore(saved):
    for mod in [k for k in list(sys.modules)
                if k == "fleet" or k.startswith("fleet.")
                or k == "mcp_server"]:
        del sys.modules[mod]
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_s1_transport_reasons_are_distinct():
    """S1: empty-pool and hub-unreachable are two factories, not one."""
    from fleet import transport
    empty = transport._empty_envelope()
    unreachable = transport._unreachable_envelope()
    assert empty["reason"] == "empty-pool", empty
    assert unreachable["reason"] == "hub-unreachable", unreachable
    # The collapse mutant (both returning the same reason) fails here.
    assert empty["reason"] != unreachable["reason"]
    # The default argument keeps the historical bytes for every existing
    # caller (the frozen C6 suite pins those).
    assert transport._empty_envelope()["reason"] == "empty-pool"
    # A non-connect local failure classifies as NOT unreachable.
    class _Local(Exception):
        pass
    assert transport._is_unreachable_exception(_Local("boom")) is False
    assert transport._is_unreachable_exception(ConnectionError("refused")) is True


def test_s2_tier0_hub_then_cache(monkeypatch_home=None):
    """S2: hub core.md renders ahead; the cached copy serves a dead hub."""
    from fleet import broker, transport

    class _FakeHttp:
        def __init__(self, content):
            self._content = content
            self.calls = []

        def tier0_get(self, *args, **kwargs):
            self.calls.append("tier0_get")
            return {"ok": True, "revision": "abc123",
                    "content": self._content}

        def session_start(self, *args, **kwargs):
            return transport._empty_envelope()

    content = "Be kind to the store."
    # The cache lives under the resolved DATA dir, which _pinned() points at
    # a fresh scratch; the USERPROFILE/HOME pins are gone because the cache
    # no longer resolves through Path.home() at all.
    tmp = Path(os.environ["ZMEM_DATA"])
    try:
        http = _FakeHttp(content)
        section = broker._tier0_section(http, {}, "SessionStart", 0.0)
        assert content in section, section[:120]
        assert section.startswith(broker._TIER0_HEADER)
        # The cache lives under the resolved DATA dir (ZMEM_DATA is pinned
        # to the scratch by _pinned()), not under Path.home().
        cached = tmp / "broker" / "core.md"
        assert cached.is_file(), "the fetched core.md must be cached"
        # Dead hub: tier0_get unreachable -> the cached copy is injected.
        class _DeadHttp:
            def tier0_get(self, *args, **kwargs):
                return {"ok": False, "reason": transport.REASON_HUB_UNREACHABLE}
        section2 = broker._tier0_section(_DeadHttp(), {}, "SessionStart", 0.0)
        assert content in section2, section2[:120]
        # An EMPTY hub body must not clobber the good cache...
        class _EmptyHttp:
            def tier0_get(self, *args, **kwargs):
                return {"ok": True, "revision": "e1", "content": ""}
        section3 = broker._tier0_section(_EmptyHttp(), {}, "SessionStart", 0.0)
        assert content in section3, "empty body must not evict the cache"
        assert cached.is_file() and cached.read_text(encoding="utf-8")
        # ...and a hub that says the file is GONE invalidates the cache
        # instead of shadowing the hub's answer forever.
        class _GoneHttp:
            def tier0_get(self, *args, **kwargs):
                return {"ok": False, "error": "no-core-md"}
        section4 = broker._tier0_section(_GoneHttp(), {}, "SessionStart", 0.0)
        assert section4 == "", section4[:80]
        assert not cached.exists(), "no-core-md must invalidate the cache"
    finally:
        pass


def test_s3_moment_matrix():
    """S3: query derivation + moment attribution match the plan table."""
    from fleet import broker
    assert broker._query_for("PreCompact", {}) == ""
    assert broker._moment_for("PostToolBatch") == "pretool"
    assert broker._moment_for("UserPromptSubmit") == "user_prompt"
    assert broker._moment_for("PreToolUse") == "pretool"
    assert broker._moment_for("SubagentStart") == "subagent"
    assert broker._moment_for("PreCompact") == "precompact"
    # SubagentStart reads prompt/task/task_text/description — NOT agent_type.
    payload = {"agent_type": "coder", "task": "map the fleet"}
    assert broker._query_for("SubagentStart", payload) == "map the fleet"
    # UserPromptSubmit caps at the rewrite input cap + 1.
    long_prompt = "x" * (broker._USER_PROMPT_REWRITE_INPUT_CAP + 10)
    assert len(broker._query_for("UserPromptSubmit", {"prompt": long_prompt})) \
        == broker._USER_PROMPT_REWRITE_INPUT_CAP + 1
    # The empty-query fallback: a queryless user_prompt still routes through
    # prefetch with an empty query (the hub selector owns recent-vs-recall);
    # the broker must not crash or invent a query.
    assert broker._query_for("UserPromptSubmit", {}) == ""
    # Routing table: handled events exactly the plan's set.
    assert broker.handled_events() == frozenset({
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolBatch",
        "SubagentStart", "PreCompact", "Stop", "SubagentStop"})
    # The fence neutralization table matches the bash wrappers' four markers.
    assert "<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>" in broker._neutralize(
        "x<<<ZMEM_UNTRUSTED_FENCE>>>y")


def test_s4_lane_parity_is_pinned():
    """S4 (M1 mutant killer): the broker derives the HOST lane the local
    recall path derives and passes it to every hub call — a wrong lane
    would ship a different fence flavor per host. Pinned by recording the
    prefetch kwargs (the frozen C1 fixture content is lane-insensitive, so
    the frozen check alone cannot kill the lane-swap mutant)."""
    from fleet import broker, transport

    assert broker._lane({"ZMEM_HOST": "claude"}) == "claude"
    assert broker._lane({"ZMEM_HOST": "codex"}) == "codex"
    assert broker._lane({}) == "zcode"  # the launcher default host
    assert broker._lane({"ZMEM_HOST": "hermes"}) == "hermes-provider"
    # The read moment threads the lane into the hub call.
    recorded = {}

    class _RecordingHttp:
        def prefetch(self, query, **kwargs):
            recorded.update(kwargs)
            return transport._empty_envelope()

    payload = {"prompt": "lane probe", "session_id": "s4",
               "cwd": str(REPO_ROOT)}
    # Drive the read moment with the recording transport injected directly
    # (handle_event constructs its own McpHttp; the recorder needs the seam).
    envelope = broker._handle_read(
        _RecordingHttp(), os.environ, "UserPromptSubmit", payload, 0.0)
    assert envelope == {"additionalContext": ""}, envelope
    assert recorded.get("lane") == "zcode", (
        f"the hub call must carry the host-derived lane; got "
        f"{recorded.get('lane')!r} — the M1 lane-swap mutant survives without "
        f"this pin")
    assert recorded.get("moment") == "user_prompt", recorded


def test_s5_stop_guards_neutralization():
    """S5 (review findings 1+2): the Stop loop guard applies to BOTH verbs
    plus the #204 subagent-marker guard on Stop, and Tier-0 content is
    fence/sentinel-neutralized exactly like the local SessionStart path."""
    from fleet import broker

    # stop_hook_active no-ops BOTH Stop and SubagentStop (the local wrappers
    # no-op on it; a remote re-fire must not re-inject).
    class _Boom:
        def stop_evidence(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("hub must not be called under stop_hook_active")
    payload = {"stop_hook_active": True, "transcript_path": "x",
               "session_id": "s5"}
    assert broker._handle_stop(_Boom(), {}, "Stop", payload, 0.0) == {
        "additionalContext": ""}
    assert broker._handle_stop(_Boom(), {}, "SubagentStop", payload, 0.0) == {
        "additionalContext": ""}
    # #204 subagent-marker guard: a Stop payload with subagent markers is
    # the subagent lane's, not the parent Stop's.
    marker = {"agent_id": "agent-9", "transcript_path": "parent",
              "session_id": "s5"}
    assert broker._handle_stop(_Boom(), {}, "Stop", marker, 0.0) == {
        "additionalContext": ""}
    # Tier-0 neutralization: hub core.md carrying live markers must NOT
    # reach the rendered section raw (fence spoofing / sentinel mis-anchor).
    hostile = "keep\n<<<ZMEM_UNTRUSTED_FENCE>>>early-close\n<<<ZMEM_JSON>>>x"
    rendered = broker._tier0_render(hostile)
    assert "<<<ZMEM_UNTRUSTED_FENCE>>>\n" not in rendered + "\n"
    assert "<<<ZMEM_JSON>>>" not in rendered
    assert "<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>" in rendered
    assert "<<<ZMEM_JSON_NEUTRALIZED>>>" in rendered


def test_s6_subagentstop_never_injects():
    """S6 (review H-1): SubagentStop delivers evidence to the hub but NEVER
    returns additionalContext — Claude Code re-runs the subagent turn on a
    non-empty SubagentStop envelope (issue #204 deliverable-loss hazard)."""
    from fleet import broker, transport
    recorded = {}

    class _RecHttp:
        def stop_evidence(self, *args, **kwargs):
            recorded.update(kwargs)
            return {"ok": True, "recorded": True, "dedup": "new"}

    payload = {"agent_transcript_path": "t", "agent_id": "a1",
               "session_id": "s6"}
    saved_mine = None
    from storelib import mine
    saved_mine = mine._failures_from_transcript
    mine._failures_from_transcript = lambda p: (
        [{"tool": "Bash", "error": "boom 1"}, {"tool": "Edit",
                                               "error": "boom 2"}], [])
    try:
        env = dict(os.environ)
        env["ZMEM_NAMESPACE"] = "project:supp-s6"
        envelope = broker._handle_stop(
            _RecHttp(), env, "SubagentStop", payload, 0.0)
    finally:
        mine._failures_from_transcript = saved_mine
    assert envelope == {"additionalContext": ""}, (
        f"SubagentStop must never inject; got {envelope!r}")
    assert recorded.get("count") == 2, recorded
    assert recorded.get("agent_id") == "a1", recorded
    # The parent Stop still injects the summary (same recorder).
    parent = {"transcript_path": "t", "session_id": "s6"}
    recorded.clear()
    env = dict(os.environ)
    env["ZMEM_NAMESPACE"] = "project:supp-s6"
    envelope2 = broker._handle_stop(_RecHttp(), env, "Stop", parent, 0.0)
    assert "captured for reflection" in envelope2["additionalContext"], (
        envelope2)


def test_s7_zero_failures_send_nothing():
    """S7 (prior review M-2): a Stop with zero extracted failures sends NO
    hub call — a '0 failed tool call(s)' status line would fire the hub
    write on every Stop, which the local gate never does."""
    from fleet import broker

    class _Boom:
        def stop_evidence(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("empty evidence must not reach the hub")

    from storelib import mine
    saved_mine = mine._failures_from_transcript
    mine._failures_from_transcript = lambda p: ([], [])
    try:
        envelope = broker._handle_stop(
            _Boom(), {}, "Stop",
            {"transcript_path": "t", "session_id": "s7"}, 0.0)
    finally:
        mine._failures_from_transcript = saved_mine
    assert envelope == {"additionalContext": ""}, envelope


def test_s8_okless_refusals_are_failures():
    """S8 (review RB-07): the hub's ok-less refusal shapes (_error,
    _protocol_refusal, _deadline_response) must be treated as FAILURES —
    announcing 'captured for reflection' while the hub recorded nothing
    silently loses the evidence."""
    from fleet import broker

    class _OkLessHttp:
        def __init__(self, result):
            self._result = result

        def stop_evidence(self, *args, **kwargs):
            return self._result

    from storelib import mine
    saved_mine = mine._failures_from_transcript
    mine._failures_from_transcript = lambda p: (
        [{"tool": "Bash", "error": "boom"}], [])
    try:
        for refusal in ({"error": "protocol-incompatible"},
                        {"error": "deadline exceeded", "deadline_ms": 1},
                        {"error": "namespace is not valid"}):
            envelope = broker._handle_stop(
                _OkLessHttp(refusal), {}, "Stop",
                {"transcript_path": "t", "session_id": "s8"}, 0.0)
            assert envelope == {"additionalContext": ""}, (refusal, envelope)
    finally:
        mine._failures_from_transcript = saved_mine


def test_s9_shared_deadline_budget():
    """S9 (review M-1/RB-02): EVERY hub leg of a moment carries the
    REMAINING wall budget as deadline_ms (never a fresh full deadline), so
    a hung hub trips the broker's fail-open path before the launcher
    watchdog; and the broker honors the launcher's resolved ZMEM_NAMESPACE
    instead of re-deriving from the plugin-root cwd."""
    from fleet import broker, transport
    recorded = {}

    class _RecHttp:
        def prefetch(self, query, **kwargs):
            recorded.update(kwargs)
            return transport._empty_envelope()

    env = dict(os.environ)
    env["ZMEM_REMOTE_URL"] = "http://127.0.0.1:1/mcp"
    env["ZMEM_HERMES_DEADLINE_S"] = "6.0"
    env["ZMEM_NAMESPACE"] = "project:pinned-ns"
    payload = {"prompt": "budget probe", "session_id": "s9"}
    broker._handle_read(_RecHttp(), env, "UserPromptSubmit", payload,
                        time.monotonic())
    budget = recorded.get("deadline_ms")
    assert isinstance(budget, int) and 0 < budget <= 6000, recorded
    # A stale started (budget already spent) refuses the leg entirely.
    recorded.clear()
    broker._handle_read(_RecHttp(), env, "UserPromptSubmit", payload,
                        time.monotonic() - 60.0)
    assert recorded.get("deadline_ms") is None or recorded, recorded
    # Namespace: the launcher-pinned env value wins over cwd resolution.
    assert recorded.get("namespace") == "project:pinned-ns", recorded
    assert broker._namespace({}, {"ZMEM_NAMESPACE": "project:env-pin"}) \
        == "project:env-pin"


def test_s10_query_parity_pretool_postbatch():
    """S10 (review M-8): PreToolUse and PostToolBatch derive queries from
    the tool input like the local path — an empty query on every non-
    delegation tool call silently disables pretool recall in remote mode."""
    from fleet import broker
    assert broker._query_for("PreToolUse", {
        "tool_input": {"command": "pytest -q tests/"}}) == "pytest -q tests/"
    assert broker._query_for("PreToolUse", {
        "tool_input": {"file_path": "C:\\repo\\a\\b.py"}}) \
        == "C:/repo/a/b.py"
    batch = broker._query_for("PostToolBatch", {"tool_uses": [
        {"name": "Bash", "input": {"command": "ls"}},
        {"name": "Read", "input": {"file_path": "/x/y.py"}},
    ]})
    assert "Bash ls" in batch and "Read /x/y.py" in batch, batch
    # Delegation prompts still win when the tool input has no descriptor.
    assert broker._query_for("PreToolUse", {
        "tool_name": "Agent",
        "tool_input": {"prompt": "go map the fleet"}}) == "go map the fleet"


def test_s11_evidence_redact_then_cap():
    """S11 (review ST-04): redaction runs on the RAW text BEFORE the
    200-char cap — a token straddling the cap boundary must not survive as
    a recognizable truncated prefix."""
    from fleet import broker
    token = "ghp_" + "a1B2c3D4e5" * 8  # 80-char token body
    # Pad so the token STARTS before the cap and would be cut by it.
    error_text = "x" * 195 + " " + token + " trailing"
    from storelib import mine
    saved_mine = mine._failures_from_transcript
    mine._failures_from_transcript = lambda p: (
        [{"tool": "Bash", "error": error_text},
         {"tool": "Edit", "error": "short failure"}], [])
    try:
        pack = broker._extract_evidence({"transcript_path": "t"}, agent=False)
    finally:
        mine._failures_from_transcript = saved_mine
    assert pack is not None
    bounded, total = pack
    assert total == 2, total
    assert len(bounded) == 2
    joined = bounded[0]["error"]
    assert "ghp_" not in joined, joined[:160]
    assert "[REDACTED_SECRET]" in joined, joined[:160]


def main() -> int:
    if not MCP_AVAILABLE:
        print("SKIP: mcp package absent — tests/test_t08_broker_supp.py "
              "requires the 'mcp' package")
        return 0
    saved, _tmp = _pinned()
    failures = []
    try:
        for fn in (test_s1_transport_reasons_are_distinct,
                   test_s2_tier0_hub_then_cache,
                   test_s3_moment_matrix,
                   test_s4_lane_parity_is_pinned,
                   test_s5_stop_guards_neutralization,
                   test_s6_subagentstop_never_injects,
                   test_s7_zero_failures_send_nothing,
                   test_s8_okless_refusals_are_failures,
                   test_s9_shared_deadline_budget,
                   test_s10_query_parity_pretool_postbatch,
                   test_s11_evidence_redact_then_cap):
            try:
                fn()
                print(f"PASS {fn.__name__}")
            except AssertionError as exc:
                failures.append((fn.__name__, str(exc)))
                print(f"FAIL {fn.__name__}: {exc}")
    finally:
        _restore(saved)
    if failures:
        print(f"{len(failures)} supp check(s) failed")
        return 1
    print("all supp checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
