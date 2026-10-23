"""Issue #287 (Workstream T PR 8) — per-machine broker, remote passive memory.

Phase 2.5 frozen acceptance checks C2-C4 (C1 is the node check
tests/test_t08_broker_remote.js; the preserving checks C5/C6 stay in
tests/test_launcher.js and tests/test_hermes_transport.py, untouched). Every
check names its standing at both ends:

- AC2 ``test_stop_sends_extracted_failures_not_paths`` — a Stop event whose
  transcript carries THREE failed tool calls (one ``tool_result`` with
  ``is_error: true``, one whose sibling ``toolUseResult`` starts with
  "Error", and one whose error text embeds a raw Windows path and a raw
  POSIX path) plus a SUCCEEDING call sends stop evidence to the hub with
  ``count == 3``; every detail error text is whitespace-collapsed and
  <= 200 chars; a ``ghp_`` + 40-alnum token in the raw transcript arrives
  redacted; the raw path bytes never arrive — the scrubbed text carries
  the ``[REDACTED_PATH]`` placeholder (contract v2); NO string anywhere in
  the sent payload is path-shaped; and the hub-side ``stop_evidence`` tool
  accepts a good payload (``ok``/``recorded`` True) while refusing a
  path-carrying detail with ``{"ok": False, "error":
  "path-shaped-field"}`` (issue #287 AC2: redact_secret_like_text-class
  secrets plus redact_training_text-class path/email scrubbing).
  RED at base (no ``skills/memory/scripts/fleet/`` package: the in-check
  import surfaces ``ModuleNotFoundError`` and the check FAILs), GREEN after
  the fix.
- AC3 ``test_subagent_stop_uses_agent_transcript`` — a SubagentStop payload
  carrying BOTH ``transcript_path`` (a parent transcript whose only failed
  call is ``ParentOnlyTool``) and ``agent_transcript_path`` (a subagent
  transcript whose failed call is ``AgentOnlyTool``) with ``agent_id``
  "agent-42" sends evidence extracted from the AGENT transcript only
  (mentions AgentOnlyTool, never ParentOnlyTool), keyed by agent_id
  "agent-42"; the broker never opens the parent transcript (a guard on
  ``builtins.open`` raises on the parent path). RED at base
  (ModuleNotFoundError), GREEN after the fix (issue #287 AC3).
- AC4 ``test_unreachable_hub_reason_is_distinct`` — with ZMEM_REMOTE_URL
  pointing at a bound-then-closed port and a 1.5 s deadline env, a
  UserPromptSubmit fails OPEN (an exit-safe dict whose
  ``additionalContext`` is reachable and empty) within the 8 s budget, the
  failure reason is the DISTINCT literal ``"hub-unreachable"`` (never
  ``"empty-pool"``), and one JSON line carrying the reason is appended to
  ``<data_dir>/broker-log.jsonl`` where ``<data_dir>`` is the resolved
  store's parent directory. RED at base (ModuleNotFoundError), GREEN after
  the fix (issue #287 AC4).

Authored tool contract the implementation must satisfy (the issue's
constraints, made executable): the new package
``skills/memory/scripts/fleet/`` exposes ``fleet.transport`` (TransportMode /
resolve_transport_mode / McpHttp with prefetch, session_start, hello,
prefetch_ack, stop_evidence; failure envelopes carry ``reason`` and the
distinct unreachable literal is ``"hub-unreachable"``) and ``fleet.broker``
(``REMOTE_URL_ENV = "ZMEM_REMOTE_URL"``, ``is_remote_mode``,
``handle_event(event, payload, env=None) -> {"additionalContext": str}``).
Stop-evidence redaction duty is CONTRACT V2 (amended pre-freeze): the
broker scrubs every outgoing detail with ``redaction.redact_training_text``
semantics — secrets AND emails AND filesystem paths (Windows + POSIX +
quoted) rewritten to ``[REDACTED_SECRET]`` / ``[REDACTED_EMAIL]`` /
``[REDACTED_PATH]`` or equivalent placeholders — before anything reaches
the hub. (v1 pinned only ``redact_secret_like_text``, which leaves
filesystem paths intact; the amendment closes that hole.)

Env discipline: every check pins a fresh GUID scratch dir (ZMEM_HOME=repo
root, ZMEM_STORE/ZMEM_DATA/ZMEM_MODELS_DIR, ZMEM_MODEL_AUTODOWNLOAD=0,
PYTHONIOENCODING=utf-8, a scratch ZMEM_MCP_TOKEN) BEFORE importing
mcp_server or fleet, purging any cached ``mcp_server``/``fleet`` modules on
entry AND exit so constants reload under the pinned env (the t06/t07
store-path-freeze discipline), and restores everything afterwards. The fleet
import lives INSIDE each check wrapped so the base failure is a clean FAIL
line plus the ``ModuleNotFoundError`` traceback text.

Runs standalone (CI runs ``python tests/test_t08_broker_events.py`` directly
— the repo's python lane is a direct-execution loop, NOT pytest) with the
t-series selector contract: no argument runs ALL checks, ``C2``/``C3``/``C4``
run one check, an unknown selector prints ``NO CHECK <arg>`` to stderr and
exits 2. Skips loudly (visible message, exit 0) only when the ``mcp``
package is absent, which CI prevents by installing
hermes-plugin/server/requirements.txt on both matrix legs.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

try:
    import mcp  # noqa: F401  (the SDK import that gates the hub surface)
    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover - CI installs requirements.txt
    MCP_AVAILABLE = False

_PIN_KEYS = (
    "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
    "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING", "ZMEM_INJECT",
    "ZMEM_INJECT_TOKEN_BUDGET", "ZMEM_NAMESPACE", "ZMEM_CROSS_PROJECT",
    "ZMEM_MCP_TOKEN", "ZMEM_MCP_TOKEN_FILE", "ZMEM_MCP_TOKENS_FILE",
    "ZMEM_MCP_DEFAULT_NS", "ZMEM_MCP_URL", "ZMEM_EMBED_PROFILE",
    "ZMEM_HOST", "ZMEM_SESSION",
    # issue #287: the broker's own knobs — ambient values would flip a
    # check into (or out of) remote mode behind the pin's back.
    "ZMEM_REMOTE_URL", "ZMEM_HERMES_MODE", "ZMEM_HERMES_DEADLINE_S",
)

# issue #287 AC4: the wall-clock bound (deadline max 8.0 s + slack).
_C4_MAX_ELAPSED_S = 8.0

# The hub's path-shaped refusal predicate (issue #287, hub contract): a
# string is path-shaped when it matches a drive root, an absolute/UNC root,
# contains a backslash, or carries a `..` path segment. Mirrored here so the
# CLIENT-side "no path in the sent payload" assertion uses the same rule the
# hub enforces (the hub-side refusal is pinned live in AC2's hub leg).
_RE_DRIVE_ROOT = re.compile(r"^[A-Za-z]:[\\/]")
_RE_ABS_ROOT = re.compile(r"^[\\/]")


def _is_path_shaped(value: str) -> bool:
    if _RE_DRIVE_ROOT.match(value) or _RE_ABS_ROOT.match(value):
        return True
    if "\\" in value:
        return True
    if value == ".." or "/../" in value or "\\..\\" in value:
        return True
    return False


def _iter_strings(obj):
    """Yield every string key and value anywhere in a JSON-ish structure."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(value)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _iter_strings(item)


def _transcript_line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)


def _assistant_tool_use(tid: str, name: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name,
         "input": {"command": "probe"}}]}}


@contextlib.contextmanager
def _pinned_env(prefix: str):
    """Fresh GUID scratch + pinned env (t06/t07 discipline, no server).

    ZMEM_STORE/ZMEM_DATA point at the scratch BEFORE any storelib/fleet
    import; cached mcp_server/fleet modules are purged on entry AND exit so
    no sibling check inherits this pin (store-path-freeze discipline).
    """
    tmp = tempfile.mkdtemp(prefix=f"zmem-t08-{prefix}-{uuid.uuid4().hex}-")
    saved = {k: os.environ.get(k) for k in _PIN_KEYS}
    for key in _PIN_KEYS:
        os.environ.pop(key, None)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZMEM_MCP_TOKEN": "t08-check-token",
    })
    for mod in [k for k in list(sys.modules)
                if k == "mcp_server" or k == "fleet"
                or k.startswith("fleet.") or k.startswith("auth")
                or k.startswith("bind_guard")]:
        del sys.modules[mod]
    try:
        yield tmp
    finally:
        for mod in [k for k in list(sys.modules)
                    if k == "mcp_server" or k == "fleet"
                    or k.startswith("fleet.")]:
            del sys.modules[mod]
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextlib.contextmanager
def _pinned_server(prefix: str):
    """The pinned env plus a real in-process hub (t06_hello_fb.py:79-96)."""
    with _pinned_env(prefix) as tmp:
        sys.path.insert(0, str(SERVER_DIR))
        try:
            import mcp_server
            server = mcp_server.build_server(
                host="127.0.0.1", port=0, use_tls=False)
            yield server, mcp_server, tmp
        finally:
            sys.path.remove(str(SERVER_DIR))


def _load_fleet():
    """Import fleet.broker/fleet.transport under the pinned env.

    At BASE the package does not exist: print the traceback (so the
    ``ModuleNotFoundError`` text is part of this run's output, per the
    frozen-check protocol) and convert to a clean FAIL line.
    """
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import fleet.broker  # noqa: F401
        import fleet.transport  # noqa: F401
        return fleet.broker, fleet.transport
    except ModuleNotFoundError:
        traceback.print_exc()
        raise AssertionError(
            "skills/memory/scripts/fleet/ does not exist at this tree — "
            "issue #287 (broker + remote mode) not implemented") from None


def _call(server, name: str, args: dict):
    return asyncio.run(
        server._tool_manager.call_tool(name, args, context=None))


class _StopEvidenceRecorder:
    """Capture what the broker sent through fleet.transport.McpHttp."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, _transport_self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return {"ok": True, "recorded": True}


# -- AC2 ----------------------------------------------------------------------

def test_stop_sends_extracted_failures_not_paths():
    """issue #287 AC2: Stop evidence is bounded, redacted, path-free.

    Redaction duty is CONTRACT V2 (amended pre-freeze):
    redact_secret_like_text-class secrets PLUS redact_training_text-class
    path/email scrubbing — the raw filesystem paths embedded in the
    transcript must arrive as ``[REDACTED_PATH]``-style placeholders,
    never as raw path bytes.
    """
    token = "ghp_" + "a1B2c3D4e5" * 4  # ghp_ + 40 alnum chars
    win_path = "C:\\data\\tokens\\ghp_abcdefghijklmno.txt"
    posix_path = "/var/private/.aws/credentials"
    path_error = f"cannot read {win_path} nor {posix_path}"
    long_error = "boom   went   the   command\n\nwith   runaway   spaces " + "x" * 300
    session = f"t08-ac2-{uuid.uuid4().hex}"

    with _pinned_server("ac2") as (server, _mod, tmp):
        fleet_broker, fleet_transport = _load_fleet()

        # Hub-side legs first leg of the contract: the new stop_evidence
        # tool accepts a good payload and refuses a path-carrying detail
        # with the stable refusal shape — and the refusal stores nothing.
        good = _call(server, "stop_evidence", {
            "namespace": "user:global", "session_id": session,
            "count": 1,
            "details": [{"tool": "HubLegTool", "error": "boom"}],
        })
        assert isinstance(good, dict) and good.get("ok") is True, (
            f"a good stop_evidence payload must be accepted: {good!r}")
        assert good.get("recorded") is True, good
        refused = _call(server, "stop_evidence", {
            "namespace": "user:global", "session_id": session,
            "count": 1,
            "details": [{"error": "C:\\data\\x\\secret.txt leaked"}],
        })
        assert isinstance(refused, dict) and refused.get("ok") is False, (
            f"a path-shaped detail must be refused: {refused!r}")
        assert refused.get("error") == "path-shaped-field", refused

        # Client-side leg: the transcript with THREE failed calls (an
        # is_error tool_result, a sibling toolUseResult starting with
        # "Error", and a third is_error embedding raw Windows + POSIX
        # paths — the contract-v2 scrubbing probe), one SUCCEEDING call,
        # one >200-char error, one ghp_ token. mine.py's walker is
        # verified against exactly this fixture (3 details, newest-first;
        # both raw paths survive mining, so scrubbing is the broker's
        # duty) — see the check-author report.
        transcript = Path(tmp) / "transcript.jsonl"
        transcript.write_text("\n".join(_transcript_line(r) for r in (
            _assistant_tool_use("toolu_01", "FailingToolOne"),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_01",
                 "is_error": True, "content": long_error}]}},
            _assistant_tool_use("toolu_02", "FailingToolTwo"),
            {"type": "user",
             "toolUseResult": f"Error: disk full and {token} leaked",
             "message": {"content": [
                 {"type": "tool_result", "tool_use_id": "toolu_02",
                  "content": ""}]}},
            _assistant_tool_use("toolu_04", "FailingToolThree"),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_04",
                 "is_error": True, "content": path_error}]}},
            _assistant_tool_use("toolu_03", "OkTool"),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_03",
                 "content": "all good"}]}},
        )) + "\n", encoding="utf-8")

        recorder = _StopEvidenceRecorder()
        real_stop_evidence = fleet_transport.McpHttp.stop_evidence
        fleet_transport.McpHttp.stop_evidence = recorder
        # Remote mode ON so the broker actually addresses the hub; the
        # recorder intercepts the transport call, so the URL is never
        # contacted.
        os.environ["ZMEM_REMOTE_URL"] = "http://127.0.0.1:9/mcp"
        try:
            envelope = fleet_broker.handle_event("Stop", {
                "hook_event_name": "Stop", "stop_hook_active": False,
                "session_id": session,
                "transcript_path": str(transcript),
                "cwd": tmp,
            })
        finally:
            fleet_transport.McpHttp.stop_evidence = real_stop_evidence

        assert isinstance(envelope, dict), (
            f"handle_event must return the hook envelope dict, got: "
            f"{envelope!r}")
        assert isinstance(envelope.get("additionalContext", ""), str), (
            f"the envelope must carry a string additionalContext: "
            f"{envelope!r}")
        assert recorder.calls, (
            "the broker must send stop_evidence to the hub for a Stop "
            "event in remote mode (nothing was captured)")
        sent = recorder.calls[0]
        count = sent["kwargs"].get("count", sent["args"][0]
                                    if sent["args"] else None)
        assert count == 3, (
            f"exactly THREE failed tool calls must be counted (the "
            f"succeeding call never counts); sent count={count!r}, "
            f"payload={sent!r}")
        details = sent["kwargs"].get("details") or []
        assert isinstance(details, list) and len(details) <= 5, (
            f"details must be capped at 5 entries: {details!r}")
        for detail in details:
            for text in _iter_strings(detail):
                assert "\n" not in text and "  " not in text and "\t" not in text, (
                    f"detail text must be whitespace-collapsed: {text!r}")
                assert len(text) <= 200, (
                    f"detail text must be capped at 200 chars "
                    f"(got {len(text)}): {text[:80]!r}…")
        flat = "\n".join(_iter_strings(sent))
        assert token not in flat, (
            "the raw ghp_ token bytes must never reach the hub — the "
            "detail text must arrive redacted")
        assert win_path not in flat and posix_path not in flat, (
            "raw filesystem path bytes must never reach the hub — the "
            "detail text must arrive scrubbed (contract v2: "
            "redact_training_text-class path rewriting)")
        assert "[REDACTED_PATH]" in flat, (
            "the scrubbed detail must carry the [REDACTED_PATH] "
            "placeholder (or equivalent), proving the paths were "
            "rewritten rather than merely dropped")
        assert ("FailingToolOne" in flat and "FailingToolTwo" in flat
                and "FailingToolThree" in flat), (
            f"all three failed tools must be named in the evidence: "
            f"{flat[:200]!r}")
        assert "OkTool" not in flat, (
            "the SUCCEEDING call must not appear in the stop evidence")
        for text in _iter_strings(sent):
            assert not _is_path_shaped(text), (
                f"no string in the sent payload may be path-shaped: {text!r}")


# -- AC3 ----------------------------------------------------------------------

def test_subagent_stop_uses_agent_transcript():
    """issue #287 AC3: SubagentStop reads the agent transcript only."""
    session = f"t08-ac3-{uuid.uuid4().hex}"
    with _pinned_env("ac3") as tmp:
        fleet_broker, fleet_transport = _load_fleet()

        parent_transcript = Path(tmp) / "parent.jsonl"
        parent_transcript.write_text("\n".join(_transcript_line(r) for r in (
            _assistant_tool_use("parent_01", "ParentOnlyTool"),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "parent_01",
                 "is_error": True,
                 "content": "parent transcript failure"}]}},
        )) + "\n", encoding="utf-8")
        agent_transcript = Path(tmp) / "subagents" / "agent-42.jsonl"
        agent_transcript.parent.mkdir(parents=True, exist_ok=True)
        agent_transcript.write_text("\n".join(_transcript_line(r) for r in (
            _assistant_tool_use("agent_01", "AgentOnlyTool"),
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "agent_01",
                 "is_error": True,
                 "content": "agent transcript failure"}]}},
        )) + "\n", encoding="utf-8")

        recorder = _StopEvidenceRecorder()
        real_stop_evidence = fleet_transport.McpHttp.stop_evidence
        fleet_transport.McpHttp.stop_evidence = recorder
        os.environ["ZMEM_REMOTE_URL"] = "http://127.0.0.1:9/mcp"

        # Guard the PARENT transcript: the broker must never open it for a
        # SubagentStop event (agent_transcript_path is load-bearing).
        real_open = builtins.open
        parent_abs = os.path.abspath(str(parent_transcript))

        def guarded_open(file, *args, **kwargs):
            try:
                target = os.path.abspath(os.fspath(file))
            except TypeError:
                target = ""
            if target == parent_abs:
                raise AssertionError(
                    "the broker opened the PARENT transcript for a "
                    "SubagentStop event — agent_transcript_path is the "
                    "only readable transcript")
            return real_open(file, *args, **kwargs)

        builtins.open = guarded_open
        try:
            envelope = fleet_broker.handle_event("SubagentStop", {
                "hook_event_name": "SubagentStop", "agent_type": "coder",
                "agent_id": "agent-42",
                "agent_transcript_path": str(agent_transcript),
                "transcript_path": str(parent_transcript),
                "session_id": session, "cwd": tmp,
                "stop_hook_active": False,
            })
        finally:
            builtins.open = real_open
            fleet_transport.McpHttp.stop_evidence = real_stop_evidence

        assert isinstance(envelope, dict), (
            f"handle_event must return the hook envelope dict, got: "
            f"{envelope!r}")
        assert recorder.calls, (
            "the broker must send stop_evidence for a SubagentStop event "
            "in remote mode")
        sent = recorder.calls[0]
        flat = "\n".join(_iter_strings(sent))
        assert "AgentOnlyTool" in flat, (
            f"the evidence must come from the AGENT transcript: {flat[:200]!r}")
        assert "ParentOnlyTool" not in flat, (
            f"the PARENT transcript's failure must never leak into "
            f"subagent evidence: {flat[:200]!r}")
        agent_id = sent["kwargs"].get("agent_id")
        assert agent_id == "agent-42", (
            f"the evidence must be keyed by the payload agent_id "
            f"'agent-42', got: {agent_id!r}")


# -- AC4 ----------------------------------------------------------------------

def test_unreachable_hub_reason_is_distinct():
    """issue #287 AC4: an unreachable hub fails open, reason hub-unreachable."""
    session = f"t08-ac4-{uuid.uuid4().hex}"
    with _pinned_env("ac4") as tmp:
        fleet_broker, _fleet_transport = _load_fleet()

        # Probe pattern: bind an ephemeral port, then close it — nothing
        # listens there, so every hub call fails to connect.
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]
        probe.close()

        os.environ["ZMEM_REMOTE_URL"] = f"http://127.0.0.1:{dead_port}/mcp"
        os.environ["ZMEM_HERMES_DEADLINE_S"] = "1.5"
        broker_log = Path(tmp) / "broker-log.jsonl"
        started = time.perf_counter()
        envelope = fleet_broker.handle_event("UserPromptSubmit", {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "t08 unreachable-hub probe",
            "session_id": session,
            "transcript_path": os.path.join(tmp, "absent.jsonl"),
            "cwd": tmp,
        })
        elapsed = time.perf_counter() - started

        assert isinstance(envelope, dict), (
            f"a failing hub must still return the exit-safe envelope dict, "
            f"got: {envelope!r}")
        assert envelope.get("additionalContext", "") == "", (
            f"an unreachable hub must fail OPEN (empty additionalContext), "
            f"got: {envelope!r}")
        assert elapsed < _C4_MAX_ELAPSED_S, (
            f"the fail-open return must respect the deadline budget "
            f"(max {_C4_MAX_ELAPSED_S}s); measured {elapsed:.2f}s")
        assert broker_log.is_file(), (
            f"the broker must append its unreachable-hub decision line to "
            f"{broker_log} (data_dir = resolved store's parent)")
        reasons = []
        for line in broker_log.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                reasons.append(record.get("reason"))
        assert "hub-unreachable" in reasons, (
            f"the broker log must carry the DISTINCT unreachable reason "
            f"'hub-unreachable' (never 'empty-pool'); saw: {reasons!r}")
        assert "empty-pool" not in reasons, (
            f"an unreachable hub must never be reported as the honest "
            f"empty-store reason 'empty-pool'; saw: {reasons!r}")


# -- standalone runner (CI direct-execution lane) -------------------------------

_CHECKS = {
    "C2": test_stop_sends_extracted_failures_not_paths,
    "C3": test_subagent_stop_uses_agent_transcript,
    "C4": test_unreachable_hub_reason_is_distinct,
}


def _all_checks():
    return (
        test_stop_sends_extracted_failures_not_paths,
        test_subagent_stop_uses_agent_transcript,
        test_unreachable_hub_reason_is_distinct,
    )


def _main(argv: list[str]) -> int:
    if not MCP_AVAILABLE:
        print("SKIP: mcp package absent — tests/test_t08_broker_events.py "
              "requires the 'mcp' package "
              "(hermes-plugin/server/requirements.txt); not installed here.")
        return 0
    selectors = argv[1:]
    for selector in selectors:
        if selector not in _CHECKS:
            print(f"NO CHECK {selector}", file=sys.stderr)
            return 2
    selected = ([_CHECKS[s] for s in selectors] if selectors
                else list(_all_checks()))
    failures = []
    for check in selected:
        try:
            check()
            print(f"PASS {check.__name__}")
        except AssertionError as exc:
            failures.append((check.__name__, str(exc)))
            print(f"FAIL {check.__name__}: {exc}")
    if failures:
        print(f"{len(failures)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
