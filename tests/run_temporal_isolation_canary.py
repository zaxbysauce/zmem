"""Single-process temporal-isolation canary (issue #389 item 4, collated
trace 248-389-temporal-asof-integrity).

Runs the affected temporal/env-writing test modules in ONE pytest
invocation TWICE — the second time with the module order SWAPPED — and
fails on any nonzero exit. The driver-level env below pins the
IMPORT-FROZEN constants (the first storelib import in the process freezes
them regardless of module order): the shared store path, the shared link
threshold, and model-absence. ZMEM_EMBED_PROFILE is deliberately NOT
pinned here — the profile is resolved per call (embed_profiles.
resolve_active_profile), and the profile-sensitive classes pin it in
setUpClass with addClassCleanup restoration.

pytest-randomly is disabled (-p no:randomly) so the two orders are the
deliberate ones the contract names.

Run: python tests/run_temporal_isolation_canary.py
(NOT named test_* — CI's per-file loop must not pick it up; the CI
single-process canary lane invokes this file directly.)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

TEMPORAL_MODULES = [
    "tests/test_temporal.py",
    "tests/test_graph_seed_arm.py",
    "tests/test_score_composition.py",
    "tests/test_explain_per_lane.py",
    "tests/test_explain_recall.py",
    "tests/test_dataset.py",
    "tests/test_subject_dedup_contract.py",
]

ENV_WRITING_MODULES = [
    "tests/test_hermes_config.py",
    "tests/test_hermes_config_feedback.py",
    "tests/test_hermes_convention_order.py",
    "tests/test_hermes_evidence.py",
    "tests/test_hermes_evidence_feedback.py",
    "tests/test_hermes_hooks.py",
    "tests/test_hermes_mcp_transport_reason.py",
    "tests/test_hermes_native.py",
    "tests/test_hermes_prefetch.py",
    "tests/test_hermes_prefetch_fb.py",
    "tests/test_hermes_transport.py",
    "tests/test_hermes_transport_executor.py",
    "tests/test_tasktext_adapter_guards.py",
    "tests/test_tasktext_bundle_pins.py",
    "tests/test_tasktext_consume_guards.py",
    "tests/test_tasktext_correlation.py",
    "tests/test_tasktext_correlation_supp.py",
    "tests/test_tasktext_fleet_grant.py",
    "tests/test_tasktext_hub_argv.py",
]

PYTEST_MODULES = TEMPORAL_MODULES + ENV_WRITING_MODULES
TIMEOUT_S = 1800
TAIL_LINES = 60


def _run_order(label: str, modules: list[str], scratch: str) -> int:
    env = os.environ.copy()
    # Driver-level pins for IMPORT-FROZEN constants (before any test module
    # import): shared store path + shared threshold + model absence.
    env["ZMEM_STORE"] = os.path.join(scratch, "store.sqlite")
    env["ZMEM_DATA"] = scratch
    env["ZMEM_LINK_THRESHOLD"] = "1.01"
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env.pop("ZMEM_TEST_NOW", None)      # no frozen clock
    env.pop("ZMEM_EMBED_PROFILE", None)  # per-class, call-time
    env["PYTHONUTF8"] = "1"

    cmd = [sys.executable, "-X", "utf8", "-m", "pytest",
           "-p", "no:randomly", "-q", "--no-header", *modules]
    print(f"[canary:{label}] {len(modules)} modules, timeout {TIMEOUT_S}s")
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO_ROOT), env=env, capture_output=True,
            text=True, timeout=TIMEOUT_S, errors="replace")
    except subprocess.TimeoutExpired:
        print(f"[canary:{label}] FAIL: exceeded {TIMEOUT_S}s")
        return 1
    tail = "\n".join((proc.stdout or "").splitlines()[-TAIL_LINES:])
    print(f"[canary:{label}] pytest rc={proc.returncode}")
    if proc.returncode != 0:
        print(f"[canary:{label}] pytest tail (last {TAIL_LINES} lines):")
        print(tail)
    return proc.returncode


def main() -> int:
    modules = [m for m in PYTEST_MODULES if (REPO_ROOT / m).is_file()]
    missing = [m for m in PYTEST_MODULES if not (REPO_ROOT / m).is_file()]
    if missing:
        print(f"[canary] NOTE: modules listed but absent, skipped: {missing}")
    if not modules:
        print("[canary] FAIL: no modules resolved")
        return 1

    scratch = tempfile.mkdtemp(prefix="zmem-isolation-canary-")
    rc_a = _run_order("order-A", modules, scratch)
    rc_b = _run_order("order-B", list(reversed(modules)), scratch)
    ok = rc_a == 0 and rc_b == 0
    print("ISOLATION-CANARY:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
