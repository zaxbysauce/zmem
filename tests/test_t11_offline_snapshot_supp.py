"""Issue #290 (T-11) supplementary pins — the supp layer over the frozen
checks (tests/test_t11_offline_snapshot.py, C1-C6).

Load-bearing pins (the fix plan item 7):

- S1  test_reconnect_transition_refreshes_and_clears_stamp — the
  SUBPROCESS-driven reconnect transition: an offline broker_cli moment
  writes the persisted stamp; a subsequent connected moment refreshes the
  snapshot from the hub and clears the stamp (the stamp is the only
  cross-moment memory — every broker moment is a fresh subprocess).
- S2  test_connected_moment_never_writes_stamp — a connected moment on a
  fresh scratch never creates the stamp (the C4 exclusive-hold proof
  relies on this).
- S3  test_failed_refresh_leaves_stamp_armed — a failed refresh keeps the
  stamp (skip re-arms).
- S4  test_refresh_cadence_gate — a fresh snapshot + no stamp never calls
  the hub (hourly cadence).
- S5  test_cap_refusal_keeps_previous_pair — a build over the (patched,
  small) cap raises and the previous pair survives.
- S6  test_dual_scope_filter_excludes_dormant — an authorized namespace
  with no 30-day activity never leaves the builder; user:global always
  does.
- S7  test_label_never_forged_by_row_content — a row whose CONTENT
  carries the label text cannot forge a second header line.
- S8  test_cli_offline_envelope_key_set — recall over a dead port with a
  valid snapshot prints the live envelope keys + exactly one additive
  ``offline_snapshot`` key; stdout stays one JSON object; stderr carries
  the note.
- S9  test_cli_expired_keeps_error_contract — an EXPIRED snapshot keeps
  the plain hub-unreachable error (the disclosed CLI asymmetry).
- S10 test_reason_vocabulary_lockstep — the 9-tuple equality across
  schema_meta, storelib.inject (both arms), eval_gold (both arms), and
  the MCP server twin.
- S11 test_hermes_lane_envelope_shape — the served envelope carries NO
  ``offline_snapshot`` key and stays inside the store envelope
  vocabulary (the strict provider validator's unknown-key rejection).
- S12 test_hooks_never_build_snapshots — no hooks/ file references the
  snapshot builder (P3: only the broker refresh and build_snapshot
  write).
- S13 test_prev_pair_restore_on_meta_failure — a meta-write failure after
  a successful sqlite replace restores the previous pair.
- S14 test_owner_only_permissions — POSIX-only: the snapshot pair is
  owner-only (0o600).

Env discipline and helpers are imported from the frozen module (same
directory; works under pytest AND the CI direct-python loop, which puts
this file's dir first on sys.path). Runs standalone:
``python tests/test_t11_offline_snapshot_supp.py`` (no argument runs ALL
pins; ``S1``..``S14`` runs one; unknown selector exits 2).
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_t11_offline_snapshot as t11  # noqa: E402 - the frozen helpers

REPO_ROOT = t11.REPO_ROOT
SCRIPTS_DIR = t11.SCRIPTS_DIR
STORE_PY = t11.STORE_PY
BROKER_CLI = SCRIPTS_DIR / "fleet" / "broker_cli.py"

_SUBPROC_TIMEOUT_S = 30
NINE_TUPLE = (
    "empty-pool", "omitted", "below-bar", "budget-drop", "below-relevance",
    "already-delivered", "expired", "hub-unreachable", "snapshot-expired",
)


def _marker() -> str:
    """A unique, NON-secret-looking marker. A raw uuid4().hex is 32 hex
    chars — the store's write-time secret redactor replaces runs like
    that with [REDACTED_SECRET], so content asserts fail. Rotating the
    hex letters (a-f) past the hex range keeps uniqueness without ever
    pattern-matching a secret shape."""
    return uuid.uuid4().hex.translate(str.maketrans("abcdef", "ghijkl"))


def _broker_env(data_dir: str, hub_url: str) -> dict:
    """The env a production broker_cli moment carries (remote mode, the
    scratch as the data dir — remote-mode legal via ZCODE_PLUGIN_DATA)."""
    env = {k: v for k, v in os.environ.items()}
    t11._strip_config_env(env)
    env.update({
        "ZMEM_HOME": str(REPO_ROOT),
        "ZMEM_REMOTE_URL": hub_url,
        "ZMEM_MCP_TOKEN": t11.HUB_TOKEN,
        "ZMEM_NAMESPACE": "user:global",
        "ZMEM_MODELS_DIR": os.path.join(data_dir, "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONIOENCODING": "utf-8",
        "ZCODE_PLUGIN_DATA": data_dir,
    })
    return env


def _broker_moment(data_dir: str, hub_url: str, query: str) -> dict:
    """One real broker_cli subprocess moment; returns the parsed envelope."""
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": query,
        "session_id": f"t11s-{uuid.uuid4().hex}",
        "transcript_path": os.path.join(data_dir, "absent.jsonl"),
        "cwd": data_dir,
    }
    proc = subprocess.run(
        [sys.executable, str(BROKER_CLI), "UserPromptSubmit"],
        input=json.dumps(payload), capture_output=True, text=True,
        env=_broker_env(data_dir, hub_url), cwd=str(REPO_ROOT),
        stdin=None, timeout=_SUBPROC_TIMEOUT_S)
    if proc.returncode != 0:
        raise AssertionError(
            f"TEST HARNESS failure: broker_cli exited {proc.returncode}; "
            f"stderr={t11._tail(proc.stderr, 400)!r}")
    m = re.search(r"<<<ZMEM_JSON>>>(.*)<<<END>>>", proc.stdout, re.S)
    if not m:
        raise AssertionError(
            f"TEST HARNESS failure: broker_cli stdout carried no sentinel "
            f"envelope: {t11._tail(proc.stdout, 300)!r}")
    return json.loads(m.group(1))


def _stamp_path(data_dir: str) -> Path:
    return Path(data_dir) / "broker" / "hub-unreachable.stamp"


def _load_snapshot_module():
    sys.path.insert(0, str(SCRIPTS_DIR))
    import importlib
    mod = importlib.import_module("fleet.snapshot")
    return mod


# -- S1: the subprocess reconnect transition -------------------------------

def test_reconnect_transition_refreshes_and_clears_stamp():
    if not t11._mcp_available():
        print("SKIP S1 (mcp package absent; CI installs it)")
        return
    with t11._pinned_env("s1") as data_dir:
        hub_dir = os.path.join(data_dir, "hub")
        canary = (
            "t11s1 hub-only canary quokka gravimetrics "
            f"{_marker()}")
        t11._seed_store(hub_dir, [("user:global", canary)])
        with t11._Hub(hub_dir) as hub:
            # LEG 1 — offline moment against a dead port: no snapshot
            # exists, the serve is the T-08 fail-open, and the moment
            # WRITES the persisted stamp.
            offline = _broker_moment(
                data_dir, t11._dead_port_url(),
                "quokka gravimetrics offline probe")
            assert offline.get("additionalContext", "") == "", (
                "LEG 1 served content with NO snapshot present")
            assert _stamp_path(data_dir).is_file(), (
                "the offline moment did not write hub-unreachable.stamp")
            # LEG 2 — connected moment against the real hub: the stamp is
            # still armed right before the moment, then the moment
            # refreshes (no snapshot existed) and clears the stamp.
            assert _stamp_path(data_dir).is_file(), (
                "stamp disappeared before the connected moment")
            served = _broker_moment(data_dir, hub.url, canary)
            assert canary in (served.get("additionalContext") or ""), (
                "the connected moment did not serve the live hub canary")
            assert not _stamp_path(data_dir).exists(), (
                "the connected moment left the stamp armed after a "
                "completed refresh")
            snap, meta_p = t11._snapshot_paths(data_dir)
            assert snap.is_file() and meta_p.is_file(), (
                "the reconnect refresh never built the snapshot pair")
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            assert float(meta.get("expires_at", 0)) > time.time(), (
                "the refreshed snapshot meta is not valid")


# -- S2: connected moments never write the stamp ---------------------------

def test_connected_moment_never_writes_stamp():
    if not t11._mcp_available():
        print("SKIP S2 (mcp package absent; CI installs it)")
        return
    with t11._pinned_env("s2") as data_dir:
        hub_dir = os.path.join(data_dir, "hub")
        t11._seed_store(
            hub_dir, [("user:global",
                       "t11s2 hub row fretwork " + _marker())])
        with t11._Hub(hub_dir) as hub:
            assert not _stamp_path(data_dir).exists(), (
                "fixture error: a stamp existed before any moment")
            _broker_moment(data_dir, hub.url, "fretwork probe")
            assert not _stamp_path(data_dir).exists(), (
                "a CONNECTED moment wrote hub-unreachable.stamp")


# -- S3/S4: refresh gating in-process ---------------------------------------

def test_failed_refresh_leaves_stamp_armed():
    with t11._pinned_env("s3") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        snap.write_stamp(env)
        # A snapshot fresh enough that ONLY the transition arms the refresh.
        src = t11._seed_store(
            os.path.join(data_dir, "src"), [("user:global", "s3 row")])
        snap.build_snapshot(source_store=src, env=env, revision=1)

        class _FailingHttp:
            def snapshot(self, deadline_ms=None):
                raise OSError("hub refused the snapshot pull")

        assert snap._maybe_refresh(_FailingHttp(), env) is False, (
            "a failed refresh reported success")
        assert snap.stamp_present(env), (
            "the failed refresh cleared the stamp (it must re-arm)")

        class _WorkingHttp:
            def snapshot(self, deadline_ms=None):
                return {"ok": True, "principal": "local", "rows": [],
                        "revocations": [], "snapshot_revision": 2,
                        "payload_bytes": 16}

        assert snap._maybe_refresh(_WorkingHttp(), env) is True, (
            "the armed transition did not refresh on a healthy hub")
        assert not snap.stamp_present(env), (
            "the completed refresh left the stamp armed")


def test_refresh_cadence_gate():
    with t11._pinned_env("s4") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        src = t11._seed_store(
            os.path.join(data_dir, "src"), [("user:global", "s4 row")])
        snap.build_snapshot(source_store=src, env=env, revision=1)

        class _MustNotBeCalled:
            def snapshot(self, deadline_ms=None):
                raise AssertionError(
                    "refresh fired for a fresh snapshot with no stamp")

        assert snap._maybe_refresh(_MustNotBeCalled(), env) is False, (
            "a fresh snapshot + no stamp triggered a refresh")


# -- S5: the cap refusal keeps the previous pair ----------------------------

def test_cap_refusal_keeps_previous_pair():
    with t11._pinned_env("s5") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        src = t11._seed_store(
            os.path.join(data_dir, "src"),
            [("user:global", "t11s5 cap-canary row one " + _marker()),
             ("user:global", "t11s5 cap-canary row two " + _marker())])
        snap.build_snapshot(source_store=src, env=env, revision=1)
        snap_path, meta_path = t11._snapshot_paths(data_dir)
        before_sql = snap_path.read_bytes()
        before_meta = meta_path.read_bytes()
        real_cap = snap.MAX_SNAPSHOT_BYTES
        snap.MAX_SNAPSHOT_BYTES = 1024  # far below any real pair
        try:
            try:
                snap.build_snapshot(source_store=src, env=env, revision=2)
            except ValueError:
                pass
            else:
                raise AssertionError(
                    "an over-cap build succeeded (the cap is load-bearing)")
        finally:
            snap.MAX_SNAPSHOT_BYTES = real_cap
        assert snap_path.read_bytes() == before_sql, (
            "the over-cap build replaced snapshot.sqlite")
        assert meta_path.read_bytes() == before_meta, (
            "the over-cap build replaced snapshot.meta.json")


# -- S6: the dual scope filter ----------------------------------------------

def test_dual_scope_filter_excludes_dormant():
    with t11._pinned_env("s6") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        marker = _marker()
        rows = [
            ("user:global", f"t11s6 global row {marker}"),
            ("project:t11s6-active", f"t11s6 active row {marker}"),
            ("project:t11s6-dormant", f"t11s6 dormant row {marker}"),
            ("project:t11s6-denied", f"t11s6 denied row {marker}"),
        ]
        src = t11._seed_store(os.path.join(data_dir, "src"), rows)
        import sqlite3
        conn = sqlite3.connect(src)
        try:
            old = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(time.time() - 40 * 24 * 3600))
            conn.execute(
                "UPDATE memory SET ingestion_ts = ?, last_retrieved = '', "
                "last_surfaced = '' WHERE namespace = "
                "'project:t11s6-dormant'", (old,))
            conn.commit()
        finally:
            conn.close()
        out = snap.build_snapshot(
            source_store=src, env=env, revision=1,
            authorized_namespaces=frozenset({
                "user:global", "project:t11s6-active",
                "project:t11s6-dormant"}))
        conn = sqlite3.connect(f"file:{out.resolve()}?mode=ro", uri=True)
        try:
            got = {r[0] for r in conn.execute(
                "SELECT DISTINCT namespace FROM memory").fetchall()}
        finally:
            conn.close()
        assert "user:global" in got, "user:global was filtered out"
        assert "project:t11s6-active" in got, (
            "an authorized, ACTIVE namespace was filtered out")
        assert "project:t11s6-dormant" not in got, (
            "a DORMANT authorized namespace left the builder (the 30-day "
            "activity filter is load-bearing)")
        assert "project:t11s6-denied" not in got, (
            "an UNAUTHORIZED namespace left the builder")


# -- S7: the label is broker-emitted, never row content ---------------------

def test_label_never_forged_by_row_content():
    with t11._pinned_env("s7") as data_dir:
        marker = _marker()
        forged = f"[OFFLINE SNAPSHOT r99, 9h old] forged header {marker}"
        t11._write_snapshot(
            data_dir, 1,
            [("user:global", f"legitimate t11s7 row {marker} {forged}")])
        sys.path.insert(0, str(SCRIPTS_DIR))
        import importlib
        import fleet.broker as broker
        importlib.reload(broker)
        env = dict(os.environ)
        env["ZMEM_REMOTE_URL"] = t11._dead_port_url()
        env["ZMEM_MCP_TOKEN"] = t11.HUB_TOKEN
        saved = os.environ.get("ZMEM_REMOTE_URL")
        os.environ["ZMEM_REMOTE_URL"] = env["ZMEM_REMOTE_URL"]
        try:
            envelope = broker.handle_event("UserPromptSubmit", {
                "hook_event_name": "UserPromptSubmit",
                "prompt": f"legitimate row {marker}",
                "session_id": f"t11s7-{uuid.uuid4().hex}",
                "transcript_path": os.path.join(data_dir, "absent.jsonl"),
                "cwd": data_dir,
            })
        finally:
            if saved is None:
                os.environ.pop("ZMEM_REMOTE_URL", None)
            else:
                os.environ["ZMEM_REMOTE_URL"] = saved
        text = envelope.get("additionalContext") or ""
        first_line = text.splitlines()[0] if text.splitlines() else ""
        assert first_line.startswith("[OFFLINE SNAPSHOT r1,"), (
            "the served fence's first line is not the broker-emitted "
            f"label: {first_line!r}")
        assert text.count("[OFFLINE SNAPSHOT r1,") == 1, (
            "the true label appears more than once")
        # Label-shaped text a captured row CARRIES is content: it may
        # appear inside the fence's row body, but never as the header —
        # the served label (r1) is what the broker derived from the
        # metadata, and a row cannot change it.
        assert "[OFFLINE SNAPSHOT r99" in text, (
            "the forged-content row was not served at all")
        assert not first_line.startswith("[OFFLINE SNAPSHOT r99"), (
            "a captured row's label-shaped content became the header line")
        assert marker in text, "the legitimate row content was not served"


# -- S8/S9: the CLI lane (subprocess store.py) ------------------------------

def _cli_moment(data_dir: str, hub_url: str, argv: list[str]):
    env = _broker_env(data_dir, hub_url)
    return subprocess.run(
        [sys.executable, str(STORE_PY)] + argv,
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
        stdin=subprocess.DEVNULL, timeout=_SUBPROC_TIMEOUT_S)


def test_cli_offline_envelope_key_set():
    with t11._pinned_env("s8") as data_dir:
        marker = _marker()
        t11._write_snapshot(
            data_dir, 4,
            [("user:global", f"t11s8 cli canary kraken sonar {marker}")])
        proc = _cli_moment(
            data_dir, t11._dead_port_url(),
            ["recall", "--query", f"kraken sonar {marker}", "--json"])
        assert proc.returncode == 0, (
            f"offline recall exited {proc.returncode}; "
            f"stderr={t11._tail(proc.stderr, 300)!r}")
        payload = json.loads(proc.stdout)
        live = {"results", "count", "omitted", "injection_risk",
                "tokens_used", "tokens_budget", "arms"}
        extra = set(payload) - live
        assert extra == {"offline_snapshot"}, (
            f"offline recall keys carried more than the one provenance "
            f"key: extra={sorted(extra)}")
        assert isinstance(payload.get("offline_snapshot"), str) and \
            payload["offline_snapshot"].startswith("[OFFLINE SNAPSHOT r4,"), (
                f"bad provenance label: {payload.get('offline_snapshot')!r}")
        assert "hub unreachable" in (proc.stderr or ""), (
            "the offline CLI serve did not note the degrade on stderr")


def test_cli_expired_keeps_error_contract():
    with t11._pinned_env("s9") as data_dir:
        t11._write_snapshot(
            data_dir, 3, [("user:global", "t11s9 expired row")],
            issued_at=time.time() - 73 * 3600,
            expires_at=time.time() - 3600)
        proc = _cli_moment(
            data_dir, t11._dead_port_url(),
            ["recall", "--query", "expired row", "--json"])
        assert proc.returncode == 1, (
            f"expired-snapshot recall exited {proc.returncode} (expected "
            f"the unchanged error contract)")
        payload = json.loads(proc.stdout)
        assert payload.get("ok") is False and \
            payload.get("reason") == "hub-unreachable", (
                f"expired snapshot changed the CLI error contract: "
                f"{payload}")


# -- S10: the reason vocabulary lockstep -------------------------------------

def test_reason_vocabulary_lockstep():
    sys.path.insert(0, str(SCRIPTS_DIR))
    for name in [n for n in list(sys.modules)
                 if n == "schema_meta" or n.startswith("storelib.")]:
        del sys.modules[name]
    import schema_meta
    from storelib import eval_gold, inject
    assert tuple(schema_meta.INJECT_SILENT_REASONS) == NINE_TUPLE, (
        f"schema_meta tuple: {schema_meta.INJECT_SILENT_REASONS}")
    assert tuple(inject.INJECT_SILENT_REASONS) == NINE_TUPLE, (
        f"inject tuple: {inject.INJECT_SILENT_REASONS}")
    assert tuple(eval_gold._injection_silent_reasons()) == NINE_TUPLE, (
        "eval_gold tally source drifted from the 9-tuple")
    saved = schema_meta.INJECT_SILENT_REASONS
    try:
        del schema_meta.INJECT_SILENT_REASONS
        assert tuple(eval_gold._injection_silent_reasons()) == NINE_TUPLE, (
            "the eval_gold partial-deployment fallback is stale")
    finally:
        schema_meta.INJECT_SILENT_REASONS = saved
    server_src = (REPO_ROOT / "hermes-plugin" / "server" /
                  "mcp_server.py").read_text(encoding="utf-8")
    m = re.search(r"_INJECT_SILENT_REASONS = \((.*?)\)", server_src, re.S)
    assert m, "the MCP server twin tuple literal was not found"
    twins = tuple(re.findall(r'"([a-z-]+)"', m.group(1)))
    assert twins == NINE_TUPLE, f"MCP server twin tuple: {twins}"
    # Final-critic B5: the hermes PROVIDER fallback literal too.
    provider_src = (REPO_ROOT / "hermes-plugin" / "__init__.py").read_text(
        encoding="utf-8")
    m = re.search(r'"INJECT_SILENT_REASONS": \((.*?)\),', provider_src, re.S)
    assert m, "the provider fallback tuple literal was not found"
    prov = tuple(re.findall(r'"([a-z-]+)"', m.group(1)))
    assert prov == NINE_TUPLE, f"provider fallback tuple: {prov}"


# -- S11: the hermes-lane envelope shape -------------------------------------

def test_hermes_lane_envelope_shape():
    with t11._pinned_env("s11") as data_dir:
        marker = _marker()
        t11._write_snapshot(
            data_dir, 6,
            [("user:global", f"t11s11 provider canary {marker}")])
        snap = _load_snapshot_module()
        envelope, state = snap._serve_offline_read(
            dict(os.environ), query=f"provider canary {marker}",
            namespace="user:global", moment="user_prompt",
            session_id=f"t11s11-{uuid.uuid4().hex}", lane="hermes-provider")
        assert state == "valid" and envelope is not None, (
            "the serve did not produce a valid envelope")
        assert "offline_snapshot" not in envelope, (
            "the provider-lane envelope carries the CLI-only "
            "offline_snapshot key (the strict validator rejects unknown "
            "keys)")
        rendered = envelope.get("rendered") or ""
        assert rendered.startswith("[OFFLINE SNAPSHOT r6,"), (
            "the label is not the rendered fence header on the provider "
            "lane")
        sys.path.insert(0, str(SCRIPTS_DIR))
        import importlib
        from storelib import inject as inject_mod
        importlib.reload(inject_mod)
        # The REAL vocabulary the strict provider validator enforces:
        # every envelope key must be a required or registered-optional
        # member (final-critic B6: the earlier _ENVELOPE_KEYS lookup
        # matched no symbol and never executed).
        vocabulary = (set(inject_mod.INJECTION_ENVELOPE_REQUIRED)
                      | set(inject_mod.INJECTION_ENVELOPE_OPTIONAL))
        stray = set(envelope) - vocabulary
        assert not stray, (
            f"envelope keys outside the store vocabulary: {stray}")


# -- S12: hooks never build snapshots ----------------------------------------

def test_hooks_never_build_snapshots():
    hooks_dir = REPO_ROOT / "hooks"
    offenders = []
    for path in hooks_dir.glob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if re.search(r"build_snapshot|fleet\.snapshot|snapshot\.sqlite",
                     text):
            offenders.append(path.name)
    assert not offenders, (
        f"hook files reference the snapshot builder/artifacts: {offenders}")


# -- S13: prev-pair restore on meta failure ----------------------------------

def test_prev_pair_restore_on_meta_failure():
    with t11._pinned_env("s13") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        src = t11._seed_store(
            os.path.join(data_dir, "src"),
            [("user:global", f"t11s13 row {_marker()}")])
        snap.build_snapshot(source_store=src, env=env, revision=7)
        snap_path, meta_path = t11._snapshot_paths(data_dir)
        before_sql = snap_path.read_bytes()
        before_meta = meta_path.read_bytes()
        real_dumps = snap.json.dumps

        def _broken_dumps(*a, **k):
            raise OSError("simulated meta-write failure")

        snap.json.dumps = _broken_dumps
        try:
            try:
                snap.build_snapshot(source_store=src, env=env, revision=8)
            except OSError:
                pass
            else:
                raise AssertionError(
                    "the meta-write failure did not propagate")
        finally:
            snap.json.dumps = real_dumps
        assert snap_path.read_bytes() == before_sql, (
            "snapshot.sqlite changed despite the failed meta write")
        assert meta_path.read_bytes() == before_meta, (
            "the meta sidecar changed despite the failed write")


# -- S14: owner-only permissions (POSIX-only leg) ----------------------------

def test_owner_only_permissions():
    if os.name == "nt":
        print("SKIP S14 (Windows: os.stat mode bits are not a reliable "
              "ACL proxy — the POSIX-only contract leg)")
        return
    with t11._pinned_env("s14") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        src = t11._seed_store(
            os.path.join(data_dir, "src"),
            [("user:global", f"t11s14 row {_marker()}")])
        out = snap.build_snapshot(source_store=src, env=env, revision=1)
        for path in (out, out.with_name("snapshot.meta.json")):
            mode = stat.S_IMODE(path.stat().st_mode)
            assert not (mode & 0o077), (
                f"{path.name} exposes group/other bits: {oct(mode)}")


# -- S15: hub-refusal payloads never destroy a good pair (final-critic B1) ---

def test_hub_refusal_shapes_keep_pair_and_stamp():
    with t11._pinned_env("s15") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        src = t11._seed_store(
            os.path.join(data_dir, "src"),
            [("user:global", f"t11s15 canary { _marker()}")])
        snap.build_snapshot(source_store=src, env=env, revision=9)
        snap.write_stamp(env)
        snap_path, meta_path = t11._snapshot_paths(data_dir)
        before_sql = snap_path.read_bytes()
        refusal_shapes = [
            {"error": "snapshot: payload exceeds the 64 MB cap; refused"},
            {"ok": False, "reason": "hub-unreachable"},
            {"isError": True, "deadline_ms": 1},
            ["not", "a", "dict"],
            {},
        ]
        for shape in refusal_shapes:
            class _RefusingHttp:
                def snapshot(self, deadline_ms=None, _shape=shape):
                    return _shape

            assert snap._maybe_refresh(_RefusingHttp(), env) is False, (
                f"a hub-answered refusal shape was treated as a completed "
                f"refresh: {shape!r}")
            assert snap_path.read_bytes() == before_sql, (
                f"a refusal shape replaced the snapshot pair: {shape!r}")
            assert snap.stamp_present(env), (
                f"a refusal shape cleared the reconnect stamp: {shape!r}")


# -- S16: unreachable SessionStart never crashes (final-critic B3) ----------

def test_unreachable_session_start_serves_fail_open():
    with t11._pinned_env("s16") as data_dir:
        sys.path.insert(0, str(SCRIPTS_DIR))
        import importlib
        import fleet.broker as broker
        importlib.reload(broker)
        marker = _marker()
        t11._write_snapshot(
            data_dir, 2, [("user:global", f"t11s16 row {marker}")])
        env = dict(os.environ)
        env["ZMEM_REMOTE_URL"] = t11._dead_port_url()
        env["ZMEM_MCP_TOKEN"] = t11.HUB_TOKEN
        saved = os.environ.get("ZMEM_REMOTE_URL")
        os.environ["ZMEM_REMOTE_URL"] = env["ZMEM_REMOTE_URL"]
        try:
            # A valid snapshot: the moment must return a HOOK ENVELOPE
            # (never raise — the UnboundLocalError regression).
            envelope = broker.handle_event("SessionStart", {
                "hook_event_name": "SessionStart",
                "session_id": f"t11s16-{uuid.uuid4().hex}",
                "transcript_path": os.path.join(data_dir, "absent.jsonl"),
                "cwd": data_dir,
            })
            assert isinstance(envelope, dict), (
                f"unreachable SessionStart did not return an envelope: "
                f"{type(envelope)}")
            assert "additionalContext" in envelope, (
                "unreachable SessionStart lost the fail-open hook shape")
        finally:
            if saved is None:
                os.environ.pop("ZMEM_REMOTE_URL", None)
            else:
                os.environ["ZMEM_REMOTE_URL"] = saved


# -- S17: a payload-built snapshot SERVES offline (final-critic B2) ----------

def test_payload_built_snapshot_serves_offline():
    with t11._pinned_env("s17") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        marker = _marker()
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # A REAL-store-shaped row (NOT NULL columns get their defaults).
        by_name = {
            "id": "t11s17-" + marker, "namespace": "user:global",
            "type": "fact", "content": f"t11s17 payload canary fretsaw {marker}",
            "tags": "", "source_ref": "", "confidence": 0.9,
            "signal": "test", "valid_from": now, "valid_until": "",
            "update_of": "", "taint": "trusted_internal",
            "ingestion_ts": now, "superseded_at": None,
            "supersede_reason": "", "merged_from": None,
            "trust_score": 1.0, "applied_count": 0, "violated_count": 0,
            "embedding": None, "embedding_model": "",
            "retrieval_count": 0, "last_retrieved": None,
            "surfaced_count": 0, "last_surfaced": None,
        }
        row = [by_name[c.strip()] for c in snap._SNAPSHOT_COLUMNS.split(",")]
        payload = {
            "ok": True, "principal": "hub-principal",
            "columns": snap._SNAPSHOT_COLUMNS, "embedding_b64": True,
            "embed_profile": "minilm",
            "rows": [row], "revocations": [],
            "snapshot_revision": 3, "payload_bytes": 64,
        }
        snap._build_from_payload(payload, env)
        envelope, state = snap._serve_offline_read(
            env, query=f"payload canary fretsaw {marker}",
            namespace="user:global", moment="user_prompt",
            session_id=f"t11s17-{uuid.uuid4().hex}", lane=None)
        assert state == "valid", f"refreshed snapshot state: {state}"
        assert envelope is not None, (
            "the payload-built snapshot served nothing (a starved schema "
            "produces empty-pool envelopes — final-critic B2)")
        rendered = envelope.get("rendered") or ""
        assert rendered.startswith("[OFFLINE SNAPSHOT r3,"), (
            f"the payload-built serve lost its label: {rendered[:80]!r}")
        assert marker in rendered, (
            "the payload row was not served from the refreshed snapshot")


# -- S18: no foreign purged ids in a scoped snapshot (final-critic B4) -------

def test_scoped_snapshot_carries_no_foreign_purged_ids():
    with t11._pinned_env("s18") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        marker = _marker()
        src = t11._seed_store(
            os.path.join(data_dir, "src"),
            [("user:global", f"t11s18 global {marker}"),
             ("project:t11s18-denied", f"t11s18 denied {marker}")])
        import sqlite3
        conn = sqlite3.connect(src)
        try:
            denied_id = conn.execute(
                "SELECT id FROM memory WHERE namespace = "
                "'project:t11s18-denied'").fetchone()[0]
            conn.execute("DELETE FROM memory WHERE namespace = "
                         "'project:t11s18-denied'")
            conn.execute(
                "INSERT INTO purged_id (id, purged_at) VALUES (?, ?)",
                (denied_id, "2026-10-24T00:00:00Z"))
            conn.commit()
        finally:
            conn.close()
        out = snap.build_snapshot(
            source_store=src, env=env, revision=1,
            authorized_namespaces=frozenset({"user:global"}))
        conn = sqlite3.connect(f"file:{out.resolve()}?mode=ro", uri=True)
        try:
            purged = [r[0] for r in conn.execute(
                "SELECT id FROM purged_id").fetchall()] \
                if conn.execute(
                    "SELECT name FROM sqlite_master WHERE name = "
                    "'purged_id'").fetchone() else []
        finally:
            conn.close()
        snap_path, meta_path = t11._snapshot_paths(data_dir)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert denied_id not in purged, (
            "a foreign-namespace purged id leaked into the scoped "
            "snapshot's purged_id table")
        assert denied_id not in (meta.get("revocations") or []), (
            "a foreign-namespace purged id leaked into the scoped "
            "snapshot's revocation metadata")


# -- S19: the provider serve leaves no env-derived modules behind (B6) -------

def test_provider_serve_purges_env_derived_modules():
    with t11._pinned_env("s19") as data_dir:
        import types
        marker = _marker()
        t11._write_snapshot(
            data_dir, 5, [("user:global", f"t11s19 row {marker}")])
        agent = types.ModuleType("agent")
        mp = types.ModuleType("agent.memory_provider")
        mp.MemoryProvider = type("MemoryProvider", (), {})
        agent.memory_provider = mp
        sys.modules.setdefault("agent", agent)
        sys.modules.setdefault("agent.memory_provider", mp)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "zmem_hermes_t11s19", REPO_ROOT / "hermes-plugin" / "__init__.py")
        provider = importlib.util.module_from_spec(spec)
        sys.modules["zmem_hermes_t11s19"] = provider
        spec.loader.exec_module(provider)
        pre = set(sys.modules)
        envelope = provider._offline_snapshot_envelope(
            f"t11s19 row {marker}", "user:global", f"t11s19-{uuid.uuid4().hex}")
        assert isinstance(envelope, dict) and envelope.get("rendered"), (
            "the provider-shape serve returned nothing (fixture error)")
        # The STRUCTURAL law, checked with an INDEPENDENT mechanism from
        # the production purge (pathlib resolve + is_relative_to, not the
        # normcase/normpath containment the provider uses — final-critic
        # r4: a pin sharing the production predicate is circular).  NO
        # module whose __file__ resolves under the scripts dir may be
        # new after one serve, and no first-party bare root or dotted
        # descendant may remain — only third-party/stdlib residue.
        scripts_p = t11.SCRIPTS_DIR.resolve()
        leaked = []
        for name in sys.modules:
            if name in pre:
                continue
            inside = False
            file_path = getattr(sys.modules[name], "__file__", None)
            if file_path:
                try:
                    inside = Path(file_path).resolve().is_relative_to(
                        scripts_p)
                except (OSError, ValueError):
                    inside = False
            if inside \
                    or name in ("store", "host", "redaction", "schema_meta",
                                "auth", "bind_guard", "storelib", "fleet",
                                "zmem_fleet_snapshot_offline") \
                    or name.startswith(("storelib.", "store.", "auth.",
                                        "bind_guard.", "fleet.")):
                leaked.append(name)
        assert not leaked, (
            f"the provider serve leaked first-party scripts-dir modules "
            f"into sys.modules: {sorted(leaked)}")
        # Belt-and-braces: the six r3 leakers by name (an import-graph
        # change could rename or add new paths; the names anchor the
        # known offenders).
        for named in ("embeddings", "embed_profiles",
                      "cross_encoder_profiles", "corrections",
                      "correction_queue", "capture_quality"):
            assert named not in sys.modules or named in pre, (
                f"the r3-named module {named!r} survived the purge")


# -- S20: the CLI offline limit clamp (fix-round behavior change) ------------

def test_cli_offline_limit_clamp():
    with t11._pinned_env("s20") as data_dir:
        marker = _marker()
        # Bulk rows in a PROJECT namespace: the recall limit caps the
        # namespace lane (near-identical rows also ride additive arms —
        # entity-link arrivals — exactly as the LIVE lane does, so the
        # pin asserts the LIMIT IS THREADED (monotonic), not exact-N).
        rows = [("project:t11s20", f"t11s20 row {i} kraken {marker}")
                for i in range(5)]
        t11._write_snapshot(data_dir, 7, rows)

        def _count(extra):
            argv = (["recall", "--query", f"kraken {marker}", "--namespace",
                     "project:t11s20", "--json"] + extra)
            proc = _cli_moment(data_dir, t11._dead_port_url(), argv)
            assert proc.returncode == 0, (
                f"offline recall {extra} exited {proc.returncode}: "
                f"{t11._tail(proc.stderr, 200)!r}")
            return len(json.loads(proc.stdout).get("results") or [])

        tight = _count(["--limit", "1"])
        wide = _count(["--limit", "5"])
        assert tight < wide, (
            f"--limit is not threaded to the offline serve: "
            f"limit=1 served {tight}, limit=5 served {wide}")


# -- S21: a foreign/corrupt sqlite at the snapshot path is ABSENT (R2) -------

def test_foreign_sqlite_is_treated_as_absent():
    with t11._pinned_env("s21") as data_dir:
        snap = _load_snapshot_module()
        env = dict(os.environ)
        snap_path, meta_path = t11._snapshot_paths(data_dir)
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        conn = sqlite3.connect(str(snap_path))
        conn.execute("CREATE TABLE unrelated (x TEXT)")
        conn.commit()
        conn.close()
        meta_path.write_text(json.dumps({
            "snapshot_revision": 1, "issued_at": time.time(),
            "expires_at": time.time() + 72 * 3600}), encoding="utf-8")
        loaded, meta = snap._load(env)
        assert loaded is None, (
            "a foreign sqlite without a memory table was handed to the "
            "selector (it would serve a silent empty envelope with no "
            "hub-unreachable record)")
        envelope, state = snap._serve_offline_read(
            env, query="anything", namespace="user:global",
            moment="user_prompt", session_id="t11s21", lane=None)
        assert envelope is None and state == "none", (
            f"the foreign-sqlite serve did not degrade to none: {state}")


# -- the runner ---------------------------------------------------------------

_PINS = {name[5:]: fn for name, fn in sorted(globals().items())
         if name.startswith("test_")}


def _run_pins() -> int:
    failures = []
    for pin_id, fn in _PINS.items():
        try:
            fn()
            print(f"PASS test_{fn.__name__}")
        except AssertionError as exc:
            failures.append((pin_id, str(exc)))
            print(f"FAIL test_{fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - a harness error is a fail
            failures.append((pin_id, f"HARNESS ERROR: {exc}"))
            print(f"ERROR test_{fn.__name__}: {exc}")
    if failures:
        print(f"\n{len(failures)} pin(s) failed")
        return 1
    print("\nall supp pins passed")
    return 0


def _run_one(key: str) -> int:
    try:
        _PINS[key]()
        print(f"PASS test_{_PINS[key].__name__}")
        return 0
    except AssertionError as exc:
        print(f"FAIL test_{_PINS[key].__name__}: {exc}")
        return 1


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.exit(_run_pins())
    key = sys.argv[1]
    if key in _PINS:
        sys.exit(_run_one(key))
    print(f"NO CHECK {key}", file=sys.stderr)
    sys.exit(2)
