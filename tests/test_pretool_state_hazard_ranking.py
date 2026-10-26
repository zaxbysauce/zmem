"""PreToolUse state-conditional hazard ranking — acceptance spec.

Defect under test: the pretool recall surface ranks lessons about
``git checkout --`` WITHOUT consulting repository state, so the
UNCONDITIONAL lesson ("always restore with git checkout --") can rank above
the CONDITIONAL hazard ("never use git checkout -- when the tree is dirty")
exactly when the tree IS dirty — the one moment the conditional row is the
row that must be read first.

POST-FIX CONTRACT pinned here (drives hooks/lib/zmem-recall-body.py — the
python body behind hooks/zmem-pretool-recall.sh — end-to-end):

- A new TEST SEAM env var ``ZMEM_TEST_REPO_STATE`` selects the simulated
  repository-state probe result inside the recall path:
  ``dirty``       — simulated uncommitted tracked changes;
  ``clean``       — simulated clean tree;
  ``unavailable`` — the probe fails.
- ``dirty``: the emitted additionalContext fence ranks the CONDITIONAL
  hazard row (row B, "Never use git checkout ...") ABOVE the unconditional
  row A (strict index ordering of their distinctive substrings).
- ``clean``: the hazard ordering boost is OFF — when both rows appear, row
  B must NOT precede row A; row A still renders.
- ``unavailable``: fail-open — the body exits 0 and emits one valid JSON
  line on stdout (recall completes without the predicate).

AMENDMENTS (a plan critic proved the seam-only checks could pass without
production wiring, so three leg families were added):

- NEGATIVE CONTROL row C ("Restore uncommitted work quickly with git
  checkout -- ..."): under ``dirty`` the conditional row B must rank above
  BOTH row A AND row C — the bump must require CONDITIONALITY, not bare
  lexicon membership (row C carries the same dirty-lexicon words
  "uncommitted" / "git checkout --" with no conditional semantics).
- PRODUCTION PROBE legs: real throwaway git repositories driven through
  the EVENT ``cwd`` (``ZMEM_TEST_REPO_STATE`` must NOT be set): a repo
  with an uncommitted modification to a tracked file ranks row B first;
  a clean repo applies no bump. The body process itself runs with its
  cwd OUTSIDE any git repository, so only the event cwd can identify the
  repo — a probe that consulted the process cwd instead sees no repo and
  fails the dirty leg.
- FALLBACK leg: event cwd set to a nonexistent path with the seam unset
  must fail open — exit 0, one parseable JSON envelope, a delivered
  seeded row, and decision-log ``state=unavailable`` (process-cwd
  fallback).

ROUND-2A AMENDMENTS:

- ROW-C ELIGIBILITY CONTROL: row C's position RELATIVE to row A must be
  IDENTICAL under the seam's dirty and clean settings — C is never
  bumped, so flipping the state hint must not move it (a faulty bump
  that raises C alongside B breaks this stability pin).
- PROBE-TIMEOUT leg: a real git repo at the event cwd with the seam
  unset and ``ZMEM_STATE_PROBE_TIMEOUT_S=0.001`` (the operator/test knob
  the fix documents, seconds, default 2.0) must make the probe genuinely
  time out — exit 0, a delivered seeded row, and ``state=unavailable``
  in ``<ZMEM_DATA>/zmem-decisions.log``.
- TIER/GATE controls — the bump never rescues a row the gates withhold
  (round-2b rewrote all three after the critic proved the round-2a
  shapes vacuous):
  (i) TRUST-GATE leg: a conditional hazard row in the SAME namespace
  whose trust_score is drained below the 0.2 injection floor through the
  REAL ``store.py contradict`` CLI (-0.10 per endpoint per DISTINCT
  pair; pairs are idempotent) stays undelivered under dirty while rows
  A/B deliver. GUARD at base (trust gating already exists).
  (ii) RELEVANCE-FLOOR leg (round-3 user:global-tier design): TWO
  conditional hazard rows seeded in ``user:global`` at confidence 0.6
  (above the pool floor; the body's argv already pulls the global tier
  via --include-global) — an anchor with strong query overlap and a
  weak sibling whose lexicon rides an unrelated-sounding sentence. The
  tier floor ``ZMEM_INJECT_FLOOR_USER_GLOBAL`` (default 0.5) judges each
  global candidate by its MAX measured relevance lane, and the lane
  measures are RELATIVE within the global pull — the anchor is REQUIRED
  to squeeze the weak row below 0.5 (executed probe: a lone weak row
  passes the default floor). Probed shape: default floor -> anchor
  delivered, weak row WITHHELD under dirty; floor=0 -> weak row
  delivered. The bump never rescues a row the pre-merge tier admission
  withholds. GUARD at base.
  (iii) CROSS-PROJECT leg: two foreign-namespace rows — a conditional
  hazard plus a lexicon-carrying unconditional control (the cross lane
  is hazard-gated, so the control must carry the lexicon to ride it) —
  MUST BOTH render in the dirty and clean arms (partial or both-absent
  fails the leg), the foreign hazard never outranks the local row A
  under dirty, and the foreign pair's relative order is IDENTICAL to a
  clean-state run (local state must not reorder foreign rows). GUARD at
  base.

All stores are throwaway temp stores; ambient zmem env is stripped from
every child process (storelib freezes the store path at first import — an
unpinned child could auto-migrate the shared operator store). Runs
standalone: python -X utf8 tests/test_pretool_state_hazard_ranking.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"

NS = "project:rank-probe"
NS_FOREIGN = "project:foreign-ns"
SID = "rank-probe-fixed-test-id"

ROW_A = ("Always restore probe files with git checkout -- after every "
         "probe and verify git status --porcelain clean")
ROW_B = ("Never use git checkout -- when the work is uncommitted; "
         "use cp backup and restore instead")
# NEGATIVE CONTROL: carries the dirty-lexicon tokens ("uncommitted",
# "git checkout --") with NO conditional semantics — a ranking bump that
# fires on bare lexicon membership would elevate this row too.
ROW_C = ("Restore uncommitted work quickly with git checkout -- after "
         "every probe")
# The same conditional hazard, seeded in a FOREIGN project namespace for
# the cross-project leg (distinctive wording so its needle can never be
# satisfied by the local row B).
ROW_B_FOREIGN = ("Never let the foreign lane use git checkout -- while "
                 "its work is uncommitted")
# A NEUTRAL control row in the same foreign namespace: NO conditional
# semantics (unconditional phrasing, no negation) but it CARRIES the
# hazard lexicon ("git checkout --", "uncommitted") — the cross-project
# lane is hazard-gated, so a lexicon-free control would never ride it
# and the "surface together" invariant would be meaningless.
ROW_FOREIGN_CONTROL = ("Sweep-day rule: foreign crews restore uncommitted "
                       "tree mess quickly with git checkout -- before "
                       "signing off")
# TRUST-GATE row: conditional hazard semantics (lexicon + "never" marker
# + negation) in the SAME namespace; its trust is drained via the real
# contradict CLI until the injection trust floor withholds it.
ROW_TRUST = ("Never use git checkout -- when the work is uncommitted; "
             "stage a cp backup first")
# RELEVANCE-FLOOR rows (round-3 user:global-tier design): lexicon +
# condition marker + negation, seeded in user:global at confidence 0.6
# (above the 0.25 pool floor). Executed probe record (2026-10-24, fresh
# throwaway store, repo at base 05c1bfa):
#   EXPLICIT-path lane probe (recall --namespace user:global --explain on
#   the pretool command query "git checkout -- hooks/zmem-pretool-recall.sh"):
#     anchor row -> score 0.6204, _rel_lex 0.6370, _rel_cos 0.5119
#     weak row   -> score 0.6367, _rel_lex 0.6667, _rel_cos 0.4223
#   The passive gated-moment internal lane measures are NOT observable
#   (the body envelope exposes no lane fields; verified by probe), so the
#   PASSIVE-path evidence is the delivery outcomes:
#     lone weak row (no anchor)            -> DELIVERED at default floor
#     A+B + anchor + weak, default floor   -> anchor delivered, weak WITHHELD
#     A+B + anchor + weak, floor=0 control -> both delivered
# The tier floor judges each global candidate by its MAX measured _rel_*
# lane, and the lane measures are RELATIVE within the global pull — the
# lone-weak delivery above is the executed proof — so the ANCHOR row must
# ride the same tier to squeeze the weak row below 0.5.
ROW_GLOBAL_ANCHOR = ("Never use git checkout -- when the work is "
                     "uncommitted; verify with git status first")
ROW_GLOBAL = ("Never use git checkout -- while archive box work is "
              "uncommitted; check the ledger first")
NEEDLE_A = "Always restore probe files"
NEEDLE_B = "Never use git checkout"
NEEDLE_C = "Restore uncommitted work quickly"
NEEDLE_B_FOREIGN = "Never let the foreign lane"
NEEDLE_FOREIGN_CONTROL = "Sweep-day rule"
NEEDLE_TRUST = "stage a cp backup first"
NEEDLE_GLOBAL = "check the ledger first"
NEEDLE_GLOBAL_ANCHOR = "verify with git status first"

# The user:global namespace (the global tier the body's argv already
# includes via --include-global).
NS_GLOBAL = "user:global"

# Nine lexically-diverse sacrificial partner rows for the trust drain:
# `add` dedups at 0.85 cosine, so near-identical fillers would fold into
# ONE row and break the distinct-pair accumulation contradict requires.
_TRUST_PARTNERS = (
    "lunar rover battery schedules drive the winter window",
    "the greenhouse tomatoes need staking before august storms arrive",
    "editorial review prefers tables over bullet spam for short facts",
    "archival paper stock prefers alkaline buffering for longevity",
    "build cache eviction scans the ubuntu runner images nightly",
    "fleet van tires swap in november before mountain routes freeze",
    "the ferry terminal kiosk closes during spring repairs each year",
    "nightly telemetry compression halves the satellite uplink budget",
    "the orchard rows get pruned only after the last hard frost",
)

# Mirror the _STRIP_ENV pattern of tests/test_subagent_tasktext.py:45-67 so
# no ambient ZMEM_* knob (or a dev box's ZMEM_REMOTE_URL) can reach the
# child processes under test. ZMEM_STATE_PROBE_TIMEOUT_S (the probe
# timeout knob the fix adds) is stripped so only the timeout leg sets it;
# the gate/floor knobs (PROMPT confidence floor, TRUST floor, GATE_NONE,
# RECENT) are stripped so only the legs that zero them set them.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_INJECT_FLOOR_COS", "ZMEM_INJECT_FLOOR_ENT",
    "ZMEM_INJECT_FLOOR_GRAPH", "ZMEM_INJECT_FLOOR_PROMPT",
    "ZMEM_INJECT_FLOOR_TRUST", "ZMEM_INJECT_FLOOR_GATE_NONE",
    "ZMEM_INJECT_FLOOR_RECENT", "ZMEM_INJECT_FLOOR_USER_GLOBAL",
    "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_TEST_REPO_STATE", "ZMEM_STATE_PROBE_TIMEOUT_S",
    "ZMEM_REMOTE_URL",
    "ZMEM_CROSS_PROJECT", "ZMEM_CROSS_PROJECT_HAZARD_VERBS",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "CLAUDE_PLUGIN_ROOT", "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
    "ZMEM_TRANSCRIPT", "ZMEM_SESSION_SOURCE", "ZMEM_STORE_RECALL_TIMEOUT_S",
)


def _clean_env(tmp: str, **extra: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env.update({
        "ZMEM_STORE": os.path.join(tmp, "store.sqlite"),
        "ZMEM_DATA": tmp,
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "PYTHONUTF8": "1",
        # Hermeticity (CHECK_WRONG amend): cap git's upward repo search at
        # the scratch root.  The fallback-probe leg (nonexistent event cwd)
        # must find NO repository from a neutral scratch dir so the probe
        # records state=unavailable deterministically -- without this pin
        # the outcome depends on whether TMP itself sits inside a git
        # working tree (the C4 suite loop redirects TMP into the repo; a
        # real harness can likewise run hooks with TMP under the project).
        # Repos the tests construct INSIDE the scratch stay visible: the
        # ceiling only stops the walk above tmp itself.
        "GIT_CEILING_DIRECTORIES": tmp,
    })
    env.update(extra)
    return env


def _add(env: dict, content: str, namespace: str = NS,
         confidence: str = "0.9") -> dict:
    """One seeded lesson row; returns the parsed --json write result."""
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"), "add",
         "--namespace", namespace, "--type", "lesson",
         "--content", content,
         "--signal", "reviewer", "--confidence", confidence, "--json"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert r.returncode == 0, f"seed failed: {r.stdout}\n{r.stderr}"
    parsed = json.loads(r.stdout.strip().splitlines()[-1])
    assert parsed.get("result") == "stored", parsed
    return parsed


def _contradict(env: dict, id_a: str, id_b: str) -> None:
    """One real `store.py contradict` call (-0.10 trust to BOTH rows).
    The CLI syntax is `--id A --id B --reason ...` (NOT --left/--right),
    and re-running the SAME pair is an exact no-op — each drain call must
    use a DISTINCT partner row to accumulate."""
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"),
         "contradict", "--id", id_a, "--id", id_b,
         "--reason", "spec trust drain"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert r.returncode == 0, f"contradict failed: {r.stdout}\n{r.stderr}"


def _row_trust(env: dict, row_id: str) -> float:
    """The row's current trust_score via the real `store.py get` CLI."""
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(SCRIPTS / "store.py"),
         "get", "--id", row_id],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert r.returncode == 0, f"get failed: {r.stdout}\n{r.stderr}"
    parsed = json.loads((r.stdout or "").strip())
    return float(parsed["trust_score"])


def _drive_body(env: dict, event_cwd: str | None = None,
                proc_cwd: str | None = None,
                sid: str = SID) -> subprocess.CompletedProcess:
    """Run the pretool recall body exactly as the pretool hook would.

    ``event_cwd`` lands in the hook EVENT payload (the production probe's
    input); ``proc_cwd`` sets the body PROCESS's working directory (kept
    outside any git repo on the production legs so only the event cwd can
    identify the repository under test); ``sid`` overrides the session id
    in BOTH the event and (via the caller's env) the ledger — a test that
    drives the body TWICE must use distinct sids or the delivery ledger
    suppresses the second run's rows as already-delivered."""
    event = {
        "session_id": sid,
        "tool_name": "Bash",
        "tool_input": {"command": "git checkout -- hooks/zmem-pretool-recall.sh"},
    }
    if event_cwd is not None:
        event["cwd"] = event_cwd
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(BODY), str(SCRIPTS / "store.py"),
         NS, "25000", "pretool"],
        input=json.dumps(event), capture_output=True, text=True, env=env,
        timeout=180, cwd=proc_cwd,
    )


def _additional_context(proc: subprocess.CompletedProcess) -> str:
    """Parse the body's one-line JSON envelope -> additionalContext."""
    text = (proc.stdout or "").strip()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            envelope = json.loads(line)
        except ValueError:
            continue
        if isinstance(envelope, dict):
            return envelope.get("additionalContext") or ""
    return ""


def _decision_log_text(tmp: str) -> str:
    """The body's decision log under the pinned data dir (the body writes
    <dirname(ZMEM_STORE)>/zmem-decisions.log — with ZMEM_STORE pinned to
    <tmp>/store.sqlite that is <tmp>/zmem-decisions.log)."""
    path = Path(tmp) / "zmem-decisions.log"
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _git(repo: Path, *argv: str) -> None:
    r = subprocess.run(["git", "-C", str(repo), *argv],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, \
        f"git {' '.join(argv)} failed in {repo}: {r.stderr[-400:]}"


def _make_git_repo(base: Path, name: str, *, dirty: bool) -> Path:
    """A real throwaway git repo: one committed tracked file, then (when
    ``dirty``) an uncommitted modification to it — the exact state the
    production repo-state probe must detect."""
    repo = base / name
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    tracked = repo / "probe.txt"
    tracked.write_text("committed line\n", encoding="utf-8")
    _git(repo, "add", "probe.txt")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "x")
    if dirty:
        tracked.write_text(
            "committed line\nUNCOMMITTED probe modification\n",
            encoding="utf-8")
    return repo


class PretoolStateHazardRankingTest(unittest.TestCase):
    """State-conditional hazard ordering through the real pretool body."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="zmem-rank-probe-")
        env = _clean_env(self._tmp)
        # Both rows must RENDER for the ordering assertions to measure
        # ranking (not gate calibration) — zero the relevance floors like
        # tests/test_subagent_tasktext.py's _FLOOR.
        env.update({"ZMEM_INJECT_FLOOR_LEX": "0", "ZMEM_INJECT_FLOOR_COS": "0"})
        self._seed_env = env
        _add(env, ROW_A)
        _add(env, ROW_B)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _run(self, repo_state: str) -> str:
        env = dict(self._seed_env)
        env["ZMEM_SESSION"] = SID
        env["ZMEM_TEST_REPO_STATE"] = repo_state
        proc = _drive_body(env)
        self.assertEqual(proc.returncode, 0,
                         f"body exited {proc.returncode}: {proc.stderr[-800:]}")
        return _additional_context(proc)

    def test_dirty_tree_ranks_conditional_hazard_first(self):
        ctx = self._run("dirty")
        self.assertIn(NEEDLE_B, ctx,
                      "row B must render under a dirty tree; ctx=%r" % ctx[:400])
        self.assertIn(NEEDLE_A, ctx,
                      "row A must also render (both rows live); ctx=%r" % ctx[:400])
        self.assertLess(
            ctx.index(NEEDLE_B), ctx.index(NEEDLE_A),
            "under ZMEM_TEST_REPO_STATE=dirty the conditional hazard row B "
            "must rank ABOVE the unconditional row A")

    def test_clean_tree_no_bump(self):
        # Under a CLEAN tree the conditional ordering boost must be OFF:
        # row A renders, and row B never ranks above it.
        ctx = self._run("clean")
        self.assertIn(NEEDLE_A, ctx,
                      "row A must render under a clean tree; ctx=%r" % ctx[:400])
        if NEEDLE_B in ctx:
            self.assertLess(
                ctx.index(NEEDLE_A), ctx.index(NEEDLE_B),
                "under ZMEM_TEST_REPO_STATE=clean the conditional hazard "
                "bump is off: 'Never use' must not precede 'Always restore'")

    def test_probe_failure_fail_open(self):
        env = dict(self._seed_env)
        env["ZMEM_SESSION"] = SID
        env["ZMEM_TEST_REPO_STATE"] = "unavailable"
        proc = _drive_body(env)
        self.assertEqual(proc.returncode, 0,
                         "a failed state probe must fail open (exit 0), got "
                         f"{proc.returncode}: {proc.stderr[-800:]}")
        text = (proc.stdout or "").strip()
        self.assertTrue(text, "the body must still emit its JSON line")
        envelope = None
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    envelope = json.loads(line)
                    break
                except ValueError:
                    continue
        self.assertIsInstance(envelope, dict,
                              "stdout must end in one valid JSON object")
        self.assertIsInstance(envelope.get("additionalContext", ""), str)
        # Strengthened per plan-critic round 1 finding 2: an EMPTY envelope
        # must not satisfy "still complete the recall" — the fixture rows
        # are live with the relevance floors zeroed, so the recall under a
        # failed probe must still DELIVER one of them…
        ctx = envelope.get("additionalContext", "")
        self.assertTrue(NEEDLE_A in ctx or NEEDLE_B in ctx,
                        "a failed state probe must not degrade the recall "
                        "itself — a seeded row should still be delivered")
        # …and the decision log must carry the audit evidence that the
        # unavailable path actually RAN (without this, the leg passes with
        # no state implementation at all).
        decision = ""
        try:
            decision = Path(os.path.join(self._tmp, "zmem-decisions.log")
                            ).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        self.assertIn("state=unavailable", decision,
                      "the decision log must record state=unavailable on "
                      "the pretool lane when the probe fails")

    # -- AMENDMENT 1a: negative control --------------------------------
    def test_dirty_bump_requires_conditionality_not_lexicon(self):
        # Row C carries the dirty-lexicon tokens with NO conditional
        # semantics. A rank bump keyed on bare lexicon membership would
        # elevate C alongside B; the bump must require CONDITIONALITY, so
        # under a dirty tree row B outranks BOTH the unconditional row A
        # and the lexicon-only negative control C.
        env = dict(self._seed_env)
        _add(env, ROW_C)
        env["ZMEM_SESSION"] = SID
        env["ZMEM_TEST_REPO_STATE"] = "dirty"
        proc = _drive_body(env)
        self.assertEqual(proc.returncode, 0,
                         f"body exited {proc.returncode}: "
                         f"{proc.stderr[-800:]}")
        ctx = _additional_context(proc)
        for needle, label in ((NEEDLE_B, "conditional row B"),
                              (NEEDLE_A, "unconditional row A"),
                              (NEEDLE_C, "negative control C")):
            self.assertIn(needle, ctx,
                          f"{label} must render (all three rows live); "
                          f"ctx={ctx[:400]!r}")
        self.assertLess(
            ctx.index(NEEDLE_B), ctx.index(NEEDLE_A),
            "under ZMEM_TEST_REPO_STATE=dirty the conditional hazard row B "
            "must rank ABOVE the unconditional row A")
        self.assertLess(
            ctx.index(NEEDLE_B), ctx.index(NEEDLE_C),
            "under ZMEM_TEST_REPO_STATE=dirty the conditional hazard row B "
            "must rank ABOVE the lexicon-only negative control C — the "
            "bump requires conditionality, not bare lexicon membership")

    # -- AMENDMENT 1b: production probe legs (seam must be UNSET) ------
    def _production_env(self) -> dict:
        env = dict(self._seed_env)
        env["ZMEM_SESSION"] = SID
        # The production probe contract: NO simulated-state seam. If the
        # env ever carries it, these legs stop proving the real git probe.
        self.assertNotIn(
            "ZMEM_TEST_REPO_STATE", env,
            "the production probe legs must run with ZMEM_TEST_REPO_STATE "
            "unset (the seam is stripped by _clean_env)")
        return env

    def test_production_probe_dirty_event_cwd_ranks_conditional_first(self):
        base = Path(self._tmp)
        repo = _make_git_repo(base, "repos-dirty", dirty=True)
        # The body PROCESS runs outside any git repo: only the EVENT cwd
        # names the repository, so a probe consulting the process cwd
        # instead sees no repo and this dirty leg fails.
        neutral = base / "neutral-cwd"
        neutral.mkdir(parents=True, exist_ok=True)
        env = self._production_env()
        _add(env, ROW_C)  # the negative control rides along here too
        proc = _drive_body(env, event_cwd=str(repo), proc_cwd=str(neutral))
        self.assertEqual(proc.returncode, 0,
                         f"body exited {proc.returncode}: "
                         f"{proc.stderr[-800:]}")
        ctx = _additional_context(proc)
        self.assertIn(NEEDLE_B, ctx,
                      "row B must render for a dirty repo named by the "
                      f"event cwd; ctx={ctx[:400]!r}")
        self.assertIn(NEEDLE_A, ctx,
                      f"row A must also render; ctx={ctx[:400]!r}")
        self.assertIn(NEEDLE_C, ctx,
                      f"row C must also render; ctx={ctx[:400]!r}")
        self.assertLess(
            ctx.index(NEEDLE_B), ctx.index(NEEDLE_A),
            "a real dirty git repo at the EVENT cwd must rank the "
            "conditional hazard row B above the unconditional row A")
        self.assertLess(
            ctx.index(NEEDLE_B), ctx.index(NEEDLE_C),
            "a real dirty git repo at the EVENT cwd must rank the "
            "conditional hazard row B above the lexicon-only control C")

    def test_production_probe_clean_event_cwd_no_bump(self):
        base = Path(self._tmp)
        repo = _make_git_repo(base, "repos-clean", dirty=False)
        neutral = base / "neutral-cwd"
        neutral.mkdir(parents=True, exist_ok=True)
        env = self._production_env()
        proc = _drive_body(env, event_cwd=str(repo), proc_cwd=str(neutral))
        self.assertEqual(proc.returncode, 0,
                         f"body exited {proc.returncode}: "
                         f"{proc.stderr[-800:]}")
        ctx = _additional_context(proc)
        self.assertIn(NEEDLE_A, ctx,
                      "row A must render for a clean repo named by the "
                      f"event cwd; ctx={ctx[:400]!r}")
        if NEEDLE_B in ctx:
            self.assertLess(
                ctx.index(NEEDLE_A), ctx.index(NEEDLE_B),
                "a CLEAN git repo at the EVENT cwd must apply no hazard "
                "bump: 'Never use' must not precede 'Always restore'")

    # -- AMENDMENT 1c: fallback (event cwd unusable, seam unset) -------
    def test_fallback_nonexistent_event_cwd_fail_open(self):
        base = Path(self._tmp)
        neutral = base / "neutral-cwd"
        neutral.mkdir(parents=True, exist_ok=True)
        env = self._production_env()
        proc = _drive_body(
            env, event_cwd=str(base / "no" / "such" / "path"),
            proc_cwd=str(neutral))
        self.assertEqual(proc.returncode, 0,
                         "an unusable event cwd must fail open (exit 0, "
                         f"process-cwd fallback), got {proc.returncode}: "
                         f"{proc.stderr[-800:]}")
        text = (proc.stdout or "").strip()
        self.assertTrue(text, "the body must still emit its JSON line")
        envelope = None
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    envelope = json.loads(line)
                    break
                except ValueError:
                    continue
        self.assertIsInstance(envelope, dict,
                              "stdout must end in one valid JSON object")
        self.assertIsInstance(envelope.get("additionalContext", ""), str)
        # Round-2a strengthening: fail-open must still DELIVER recall (a
        # seeded row reaches the context) and must record the probe's
        # outcome in the decision log.
        ctx = _additional_context(proc)
        self.assertIn(
            NEEDLE_A, ctx,
            "the process-cwd fallback must still deliver a seeded row; "
            f"ctx={ctx[:400]!r}")
        self.assertIn(
            "state=unavailable", _decision_log_text(self._tmp),
            "the fallback's failed probe must be recorded as "
            "state=unavailable in <ZMEM_DATA>/zmem-decisions.log")

    # -- ROUND-2a 1a: row-C eligibility stability across states --------
    def test_negative_control_never_bumped_across_states(self):
        # Row C is never bump-eligible: its position RELATIVE to row A
        # must be IDENTICAL under the dirty and clean hints. A faulty fix
        # that bumps C alongside B (bare lexicon membership) moves C
        # relative to A when the hint flips and breaks this pin.
        env = dict(self._seed_env)
        _add(env, ROW_C)
        env["ZMEM_SESSION"] = SID

        def _relative(ctx: str) -> str:
            if NEEDLE_C not in ctx:
                return "C-absent"
            if NEEDLE_A not in ctx:
                return "A-absent"
            return ("A<C" if ctx.index(NEEDLE_A) < ctx.index(NEEDLE_C)
                    else "C<A")

        orders = {}
        for state in ("dirty", "clean"):
            # Distinct session id per run: the delivery ledger suppresses
            # rows already delivered under the same sid, which would blank
            # the second run's context and mask the ordering.
            run_sid = "%s-%s" % (SID, state)
            run_env = dict(env)
            run_env["ZMEM_SESSION"] = run_sid
            run_env["ZMEM_TEST_REPO_STATE"] = state
            proc = _drive_body(run_env, sid=run_sid)
            self.assertEqual(proc.returncode, 0,
                             f"body exited {proc.returncode} ({state}): "
                             f"{proc.stderr[-800:]}")
            ctx = _additional_context(proc)
            self.assertIn(
                NEEDLE_A, ctx,
                f"row A must render in both states (sanity); "
                f"{state} ctx={ctx[:400]!r}")
            orders[state] = _relative(ctx)
        self.assertEqual(
            orders["dirty"], orders["clean"],
            "row C is never bumped: its position relative to row A must "
            "not change when the state hint flips dirty->clean (got "
            f"dirty={orders['dirty']!r} clean={orders['clean']!r})")

    # -- ROUND-2a 1b: the production probe's timeout knob --------------
    def test_probe_timeout_fail_open_state_unavailable(self):
        base = Path(self._tmp)
        repo = _make_git_repo(base, "repos-timeout", dirty=True)
        neutral = base / "neutral-cwd"
        neutral.mkdir(parents=True, exist_ok=True)
        env = self._production_env()
        env["ZMEM_STATE_PROBE_TIMEOUT_S"] = "0.001"
        proc = _drive_body(env, event_cwd=str(repo), proc_cwd=str(neutral))
        self.assertEqual(proc.returncode, 0,
                         "a probe timeout must fail open (exit 0), got "
                         f"{proc.returncode}: {proc.stderr[-800:]}")
        ctx = _additional_context(proc)
        self.assertIn(
            NEEDLE_A, ctx,
            "recall must still deliver a seeded row while the probe "
            f"times out; ctx={ctx[:400]!r}")
        self.assertIn(
            "state=unavailable", _decision_log_text(self._tmp),
            "ZMEM_STATE_PROBE_TIMEOUT_S=0.001 against a real repo must "
            "genuinely time the probe out and record state=unavailable "
            "in <ZMEM_DATA>/zmem-decisions.log")

    # -- ROUND-2b tier/gate controls (round-2a shapes proven vacuous) ---
    def test_trust_gate_low_trust_row_not_rescued_by_bump(self):
        # TRUST-GATE leg: a conditional hazard row in the SAME namespace,
        # with its trust_score drained below the 0.2 injection floor via
        # the REAL contradict CLI (-0.10 per endpoint per DISTINCT pair;
        # 1.0 default - 9 x 0.10 = 0.1 < 0.2). GUARD leg: green at base
        # precisely because trust gating exists — it pins the invariant
        # across the fix (the state bump must never rescue the row).
        env = dict(self._seed_env)
        target = _add(env, ROW_TRUST)
        target_id = target["id"]
        partners = [_add(env, content)["id"]
                    for content in _TRUST_PARTNERS]
        for partner_id in partners:
            _contradict(env, target_id, partner_id)
        trust = _row_trust(env, target_id)
        self.assertLess(
            trust, 0.2,
            f"self-check: 9 distinct contradicts must drain trust below "
            f"the 0.2 injection floor; got {trust}")
        run_sid = SID + "-trust"
        env["ZMEM_SESSION"] = run_sid
        env["ZMEM_TEST_REPO_STATE"] = "dirty"
        proc = _drive_body(env, sid=run_sid)
        self.assertEqual(proc.returncode, 0,
                         f"body exited {proc.returncode}: "
                         f"{proc.stderr[-800:]}")
        ctx = _additional_context(proc)
        self.assertIn(
            NEEDLE_A, ctx,
            "sanity: a TRUSTED unconditional row must still deliver; "
            f"ctx={ctx[:400]!r}")
        self.assertNotIn(
            NEEDLE_TRUST, ctx,
            "a conditional hazard row whose trust_score sits below the "
            "0.2 injection floor must stay undelivered even under a "
            "dirty tree — the state bump never rescues a row the trust "
            f"gate withholds; ctx={ctx[:400]!r}")

    def test_relevance_floor_not_bypassed_by_bump(self):
        # RELEVANCE-FLOOR leg (round-3 user:global-tier design): the
        # round-2b 0.1-confidence shape only exercised the SQL
        # confidence pre-filter, never a POST-POOL relevance gate. This
        # leg seeds a conditional hazard row in the GLOBAL tier at
        # confidence 0.6 (above the 0.25 pool floor; the body's argv
        # already pulls the global tier via --include-global), then
        # varies the tier's post-pool relevance floor
        # ZMEM_INJECT_FLOOR_USER_GLOBAL (default 0.5):
        # (i) default 0.5: the global row's MAX measured relevance lane
        #     sits below 0.5, so the tier seam withholds it at the gated
        #     pretool moment — the needle is ABSENT under dirty (the
        #     bump cannot rescue it: the withhold happens at pre-merge
        #     admission).
        # (ii) ZMEM_INJECT_FLOOR_USER_GLOBAL=0: the same row IS
        #      delivered — pool + tier eligibility proven.
        # Content chosen by EXECUTED PROBE (both arms, real body): the
        # round-3 example wording ("...verify with git status first")
        # measured max-lane >= 0.5 and delivered even at the default
        # floor; this wording ("...while archive box work is
        # uncommitted; check the ledger first") measured below 0.5 —
        # withheld at default, delivered at 0.
        env = dict(self._seed_env)
        _add(env, ROW_GLOBAL_ANCHOR, namespace=NS_GLOBAL, confidence="0.6")
        _add(env, ROW_GLOBAL, namespace=NS_GLOBAL, confidence="0.6")

        # Arm (i): default LEX/COS floors (pop the setUp zeroing — the
        # only varied knob is the user:global tier floor) + default
        # ZMEM_INJECT_FLOOR_USER_GLOBAL (0.5), seam dirty.
        sid1 = SID + "-ug-default"
        arm1 = dict(env)
        arm1.pop("ZMEM_INJECT_FLOOR_LEX", None)
        arm1.pop("ZMEM_INJECT_FLOOR_COS", None)
        arm1["ZMEM_SESSION"] = sid1
        arm1["ZMEM_TEST_REPO_STATE"] = "dirty"
        proc = _drive_body(arm1, sid=sid1)
        self.assertEqual(proc.returncode, 0,
                         f"body exited {proc.returncode}: "
                         f"{proc.stderr[-800:]}")
        ctx1 = _additional_context(proc)
        self.assertIn(
            NEEDLE_A, ctx1,
            f"sanity: the LOCAL row must deliver in arm (i); "
            f"ctx={ctx1[:400]!r}")
        self.assertIn(
            NEEDLE_GLOBAL_ANCHOR, ctx1,
            "sanity: the tier's >=0.5 ANCHOR row must deliver in arm (i) "
            "— the tier is live and the floor admits a strong row, so "
            f"the weak row's absence below is the floor's doing; "
            f"ctx={ctx1[:400]!r}")
        self.assertNotIn(
            NEEDLE_GLOBAL, ctx1,
            "the user:global conditional row, whose max measured "
            "relevance lane sits below the 0.5 tier floor, must stay "
            "undelivered under dirty — the state bump never rescues a "
            "row the pre-merge tier admission withholds; "
            f"ctx={ctx1[:400]!r}")

        # Arm (ii): the SAME row with the tier floor disabled (0 is the
        # documented honest-disable value) and a DISTINCT session id.
        sid2 = SID + "-ug-zero"
        arm2 = dict(env)
        arm2.pop("ZMEM_INJECT_FLOOR_LEX", None)
        arm2.pop("ZMEM_INJECT_FLOOR_COS", None)
        arm2["ZMEM_SESSION"] = sid2
        arm2["ZMEM_TEST_REPO_STATE"] = "dirty"
        arm2["ZMEM_INJECT_FLOOR_USER_GLOBAL"] = "0"
        proc = _drive_body(arm2, sid=sid2)
        self.assertEqual(proc.returncode, 0,
                         f"body exited {proc.returncode}: "
                         f"{proc.stderr[-800:]}")
        self.assertIn(
            NEEDLE_GLOBAL, _additional_context(proc),
            "control arm: with ZMEM_INJECT_FLOOR_USER_GLOBAL=0 the same "
            "global row must be delivered — otherwise arm (i) withholds "
            "for a reason other than the tier relevance floor and the "
            "pin is vacuous")

    def test_cross_project_foreign_rows_not_reordered_by_local_state(self):
        # CROSS-PROJECT leg (round-3 unconditionality): TWO rows in a
        # foreign namespace — a conditional hazard plus a lexicon-carrying
        # unconditional control (the cross lane is hazard-gated, so the
        # control carries the lexicon to ride the same lane). The
        # REQUIRED layout is both foreign needles PRESENT in BOTH the
        # dirty and clean contexts (partial or both-absent fails the leg
        # outright — a vacuous cross lane must not pass), then, always:
        # the foreign hazard never outranks the LOCAL row A under dirty,
        # and the foreign pair's relative order is IDENTICAL across the
        # dirty/clean arms (local state must not reorder foreign rows).
        env = dict(self._seed_env)
        _add(env, ROW_B_FOREIGN, namespace=NS_FOREIGN)
        _add(env, ROW_FOREIGN_CONTROL, namespace=NS_FOREIGN)

        contexts = {}
        for state in ("dirty", "clean"):
            run_sid = "%s-xproj-%s" % (SID, state)
            run_env = dict(env)
            run_env["ZMEM_SESSION"] = run_sid
            run_env["ZMEM_TEST_REPO_STATE"] = state
            proc = _drive_body(run_env, sid=run_sid)
            self.assertEqual(proc.returncode, 0,
                             f"body exited {proc.returncode} ({state}): "
                             f"{proc.stderr[-800:]}")
            contexts[state] = _additional_context(proc)
        dirty_ctx, clean_ctx = contexts["dirty"], contexts["clean"]

        # REQUIRED two-row foreign layout in BOTH arms — no conditional.
        self.assertIn(
            NEEDLE_B_FOREIGN, dirty_ctx,
            "the foreign conditional hazard row MUST render in the dirty "
            "arm (a cross lane that drops it makes this leg vacuous); "
            f"ctx={dirty_ctx[:400]!r}")
        self.assertIn(
            NEEDLE_FOREIGN_CONTROL, dirty_ctx,
            "the foreign control row MUST render in the dirty arm (both "
            f"foreign rows ride the same lane); ctx={dirty_ctx[:400]!r}")
        self.assertIn(
            NEEDLE_B_FOREIGN, clean_ctx,
            "the foreign conditional hazard row MUST render in the clean "
            f"comparison arm too; ctx={clean_ctx[:400]!r}")
        self.assertIn(
            NEEDLE_FOREIGN_CONTROL, clean_ctx,
            "the foreign control row MUST render in the clean comparison "
            f"arm too; ctx={clean_ctx[:400]!r}")
        self.assertIn(
            NEEDLE_A, dirty_ctx,
            f"sanity: the LOCAL row must render under dirty; "
            f"ctx={dirty_ctx[:400]!r}")

        def _order(ctx: str) -> str:
            return ("hazard<control"
                    if ctx.index(NEEDLE_B_FOREIGN)
                    < ctx.index(NEEDLE_FOREIGN_CONTROL)
                    else "control<hazard")

        # UNCONDITIONAL: local dirty state never lifts the foreign hazard
        # above the local row, and never reorders the foreign pair.
        self.assertLess(
            dirty_ctx.index(NEEDLE_A), dirty_ctx.index(NEEDLE_B_FOREIGN),
            "the FOREIGN conditional hazard must not outrank the LOCAL "
            "unconditional row under local dirty state")
        self.assertEqual(
            _order(dirty_ctx), _order(clean_ctx),
            "local dirty state must not reorder FOREIGN rows relative to "
            "each other (dirty=%r clean=%r)"
            % (_order(dirty_ctx), _order(clean_ctx)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
