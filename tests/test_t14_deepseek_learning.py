"""Arm's-length frozen checks for the DeepSeek adapter learning loop —
issue #375 (T-14). RED at base 09dfbf78: the plugin does not exist (the
scenario driver exits 1 on its plugin import), mine-history has no
``--source deepseek`` adapter, doctor --fleet has no deepseek-plugin
check, and the committed support report is absent.

Same driver/env/timeout contract as tests/test_t13_deepseek_contract.py
(spawn ``node tests/fixtures/deepseek/scenario.mjs``; isolated GUID env
per check; 90 s subprocess budget; runtime legs skip "dsh runtime not
installed" when the dsh runtime is genuinely absent — EXCEPT
test_source_provenance and test_compatibility_claim_gate, which are
python-only and run without the runtime). Runs under pytest
(``python -X utf8 -m pytest --disable-plugin-autoload --noconftest -p
no:cacheprovider -q tests/test_t14_deepseek_learning.py``) and standalone.
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import deepseek_harness as harness  # noqa: E402
from deepseek_assertions import (  # noqa: E402
    capture_queue, support_report_problems, zmem_msgs)

SECRET_TOKEN = ('AKIAZMEMT13' + 'SECRETAKIA'
               + 'ZMEMT13SECRET')  # concatenated (push hygiene)
CORRECTION_MARK = "torqued to 12 Nm"
INJECTED_MARK = "ZMEM_INJECTED_CONTEXT_MARKER_771"
DSH_EXPORT_HEADER = {"format": "dsh-session-export", "version": 1}


def _local_env(store: Path, data: Path) -> dict:
    return {"ZMEM_STORE": str(store), "ZMEM_DATA": str(data)}


def _grep_dir_for(root: Path, needle: str) -> "list[str]":
    hits = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                if needle in path.read_text(encoding="utf-8",
                                            errors="replace"):
                    hits.append(str(path))
            except OSError:
                continue
    return hits


def test_corrections_and_failures():
    """User corrections are captured with attribution and redaction; tool
    failures are classified; successful tools, injected zmem context and
    replayed duplicates are NOT captured."""
    harness.require_runtime()
    with harness.isolated_env("t14-corrections") as tmp:
        data = tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        obs = harness.ok_or_red(harness.run_scenario(
            "corrections_and_failures", tmp / "state.json",
            _local_env(tmp / "store.sqlite", data)),
            "corrections_and_failures")
        state = obs.get("pluginState") or {}
        queue = capture_queue(state)
        assert queue, (
            f"the plugin state must report its capture queue: {state!r}")
        corrections = [e for e in queue if CORRECTION_MARK in json.dumps(e)]
        assert corrections, (
            f"a real user correction must land in the capture queue: "
            f"{json.dumps(queue)[:400]!r}")
        entry = corrections[0]
        blob = json.dumps(entry)
        assert entry.get("host") == "deepseek" or "deepseek" in blob, (
            f"the capture entry must carry host attribution: {entry!r}")
        assert entry.get("session") and entry.get("namespace"), (
            "the capture entry must carry session+namespace attribution: "
            f"{entry!r}")
        # REDACTION: the secret-shaped token never appears in the queue,
        # the state file, or ANY file under the isolated data dir.
        assert SECRET_TOKEN not in json.dumps(state), (
            "the secret-shaped token leaked into the plugin state")
        assert not _grep_dir_for(data, SECRET_TOKEN) and \
            not _grep_dir_for(tmp, SECRET_TOKEN), (
            "the secret-shaped token leaked into the isolated data dir")
        assert any(
            e.get("kind") == "failure"
            and "connection refused" in str(e.get("text") or "")
            for e in queue), (
            f"a tool failure must be captured and classified as failure: "
            f"{json.dumps(queue)[:400]!r}")
        assert not any("toy-ok" in json.dumps(e) or "zmem_toy_ok" in
                       json.dumps(e) for e in queue), (
            "a SUCCESSFUL tool outcome must NOT be captured")
        assert not any(INJECTED_MARK in json.dumps(e) for e in queue), (
            "an injected zmem-context message must NOT be captured as a "
            "user correction")
        # Duplicate suppression is keyed on CONTENT+SESSION (plan-critic
        # round-2 R2-6): the replay leg sends the SAME text as two fresh
        # native messages (each createUserMessage gets a fresh identity in
        # the pinned runtime, so a native-message-id key would see two
        # events, not one replay). The deterministic key the implementation
        # must apply is the identical (kind, text) pair within the session
        # — a content hash — so the queue holds exactly ONE entry for the
        # identical pair.
        assert len(corrections) == 1, (
            f"the identical (kind, text) pair sent twice must yield exactly "
            f"ONE capture entry (content+session dedup key, not native "
            f"message/event id): {len(corrections)} correction entries")
        # DIFFERENT text stays separate: the classified tool-failure entry
        # above (a different kind and text) remains its OWN capture entry
        # alongside the correction entry — only the identical (kind, text)
        # pair dedups, so distinct texts never merge into one key.
        others = [e for e in queue if e != corrections[0]]
        assert others, (
            f"entries with DIFFERENT text must remain separate capture "
            f"entries (only the identical (kind, text) pair dedups): "
            f"{json.dumps(queue)[:400]!r}")
        # Explicit event-id replay leg (R2-6, additive scenario support):
        # CAPTURE_EVENT_ID forwards ONE explicit id onto BOTH correction
        # messages' source (the driver reports it back), so this leg
        # replays the SAME explicit id — still exactly one capture entry.
        obs_id = harness.ok_or_red(harness.run_scenario(
            "corrections_and_failures", tmp / "state-evid.json",
            {**_local_env(tmp / "store.sqlite", data),
             "CAPTURE_EVENT_ID": "t14-explicit-replay-771"}),
            "corrections_and_failures")
        assert obs_id.get("captureEventId") == "t14-explicit-replay-771", (
            f"the driver must pin the explicit capture-event-id leg: "
            f"{obs_id.get('captureEventId')!r}")
        queue_id = capture_queue(obs_id.get("pluginState") or {})
        corrections_id = [e for e in queue_id
                          if CORRECTION_MARK in json.dumps(e)]
        assert corrections_id, (
            f"the explicit-id leg must still capture the correction: "
            f"{json.dumps(queue_id)[:400]!r}")
        assert len(corrections_id) == 1, (
            f"replaying the SAME explicit capture event id must not "
            f"duplicate the capture entry: {len(corrections_id)} entries")
        # The injected zmem message DID reach the model (sanity: the
        # driver delivered it) — pin the fixture, not the capture.
        assert any((m.get("text80") or "").startswith(INJECTED_MARK[:40])
                   or INJECTED_MARK[:40] in (m.get("text80") or "")
                   for r in obs.get("requests") or [] for m in r["messages"])


def test_retention_governance():
    """ZMEM_AUTO_RETAIN=0 disables automatic retention (candidates only);
    =1 makes it effective; replayed duplicate corrections dedup by the
    content+session key; caller-claimed signal/taint escalation is
    clamped."""
    harness.require_runtime()
    with harness.isolated_env("t14-retention") as tmp:
        data = tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        base = _local_env(tmp / "store.sqlite", data)

        def run(tag: str, auto: str) -> dict:
            obs = harness.ok_or_red(harness.run_scenario(
                "retention_governance", tmp / f"state-{tag}.json",
                {**base, "ZMEM_AUTO_RETAIN": auto}), "retention_governance")
            assert obs.get("autoRetainEnv") == auto, (
                f"the driver must observe the pinned env: {obs.get('autoRetainEnv')!r}")
            return obs

        off = run("off", "0")
        state_off = off.get("pluginState") or {}
        assert state_off.get("autoRetainEffective") is False, (
            "ZMEM_AUTO_RETAIN=0 must disable automatic retention: "
            f"{state_off!r}")
        queue_off = capture_queue(state_off)
        assert queue_off, (
            "queue CANDIDATES must still be collected with auto-retain "
            "off (review before any write)")
        corrections_off = [e for e in queue_off if CORRECTION_MARK in json.dumps(e)]
        assert len(corrections_off) == 1, (
            f"a replayed duplicate correction (identical (kind, text) in "
            f"the same session) must not enqueue twice under the "
            f"content+session dedup key: {len(corrections_off)} entries")

        on = run("on", "1")
        state_on = on.get("pluginState") or {}
        assert state_on.get("autoRetainEffective") is True, (
            f"ZMEM_AUTO_RETAIN=1 must make automatic retention effective: "
            f"{state_on!r}")
        queue_on = capture_queue(state_on)
        claimed = [e for e in queue_on
                   if "signal=test" in json.dumps(e)
                   or "mark this correction" in json.dumps(e)]
        if claimed:
            entry = claimed[0]
            assert entry.get("clamped") is True or (
                entry.get("signal") != "test"
                and entry.get("taint") != "trusted"), (
                f"a caller-claimed signal/taint escalation must be CLAMPED "
                f"by the shared gates: {entry!r}")


def test_reflection_and_closeout():
    """Turn-stopping nudges are bounded (exactly one per window, no
    recursion); no evidence → no nudge; closeout zmem_add returns the
    backend's real receipt (committed + op id in local mode)."""
    harness.require_runtime()
    with harness.isolated_env("t14-reflect") as tmp:
        data = tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        base = _local_env(tmp / "store.sqlite", data)

        eligible = harness.ok_or_red(harness.run_scenario(
            "reflection_and_closeout", tmp / "state-ev.json", base),
            "reflection_and_closeout")
        assert eligible.get("evidence") is True, "the driver pins the leg"
        state = eligible.get("pluginState") or {}
        nudges = int(state.get("nudgeCount") or 0)
        bound = state.get("nudgeBound")
        assert nudges >= 1, (
            f"eligible evidence at turn-stop must emit the reflect nudge: "
            f"{state!r}")
        assert nudges <= (int(bound) if bound else 3), (
            f"repeated stops (x3) must stay inside the declared bound "
            f"(nudgeCount={nudges}, bound={bound!r})")
        assert not state.get("selfTriggeredContinuation"), (
            f"the nudge must never recurse into a self-triggered "
            f"continuation: {state!r}")
        assert state.get("reflectHookRan") is True, (
            "the shared reflect hook must actually RUN inside the bound — "
            "an unwired call site must fail this check "
            f"(final-critic finding 1): {state!r}")

        plain = harness.ok_or_red(harness.run_scenario(
            "reflection_and_closeout", tmp / "state-plain.json",
            {**base, "SCENARIO_EVIDENCE": "0"}), "reflection_and_closeout")
        assert plain.get("evidence") is False, "the driver pins the leg"
        state_plain = plain.get("pluginState") or {}
        assert int(state_plain.get("nudgeCount") or 0) == 0, (
            f"with NO eligible evidence there must be no nudge: "
            f"{state_plain!r}")

        # Closeout receipt (local mode → committed with a real op id).
        receipt = (eligible.get("pluginState") or {}).get("closeoutReceipt")
        tool_texts = [e.get("toolText120") or "" for e
                      in eligible.get("events", [])
                      if e.get("type") == "tool/result"]
        committed = (isinstance(receipt, dict)
                     and receipt.get("status") == "committed"
                     and receipt.get("op_id")) or any(
            "committed" in t and "op_id" in t for t in tool_texts)
        assert committed, (
            f"the closeout zmem_add must return the backend's real receipt "
            f"(committed with op id in local mode); receipt={receipt!r} "
            f"tool results={tool_texts!r}")


# -- python-only: no dsh runtime needed ---------------------------------------

def _write_export(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [json.dumps(DSH_EXPORT_HEADER)] + [json.dumps(l) for l in lines]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def _mine(source_dir: Path, store: Path, data: Path):
    env = harness.store_env(store, data)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(harness.STORE_PY), "mine-history",
         "--source", "deepseek", "--transcript-dir", str(source_dir),
         "--json"],
        capture_output=True, text=True, env=env, cwd=str(harness.REPO_ROOT),
        stdin=subprocess.DEVNULL, timeout=90)


def test_source_provenance():
    """mine-history --source deepseek parses the versioned dsh
    session-export JSONL with dsh-session:<name> provenance and refuses
    Claude-JSONL shapes, unsafe path refs, malformed lines and
    unsupported generations."""
    with harness.isolated_env("t14-provenance") as tmp:
        store, data = tmp / "store.sqlite", tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        good = tmp / "exports-good"
        _write_export(good / "session-alpha.jsonl", [
            {"seq": 1, "type": "user/message", "data": {
                "source": {"kind": "user"},
                "message": {"role": "user", "content": [{"type": "text",
                    "text": "Actually, correction: the flange order is "
                            "reversed — assemble beta before alpha."}]}}},
            {"seq": 2, "type": "tool/call", "data": {
                "name": "apply_patch", "arguments": {"path": "src/a.ts"}}},
            {"seq": 3, "type": "tool/result", "data": {
                "error": {"message": "deploy failed: connection refused"},
                "message": {"role": "tool", "content": [{"type": "text",
                    "text": "deploy failed"}]}}},
            {"seq": 4, "type": "compaction/start", "data": {}},
            {"seq": 5, "type": "compaction/end", "data": {}},
            {"seq": 6, "type": "session/resume", "data": {"from": "alpha"}},
            {"seq": 7, "type": "session/fork", "data": {"child": "beta"}},
        ])
        proc = _mine(good, store, data)
        assert proc.returncode == 0, (
            f"mine-history --source deepseek failed at base (expected RED: "
            f"the adapter does not exist yet): rc={proc.returncode}; "
            f"stderr={harness.tail(proc.stderr)!r}; stdout tail="
            f"{harness.tail(proc.stdout)!r}")
        report = json.loads(proc.stdout)
        candidates = report.get("candidates") or []
        assert candidates, "the export's correction/failure records must " \
            f"yield candidates: {report!r}"
        assert all(str(c.get("source") or "").startswith("dsh-session:")
                   for c in candidates), (
            f"every mined candidate must carry dsh-session:<name> source "
            f"provenance: {[c.get('source') for c in candidates]!r}")
        assert any("flange order" in str(c.get("message") or "")
                   for c in candidates), (
            "the user correction must be mined")
        assert any("deploy failed" in str(c.get("message") or "")
                   or "connection refused" in str(c.get("message") or "")
                   for c in candidates), (
            "the tool-failure record must be mined")

        claude_dir = tmp / "exports-claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        (claude_dir / "cc.jsonl").write_text(json.dumps({
            "type": "user", "message": {"content": [{"type": "text",
                "text": "Actually, correction: claude-shaped transcript"}]}}
        ) + "\n", encoding="utf-8")
        proc = _mine(claude_dir, store, data)
        assert proc.returncode != 0 and ("claude" in (
            proc.stdout + proc.stderr).lower() or "shape" in (
            proc.stdout + proc.stderr).lower()), (
            f"a Claude-JSONL-shaped file must be REFUSED with the shape "
            f"mismatch named: rc={proc.returncode} out="
            f"{harness.tail(proc.stdout + proc.stderr)!r}")

        traversal = tmp / "exports-traversal"
        _write_export(traversal / "evil.jsonl", [
            {"seq": 1, "type": "user/message", "data": {
                "source_ref": "../../etc/passwd",
                "message": {"content": [{"type": "text",
                    "text": "Actually, correction: traversal probe"}]}}}])
        proc = _mine(traversal, store, data)
        blob = (proc.stdout + proc.stderr).lower()
        assert proc.returncode != 0 and ("path" in blob or "unsafe" in blob), (
            f"an unsafe path-traversal ref must be refused: rc="
            f"{proc.returncode} out={harness.tail(blob)!r}")

        malformed = tmp / "exports-malformed"
        malformed.mkdir(parents=True, exist_ok=True)
        (malformed / "bad.jsonl").write_text(
            json.dumps(DSH_EXPORT_HEADER) + "\nnot json {{\n",
            encoding="utf-8")
        proc = _mine(malformed, store, data)
        blob = (proc.stdout + proc.stderr).lower()
        assert proc.returncode != 0 and any(
            w in blob for w in ("malformed", "parse", "json")), (
            f"a malformed line must be refused: rc={proc.returncode} "
            f"out={harness.tail(blob)!r}")

        v99 = tmp / "exports-v99"
        v99.mkdir(parents=True, exist_ok=True)
        (v99 / "future.jsonl").write_text(json.dumps({
            "format": "dsh-session-export", "version": 99}) + "\n",
            encoding="utf-8")
        proc = _mine(v99, store, data)
        blob = (proc.stdout + proc.stderr).lower()
        assert proc.returncode != 0 and ("version" in blob
                                         or "unsupported" in blob), (
            f"a version-99 export must be refused as an unsupported "
            f"generation: rc={proc.returncode} out={harness.tail(blob)!r}")


def test_recovery_and_diagnostics():
    """Disconnect retains pending ops; reconnect+flush commits receipts
    with read-your-writes; snapshot expiry is honored; doctor --fleet
    reports the deepseek-plugin check without ever leaking the token."""
    harness.require_runtime()
    if not harness.mcp_available():  # pragma: no cover - CI installs
        print("SKIP: mcp package absent — recovery needs the real hub")
        return
    with harness.isolated_env("t14-recovery") as tmp:
        hub_dir = tmp / "hub"
        harness.seed_store(hub_dir / "store.sqlite", hub_dir, [(
            "user:global", "fact",
            "t14 recovery hub canary: quartz windlass ledger")])
        with harness.Hub(hub_dir) as hub:
            data = tmp / "data-remote"
            data.mkdir(parents=True, exist_ok=True)
            obs = harness.ok_or_red(harness.run_scenario(
                "reconnect_flush", tmp / "state.json", {
                    "ZCODE_PLUGIN_DATA": str(data),
                    "ZMEM_REMOTE_URL": "http://127.0.0.1:1",
                    "ZMEM_MCP_TOKEN": harness.HUB_TOKEN,
                    "ZMEM_LIVE_HUB": hub.url,
                }), "reconnect_flush")
            state = obs.get("pluginState") or {}
            receipts = state.get("receipts")
            queue = data / "queue" / "ops-local.jsonl"
            committed = (isinstance(receipts, list) and any(
                str(r.get("status") or "").find("commit") >= 0
                for r in receipts if isinstance(r, dict)))
            if not committed and queue.is_file():
                entries = harness.queue_entries(queue)
                committed = any(e.get("status") not in (None, "pending")
                                or e.get("receipt") for e in entries)
            assert committed, (
                f"after the reconnect the queued ops must flush to the "
                f"live hub and the receipts commit: receipts={receipts!r}")
            # Read-your-writes: the queued row is recallable via the hub.
            env = harness.scenario_env(tmp / "unused.json", {
                "ZCODE_PLUGIN_DATA": str(data),
                "ZMEM_REMOTE_URL": hub.url,
                "ZMEM_MCP_TOKEN": harness.HUB_TOKEN})
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", str(harness.STORE_PY),
                 "recall", "--query", "reconnect probe row", "--namespace",
                 "user:global", "--json"],
                capture_output=True, text=True, env=env,
                cwd=str(harness.REPO_ROOT), stdin=subprocess.DEVNULL,
                timeout=90)
            assert proc.returncode == 0 and "reconnect probe row" \
                in proc.stdout, (
                f"read-your-writes: the flushed row must be recallable; "
                f"rc={proc.returncode} stdout={proc.stdout[:300]!r} "
                f"stderr={harness.tail(proc.stderr)!r}")

            # Snapshot expiry/skew (the t11 pattern): dead hub + expired
            # snapshot → nothing served, distinct reason.
            data_snap = tmp / "data-snap"
            src = tmp / "snap-src"
            harness.seed_store(src / "store.sqlite", src, [(
                "user:global", "fact", "t14 expired snapshot row")])
            harness.build_snapshot(src / "store.sqlite", data_snap)
            meta_path = data_snap / "broker" / "snapshot.meta.json"
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            meta["expires_at"] = time.time() - 3600.0
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            obs_snap = harness.ok_or_red(harness.run_scenario(
                "offline_snapshot", tmp / "state-snap.json", {
                    "ZCODE_PLUGIN_DATA": str(data_snap),
                    "ZMEM_REMOTE_URL": "http://127.0.0.1:1",
                    "ZMEM_MCP_TOKEN": harness.HUB_TOKEN,
                }), "offline_snapshot")
            assert not any(zmem_msgs(r) for r in
                           obs_snap.get("requests") or []), (
                "an expired snapshot must never serve rows")
            reason = str((obs_snap.get("pluginState") or {})
                         .get("reason") or "")
            assert "snapshot-expired" in reason \
                or "hub-unreachable" in reason, (
                f"the expiry reason must be recorded: {reason!r}")

        # doctor --fleet: the eighth fleet check, privacy-safe.
        token = "t14-doctor-token-do-not-print-771c"
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZMEM_")}
        env.update({
            "ZMEM_HOME": str(harness.REPO_ROOT),
            "ZMEM_HOST": "deepseek",
            "ZMEM_STORE": str(tmp / "doctor-store.sqlite"),
            "ZMEM_DATA": str(tmp / "doctor-data"),
            "ZMEM_MCP_TOKEN": token,
            "ZMEM_MODELS_DIR": str(tmp / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONIOENCODING": "utf-8",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        })
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", str(harness.DOCTOR_PY),
             "--fleet", "--format", "json"],
            capture_output=True, text=True, env=env,
            cwd=str(harness.REPO_ROOT), stdin=subprocess.DEVNULL,
            timeout=120)
        assert proc.stdout.strip(), (
            f"doctor --fleet wrote no JSON (rc={proc.returncode}); "
            f"stderr={harness.tail(proc.stderr)!r}")
        report = json.loads(proc.stdout)
        check = next((c for c in report.get("checks", [])
                      if c.get("id") == "deepseek-plugin"), None)
        assert check is not None, (
            f"doctor --fleet must expose the deepseek-plugin check (the "
            f"eighth fleet check); ids: "
            f"{[c.get('id') for c in report.get('checks', [])]!r}")
        assert check.get("status") in ("pass", "warn", "fail"), (
            f"the deepseek-plugin check must report a verdict: {check!r}")
        details = json.dumps(check).lower()
        assert "version" in details and ("activation" in details
                                         or "restart" in details), (
            f"the deepseek-plugin check must report version + activation/"
            f"restart state: {check!r}")
        assert token not in proc.stdout, (
            "PRIVACY: the doctor report must NEVER echo credential values")


def test_compatibility_claim_gate():
    """The COMMITTED T-14 support report validates (schema
    zmem-deepseek-support-v1, per-combination evidence refs, tested
    floors, the frozen capability-gap disclosures); first-class claims
    without evidence fail. No runtime."""
    assert harness.SUPPORT_REPORT.is_file(), (
        "docs/host-gate-evidence/deepseek/support-report.json is ABSENT — "
        "the T-14 compatibility claim is a committed artifact the gate "
        "validates (produce it from real canary runs; runtime legs "
        "skipping in CI cannot vacuously pass this gate)")
    report = json.loads(harness.SUPPORT_REPORT.read_text(encoding="utf-8"))
    problems = support_report_problems(report)
    assert not problems, "support-report claim-gate violations: " \
        + "; ".join(problems)
    # Doctored legs: a macOS first-class claim without evidence fails
    # validation, and dropping the frozen disclosures fails too.
    doctored = json.loads(json.dumps(report))
    claimed = doctored.get("claimed")
    if isinstance(claimed, list):
        claimed.append({"os": "macos", "carrier": "desktop"})
    else:
        doctored["claimed"] = [{"os": "macos", "carrier": "desktop",
                                "evidence": ""}]
    problems = support_report_problems(doctored)
    assert any("without evidence" in p for p in problems), (
        "a doctored first-class claim without an evidence ref must FAIL "
        "validation")
    stripped = json.loads(json.dumps(report))
    for key in ("capability_gaps", "gaps"):
        stripped.pop(key, None)
    assert support_report_problems(stripped), (
        "a report with the frozen capability-gap disclosures removed must "
        "FAIL validation")


_CHECKS = {
    "corrections_and_failures": test_corrections_and_failures,
    "retention_governance": test_retention_governance,
    "reflection_and_closeout": test_reflection_and_closeout,
    "source_provenance": test_source_provenance,
    "recovery_and_diagnostics": test_recovery_and_diagnostics,
    "compatibility_claim_gate": test_compatibility_claim_gate,
}


def _main(argv: list[str]) -> int:
    return harness.main_runner(_CHECKS, argv)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
