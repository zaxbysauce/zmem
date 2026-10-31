"""Arm's-length frozen checks for the DeepSeek (dsh/Cordis) adapter —
issue #374 (T-13). RED at base 09dfbf78: deepseek-plugin/ does not exist,
so the frozen scenario driver's plugin import fails (exit 1,
{"ok":false}) and the committed canary report is absent.

Driver: spawn ``node tests/fixtures/deepseek/scenario.mjs`` with env
SCENARIO/ZMEM_DEEPSEEK_STATE/ZMEM_ROOT/PYTHON (+ leg env); parse the ONE
stdout JSON line (the driver never decides pass/fail). Runtime resolves
from deepseek-plugin/node_modules or env ZMEM_DEEPSEEK_RUNTIME; runtime
legs skip "dsh runtime not installed" when absent — EXCEPT
test_install_release_contract and test_native_canary_claim_gate, which
run WITHOUT the runtime (the claim gate validates the COMMITTED report
docs/host-gate-evidence/deepseek/canary-report.json).

Env discipline (the t08/t10/t11 pinning): every check runs inside
isolated_env (fresh GUID scratch, ambient ZMEM_* + plugin-data stripped
BEFORE pinning, restored after; ZMEM_MODELS_DIR nonexistent +
ZMEM_MODEL_AUTODOWNLOAD=0 in every spawned env; no in-process storelib
import). Subprocess budget 90 s (kill + fail). Runs under pytest
(``python -X utf8 -m pytest --disable-plugin-autoload --noconftest -p
no:cacheprovider -q tests/test_t13_deepseek_contract.py``) and standalone.
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
import deepseek_harness as harness  # noqa: E402
from deepseek_assertions import (  # noqa: E402
    CANARY_EXPECTED_PACKAGES, SNAPSHOT_LABEL, ZMEM_KIND, admissions,
    canary_problems, release_surface_problems, zmem_msgs)

# The repo's actual context fence token (pinned from code, not invented):
# storelib/recall.py ZMEM_FENCE_OPEN = "<<<ZMEM_UNTRUSTED_FENCE>>>" (the
# rendered delivered text opens with that line, ZMEM_FENCE_CLOSE ends it),
# and fleet/broker.py _NEUTRALIZE_TABLE rewrites it to
# "<<<ZMEM_UNTRUSTED_FENCE_NEUTRALIZED>>>" on remote envelopes. The prefix
# below matches both flavors.
ZMEM_FENCE_PREFIX = "<<<ZMEM_UNTRUSTED_FENCE"

# The delivery-ledger side-effect file name (storelib/delivery_ledger.py
# ledger_path): sha256(_session_key(...))[:32] + suffix under <data>/ops/.
# The ledger data dir resolves through the STORE precedence
# (storelib/inject.py _injection_data_dir: ZMEM_STORE's parent wins over
# ZMEM_DATA), so the probe checks the store's parent ops/ and the ZMEM_DATA
# ops/ alike.
_LEDGER_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(ledger|pending)$")
_LEDGER_CONTENT_RE = re.compile(r"deliver|reserv|ack|\"id\"|\bid\b", re.I)



def _seed_one_memory(store: Path, data_dir: Path, content: str,
                     source_ref: str = "session:t13-seed") -> None:
    """Seed ONE deterministic memory into the isolated store through the
    PUBLIC store.py add CLI with its REAL flags (storelib/cli.py p_add:
    --namespace/--type/--content/--source-ref/--confidence/--json) — the
    seeding must go through the same public surface a host uses."""
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(harness.STORE_PY), "add",
         "--namespace", "user:global", "--type", "fact",
         "--content", content, "--source-ref", source_ref,
         "--confidence", "0.9", "--json"],
        capture_output=True, text=True,
        env=harness.store_env(store, data_dir),
        cwd=str(harness.REPO_ROOT), stdin=subprocess.DEVNULL,
        timeout=harness.STORE_SUBPROC_TIMEOUT_S)
    assert proc.returncode == 0, (
        f"TEST HARNESS failure (not an adapter verdict): store.py add "
        f"seeding failed rc={proc.returncode}; stderr="
        f"{harness.tail(proc.stderr)!r}")


def _ledger_sidecar_files(*data_dirs: Path) -> list:
    """Ledger/reservation-derived sidecars under any of the isolated
    <dir>/ops/ trees whose name is sha256(key)[:32] + '.ledger' (or the
    '.pending' reservation sidecar — delivery_ledger.py ledger_path/
    pending_path). '.ledger' files sort first (the primary artifact);
    unrelated ops markers (e.g. the CLI add '.wrote' token) never match."""
    found = []
    for d in data_dirs:
        ops = d / "ops"
        if not ops.is_dir():
            continue
        found.extend(p for p in ops.iterdir()
                     if _LEDGER_NAME_RE.match(p.name))
    found.sort(key=lambda p: (p.suffix != ".ledger", p.name))
    return found



def test_first_request_delivery():
    """First model-bound request carries the fenced context; rejected
    input is never acknowledged; reservation ack is idempotent."""
    harness.require_runtime()
    with harness.isolated_env("t13-first") as tmp:
        data = tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        store = tmp / "store.sqlite"
        base_env = {"ZMEM_STORE": str(store), "ZMEM_DATA": str(data)}
        operator_before = harness.operator_store_fingerprint()
        # Seeded-memory leg (issue #374 AC1): ONE deterministic row lands in
        # the isolated store BEFORE the scenario via the public add CLI, so
        # the first delivery's content is attributable, not incidental.
        # The seed must LEXICALLY OVERLAP the query the scenario actually
        # sends (scenario.mjs first_request_delivery hardcodes
        # 'first delivery probe: summarize the windlass ledger' — no query
        # env override exists, so the SEED content carries the query's
        # distinctive terms; plan-critic round-2 R2-2 proved the old
        # 't13 seeded memory alpha-<uid>' seed returns reason empty-pool
        # through the real seam). The uniqueness suffix is 12 hex chars
        # ONLY: redaction.py's hex-run rule (\b[0-9a-fA-F]{32,}\b)
        # replaces a full 32-hex uuid4().hex in content with
        # [REDACTED_SECRET], which would erase the needle.
        # EXECUTED PAIR PROOF (2026-10-27, this host, model-absent FTS
        # via ZMEM_MODELS_DIR pinning, store.py prefetch --query "first
        # delivery probe: summarize the windlass ledger" --namespace
        # user:global --session-id probe --moment user_prompt --reserve
        # --json): seed content 't13 seeded memory alpha-497de4efd0ba:
        # first delivery probe summarize the windlass ledger' →
        # {"reason": "injected", "count": 1, "reservation":
        # "be59fcf0f3b147138e4091de53662ebf", "rendered": non-empty,
        # opening <<<ZMEM_UNTRUSTED_FENCE>>> and carrying the seed
        # verbatim}. (--lane deepseek itself is part of the not-yet-landed
        # adapter surface at base — argparse rejects it — so the probe ran
        # --lane zcode; the FTS pool is lane-independent.)
        uid = uuid.uuid4().hex[:12]
        seeded = (f"t13 seeded memory alpha-{uid}: first delivery probe "
                  f"summarize the windlass ledger")
        seeded_prefix = f"t13 seeded memory alpha-{uid}"
        _seed_one_memory(store, data, seeded)
        obs = harness.ok_or_red(harness.run_scenario(
            "first_request_delivery", tmp / "state.json", base_env),
            "first_request_delivery")
        requests = obs.get("requests") or []
        assert requests, "the scripted adapter saw no model requests"
        zmem1 = zmem_msgs(requests[0])
        assert zmem1, (
            "issue #374 AC1: the FIRST model-bound request must carry the "
            "fenced zmem context (sourceKind 'zmem'); request 1 messages: "
            f"{requests[0]['messages']!r}")
        state = obs.get("pluginState") or {}
        delivered = state.get("deliveredText")
        assert isinstance(delivered, str) and delivered, (
            f"the plugin state must report deliveredText; got {state!r}")
        assert any((m.get("text80") or "") and m["text80"] in delivered
                   for m in zmem1), (
            "deliveredText must appear inside request 1's zmem message; "
            f"delivered={delivered[:120]!r} zmem={zmem1!r}")
        assert seeded_prefix in delivered \
            or seeded_prefix in "".join(m.get("text80") or "" for m in zmem1), (
            f"issue #374 AC1: the SEEDED memory {seeded_prefix!r} must reach "
            f"the FIRST request's zmem-sourced message; delivered="
            f"{delivered[:200]!r} zmem={zmem1!r}")
        assert any(line.startswith(ZMEM_FENCE_PREFIX)
                   for line in delivered.splitlines()), (
            f"the delivered text must open its untrusted context with the "
            f"repo fence token {ZMEM_FENCE_PREFIX!r} (storelib/recall.py "
            f"ZMEM_FENCE_OPEN / broker _NEUTRALIZE_TABLE); delivered="
            f"{delivered[:200]!r}")
        assert state.get("reserved") is True and state.get("reservationId") \
            and state.get("acked") is True, (
            f"delivery-ledger reservation must be reserved+acked: {state!r}")
        # Delivery-ledger side effect on disk: the isolated ops/ tree holds
        # the ledger (or reservation sidecar) named sha256(key)[:32] +
        # suffix, non-empty, mentioning delivered ids / reservation state.
        ledgers = _ledger_sidecar_files(tmp, data)
        assert ledgers, (
            f"the delivery ledger must land in the isolated ops/ tree "
            f"(sha256(key)[:32] + '.ledger' under <data>/ops/); ops dirs: "
            f"{(tmp / 'ops').is_dir()=} {(data / 'ops').is_dir()=}")
        ledger_text = ledgers[0].read_text(encoding="utf-8",
                                           errors="replace")
        assert ledger_text.strip(), (
            f"the delivery ledger {ledgers[0]} must not be empty")
        assert _LEDGER_CONTENT_RE.search(ledger_text), (
            f"the delivery ledger must record delivered ids or reservation "
            f"state; {ledgers[0].name} head={ledger_text[:200]!r}")
        assert len(admissions(obs, "ds-first-delivery")) == 1, \
            "exactly one zmem user/message admission per session"
        # Series-marker probe (plan-critic round-2 R2-9, coordinated with
        # helpers.mjs makeSeriesProbe / scenario.mjs registration order):
        # the scenario registers an outermost OBSERVER before the plugin
        # and the marker-INJECTOR innermost (after the plugin), so the
        # plugin RECEIVES startsRequestSeries: true on the base decision
        # and must preserve the marker through its spread mutations while
        # appending exactly ONE zmem message.
        series = obs.get("series") or {}
        assert series.get("seriesPreserved") is True, (
            f"issue #374 AC1: the plugin must PRESERVE the injected "
            f"startsRequestSeries marker when it appends its zmem message "
            f"(final messages must equal base + 1, marker still true): "
            f"{series!r}")

        # Rejection leg: a scripted rejecting pre-step listener registered
        # BEFORE the plugin; that agent's input is never admitted/acked.
        obs2 = harness.ok_or_red(harness.run_scenario(
            "first_request_delivery", tmp / "state-reject.json",
            {**base_env, "SCENARIO_REJECT": "1"}), "first_request_delivery")
        rej = obs2.get("rejected") or {}
        assert rej.get("requestCountAfter") == rej.get("requestCountBefore"), (
            f"rejected input must never reach the model: {rej!r}")
        assert ZMEM_KIND not in (rej.get("admittedSourceKinds") or []), (
            f"no zmem context admitted for the rejected agent: {rej!r}")
        rej_state = harness.deepseek_agent_state(obs2.get("pluginState"),
                                                 "ds-rejected-input")
        assert rej_state is not None and rej_state.get("acked") is False, (
            f"rejected/cancelled input must NOT be acknowledged; state for "
            f"ds-rejected-input: {rej_state!r}")

        # Idempotency leg: rerun with the SAME session id against the same
        # isolated store — no duplicate admission, ack idempotent.
        obs3 = harness.ok_or_red(harness.run_scenario(
            "first_request_delivery", tmp / "state-idem.json", base_env),
            "first_request_delivery")
        assert len(admissions(obs3, "ds-first-delivery")) == 1, (
            "a rerun with the same session id must not re-admit zmem context")
        state3 = obs3.get("pluginState") or {}
        assert state3.get("reserved") is True and state3.get("acked") is True, (
            f"the reservation ack must be idempotent on rerun: {state3!r}")
        assert harness.operator_store_fingerprint() == operator_before, (
            "the isolated run must never write the operator's default "
            "~/.zmem/store.sqlite (fingerprint compared around the whole "
            "scenario — the launcher's default path never engaged)")



def test_backend_parity():
    """Local and remote backends behave identically at the seams: local
    uses the isolated store only; remote enqueues receipts, never writes
    a local store, renders the pending overlay, and flush commits."""
    harness.require_runtime()
    with harness.isolated_env("t13-parity") as tmp:
        data = tmp / "data-local"
        data.mkdir(parents=True, exist_ok=True)
        store = tmp / "local-store.sqlite"
        obs = harness.ok_or_red(harness.run_scenario(
            "backend_parity", tmp / "state-local.json",
            {"ZMEM_STORE": str(store), "ZMEM_DATA": str(data)}),
            "backend_parity")
        state = obs.get("pluginState") or {}
        assert state.get("backendMode") == "local", (
            f"local leg must report backendMode 'local': {state!r}")
        used = state.get("storePathUsed")
        assert used and os.path.normcase(os.path.normpath(str(used))) \
            == os.path.normcase(os.path.normpath(str(store))), (
            f"storePathUsed must be the isolated ZMEM_STORE: {used!r}")
        assert not state.get("secondStoreCreated"), (
            f"the plugin must never initialize a second store: {state!r}")
        assert isinstance(state.get("envelopeKeys"), list) \
            and state["envelopeKeys"], "the closed envelope key set must " \
            f"be reported: {state!r}"

        if not harness.mcp_available():  # pragma: no cover - CI installs
            print("SKIP: mcp package absent — remote parity leg needs the "
                  "real hub")
            return
        hub_dir = tmp / "hub"
        harness.seed_store(hub_dir / "store.sqlite", hub_dir, [(
            "user:global", "fact",
            "t13 parity hub canary: zirconia windlass ledger binder")])
        with harness.Hub(hub_dir) as hub:
            data_r = tmp / "data-remote"
            data_r.mkdir(parents=True, exist_ok=True)
            obs_r = harness.ok_or_red(harness.run_scenario(
                "backend_parity", tmp / "state-remote.json", {
                    "ZCODE_PLUGIN_DATA": str(data_r),
                    "ZMEM_DATA": str(data_r),
                    "ZMEM_REMOTE_URL": hub.url,
                    "ZMEM_MCP_TOKEN": harness.HUB_TOKEN,
                }), "backend_parity")
            state_r = obs_r.get("pluginState") or {}
            assert state_r.get("backendMode") == "remote", (
                f"remote leg must report backendMode 'remote': {state_r!r}")
            assert not (data_r / "store.sqlite").exists(), (
                "remote mode must NEVER create/write a local store")
            queue = data_r / "queue" / "ops-local.jsonl"
            assert queue.is_file(), (
                "the explicit zmem_add write must enqueue into the durable "
                "op queue (remote_gate bands), never a local store write")
            pending_before = harness.queue_entries(queue)
            assert pending_before, "the queue file must hold the queued add"
            assert int(state_r.get("pendingReceipts") or 0) >= 1 or any(
                e.get("status") == "pending" for e in pending_before), (
                f"a pending receipt must be outstanding: {state_r!r}")
            # Pending overlay BEFORE the flush, then flush via the remote
            # read path (remote_gate flushes on reads; apply-ops commits).
            env = harness.scenario_env(tmp / "unused.json", {
                "ZCODE_PLUGIN_DATA": str(data_r),
                "ZMEM_DATA": str(data_r),
                "ZMEM_REMOTE_URL": hub.url,
                "ZMEM_MCP_TOKEN": harness.HUB_TOKEN,
            })
            proc = subprocess.run(
                [sys.executable, "-X", "utf8", str(harness.STORE_PY),
                 "recall", "--query", "zirconia windlass", "--namespace",
                 "user:global", "--json"],
                capture_output=True, text=True, env=env,
                cwd=str(harness.REPO_ROOT), stdin=subprocess.DEVNULL,
                timeout=90)
            assert proc.returncode == 0, (
                f"remote recall failed: {harness.tail(proc.stderr)!r}")
            # The overlay renders BEFORE the flush on the read path, but
            # the plugin's OWN prefetch after the reconnect already ran a
            # bounded flush (the sanctioned C12 reconnect design), so the
            # ops may legitimately be COMMITTED by this recall: accept
            # either the pending overlay or the already-flushed receipts.
            after_probe = harness.queue_entries(queue)
            already_flushed = any(
                e.get("status") not in (None, "pending")
                or e.get("receipt") for e in after_probe)
            assert "[PENDING]" in proc.stdout or already_flushed, (
                "the pending read-your-writes overlay must render BEFORE "
                "the flush (or the plugin's reconnect flush already "
                f"committed the ops); stdout={proc.stdout[:300]!r}")
            after = harness.queue_entries(queue)
            assert any(e.get("status") not in (None, "pending")
                       or e.get("receipt") for e in after), (
                f"the flush (hub apply-ops) must commit the queued receipt; "
                f"queue now: {after!r}")



def _identity_zmem_text(ob: dict) -> str:
    """Every zmem-sourced text one identity_and_lifecycle observation
    delivered: the per-agent request messages plus the per-session
    deliveredText the plugin state reports (text80 alone is capped at 80
    chars — the seed needle can sit past it)."""
    parts = []
    for info in (ob.get("agents") or {}).values():
        if not isinstance(info, dict):
            continue
        for req in list(info.get("requests", [])) \
                + list(info.get("postCompactionRequests", [])):
            for msg in req.get("messages") or []:
                parts.append(msg.get("text80") or "")
    state = ob.get("pluginState") or {}
    for session in ("ds-agent-a", "ds-agent-b"):
        sec = harness.deepseek_agent_state(state, session)
        if isinstance(sec, dict) and isinstance(sec.get("deliveredText"), str):
            parts.append(sec["deliveredText"])
    return "\n".join(p for p in parts if p)



def test_identity_and_lifecycle():
    """Two concurrent agents never bleed context; principals keep separate
    ledgers; fork/subagent lineage; compaction resets exactly once per
    compaction/end; dispose keeps persisted state; a same-store
    identity-collision leg (two principals, ONE store, identical native
    session ids) keeps independent first deliveries and distinct ledger
    sidecar files."""
    harness.require_runtime()
    with harness.isolated_env("t13-identity") as tmp:
        # One seeded memory PER QUERY each principal leg issues, each leg
        # in its own isolated store (the fleet model: each principal's box
        # has its own store) — behavioral isolation is then observable in
        # WHAT is delivered. The seeds carry the distinctive terms of the
        # queries scenario.mjs identity_and_lifecycle HARDCODES (no query
        # env override exists): agent A 'agent A probe: alpha ledger',
        # agent B 'agent B probe: beta ledger', and the post-compaction
        # probe 'post-compaction probe: delivery continues' (plan-critic
        # round-2 R2-2: the old 'bravo' seed matched neither 'alpha' nor
        # 'beta', and the post-compaction query had no undelivered match —
        # every pair below verified through the real store.py prefetch
        # seam, reason injected, count >= 1).
        uid = uuid.uuid4().hex[:8]
        seed_a = (f"t13 identity principal-a seed alpha-{uid}: "
                  f"agent A probe alpha ledger windlass")
        seed_post_a = (f"t13 identity post-compaction seed alpha-{uid}: "
                       f"post-compaction probe delivery continues")
        seed_b = (f"t13 identity principal-b seed bravo-{uid}: "
                  f"agent B probe beta ledger windlass")
        seed_post_b = (f"t13 identity post-compaction seed bravo-{uid}: "
                       f"post-compaction probe delivery continues")
        needle_a = "t13 identity principal-a seed"
        needle_b = "t13 identity principal-b seed"
        store_a, store_b = tmp / "store-a.sqlite", tmp / "store-b.sqlite"
        data_a, data_b = tmp / "data-a", tmp / "data-b"
        data_a.mkdir(parents=True, exist_ok=True)
        data_b.mkdir(parents=True, exist_ok=True)
        _seed_one_memory(store_a, data_a, seed_a)
        _seed_one_memory(store_a, data_a, seed_post_a)
        _seed_one_memory(store_b, data_b, seed_b)
        _seed_one_memory(store_b, data_b, seed_post_b)

        def run(principal: str, tag: str, store: Path, data_dir: Path) -> dict:
            return harness.ok_or_red(harness.run_scenario(
                "identity_and_lifecycle", tmp / f"state-{tag}.json",
                {"ZMEM_STORE": str(store), "ZMEM_DATA": str(data_dir),
                 "ZMEM_PRINCIPAL": principal}),
                "identity_and_lifecycle")

        obs = run("t13-principal-a", "a", store_a, data_a)
        agents = obs.get("agents") or {}
        a_info, b_info = agents.get("ds-agent-a"), agents.get("ds-agent-b")
        assert a_info and b_info, f"both agents must be observed: {agents!r}"
        a_reqs = list(a_info.get("requests", [])) \
            + list(a_info.get("postCompactionRequests", []))
        assert any(zmem_msgs(r) for r in a_reqs), "agent A gets its context"
        assert any(zmem_msgs(r) for r in b_info["requests"]), \
            "agent B gets its context"
        state = obs.get("pluginState") or {}
        for session in ("ds-agent-a", "ds-agent-b"):
            section = harness.deepseek_agent_state(state, session)
            assert section is not None, (
                f"plugin state must carry a per-session section for "
                f"{session}: {state!r}")
            assert section.get("namespace") == "user:global" \
                and section.get("lane") == "deepseek", (
                f"per-session identity must pin namespace+lane: {section!r}")
            assert section.get("ledgerKeyDistinct") is True, (
                f"ledger keys must be distinct per session/principal: "
                f"{section!r}")
        # BEHAVIORAL isolation (issue #374 AC3 — stronger than, and in
        # addition to, the self-reported ledgerKeyDistinct above): what
        # principal-a's agents actually RECEIVED carries the A-seed and
        # never the B-seed (and vice versa below).
        text_a = _identity_zmem_text(obs)
        assert needle_a in text_a, (
            f"principal-a's delivered context must carry its own seeded "
            f"memory {needle_a!r}: {text_a[:200]!r}")
        assert needle_b not in text_a, (
            "CROSS-PRINCIPAL BLEED: principal-a received principal-b's "
            f"seeded memory {needle_b!r}")
        # Observed lineage (coordinate with the scenario's fork/subagent
        # fields): the fork child's session id differs from the parent's,
        # and the subagent declares its path with its own session id.
        fork = obs.get("fork") or {}
        assert fork.get("session") and fork.get("parent") \
            and fork["session"] != fork["parent"], (
            f"the observed fork lineage must report session+parent ids "
            f"that differ: {fork!r}")
        subagent_obs = obs.get("subagent") or {}
        assert subagent_obs.get("path") in ("native", "simulated"), (
            f"observed.subagent must declare path 'native' or 'simulated': "
            f"{subagent_obs!r}")
        assert subagent_obs.get("session") \
            and subagent_obs["session"] != fork.get("parent"), (
            f"the subagent must report a session id distinct from the "
            f"parent's: {subagent_obs!r}")
        delivered_a = (harness.deepseek_agent_state(state, "ds-agent-a")
                       or {}).get("deliveredText")
        delivered_b = (harness.deepseek_agent_state(state, "ds-agent-b")
                       or {}).get("deliveredText")
        if all(isinstance(d, str) and d for d in (delivered_a, delivered_b)) \
                and delivered_a != delivered_b:
            join = lambda reqs: "".join(m.get("text80") or "" for r in reqs for m in r["messages"])
            b_texts, a_texts = join(b_info["requests"]), join(a_reqs)
            # NEEDLE-based discrimination (amended): a fixed [:40] slice is
            # the SHARED fence + '# Relevant memories' header — identical
            # for every delivery — while both agents share the leg's store
            # and an overlapping English query legitimately FTS-matches the
            # other agent's seed (sessions are not a recall scope). The
            # per-principal NEEDLES are the honest discriminator.
            assert needle_a not in b_texts and \
                needle_b not in a_texts, (
                "CROSS-AGENT BLEED: each agent's own seeded memory must "
                "never appear in the other agent's requests")
        assert obs.get("compaction", {}).get("appended") is True, (
            f"the synthetic compaction markers must append: "
            f"{obs.get('compaction')!r}")
        assert state.get("compactionResets") == 1, (
            "exactly one ledger re-serialization per compaction/end; got "
            f"{state.get('compactionResets')!r}")
        assert any(zmem_msgs(r)
                   for r in a_info.get("postCompactionRequests", [])), (
            "delivery must continue after compaction")
        post_state = obs.get("pluginStateAfterDispose")
        assert post_state and harness.deepseek_agent_state(
            post_state, "ds-agent-a") is not None, (
            "agent dispose must NOT clear persisted-session state "
            f"(persistsAcrossDispose); post-dispose: {post_state!r}")
        # Identical session ids under a DIFFERENT principal keep separate
        # ledgers (same session ids, second principal's own store).
        obs_b = run("t13-principal-b", "b", store_b, data_b)
        text_b = _identity_zmem_text(obs_b)
        assert needle_b in text_b, (
            f"principal-b's delivered context must carry its own seeded "
            f"memory {needle_b!r}: {text_b[:200]!r}")
        assert needle_a not in text_b, (
            "CROSS-PRINCIPAL BLEED: principal-b received principal-a's "
            f"seeded memory {needle_a!r}")
        state_b = obs_b.get("pluginState") or {}
        for session in ("ds-agent-a", "ds-agent-b"):
            sec_b = harness.deepseek_agent_state(state_b, session)
            assert sec_b is not None and \
                sec_b.get("ledgerKeyDistinct") is True, (
                f"principal 't13-principal-b' must keep distinct ledgers "
                f"for {session}: {sec_b!r}")
            sec_a = harness.deepseek_agent_state(state, session)
            keys = [sec.get("ledgerKey") for sec in (sec_a, sec_b)
                    if isinstance(sec, dict)]
            if len(keys) == 2 and all(keys):
                assert keys[0] != keys[1], (
                    "identical session ids under different principals must "
                    f"map to distinct ledger keys: {keys!r}")

        # Same-store identity-collision leg (plan-critic round-2 R2-7): ONE
        # store + ONE data dir, TWO scenario runs with the SAME native
        # session ids (ds-agent-a / ds-agent-b) but ZMEM_PRINCIPAL
        # t13-principal-a then t13-principal-b, same namespace, TWO distinct
        # memories both matching the agents' queries' shared terms
        # (agent/probe/ledger — verified through the real prefetch seam:
        # each query surfaces BOTH rows). The delivery ledger keys
        # principal|harness|session, so principal-b's FIRST delivery must
        # NOT be suppressed by principal-a's delivery of the same rows, and
        # the two runs' ledger sidecar FILES under <store-parent>/ops/ must
        # differ (the file name is sha256(key)[:32] and the key embeds the
        # principal → distinct sha256-derived names). This asserts DELIVERY
        # IDENTITY only — content retrieval is namespace-scoped, not
        # principal-scoped, so cross-principal row visibility here is
        # expected, not a leak.
        col = tmp / "collision"
        col.mkdir(parents=True, exist_ok=True)
        store_col = col / "store.sqlite"
        data_col = col / "data"
        data_col.mkdir(parents=True, exist_ok=True)
        row_a = (f"t13 collision row alpha-{uid}: "
                 f"agent probe ledger windlass binder")
        row_b = (f"t13 collision row bravo-{uid}: "
                 f"agent probe ledger windlass binder")
        needle_col_a = "t13 collision row alpha"
        needle_col_b = "t13 collision row bravo"
        _seed_one_memory(store_col, data_col, row_a)
        _seed_one_memory(store_col, data_col, row_b)

        def run_col(principal: str, tag: str) -> dict:
            return harness.ok_or_red(harness.run_scenario(
                "identity_and_lifecycle", col / f"state-{tag}.json",
                {"ZMEM_STORE": str(store_col), "ZMEM_DATA": str(data_col),
                 "ZMEM_PRINCIPAL": principal}),
                "identity_and_lifecycle")

        obs_col_a = run_col("t13-principal-a", "col-a")
        text_col_a = _identity_zmem_text(obs_col_a)
        assert needle_col_a in text_col_a, (
            f"same-store collision: principal-a's first delivery must "
            f"carry its own row {needle_col_a!r}: {text_col_a[:200]!r}")
        ledgers_col_a = _ledger_sidecar_files(col, data_col)
        assert ledgers_col_a, (
            f"same-store collision: principal-a's run must leave its "
            f"delivery-ledger sidecar under the SHARED ops/ tree "
            f"(sha256(key)[:32] + '.ledger'): {(col / 'ops').is_dir()=} "
            f"{(data_col / 'ops').is_dir()=}")
        obs_col_b = run_col("t13-principal-b", "col-b")
        text_col_b = _identity_zmem_text(obs_col_b)
        assert needle_col_b in text_col_b, (
            f"same-store collision: principal-b's FIRST delivery must not "
            f"be suppressed by principal-a's delivery of the same rows "
            f"(the ledger keys principal|harness|session) and must deliver "
            f"the other row {needle_col_b!r}: {text_col_b[:200]!r}")
        col_b_reqs = list((obs_col_b.get("agents") or {})
                          .get("ds-agent-a", {}).get("requests", []))
        assert any(zmem_msgs(r) for r in col_b_reqs), (
            "same-store collision: principal-b's rerun with the SAME "
            "native session ids against the SAME store must deliver zmem "
            "context — an independent first delivery, not a suppressed "
            "rerun")
        ledgers_col_b = _ledger_sidecar_files(col, data_col)
        names_a = {p.name for p in ledgers_col_a}
        assert any(p.name not in names_a for p in ledgers_col_b), (
            f"same-store collision: principal-b's run must write its OWN "
            f"ledger sidecar FILES (distinct sha256-derived names from "
            f"principal-a's — the ledger key embeds the principal); "
            f"a-run={sorted(names_a)} "
            f"b-run={sorted(p.name for p in ledgers_col_b)}")



def test_timing_and_policy():
    """Pretool guidance is next-request only (or honestly declared as a
    capability gap); ZMEM_CAPTURE=0 is passive-off; oversized payloads
    are bounded; deadline applied; malformed backend fails open."""
    harness.require_runtime()
    with harness.isolated_env("t13-policy") as tmp:
        data = tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        base_env = {"ZMEM_STORE": str(tmp / "store.sqlite"),
                    "ZMEM_DATA": str(data)}
        obs = harness.ok_or_red(harness.run_scenario(
            "timing_and_policy", tmp / "state.json", base_env),
            "timing_and_policy")
        state = obs.get("pluginState") or {}
        inflight = obs.get("inFlightRequests") or []
        tool_events = obs.get("toolEvents") or []
        assert any(t.get("phase") == "pre-execute" for t in tool_events) and \
            any(t.get("phase") == "result" for t in tool_events), (
            f"the tools pipeline must fire pre-execute and result: "
            f"{tool_events!r}")
        assert state.get("deadlineApplied") is True, (
            f"the plugin must apply its request deadline: {state!r}")
        guidance = state.get("guidance")
        tool_guidance = state.get("toolGuidance")
        inflight_text = "".join((m.get("text80") or "")
                                for r in inflight for m in zmem_msgs(r))
        if guidance == "next-request-gap":
            pass  # the honest capability-gap declaration the AC accepts
        else:
            needle = tool_guidance[:40] if isinstance(tool_guidance, str) \
                and tool_guidance else "tool"
            assert needle not in inflight_text.lower(), (
                f"pretool guidance must be NEXT-request only, never the "
                f"in-flight one; guidance={guidance!r} in-flight zmem text="
                f"{inflight_text[:120]!r}")

        # Kill-switch leg: ZMEM_CAPTURE=0 → passive-off, no context.
        obs2 = harness.ok_or_red(harness.run_scenario(
            "timing_and_policy", tmp / "state-off.json",
            {**base_env, "ZMEM_CAPTURE": "0"}), "timing_and_policy")
        assert not any(zmem_msgs(r) for r in obs2.get("requests") or []), (
            "ZMEM_CAPTURE=0 (kill switch) must deliver NO zmem context")
        state2 = obs2.get("pluginState") or {}
        assert not state2.get("deliveredText"), (
            f"passive-off must record no delivered text: {state2!r}")

        # Oversized-payload leg: input far over the bound → bounded/clamped.
        obs3 = harness.ok_or_red(harness.run_scenario(
            "timing_and_policy", tmp / "state-big.json",
            {**base_env, "SCENARIO_OVERSIZED": "1"}), "timing_and_policy")
        state3 = obs3.get("pluginState") or {}
        big = state3.get("deliveredText")
        assert state3.get("bounded") is True or (isinstance(big, str)
                                                 and len(big) < 180000), (
            "oversized input must be bounded/clamped by the declared "
            f"bound: {sorted(state3.keys())!r}")

        # Malformed backend reply: ZMEM_STORE at an unreadable file →
        # fail open (empty envelope + reason), exit stays clean.
        garbage = tmp / "garbage.sqlite"
        garbage.write_bytes(b"this is not a sqlite database at all" * 8)
        obs4 = harness.ok_or_red(harness.run_scenario(
            "timing_and_policy", tmp / "state-bad.json",
            {**base_env, "ZMEM_STORE": str(garbage)}), "timing_and_policy")
        assert not any(zmem_msgs(r) for r in obs4.get("requests") or []), (
            "a malformed backend reply must fail OPEN with an empty envelope")
        assert (obs4.get("pluginState") or {}).get("reason"), (
            "the fail-open must record its reason: "
            f"{obs4.get('pluginState')!r}")



def test_offline_snapshot():
    """Unreachable hub serves the labelled snapshot; expired serves
    nothing with the distinct reason; namespace scope never leaks."""
    harness.require_runtime()
    with harness.isolated_env("t13-offline") as tmp:
        data = tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        marker = "t13-offline-canary: zirconia windlass calibration binder"
        src_dir = tmp / "snapshot-src"
        harness.seed_store(src_dir / "store.sqlite", src_dir,
                           [("user:global", "fact", marker)])
        harness.build_snapshot(src_dir / "store.sqlite", data,
                               namespace="user:global")
        remote_env = {"ZCODE_PLUGIN_DATA": str(data),
                      "ZMEM_DATA": str(data),
                      "ZMEM_REMOTE_URL": "http://127.0.0.1:1",
                      "ZMEM_MCP_TOKEN": harness.HUB_TOKEN}
        obs = harness.ok_or_red(harness.run_scenario(
            "offline_snapshot", tmp / "state.json", remote_env),
            "offline_snapshot")
        state = obs.get("pluginState") or {}
        delivered = state.get("deliveredText")
        if not (isinstance(delivered, str) and delivered):
            delivered = "".join(m.get("text80") or "" for r
                                in obs.get("requests") or []
                                for m in zmem_msgs(r))
        assert isinstance(delivered, str) and \
            delivered.startswith(SNAPSHOT_LABEL), (
            f"the offline serve must carry the label prefix "
            f"{SNAPSHOT_LABEL!r}; delivered={delivered[:120]!r}")
        assert marker[:40] in delivered, (
            f"the snapshot-only canary row must be served; delivered="
            f"{delivered[:200]!r}")

        # Expired snapshot: meta edited to the past → nothing presented as
        # current memory, distinct reason recorded.
        meta_path = data / "broker" / "snapshot.meta.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["expires_at"] = time.time() - 3600.0
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        obs2 = harness.ok_or_red(harness.run_scenario(
            "offline_snapshot", tmp / "state-exp.json", remote_env),
            "offline_snapshot")
        assert not any(zmem_msgs(r) for r in obs2.get("requests") or []), (
            "an EXPIRED snapshot must never be presented as current memory")
        reason = str((obs2.get("pluginState") or {}).get("reason") or "")
        assert "snapshot-expired" in reason or "hub-unreachable" in reason, \
            ("the distinct expiry/unreachable reason must be recorded: "
             f"{obs2.get('pluginState')!r}")

        # Namespace scope: snapshot authorized for project:t13-alpha, agent
        # pinned to project:t13-beta → no rows leak.
        data_ns = tmp / "data-ns"
        src_ns = tmp / "snapshot-src-ns"
        alpha_marker = "t13-alpha-secret: cobalt windlass binder ledger"
        harness.seed_store(src_ns / "store.sqlite", src_ns,
                           [("project:t13-alpha", "fact", alpha_marker)])
        harness.build_snapshot(src_ns / "store.sqlite", data_ns,
                               namespace="project:t13-alpha")
        obs3 = harness.ok_or_red(harness.run_scenario(
            "offline_snapshot", tmp / "state-ns.json", {
                "ZCODE_PLUGIN_DATA": str(data_ns),
                "ZMEM_DATA": str(data_ns),
                "ZMEM_REMOTE_URL": "http://127.0.0.1:1",
                "ZMEM_MCP_TOKEN": harness.HUB_TOKEN,
                "ZMEM_NAMESPACE": "project:t13-beta",
            }), "offline_snapshot")
        texts3 = [m.get("text80") or "" for r in obs3.get("requests") or []
                  for m in r["messages"]]
        assert not any(alpha_marker[:30] in t for t in texts3), (
            "NAMESPACE LEAK: an unauthorized project namespace's snapshot "
            f"row reached the agent: {texts3!r}")



def test_install_release_contract():
    """The 8-manifest release contract: versions agree, peerDeps pinned,
    files present, profile simulation, drift coverage. No runtime."""
    version = harness.release_version()
    problems = release_surface_problems(
        harness.REPO_ROOT, harness.PLUGIN_DIR, harness.CANONICAL_MANIFESTS,
        version)
    assert not problems, "release-surface contract violations (issue #374): "\
        + "; ".join(problems)
    with harness.isolated_env("t13-install") as tmp:
        # Drift coverage: the surface walk includes the plugin dir.
        sys.path.insert(0, str(harness.SCRIPTS_DIR))
        try:
            spec = importlib.util.spec_from_file_location(
                "drift_ds_t13", harness.DRIFT_PY)
            drift = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(drift)
            surfaces = drift.surface_files(harness.REPO_ROOT)
        finally:
            sys.path.remove(str(harness.SCRIPTS_DIR))
        assert "deepseek-plugin/index.mjs" in surfaces, (
            "drift surface_files() must cover deepseek-plugin/ "
            f"(_SURFACE_DIRS registration); sample: {surfaces[:5]!r}")
        # Profile simulation through the frozen driver (node, no runtime).
        if harness.node_binary() is None:  # pragma: no cover - node on PATH
            print("SKIP: node absent — profile-simulation leg skipped "
                  "(manifest assertions above already ran)")
            return
        obs = harness.ok_or_red(harness.run_scenario(
            "install_release_contract", tmp / "state-install.json", {}),
            "install_release_contract")
        assert obs.get("profileOk") is True, (
            f"the simulated profile root must install the package "
            f"(symlink-free copy + cordis.yml): {obs!r}")
        assert obs.get("upgradeOk") is True, (
            "a fake in-profile version bump must never move the canonical "
            f"release surface: {obs!r}")
        assert obs.get("uninstallRetained") is True, \
            "uninstall must retain the profile's store file"
        assert obs.get("installModuleUsed") is True, (
            "the install/upgrade/uninstall simulation must run through the "
            "SHIPPED deepseek-plugin/install.mjs exports (no parallel "
            f"hand-rolled copy): {obs!r}")
        assert obs.get("operatorPatchSurvived") is True, (
            "the shipped upgrade() must preserve operator patch rows "
            f"(files the package does not ship): {obs!r}")
        assert obs.get("manifestVersion") == version \
            == obs.get("releaseVersion")



def test_native_canary_claim_gate():
    """The COMMITTED native canary report validates: schema, version
    binding vs release-manifest, the frozen headless claimed set, all six
    lanes pass; a doctored stale-version report is rejected. No runtime."""
    version = harness.release_version()
    assert harness.CANARY_REPORT.is_file(), (
        "docs/host-gate-evidence/deepseek/canary-report.json is ABSENT — "
        "the native canary report is a committed claim-gate artifact "
        "(produce it by running deepseek-plugin/canary.mjs; runtime legs "
        "skipping in CI cannot vacuously pass this gate)")
    report = json.loads(harness.CANARY_REPORT.read_text(encoding="utf-8"))
    # The frozen claimed set (plan-critic round-2 R2-8): the validator
    # compares the EXACT DECLARED PROJECTION {carrier, platform} of each
    # claimed entry — exact list equality, same order — and separately
    # validates every actual entry's node field against the node version
    # shape ^v\d+\.\d+\.\d+$. The scenario emits {carrier, platform, node}
    # entries (node = process.version, host-dependent), so CI-declared
    # entries still compare exactly on the declared projection without
    # the expectation ever being derived from the report it validates.
    expected_claimed = [{"carrier": "headless", "platform": "win32"}]
    extra_claims = os.environ.get("ZMEM_T13_CANARY_EXTRA_CLAIMS", "").strip()
    if extra_claims:
        try:
            parsed = json.loads(extra_claims)
        except ValueError as exc:
            raise AssertionError(
                f"ZMEM_T13_CANARY_EXTRA_CLAIMS is not a JSON list: {exc}") \
                from None
        assert isinstance(parsed, list), (
            "ZMEM_T13_CANARY_EXTRA_CLAIMS must be a JSON list of claimed "
            "entries")
        expected_claimed.extend(parsed)
    problems = canary_problems(report, version,
                               expected_claimed=expected_claimed,
                               expected_packages=CANARY_EXPECTED_PACKAGES)
    assert not problems, "canary claim-gate violations: " + "; ".join(problems)
    doctored = json.loads(json.dumps(report))
    doctored["zmem_version"] = "0.0.1-stale"
    assert canary_problems(doctored, version,
                           expected_claimed=expected_claimed,
                           expected_packages=CANARY_EXPECTED_PACKAGES), (
        "a doctored stale-version canary report must FAIL claim validation")


# -- standalone runner (the t-series convention) -------------------------------

def test_package_json_matches_release():
    """F7/TC-10: deepseek-plugin/package.json is bound to the release
    version (C5 pins package.json == plugin.json; the release gate pins
    plugin.json == release; this closes the last edge: package.json vs
    release-manifest directly). No runtime."""
    import json as _json
    pkg = _json.loads((harness.PLUGIN_DIR / "package.json")
                      .read_text(encoding="utf-8"))
    assert pkg.get("version") == harness.release_version(), (
        "deepseek-plugin/package.json version "
        f"{pkg.get('version')!r} != release version "
        f"{harness.release_version()!r}")


_CHECKS = {
    "first_request_delivery": test_first_request_delivery,
    "backend_parity": test_backend_parity,
    "identity_and_lifecycle": test_identity_and_lifecycle,
    "timing_and_policy": test_timing_and_policy,
    "offline_snapshot": test_offline_snapshot,
    "install_release_contract": test_install_release_contract,
    "native_canary_claim_gate": test_native_canary_claim_gate,
    "package_json_matches_release": test_package_json_matches_release,
}


def _main(argv: list[str]) -> int:
    return harness.main_runner(_CHECKS, argv)


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
