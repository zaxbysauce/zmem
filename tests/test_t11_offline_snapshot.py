"""Issue #290 (Workstream T PR 11) — declared offline mode: the labelled
broker snapshot. Frozen acceptance checks AC1-AC5. RED at base c729ffbb,
GREEN after the fix.

The fleet contract (docs/FLEET.md "Degrade, never block" matrix): when the
hub is unreachable, every Inject moment serves passive memory from a valid,
unexpired broker snapshot labelled ``[OFFLINE SNAPSHOT r<rev>, <age> old]``
(T-11); with no valid unexpired snapshot it injects nothing and records
``reason: "hub-unreachable"``. "Valid, unexpired" is settled by issue #290:
72 h expiry (``reason: "snapshot-expired"`` when the window is exceeded),
64 MB cap, owner-only file permissions, hourly + on-reconnect refresh,
scope limited to ``user:global`` plus authorized project namespaces touched
in the last 30 days, and a reconnect SUPERSEDES the snapshot rather than
merging with it. Every check names its standing at both ends:

- AC1 ``test_unreachable_hub_serves_labelled_snapshot`` — with
  ZMEM_REMOTE_URL at a bound-then-closed dead port and a valid unexpired
  snapshot (revision 1) at ``<data>/broker/snapshot.sqlite`` whose only
  distinctive row is a per-run canary that exists NOWHERE else,
  ``fleet.broker.handle_event("UserPromptSubmit", {...})`` must serve that
  row through the same selector code, and the rendered block must carry the
  broker-emitted label prefix ``[OFFLINE SNAPSHOT r1,`` (the revision from
  the snapshot metadata; the age text after the comma is advisory and not
  pinned). RED at base (the broker fails open with an empty
  additionalContext and reason "hub-unreachable" — no snapshot serve path
  exists), GREEN after the fix.
- AC2 ``test_expired_snapshot_is_never_served`` — a snapshot whose
  expires_at is in the past (issued 73 h ago, expired 1 h ago — the 72 h
  default window exceeded) serves NOTHING (no canary, no label in
  additionalContext) AND the broker log records the DISTINCT reason
  ``"snapshot-expired"``. The safety half passes at base (base serves
  nothing anyway); the reason assertion is the RED anchor — base logs
  ``"hub-unreachable"`` because the snapshot-expiry feature is missing.
  GREEN after the fix.
- AC3 ``test_refresh_applies_hub_revocations`` — after a snapshot at r1
  containing row X, the hub supersedes/purges X and the broker refreshes to
  r2; with the hub then unreachable, the served set comes from the
  REFRESHED snapshot: the label reads ``[OFFLINE SNAPSHOT r2,``, a
  surviving row is served, and the revoked row X is NOT served. The issue
  does not freeze the refresh mechanism, so the refresh is driven at the
  artifact level — r2 is built exactly the way r1 was, minus the revoked
  row (the issue's contract is that a refresh to r2 removes the row; the
  builder surface itself is exercised in AC5). RED at base (no label, no
  served rows — the serve path is missing), GREEN after the fix.
- AC4 ``test_connected_calls_never_read_snapshot`` — with a valid snapshot
  present AND the real hub reachable (tests/support/t08_hub.py child on an
  ephemeral port, seeded with a hub-only canary), a read moment returns the
  LIVE hub row set while the snapshot file is EXCLUSIVELY HELD by the test
  (sqlite3 connect + BEGIN EXCLUSIVE held open across the whole broker
  call — if the broker had tried to read the snapshot its connect/read
  would fail on the lock): the call still succeeds, the hub-only canary is
  served, and the snapshot-only canary is not. A second, offline control
  leg (dead port) proves the same snapshot IS valid and would be served
  offline — that leg is the RED anchor at base (empty context), so AC4 is
  RED at base for the missing-serve reason, GREEN after the fix.
- AC5 ``test_snapshot_scope_cap_and_permissions`` — the snapshot-BUILDING
  surface honors the contract. The builder is new surface and the issue
  does not freeze its seam, so the check capability-probes the tree
  (``fleet.snapshot``, then any ``fleet.*`` public callable whose name
  builds/refreshes a snapshot, matched against an ``inspect.signature``
  kwargs bank the way the repo's t-series probes audit new seams); at base
  NO seam exists and the check is RED for exactly that reason. Driving the
  discovered seam for a scoped principal (source rows in ``user:global``
  AND an unauthorized ``project:t11-denied`` namespace), the artifacts a
  correct implementation must produce are asserted: the snapshot file
  exists at the contract path, is a SQLite db whose memory table contains
  ONLY allowed-namespace rows (the denied canary never leaves), is <= 64
  MB, denies group/other on POSIX (on Windows os.stat owner bits are not a
  reliable ACL proxy, so the mode leg is POSIX-only), and the metadata JSON
  sidecar records issued_at/expires_at with a default window of exactly
  72 h (small slack).

C6 (the preserving distinct-reason check) already lives in
tests/test_t08_broker_events.py::test_unreachable_hub_reason_is_distinct
and is deliberately NOT duplicated here.

HOW SNAPSHOT FIXTURES COME TO EXIST (accepted test-authoring deviation for
a new-feature check, kept minimal and clearly marked): the issue does not
freeze the build mechanism, but the checks must create snapshots
deterministically. The helper ``_write_snapshot`` constructs the artifacts
A CORRECT IMPLEMENTATION WOULD PRODUCE: a REAL store-schema SQLite file at
``<data>/broker/snapshot.sqlite`` (seeded through the public ``store.py
add`` CLI into a scratch source store, WAL-checkpointed, single-file
copied — the schema the selector reads: ``memory`` + ``memory_fts`` +
``memory_expiry`` etc. all exist because the file IS a real store) plus
``<data>/broker/snapshot.meta.json`` carrying ``snapshot_revision``,
``issued_at``, ``expires_at`` (epoch seconds primary; ``*_iso`` twins
advisory). The helper NEVER imports the not-yet-existing ``fleet.snapshot``
module, so at base every check is RED because the SERVE path (or the
builder seam) is missing, never because the helper failed; a fixture
self-check re-opens the copy read-only and verifies every seeded row landed
(a helper bug fails loudly as a harness error, never masquerading as an
issue #290 verdict). At the fix tree the builder is capability-probed and
driven in AC5; the serve checks pin the CONTRACT ARTIFACTS (path, meta
keys, label) which the implementation's serve path must read regardless of
how it builds them — this keeps the check implementable both ways.

Env discipline (the t08/t10 pinning): every check pins a fresh GUID scratch
dir with ALL ambient ``ZMEM_*`` and both plugin-data channels stripped
BEFORE pinning only what the check needs (ZMEM_HOME=repo root, models off,
PYTHONIOENCODING=utf-8, the data dir via ZCODE_PLUGIN_DATA — remote-mode
legal, and deliberately NOT ZMEM_STORE/ZMEM_DATA, which issue #289 makes a
config error together with ZMEM_REMOTE_URL), purging cached
fleet/fleet.*/mcp_server/host/storelib modules on entry AND exit so
path/env-derived constants reload under the pin, and restoring everything
afterwards. Dead-port probes (bind, read port, close) make the hub
unreachable without a hub; the reachable-hub leg spawns the real t08 hub
child. Subprocess budgets are 30 s per spawn.

Runs under pytest (``python -m pytest tests/test_t11_offline_snapshot.py``)
and standalone (``python tests/test_t11_offline_snapshot.py`` — no argument
runs ALL checks, ``C1``..``C5`` run one, an unknown selector prints ``NO
CHECK <arg>`` to stderr and exits 2). AC4 skips loudly (visible message)
only when the ``mcp`` package is absent, which CI prevents by installing
hermes-plugin/server/requirements.txt.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib
import inspect
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
HUB_PY = REPO_ROOT / "tests" / "support" / "t08_hub.py"

# tests/support/t08_hub.py's fixed single-token auth (the hub accepts
# exactly this token; the broker sends it from ZMEM_MCP_TOKEN).
HUB_TOKEN = "t08-hub-token"

# The contract label prefix, pinned through the revision + comma (the age
# text after the comma is advisory — computed on a possibly skewed client
# clock, so the checks never pin it).
SNAPSHOT_LABEL_PREFIX = "[OFFLINE SNAPSHOT r{rev},"

# issue #290 AC2/AC5: the 72 h default expiry window (seconds).
SNAPSHOT_TTL_S = 72 * 3600.0

# AC5: the 64 MB snapshot cap (bytes).
SNAPSHOT_CAP_BYTES = 64 * 1024 * 1024

# AC5: the denied-namespace probe row lives in an unauthorized project
# namespace (shaped like the fleet fixture slugs, e.g. project:fixture-280).
DENIED_NAMESPACE = "project:t11-denied"

# Subprocess budget per store.py seeding spawn (cold interpreter + storelib
# import; empirically a few seconds, 30 s is the t-series ceiling).
_SUBPROC_TIMEOUT_S = 30

# Wall-clock guards: an offline serve is deadline-bounded (1.5 s probe
# deadline + snapshot read); a hub roundtrip rides the 6.0 s transport
# deadline. Generous upper bounds, harness sanity only.
_OFFLINE_CALL_BUDGET_S = 10.0
_HUB_CALL_BUDGET_S = 15.0
_HUB_HANDSHAKE_TIMEOUT_S = 120.0

# Modules whose cached state is env-derived (the broker's data dir resolves
# at call time through host.resolve_store_path) — purged on entry AND exit
# of every pinned context (the t06/t08/t09/t10 discipline).
_PURGE_EXACT = (
    "mcp_server", "fleet", "host", "redaction", "schema_meta", "store",
    "auth", "bind_guard",
)
_PURGE_PREFIXES = ("fleet.", "storelib.", "store.", "auth.", "bind_guard.")


def _purge_cached_modules() -> None:
    """Drop env-derived module singletons so no sibling check inherits a
    data dir pinned under a previous scratch."""
    doomed = [name for name in sys.modules
              if name in _PURGE_EXACT or name.startswith(_PURGE_PREFIXES)]
    for name in doomed:
        del sys.modules[name]


def _strip_config_env(target: dict) -> None:
    """Remove every ambient var the store-resolution / remote-mode chain
    reads (the full ZMEM_* prefix sweep plus BOTH plugin-data channels)."""
    for key in list(target):
        if key.startswith("ZMEM_") or key in ("CLAUDE_PLUGIN_DATA",
                                              "ZCODE_PLUGIN_DATA"):
            del target[key]


@contextlib.contextmanager
def _pinned_env(prefix: str):
    """Fresh GUID scratch + pinned env (the t08/t10 discipline, no server).

    The data dir resolves through ZCODE_PLUGIN_DATA (remote-mode legal —
    issue #289 forbids ZMEM_STORE/ZMEM_DATA together with ZMEM_REMOTE_URL),
    so the broker's data dir is the scratch and the snapshot artifacts live
    at ``<scratch>/broker/snapshot.sqlite``. ZMEM_NAMESPACE pins the
    client-side namespace deterministically (broker._namespace honors it
    first). Cached fleet/storelib/host modules are purged on entry AND exit.
    """
    tmp = tempfile.mkdtemp(prefix=f"zmem-t11-{prefix}-{uuid.uuid4().hex}-")
    saved = {k: v for k, v in os.environ.items()
             if k.startswith("ZMEM_") or k in ("CLAUDE_PLUGIN_DATA",
                                               "ZCODE_PLUGIN_DATA")}
    for key in list(saved):
        os.environ.pop(key, None)
    os.environ.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_MODELS_DIR": os.path.join(tmp, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZCODE_PLUGIN_DATA": tmp,
        "ZMEM_NAMESPACE": "user:global",
        "ZMEM_MCP_TOKEN": HUB_TOKEN,
    })
    _purge_cached_modules()
    try:
        yield tmp
    finally:
        _purge_cached_modules()
        shutil.rmtree(tmp, ignore_errors=True)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _tail(text: str, limit: int = 400) -> str:
    return (text or "").strip()[-limit:]


def _mcp_available() -> bool:
    try:
        import mcp  # noqa: F401
        return True
    except ImportError:  # pragma: no cover - CI installs requirements.txt
        return False


def _dead_port_url() -> str:
    """A bound-then-closed loopback port (the t08/t10 dead-port probe):
    nothing listens, so every hub contact fails to connect."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}/mcp"


def _seed_store(source_dir: str, rows: list[tuple[str, str]]) -> str:
    """Seed a REAL local store via the public store.py add CLI (one spawn
    per row) and return the store path.

    The seeded rows are what a correct snapshot builder would copy: real
    ``memory`` rows (FTS-indexed, confidence 0.9 so every recall lane can
    see them — the t08_hub seeding note), in the caller's namespaces. A
    WAL checkpoint quiesces the source into ONE self-contained sqlite file
    before anyone copies it. A seeding failure is a TEST HARNESS error and
    is raised as such (never an issue #290 verdict).
    """
    os.makedirs(source_dir, exist_ok=True)
    store_path = os.path.join(source_dir, "store.sqlite")
    env = {k: v for k, v in os.environ.items()}
    _strip_config_env(env)
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_STORE": store_path,
        "ZMEM_DATA": source_dir,
        "ZMEM_MODELS_DIR": os.path.join(source_dir, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
    })
    for namespace, content in rows:
        proc = subprocess.run(
            [sys.executable, str(STORE_PY), "add",
             "--namespace", namespace, "--type", "fact",
             "--content", content, "--confidence", "0.9", "--json"],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL, timeout=_SUBPROC_TIMEOUT_S)
        if proc.returncode != 0:
            raise AssertionError(
                f"TEST HARNESS failure (not an issue #290 verdict): the "
                f"store.py add seeding subprocess failed with rc="
                f"{proc.returncode}; stderr={_tail(proc.stderr)!r}")
    conn = sqlite3.connect(store_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return store_path


def _snapshot_paths(data_dir: str) -> tuple[Path, Path]:
    """The contract artifact paths under the broker's data dir."""
    broker_dir = Path(data_dir) / "broker"
    return broker_dir / "snapshot.sqlite", broker_dir / "snapshot.meta.json"


def _iso(ts: float) -> str:
    return datetime.datetime.fromtimestamp(
        ts, datetime.timezone.utc).isoformat()


def _parse_ts(value) -> "float | None":
    """Lenient timestamp parse (epoch number or ISO 8601 string)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
        try:
            return datetime.datetime.fromisoformat(
                value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _write_snapshot(data_dir: str, revision: int,
                    rows: list[tuple[str, str]], *,
                    issued_at: "float | None" = None,
                    expires_at: "float | None" = None) -> tuple[Path, Path]:
    """TEST FIXTURE HELPER (clearly marked; see the module docstring).

    Constructs the snapshot artifacts A CORRECT IMPLEMENTATION WOULD
    PRODUCE — the issue does not freeze the build mechanism, and this
    helper is the accepted deterministic stand-in: it seeds a REAL
    store-schema SQLite through the public store.py add CLI (so every
    table the selector reads exists and is populated) and installs it at
    ``<data>/broker/snapshot.sqlite``, plus the metadata sidecar
    ``snapshot.meta.json`` carrying snapshot_revision / issued_at /
    expires_at (epoch seconds primary, ``*_iso`` twins advisory; the
    default expiry is the 72 h contract window). The helper NEVER imports
    the not-yet-existing fleet.snapshot module. A fixture self-check
    re-opens the installed copy read-only and verifies every seeded row
    landed, so a helper bug fails loudly as a harness error and never
    masquerades as an issue #290 verdict. At the fix tree the builder
    surface itself is capability-probed and driven in AC5; the serve
    checks (AC1-AC4) pin these CONTRACT ARTIFACTS, which the
    implementation's serve path must read regardless of how it builds
    them.
    """
    source_dir = os.path.join(data_dir, f"snapshot-src-r{revision}")
    store_path = _seed_store(source_dir, rows)
    snap_path, meta_path = _snapshot_paths(data_dir)
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(store_path, snap_path)
    issued = time.time() if issued_at is None else float(issued_at)
    expires = (issued + SNAPSHOT_TTL_S if expires_at is None
               else float(expires_at))
    meta = {
        "snapshot_revision": int(revision),
        "issued_at": issued,
        "expires_at": expires,
        "issued_at_iso": _iso(issued),
        "expires_at_iso": _iso(expires),
    }
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    conn = sqlite3.connect(f"file:{snap_path}?mode=ro", uri=True)
    try:
        found = {row[0] for row in conn.execute("SELECT content FROM memory")}
    finally:
        conn.close()
    for _namespace, content in rows:
        assert content in found, (
            f"TEST HARNESS failure (not an issue #290 verdict): the "
            f"snapshot fixture at {snap_path} is missing its seeded row "
            f"{content[:60]!r} — the copy/checkpoint step dropped it")
    return snap_path, meta_path


def _load_broker():
    """Import fleet.broker under the pinned env (the t08 pattern)."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import fleet.broker  # noqa: F401
        return fleet.broker
    except Exception:  # noqa: BLE001 - defensive: surface a clean FAIL
        traceback.print_exc()
        raise AssertionError(
            "TEST HARNESS failure (not an issue #290 verdict): "
            "fleet.broker did not import under the pinned env") from None


def _broker_reasons(data_dir: str) -> list:
    """Every ``reason`` value in the broker's decision log (t08 AC4)."""
    log_path = Path(data_dir) / "broker-log.jsonl"
    reasons: list = []
    if not log_path.is_file():
        return reasons
    for line in log_path.read_text(encoding="utf-8",
                                   errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            reasons.append(record.get("reason"))
    return reasons


def _prompt_event(query: str, data_dir: str) -> dict:
    """A minimal UserPromptSubmit payload (the t08 AC4 shape)."""
    return {
        "hook_event_name": "UserPromptSubmit",
        "prompt": query,
        "session_id": f"t11-{uuid.uuid4().hex}",
        "transcript_path": os.path.join(data_dir, "absent.jsonl"),
        "cwd": data_dir,
    }


def _canary(tag: str, topic: str) -> tuple[str, str]:
    """A (content, matching-query) pair whose content exists ONLY in the
    fixture that seeded it (per-run unique marker + rare topic terms so
    the lexical selector surfaces it)."""
    marker = f"t11-{tag}-{uuid.uuid4().hex[:10]}"
    content = (f"{marker}: the {topic} zirconia windlass calibration "
               f"binder needs nightly torquing before dawn inspection")
    query = f"{topic} zirconia windlass calibration binder torquing"
    return content, query


class _Hub:
    """The real MCP hub (tests/support/t08_hub.py) as a child process, per
    the tests/test_t10_cli_remote.py consumer pattern: spawn with stdin
    held open, poll the ONE ``T08_HUB <port>`` handshake line, shut down
    by closing stdin (then killing, belt and suspenders). The hub store is
    PRE-SEEDED by the caller, so no --seed replay is needed."""

    def __init__(self, hub_dir: str):
        self._hub_dir = hub_dir
        self.url = ""
        self._proc: "subprocess.Popen | None" = None
        self._out_path = os.path.join(hub_dir, "hub-stdout.log")
        self._err_path = os.path.join(hub_dir, "hub-stderr.log")

    def __enter__(self) -> "_Hub":
        Path(self._hub_dir).mkdir(parents=True, exist_ok=True)
        env = {k: v for k, v in os.environ.items()}
        _strip_config_env(env)  # the hub pins its own store; start clean
        out = open(self._out_path, "ab")
        err = open(self._err_path, "ab")
        try:
            self._proc = subprocess.Popen(
                [sys.executable, str(HUB_PY),
                 "--store", os.path.join(self._hub_dir, "store.sqlite"),
                 "--namespace", "user:global"],
                stdin=subprocess.PIPE, stdout=out, stderr=err,
                env=env, cwd=str(REPO_ROOT))
        finally:
            out.close()
            err.close()

        handshake = ""
        deadline = time.monotonic() + _HUB_HANDSHAKE_TIMEOUT_S
        while time.monotonic() < deadline:
            text = ""
            if os.path.exists(self._out_path):
                text = Path(self._out_path).read_text(
                    encoding="utf-8", errors="replace")
            nl = text.find("\n")
            if nl >= 0:
                handshake = text[:nl].strip()
                break
            if self._proc.poll() is not None:
                break
            time.sleep(0.1)
        if not re.match(r"^T08_HUB \d+$", handshake or ""):
            stderr_tail = ""
            if os.path.exists(self._err_path):
                stderr_tail = _tail(Path(self._err_path).read_text(
                    encoding="utf-8", errors="replace"), 800)
            self._shutdown()
            raise AssertionError(
                f"TEST HARNESS failure (not an issue #290 verdict): the "
                f"t08 hub helper never reached its T08_HUB handshake "
                f"(got {handshake!r}; hub exit="
                f"{self._proc.returncode if self._proc else '?'}; "
                f"stderr tail: {stderr_tail!r})")
        port = handshake.split(" ")[1]
        self.url = f"http://127.0.0.1:{port}/mcp"
        return self

    def _shutdown(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass

    def __exit__(self, *exc) -> None:
        self._shutdown()


# --- the T-11 snapshot-builder capability probe (AC5) ----------------------

_BUILDER_MODULE_NAMES = ("snapshot", "broker", "transport", "opqueue",
                         "broker_cli")


def _discover_snapshot_builder():
    """Capability-probe the tree for the T-11 snapshot-builder seam.

    The issue does not freeze the mechanism, so the probe looks for
    whatever public surface a correct implementation would expose: the
    natural ``fleet.snapshot`` module first, then every curated ``fleet.*``
    module's PUBLIC callables whose name mentions building/refreshing a
    snapshot (name scoring prefers build > refresh). Returns
    ``(qualname, callable)`` for the best candidate or ``None`` when no
    seam exists anywhere — which is exactly the base tree (the fleet
    package carries zero snapshot surface today)."""
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    modules: list[tuple[str, object]] = []
    for name in _BUILDER_MODULE_NAMES:
        try:
            module = importlib.import_module(f"fleet.{name}")
        except ImportError:
            continue
        modules.append((f"fleet.{name}", module))
    candidates: list[tuple[int, str, object]] = []
    for module_name, module in modules:
        for attr in dir(module):
            lowered = attr.lower()
            if attr.startswith("_") or "snapshot" not in lowered:
                continue
            func = getattr(module, attr)
            if (not callable(func) or inspect.isclass(func)
                    or inspect.ismodule(func)):
                continue
            score = 0
            if "build" in lowered:
                score = 3
            elif "refresh" in lowered:
                score = 2
            elif "write" in lowered or "create" in lowered:
                score = 1
            candidates.append((score, f"{module_name}.{attr}", func))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1], candidates[0][2]


def _builder_kwargs(data_dir: str, source_store: str,
                    snap_path: Path) -> dict:
    """The inspect.signature kwargs bank for driving the discovered
    builder: only the params the seam actually declares are passed (the
    repo's capability-probe discipline — audit the signature, map by
    name, never guess positionally)."""
    env = {k: v for k, v in os.environ.items()}
    return {
        # where the snapshot artifact must land (the contract path)
        "data_dir": data_dir,
        "snapshot_dir": str(Path(data_dir) / "broker"),
        "snapshot_path": str(snap_path),
        "path": str(snap_path),
        "out_path": str(snap_path),
        "dest": str(snap_path),
        # the scoped principal's source rows
        "store_path": source_store,
        "store": source_store,
        "source_store": source_store,
        "source": source_store,
        # the T-04 selection/authorization scope (issue #290 AC5)
        "namespace": "user:global",
        "namespaces": frozenset({"user:global"}),
        "authorized_namespaces": frozenset({"user:global"}),
        "principal": "local",
        "env": env,
        "revision": 1,
        "snapshot_revision": 1,
    }


# -- AC1 ----------------------------------------------------------------------

def test_unreachable_hub_serves_labelled_snapshot():
    """issue #290 AC1: an unreachable hub serves the labelled snapshot."""
    content, query = _canary("ac1", "ceramic")
    with _pinned_env("ac1") as data_dir:
        snap_path, _meta_path = _write_snapshot(data_dir, 1,
                                                [("user:global", content)])
        fleet_broker = _load_broker()

        os.environ["ZMEM_REMOTE_URL"] = _dead_port_url()
        os.environ["ZMEM_HERMES_DEADLINE_S"] = "1.5"
        started = time.perf_counter()
        envelope = fleet_broker.handle_event(
            "UserPromptSubmit", _prompt_event(query, data_dir))
        elapsed = time.perf_counter() - started

        assert isinstance(envelope, dict), (
            f"handle_event must return the exit-safe envelope dict, got: "
            f"{envelope!r}")
        ctx = envelope.get("additionalContext", "")
        assert isinstance(ctx, str), (
            f"the envelope must carry a string additionalContext: "
            f"{envelope!r}")
        # (a) THE RED ANCHOR: a row that exists ONLY in the snapshot must
        # be served by the same selector code — at base the context is
        # empty (fail-open, reason hub-unreachable) and this names the
        # missing serve path.
        assert content in ctx, (
            f"issue #290 AC1 (T-11 offline snapshot serve): with the hub "
            f"unreachable and a valid unexpired snapshot at {snap_path} "
            f"(revision 1; the fixture self-check verified the canary row "
            f"is in the file, and it exists NOWHERE else), the broker "
            f"served NO snapshot rows — the offline snapshot serve path "
            f"is missing at this tree; additionalContext={ctx[:200]!r}")
        # (b) the broker-emitted label, pinned through the revision + comma
        # (the age text after the comma is advisory and never pinned).
        assert SNAPSHOT_LABEL_PREFIX.format(rev=1) in ctx, (
            f"issue #290 AC1: the snapshot-served block must be prefixed "
            f"with the label {SNAPSHOT_LABEL_PREFIX.format(rev=1)!r} (rev "
            f"from the snapshot metadata; the <age> text is advisory); saw "
            f"additionalContext={ctx[:200]!r}")
        assert elapsed < _OFFLINE_CALL_BUDGET_S, (
            f"the offline serve must stay inside the deadline budget; "
            f"measured {elapsed:.2f}s")


# -- AC2 ----------------------------------------------------------------------

def test_expired_snapshot_is_never_served():
    """issue #290 AC2: an expired snapshot serves nothing and the broker
    logs the distinct reason snapshot-expired."""
    content, query = _canary("ac2", "palladium")
    with _pinned_env("ac2") as data_dir:
        # The 72 h default window far exceeded: issued 73 h ago, expired
        # 1 h ago.
        _write_snapshot(data_dir, 1, [("user:global", content)],
                        issued_at=time.time() - (SNAPSHOT_TTL_S + 3600.0),
                        expires_at=time.time() - 3600.0)
        fleet_broker = _load_broker()

        os.environ["ZMEM_REMOTE_URL"] = _dead_port_url()
        os.environ["ZMEM_HERMES_DEADLINE_S"] = "1.5"
        envelope = fleet_broker.handle_event(
            "UserPromptSubmit", _prompt_event(query, data_dir))
        assert isinstance(envelope, dict), (
            f"handle_event must return the exit-safe envelope dict, got: "
            f"{envelope!r}")
        ctx = envelope.get("additionalContext", "")

        # The safety property first: an expired snapshot is NEVER served.
        assert content not in ctx, (
            f"issue #290 AC2 SAFETY VIOLATION: an expired snapshot (72 h "
            f"window exceeded) must never be served, but its canary row "
            f"reached additionalContext: {ctx[:200]!r}")
        assert "[OFFLINE SNAPSHOT" not in ctx, (
            f"issue #290 AC2 SAFETY VIOLATION: the offline-snapshot label "
            f"must never appear for an expired snapshot; saw "
            f"additionalContext={ctx[:200]!r}")
        # THE RED ANCHOR: the distinct expiry reason. At base the broker
        # logs only "hub-unreachable" — the snapshot-expiry feature is
        # the missing piece this check names.
        reasons = _broker_reasons(data_dir)
        assert "snapshot-expired" in reasons, (
            f"issue #290 AC2: with only an EXPIRED snapshot present and "
            f"the hub unreachable, the broker log must record the DISTINCT "
            f"reason \"snapshot-expired\" — the snapshot-expiry feature "
            f"is missing at this tree; broker-log reasons: {reasons!r}")


# -- AC3 ----------------------------------------------------------------------

def test_refresh_applies_hub_revocations():
    """issue #290 AC3: a refresh to r2 removes the hub-revoked row and the
    offline serve reads the refreshed snapshot."""
    revoked_content, _revoked_query = _canary("ac3-revoked", "meridian")
    survivor_content, survivor_query = _canary("ac3-keep", "quartz")
    with _pinned_env("ac3") as data_dir:
        # r1: the pre-revocation snapshot holds BOTH rows.
        _write_snapshot(data_dir, 1, [
            ("user:global", revoked_content),
            ("user:global", survivor_content),
        ])
        # The refresh: the hub superseded (or purged) the revoked row, and
        # a reconnect SUPERSEDES the snapshot rather than merging with it
        # (docs/FLEET.md). The issue does not freeze the refresh
        # mechanism, so the check drives it at the artifact level — r2 is
        # built exactly the way r1 was, minus the revoked row (the issue's
        # contract is that a refresh to r2 removes the row; the builder
        # seam itself is exercised in AC5).
        _write_snapshot(data_dir, 2, [("user:global", survivor_content)])
        snap_path, meta_path = _snapshot_paths(data_dir)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta.get("snapshot_revision") == 2, (
            f"TEST HARNESS failure (not an issue #290 verdict): the r2 "
            f"fixture meta must carry snapshot_revision 2, got {meta!r}")

        fleet_broker = _load_broker()
        os.environ["ZMEM_REMOTE_URL"] = _dead_port_url()
        os.environ["ZMEM_HERMES_DEADLINE_S"] = "1.5"
        envelope = fleet_broker.handle_event(
            "UserPromptSubmit", _prompt_event(survivor_query, data_dir))
        assert isinstance(envelope, dict), (
            f"handle_event must return the exit-safe envelope dict, got: "
            f"{envelope!r}")
        ctx = envelope.get("additionalContext", "")

        # THE RED ANCHOR: the serve must read the REFRESHED snapshot —
        # at base no serve path exists and the label is absent entirely.
        assert SNAPSHOT_LABEL_PREFIX.format(rev=2) in ctx, (
            f"issue #290 AC3 (refresh applies hub revocations): after the "
            f"refresh to r2 at {snap_path} and with the hub unreachable, "
            f"the serve must carry the label "
            f"{SNAPSHOT_LABEL_PREFIX.format(rev=2)!r} — the offline "
            f"snapshot serve path is missing at this tree; saw "
            f"additionalContext={ctx[:200]!r}")
        assert survivor_content in ctx, (
            f"issue #290 AC3: a row that SURVIVED the hub revocation must "
            f"still be served from the refreshed r2 snapshot; saw "
            f"additionalContext={ctx[:200]!r}")
        assert revoked_content not in ctx, (
            f"issue #290 AC3: the hub-REVOKED row must NOT be served "
            f"after the refresh to r2 removed it (a reconnect supersedes, "
            f"never merges); saw additionalContext={ctx[:200]!r}")


# -- AC4 ----------------------------------------------------------------------

def test_connected_calls_never_read_snapshot():
    """issue #290 AC4: a connected read moment returns the live hub row
    set and never opens the snapshot (proved by an exclusive hold)."""
    if not _mcp_available():  # pragma: no cover - CI installs requirements
        print("SKIP: mcp package absent — test_connected_calls_never_read_"
              "snapshot needs the real hub (hermes-plugin/server/"
              "requirements.txt)")
        return
    snap_content, snap_query = _canary("ac4-snap", "obsidian")
    hub_content, hub_query = _canary("ac4-hub", "tungsten")
    hub_root = tempfile.mkdtemp(prefix=f"zmem-t11-ac4hub-{uuid.uuid4().hex}-")
    with _pinned_env("ac4") as data_dir:
        try:
            # The client snapshot (r1) holds a row that exists ONLY in the
            # snapshot; the hub store holds a DIFFERENT row that exists
            # only hub-side.
            snap_path, _meta_path = _write_snapshot(
                data_dir, 1, [("user:global", snap_content)])
            hub_dir = os.path.join(hub_root, "hub")
            _seed_store(hub_dir, [("user:global", hub_content)])
            with _Hub(hub_dir) as hub:
                fleet_broker = _load_broker()

                # LEG 1 — connected: hold the snapshot EXCLUSIVELY open
                # (BEGIN EXCLUSIVE) across the whole broker call. If the
                # broker had tried to read the snapshot, its connect/read
                # would fail or block on the lock; the call must still
                # succeed with the LIVE hub row set.
                hold = sqlite3.connect(str(snap_path), timeout=0.5)
                envelope = None
                try:
                    hold.execute("BEGIN EXCLUSIVE")
                    os.environ["ZMEM_REMOTE_URL"] = hub.url
                    started = time.perf_counter()
                    envelope = fleet_broker.handle_event(
                        "UserPromptSubmit",
                        _prompt_event(hub_query, data_dir))
                    elapsed = time.perf_counter() - started
                finally:
                    hold.rollback()
                    hold.close()
                assert isinstance(envelope, dict), (
                    f"a hub-connected read moment must return the exit-safe "
                    f"envelope dict even while the snapshot is exclusively "
                    f"held, got: {envelope!r}")
                ctx = envelope.get("additionalContext", "")
                assert hub_content in ctx, (
                    f"issue #290 AC4: with the hub reachable the read "
                    f"moment must return the LIVE hub row set (the "
                    f"hub-only canary) — the snapshot was exclusively "
                    f"held for the whole call, so a snapshot read would "
                    f"have failed on the lock; saw "
                    f"additionalContext={ctx[:200]!r}")
                assert snap_content not in ctx, (
                    f"issue #290 AC4: a hub-connected read moment must "
                    f"never serve snapshot-only rows; saw "
                    f"additionalContext={ctx[:200]!r}")
                assert elapsed < _HUB_CALL_BUDGET_S, (
                    f"the connected call must stay inside the transport "
                    f"deadline budget; measured {elapsed:.2f}s")

                # LEG 2 — offline control: the SAME snapshot (hold now
                # released) must be valid and servable offline. This leg
                # is the RED anchor at base: it proves leg 1's emptiness
                # of snapshot rows was a CHOICE (connected), not an
                # invalid fixture — and at base it fails because no
                # serve path exists.
                os.environ["ZMEM_REMOTE_URL"] = _dead_port_url()
                os.environ["ZMEM_HERMES_DEADLINE_S"] = "1.5"
                envelope2 = fleet_broker.handle_event(
                    "UserPromptSubmit",
                    _prompt_event(snap_query, data_dir))
                assert isinstance(envelope2, dict), (
                    f"handle_event must return the exit-safe envelope "
                    f"dict, got: {envelope2!r}")
                ctx2 = envelope2.get("additionalContext", "")
                assert snap_content in ctx2, (
                    f"issue #290 AC4 offline control leg: the same "
                    f"snapshot is valid and unexpired, so with the hub "
                    f"unreachable it MUST be served — the offline "
                    f"snapshot serve path is missing at this tree (leg 1 "
                    f"proved the fixture was withheld only because the "
                    f"hub was reachable); saw "
                    f"additionalContext={ctx2[:200]!r}")
                assert SNAPSHOT_LABEL_PREFIX.format(rev=1) in ctx2, (
                    f"issue #290 AC4 offline control leg: the served block "
                    f"must carry the label "
                    f"{SNAPSHOT_LABEL_PREFIX.format(rev=1)!r}; saw "
                    f"additionalContext={ctx2[:200]!r}")
        finally:
            shutil.rmtree(hub_root, ignore_errors=True)


# -- AC5 ----------------------------------------------------------------------

def test_snapshot_scope_cap_and_permissions():
    """issue #290 AC5: the snapshot builder honors scope, cap, permissions
    and the 72 h expiry window."""
    with _pinned_env("ac5") as data_dir:
        # THE RED ANCHOR: capability-probe the tree for the builder seam —
        # at base the fleet package carries ZERO snapshot surface.
        seam = _discover_snapshot_builder()
        assert seam is not None, (
            "issue #290 AC5: no snapshot-builder seam exists anywhere in "
            "the fleet package (probed fleet.snapshot plus every curated "
            "fleet.* module's public callables whose name builds or "
            "refreshes a snapshot, capability-matched via "
            "inspect.signature) — the T-11 snapshot builder surface is "
            "missing at this tree")
        qualname, builder = seam

        # A source store holding one ALLOWED row (user:global) and one
        # DENIED row (an unauthorized project namespace, touched now — so
        # only the authorization scope, never recency, can exclude it).
        allowed_content, _allowed_query = _canary("ac5-allowed", "cobalt")
        denied_content, _denied_query = _canary("ac5-denied", "nickel")
        source_dir = os.path.join(data_dir, "builder-source")
        store_path = _seed_store(source_dir, [
            ("user:global", allowed_content),
            (DENIED_NAMESPACE, denied_content),
        ])
        snap_path, meta_path = _snapshot_paths(data_dir)

        # Drive the seam with only the params its signature declares (the
        # capability-probe discipline); stale artifacts are removed first
        # so existence proves the builder wrote them.
        bank = _builder_kwargs(data_dir, store_path, snap_path)
        params = inspect.signature(builder).parameters
        call_kwargs = {name: bank[name] for name in params
                       if name in bank}
        uncovered = [name for name, param in params.items()
                     if param.default is inspect.Parameter.empty
                     and param.kind in (
                         inspect.Parameter.POSITIONAL_OR_KEYWORD,
                         inspect.Parameter.KEYWORD_ONLY)
                     and name not in call_kwargs
                     and param.kind != inspect.Parameter.VAR_POSITIONAL]
        assert not uncovered, (
            f"issue #290 AC5: the discovered builder seam {qualname} "
            f"requires parameters the capability probe cannot supply "
            f"({uncovered!r}); signature: "
            f"{str(inspect.signature(builder))[:300]}")
        snap_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        try:
            builder(**call_kwargs)
        except Exception as exc:  # noqa: BLE001 - loud, never silent
            traceback.print_exc()
            raise AssertionError(
                f"issue #290 AC5: the discovered snapshot builder "
                f"{qualname} raised while building for a scoped principal "
                f"({type(exc).__name__}: {exc}) — the builder surface "
                f"exists but is not drivable through its declared "
                f"signature") from None

        # Artifact 1: the snapshot file exists at the contract path and is
        # a readable SQLite db.
        assert snap_path.is_file(), (
            f"issue #290 AC5: building through {qualname} must produce the "
            f"snapshot at the contract path {snap_path} (the same path "
            f"AC1-AC4's serve reads); the file is absent")
        conn = sqlite3.connect(f"file:{snap_path}?mode=ro", uri=True)
        try:
            namespaces = {row[0] for row in conn.execute(
                "SELECT DISTINCT namespace FROM memory")}
            contents = {row[0] for row in conn.execute(
                "SELECT content FROM memory")}
        finally:
            conn.close()
        assert allowed_content in contents, (
            f"issue #290 AC5: the allowed user:global row must be present "
            f"in the built snapshot; memory contents: "
            f"{sorted(c[:60] for c in contents)!r}")
        assert denied_content not in contents, (
            f"issue #290 AC5 SCOPE VIOLATION: the snapshot's memory table "
            f"must contain ONLY rows from user:global and authorized "
            f"namespaces touched in the last 30 days — the unauthorized "
            f"{DENIED_NAMESPACE} canary row left the hub in the snapshot")
        assert namespaces <= {"user:global"}, (
            f"issue #290 AC5 SCOPE VIOLATION: every namespace in the "
            f"snapshot's memory table must be in the authorized scope; "
            f"saw: {sorted(namespaces)!r}")

        # Artifact 2: the 64 MB cap.
        size = snap_path.stat().st_size
        assert size <= SNAPSHOT_CAP_BYTES, (
            f"issue #290 AC5 CAP VIOLATION: the snapshot is {size} bytes "
            f"(cap {SNAPSHOT_CAP_BYTES})")

        # Artifact 3: owner-only file permissions (POSIX; on Windows the
        # os.stat owner bits are not a reliable ACL proxy, so this leg is
        # POSIX-only by design and the meta-sidecar leg below carries the
        # Windows check).
        if os.name != "nt":
            mode = snap_path.stat().st_mode & 0o777
            assert mode & 0o077 == 0, (
                f"issue #290 AC5 PERMISSION VIOLATION: the snapshot file "
                f"must deny group/other (owner-only); mode "
                f"{oct(mode)} on {snap_path}")

        # Artifact 4: the metadata sidecar records issued_at/expires_at
        # with the 72 h default window (small slack for clock/format
        # rounding).
        assert meta_path.is_file(), (
            f"issue #290 AC5: the snapshot metadata sidecar must exist at "
            f"{meta_path} (carrying snapshot_revision, issued_at, "
            f"expires_at)")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        issued = _parse_ts(meta.get("issued_at"))
        expires = _parse_ts(meta.get("expires_at"))
        assert issued is not None and expires is not None, (
            f"issue #290 AC5: the snapshot metadata sidecar must record "
            f"parseable issued_at and expires_at (epoch seconds or ISO "
            f"8601); got: issued_at={meta.get('issued_at')!r} "
            f"expires_at={meta.get('expires_at')!r}")
        delta = expires - issued
        assert (SNAPSHOT_TTL_S - 600.0) <= delta <= (SNAPSHOT_TTL_S + 600.0), (
            f"issue #290 AC5 EXPIRY VIOLATION: the default snapshot window "
            f"must be exactly 72 h (expires_at - issued_at = "
            f"{delta / 3600.0:.3f} h, allowed "
            f"{(SNAPSHOT_TTL_S - 600.0) / 3600.0:.3f}.."
            f"{(SNAPSHOT_TTL_S + 600.0) / 3600.0:.3f} h)")


# -- standalone runner (CI direct-execution lane) ------------------------------

_CHECKS = {
    "C1": test_unreachable_hub_serves_labelled_snapshot,
    "C2": test_expired_snapshot_is_never_served,
    "C3": test_refresh_applies_hub_revocations,
    "C4": test_connected_calls_never_read_snapshot,
    "C5": test_snapshot_scope_cap_and_permissions,
}


def _all_checks():
    return tuple(_CHECKS[selector] for selector in ("C1", "C2", "C3",
                                                    "C4", "C5"))


def _main(argv: list[str]) -> int:
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
