"""Frozen pure-assertion helpers shared by the DeepSeek adapter checks
(tests/test_t13_deepseek_contract.py + tests/test_t14_deepseek_learning.py).
No env, no subprocesses — those live in tests/support/deepseek_harness.py.
Tolerant where the interface freeze pins a RESULT contract without pinning
the plugin state file's inner nesting; strict where it pins literals.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urlparse

ZMEM_KIND = "zmem"
SNAPSHOT_LABEL = "[OFFLINE SNAPSHOT r"
CANARY_LANES = ("delivered", "acked", "offline", "tools", "capture",
                "compaction")
SUPPORT_SCHEMA = "zmem-deepseek-support-v1"
SUPPORT_GAP_DISCLOSURES = ("compaction-synthetic", "real-compactor-absent",
                           "desktop-carrier-not-tested")

# The repo root docs/ and tests/ evidence refs resolve against (the
# validator's repo_root default — support-report artifacts are committed
# repo-relative).
ASSERTIONS_REPO_ROOT = Path(__file__).resolve().parents[2]

# The pinned dsh runtime the adapter is certified against (the peerDependencies
# pin in deepseek-plugin/package.json). plugin.json runtime.tested carries THIS
# pin — NOT the zmem release-manifest version (runtime.tested names the dsh
# runtime the plugin was tested under; conflating it with the release version
# was a plan-critic defect).
DSH_RUNTIME_PINNED = "0.2.1-rc.2"

# The minimum pinned runtime.packages map a valid canary report must carry
# (canary_problems expected_packages): every key's value must match exactly.
CANARY_EXPECTED_PACKAGES = {
    "@deepseek-ai/cordis": "4.0.5",
    "@deepseek-ai/dsh-agent-loop": DSH_RUNTIME_PINNED,
}

# A canary claimed entry's EXACT DECLARED PROJECTION (plan-critic round-2
# R2-8): the expected set freezes {carrier, platform} per entry — the
# node/engine field is validated SEPARATELY against _NODE_VERSION_RE so
# CI legs (a different node build than the producing host) still compare
# exactly on the declared projection. The expected value is an explicit
# frozen declaration, never derived from the report being validated.
CANARY_CLAIM_PROJECTION = ("carrier", "platform")
_NODE_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# The frozen scenario names a local-run evidence ref may cite (the driver's
# SCENARIOS registry in tests/fixtures/deepseek/scenario.mjs): a local-run
# ref must name a scenario that can actually be run, not any [a-z_]+ word.
SUPPORT_EVIDENCE_SCENARIOS = frozenset({
    "first_request_delivery", "backend_parity", "identity_and_lifecycle",
    "timing_and_policy", "offline_snapshot", "install_release_contract",
    "native_canary", "corrections_and_failures", "retention_governance",
    "reflection_and_closeout", "reconnect_flush",
})

# An issue/PR evidence ref: exactly #NNN.
_ISSUE_REF_RE = re.compile(r"^#\d+$")


def zmem_msgs(request: dict) -> list:
    """The zmem-sourced messages inside one observed model request."""
    return [m for m in (request or {}).get("messages", [])
            if m.get("sourceKind") == ZMEM_KIND]


def admissions(observed: dict, session: str) -> list:
    """The session's zmem user/message admission events."""
    return [e for e in (observed or {}).get("events", [])
            if e.get("session") == session and e.get("type") == "user/message"
            and e.get("sourceKind") == ZMEM_KIND]


def manifest_version(path: Path) -> "str | None":
    """Lenient version extraction (JSON or YAML manifests alike)."""
    text = Path(path).read_text(encoding="utf-8")
    match = re.search(r'"?version"?\s*[:=]\s*["\']?([0-9]+\.[0-9]+\.[0-9]+'
                      r'[-+.\w]*)', text)
    return match.group(1) if match else None


def capture_queue(state) -> list:
    """The plugin state's capture queue, found tolerantly."""
    if not isinstance(state, dict):
        return []
    queue = state.get("captureQueue")
    if isinstance(queue, list):
        return queue
    stack, found = [state], []
    while stack and not found:
        cur = stack.pop()
        if not isinstance(cur, dict):
            continue
        value = cur.get("captureQueue")
        if isinstance(value, list):
            found.extend(value)
        for child in cur.values():
            if isinstance(child, (dict, list)):
                stack.append(child)
    return found


def release_surface_problems(repo_root: Path, plugin_dir: Path,
                             canonical_manifests, version: str) -> list:
    """Pure-fs validation of the 8-manifest release contract (issue #374
    AC6): plugin.json version == the release-manifest version while
    runtime.tested == the pinned dsh runtime DSH_RUNTIME_PINNED
    ("0.2.1-rc.2" — the dsh runtime the plugin was tested under, NOT the
    release-manifest version), every canonical manifest agrees,
    package.json identity + pinned peerDependencies + no runtime deps, and
    the nine committed files exist. Returns the violation list (empty =
    valid)."""
    problems = []
    plugin_json = plugin_dir / "plugin.json"
    if not plugin_json.is_file():
        return [f"deepseek-plugin/plugin.json is missing (the eighth "
                f"canonical release manifest): {plugin_json}"]
    pj = json.loads(plugin_json.read_text(encoding="utf-8"))
    if pj.get("version") != version:
        problems.append(f"plugin.json version {pj.get('version')!r} != "
                        f"release-manifest version {version!r}")
    if str((pj.get("runtime") or {}).get("tested") or "") != DSH_RUNTIME_PINNED:
        problems.append(f"plugin.json runtime.tested "
                        f"{(pj.get('runtime') or {}).get('tested')!r} must "
                        f"equal the pinned dsh runtime {DSH_RUNTIME_PINNED!r} "
                        f"(NOT the release-manifest version {version!r})")
    for rel in canonical_manifests:
        path = repo_root / rel
        if not path.is_file():
            problems.append(f"canonical manifest {rel} is missing")
            continue
        found = manifest_version(path)
        if found != version:
            problems.append(f"canonical manifest {rel} reports version "
                            f"{found!r}, expected {version!r}")
    pkg_path = plugin_dir / "package.json"
    if not pkg_path.is_file():
        problems.append("deepseek-plugin/package.json is missing")
    else:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        if not (pkg.get("name") == "@zaxbyhub/zmem-deepseek"
                and pkg.get("type") == "module"
                and pkg.get("main") == "index.mjs"):
            problems.append(f"package.json identity wrong: "
                            f"{ {k: pkg.get(k) for k in ('name', 'type', 'main')} }")
        peers = pkg.get("peerDependencies") or {}
        dsh = {k: v for k, v in peers.items() if k != "@deepseek-ai/cordis"}
        if not dsh or not all(v == "0.2.1-rc.2" for v in dsh.values()):
            problems.append(f"the dsh peerDependencies must be pinned "
                            f"0.2.1-rc.2: {peers!r}")
        if peers.get("@deepseek-ai/cordis") != "^4.0.5":
            problems.append(f"the cordis peerDependency must be ^4.0.5: "
                            f"{peers!r}")
        if pkg.get("dependencies"):
            problems.append(f"no runtime dependencies allowed: "
                            f"{pkg.get('dependencies')!r}")
    for name in ("plugin.json", "package.json", "package-lock.json",
                 "index.mjs", "launcher.mjs", "identity.mjs", "tools.mjs",
                 "capture.mjs", "canary.mjs"):
        if not (plugin_dir / name).is_file():
            problems.append(f"deepseek-plugin/{name} is missing from the "
                            f"committed surface")
    return problems


def canary_problems(report, expected_version: str,
                    expected_claimed: "list | None" = None,
                    expected_packages: "dict | None" = None) -> list:
    """Claim-gate validation for the committed native canary report
    (interface-freeze: schema zmem-deepseek-canary-v1, zmem_version bound
    to release-manifest.json, the frozen headless claimed set, six lanes
    all pass). Returns the violation list (empty = valid).

    ``expected_claimed`` (frozen callers pass it): the EXACT DECLARED
    PROJECTION each claimed entry must match — exact list equality, same
    order, on the {carrier, platform} projection (CANARY_CLAIM_PROJECTION)
    of every entry (the frozen declared set plus any CI-produced entries
    the caller declares). Each actual entry's ``node`` engine field is
    validated separately against ^v\\d+\\.\\d+\\.\\d+$ so CI legs (a
    different node build than the producing host) still compare exactly on
    the declared projection; the expected value is an explicit frozen
    declaration, NEVER derived from the report being validated
    (plan-critic round-2 R2-8). When omitted, the legacy per-entry
    heuristic applies instead.
    ``expected_packages``: the pinned runtime.packages map the report must
    carry (CANARY_EXPECTED_PACKAGES at minimum — every key's value checked).
    """
    if not isinstance(report, dict):
        return ["report is not a JSON object"]
    problems = []
    if report.get("schema") != "zmem-deepseek-canary-v1":
        problems.append(f"schema {report.get('schema')!r} is not "
                        "'zmem-deepseek-canary-v1'")
    if not report.get("generated_at"):
        problems.append("generated_at missing")
    if report.get("zmem_version") != expected_version:
        problems.append(f"zmem_version {report.get('zmem_version')!r} != "
                        f"release-manifest version {expected_version!r}")
    runtime = report.get("runtime")
    pkgs = runtime.get("packages") if isinstance(runtime, dict) else None
    if expected_packages is not None:
        if not isinstance(pkgs, dict):
            problems.append("runtime.packages must record the pinned "
                            "@deepseek-ai versions as a JSON object")
        else:
            for name, pinned in expected_packages.items():
                if pkgs.get(name) != pinned:
                    problems.append(
                        f"runtime.packages[{name!r}] must be pinned "
                        f"{pinned!r}: got {pkgs.get(name)!r}")
    elif not isinstance(pkgs, dict) or not any("cordis" in k for k in pkgs):
        problems.append("runtime.packages must record the pinned "
                        "@deepseek-ai versions incl. cordis")
    claimed = report.get("claimed")
    if expected_claimed is not None:
        if not isinstance(claimed, list):
            problems.append(f"claimed set {claimed!r} is not a list")
        else:
            projection = [
                {k: e.get(k) for k in CANARY_CLAIM_PROJECTION}
                if isinstance(e, dict) else None
                for e in claimed]
            expected = [
                {k: e.get(k) for k in CANARY_CLAIM_PROJECTION}
                for e in expected_claimed]
            if projection != expected:
                problems.append(
                    f"claimed {projection!r} != the frozen expected "
                    f"projection {expected!r} (exact "
                    f"{sorted(CANARY_CLAIM_PROJECTION)} equality, same "
                    f"order; node/engine validated separately)")
            for entry in claimed:
                if not isinstance(entry, dict):
                    problems.append(f"claimed entry {entry!r} is not an "
                                    "object")
                    continue
                node = entry.get("node")
                if not isinstance(node, str) \
                        or not _NODE_VERSION_RE.match(node):
                    problems.append(
                        f"claimed entry node {node!r} must be a node "
                        f"version matching ^v\\d+\\.\\d+\\.\\d+$: "
                        f"{entry!r}")
    elif not isinstance(claimed, list) or not claimed:
        problems.append("claimed set is empty")
    else:
        for entry in claimed:
            if not isinstance(entry, dict) or entry.get("carrier") != "headless":
                problems.append(f"claimed entry {entry!r} does not match "
                                "the frozen declaration carrier=headless")
            elif not entry.get("platform") or not entry.get("node"):
                problems.append(f"claimed entry missing platform/node: "
                                f"{entry!r}")
    lanes = report.get("lanes")
    for lane in CANARY_LANES:
        entry = lanes.get(lane) if isinstance(lanes, dict) else None
        if not isinstance(entry, dict) or entry.get("status") != "pass":
            problems.append(f"lane {lane} is not pass: {entry!r}")
    return problems


def _evidence_ref_problems(entry: dict, evidence,
                           repo_root=ASSERTIONS_REPO_ROOT,
                           allowed_local_runs=SUPPORT_EVIDENCE_SCENARIOS) \
        -> list:
    """Every evidence reference must be a non-empty string naming a
    committed docs/ or tests/ artifact THAT EXISTS under ``repo_root``,
    an http(s) URL that parses with a netloc, an issue/PR ref (#NNN), or
    a local-run scenario (``local-run:<name>`` with <name> inside the
    frozen scenario registry ``allowed_local_runs``). Bare prose and
    non-resolvable paths are rejected — a first-class claim's evidence
    must be machine-locatable (plan-critic round-2 R2-10)."""
    refs = evidence if isinstance(evidence, list) else [evidence]
    problems = []
    for ref in refs:
        if not isinstance(ref, str) or not ref.strip():
            problems.append(f"evidence ref must be a non-empty string: "
                            f"{entry!r}")
        elif ref.startswith(("docs/", "tests/")):
            if repo_root is None or not (Path(repo_root) / ref).exists():
                problems.append(
                    f"evidence ref {ref!r} names no committed artifact "
                    f"under the repo root {str(repo_root)!r}: {entry!r}")
        elif ref.startswith(("http://", "https://")):
            if not urlparse(ref).netloc:
                problems.append(
                    f"evidence ref {ref!r} is not a parsable http(s) URL "
                    f"(urlparse found no netloc): {entry!r}")
        elif _ISSUE_REF_RE.match(ref):
            pass
        elif ref.startswith("local-run:") \
                and ref[len("local-run:"):] in allowed_local_runs:
            pass
        else:
            problems.append(
                f"evidence ref {ref!r} is bare prose — it must be an "
                "EXISTING docs/ or tests/ artifact under the repo root, "
                "an http(s) URL, an issue/PR ref (#NNN), or "
                "local-run:<scenario-name> with scenario-name inside the "
                f"frozen scenario set: {entry!r}")
    return problems


def support_report_problems(report, repo_root=ASSERTIONS_REPO_ROOT,
                            allowed_local_runs=SUPPORT_EVIDENCE_SCENARIOS,
                            expected_version=None) -> list:
    """Claim-gate validation for the committed T-14 support report
    (schema zmem-deepseek-support-v1): every claimed combination carries a
    per-combination evidence ref, tested floors are disclosed, and the
    frozen capability-gap disclosures (compaction-synthetic,
    real-compactor-absent, desktop-carrier-not-tested) are present.
    First-class claims without evidence fail validation. ``repo_root`` is
    the root docs/ and tests/ evidence paths resolve against (default:
    the repo this validator lives in); ``allowed_local_runs`` is the
    frozen scenario-name set local-run refs may cite; ``expected_version``
    (issue #416 AC3) optionally binds ``report["zmem_version"]`` to the
    release version exactly as ``canary_problems`` does — None (the
    default, and the standing t14 call) preserves the legacy structural
    checks only, so the committed support report's version lag does not
    fail CI while it awaits its dsh-host regeneration."""
    if not isinstance(report, dict):
        return ["report is not a JSON object"]
    problems = []
    if report.get("schema") != SUPPORT_SCHEMA:
        problems.append(f"schema {report.get('schema')!r} is not "
                        f"{SUPPORT_SCHEMA!r}")
    claimed = report.get("claimed")
    if not isinstance(claimed, list) or not claimed:
        problems.append("claimed combinations list is empty")
    else:
        for entry in claimed:
            if not isinstance(entry, dict):
                problems.append(f"claimed entry not an object: {entry!r}")
                continue
            evidence = entry.get("evidence") or entry.get("evidence_ref") \
                or entry.get("evidenceRefs")
            if not evidence:
                problems.append(f"first-class claim without evidence ref: "
                                f"{entry!r}")
            else:
                problems.extend(_evidence_ref_problems(
                    entry, evidence, repo_root, allowed_local_runs))
    floors = report.get("tested_floors") or report.get("floors")
    if not floors:
        problems.append("tested floors are not disclosed")
    gaps = report.get("capability_gaps")
    if gaps is None:
        gaps = report.get("gaps")
    if isinstance(gaps, list):
        gaps_text = "-".join(str(g).lower() for g in gaps)
    elif isinstance(gaps, dict):
        gaps_text = "-".join(str(k).lower() for k in gaps)
    elif isinstance(gaps, str):
        gaps_text = re.sub(r"[\s_]+", "-", gaps.lower())
    else:
        gaps_text = ""
        problems.append("capability gaps are not disclosed")
    for gap in SUPPORT_GAP_DISCLOSURES:
        if gap not in gaps_text:
            problems.append(f"capability-gap disclosure missing: {gap}")
    if expected_version is not None and \
            report.get("zmem_version") != expected_version:
        problems.append(
            f"zmem_version {report.get('zmem_version')!r} != "
            f"release-manifest version {expected_version!r}")
    return problems


# Issue #416 AC3 (plan-critic disclosure-ratchet): the KNOWN, deliberately
# tolerated version lag of the committed support report while it awaits
# its real dsh-host regeneration (the issue forbids hand-editing evidence
# artifacts and names the regeneration as its own delivery). The standing
# t14 gate asserts evidence_version_problems returns EXACTLY the single
# disclosed-divergence entry for this tuple; any OTHER divergence — either
# artifact moving without the other, or a stale disclosure left behind
# after the regeneration — fails the gate. Remove this constant (and the
# ratchet leg) in the regeneration PR, when both artifacts agree.
SUPPORT_DISCLOSED_STALE_VERSION = "0.127.0"


def evidence_version_problems(canary_report, support_report,
                               release_version,
                               known_divergence=None) -> list:
    """Issue #416 AC3: bind canary-report.json, support-report.json, and
    the current release version together and REPORT divergence instead of
    letting the artifacts drift silently.

    Returns one problem per pairwise divergence (canary-vs-release,
    support-vs-release, canary-vs-support). ``known_divergence`` is the
    plan-critic disclosure-ratchet seam: when it equals the observed
    (canary_version, support_version) pair, the raw problems are replaced
    by EXACTLY one 'known-divergence (disclosed)' entry — green only for
    the deliberately disclosed lag, never for anything else."""
    cv = canary_report.get("zmem_version") if isinstance(
        canary_report, dict) else None
    sv = support_report.get("zmem_version") if isinstance(
        support_report, dict) else None
    problems = []
    if cv != release_version:
        problems.append(
            f"canary zmem_version {cv!r} != release version "
            f"{release_version!r}")
    if sv != release_version:
        problems.append(
            f"support zmem_version {sv!r} != release version "
            f"{release_version!r}")
    if cv != sv:
        problems.append(
            f"canary zmem_version {cv!r} != support zmem_version {sv!r}")
    if problems:
        if known_divergence == (cv, sv):
            return [f"known-divergence (disclosed): canary {cv!r} vs support "
                    f"{sv!r} at release {release_version!r} — the committed "
                    "support report awaits its dsh-host regeneration "
                    "(issue #416 named delivery)"]
        return problems
    if known_divergence is not None:
        # Forcing function: once the artifacts agree, a leftover disclosure
        # is itself a failure — the regeneration PR must retire it.
        return [f"stale known-divergence disclosure: canary {cv!r} and "
                f"support {sv!r} agree at release {release_version!r} — "
                "retire SUPPORT_DISCLOSED_STALE_VERSION (regeneration PR)"]
    return problems
