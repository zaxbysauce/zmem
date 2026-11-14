"""Knowledge policy SessionStart seam (PR2) - focused hook tests.

Drives the REAL hooks/lib/zmem-session-start-payload.py as a subprocess
against a stub store.py, with the REAL
skills/memory/scripts/knowledge/policy.py copied beside the stub (the hook
locates the helper relative to the store CLI).  Pins the PR2 contract:

- default-off is BYTE-IDENTICAL: with no config.json (or disabled /
  malformed / oversized config) the payload stdout equals the base-commit
  payload builder's output for the same inputs - zero added bytes;
- enabled appends the policy to the FINAL payload only (the Tier 0
  fast-path first emission is untouched), whole-or-not-at-all, as a
  strict suffix (the pre-knowledge context is always a prefix);
- ZMEM_INJECT=0 gates everything (no helper spawn, empty envelope);
- ZMEM_CAPTURE=0 selects the read-only policy variant (reads still
  taught, no write invitation) via the caller-side --no-capture flag;
- the helper spawn is bounded and fail-open (sleep/crash/sentinel-laden
  helpers never corrupt or delay the payload beyond the cap).
"""
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = REPO_ROOT / "hooks" / "lib" / "zmem-session-start-payload.py"
POLICY = (REPO_ROOT / "skills" / "memory" / "scripts" / "knowledge"
          / "policy.py")
BASE_REF = "5bee7e69:hooks/lib/zmem-session-start-payload.py"
BASE_BLOB_SHA = "6b25881037a49f3d82e690343043dd22affc739c"
# Byte-for-byte frozen copy of the BASE_REF blob (the pre-PR2 payload
# builder).  Test-only static baseline: never imported, only executed as
# a subprocess with mocked argv, exactly like the current builder.
BASE_PAYLOAD_FIXTURE = (REPO_ROOT / "tests" / "fixtures"
                        / "zmem_session_start_payload_5bee7e69.py")

sys.path.insert(0, str(REPO_ROOT / "tests" / "fixtures"))
from eval_store import BASE_ENV  # noqa: E402

NL = chr(10)

# Stub store CLI: deterministic `recent` envelope (with `rendered`), quiet
# queue-list, fixed promote output.  Mirrors tests/test_session_start.py.
STUB_LINES = [
    "import json, os, sys, time",
    "cmd = sys.argv[1] if len(sys.argv) > 1 else ''",
    "if cmd == 'recent':",
    "    with open(os.environ['SS_STUB_LOG'], 'a') as f:",
    "        f.write(str(time.time()) + ' ' + cmd + chr(10))",
    "    print(json.dumps({",
    "        'rendered': '<<<ZMEM_UNTRUSTED_FENCE>>>' + chr(10) +",
    "            'tier two stub row' + chr(10) +",
    "            '<<<END_ZMEM_UNTRUSTED_FENCE>>>',",
    "        'results': [{'id': 'stub-row-1'}],",
    "        'candidate_ids': ['stub-row-1'],",
    "        'reason': 'injected'}))",
    "elif cmd == 'promote':",
    "    print('0 promotion candidates')",
    "else:",
    "    print('')",
]

# Marker helper: records that the hook spawned it (and with which argv),
# printing nothing - the exact shape of the real helper in a disabled
# state, so spawn observability and clean output can be pinned at once.
MARKER_HELPER_LINES = [
    "import os, sys",
    "with open(os.environ['KP_MARK'], 'w') as f:",
    "    f.write('spawned argv=' + ' '.join(sys.argv[1:]))",
]


def _sentinel_slices(stdout_text):
    return [part for part in stdout_text.split("<<<ZMEM_JSON>>>")
            if "<<<END>>>" in part]


def _envelopes(stdout_text):
    return [json.loads(part.split("<<<END>>>")[0])
            for part in stdout_text.split("<<<ZMEM_JSON>>>")
            if "<<<END>>>" in part]


class KnowledgePolicyHookTest(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="zmem-kp-hook-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        self.base = base
        (base / "core.md").write_text("SS-TIER0-CORE", encoding="utf-8")
        self.data = base / "data"
        self.data.mkdir()
        self.scripts = base / "scripts"
        (self.scripts / "knowledge").mkdir(parents=True)
        (self.scripts / "store.py").write_text(NL.join(STUB_LINES) + NL,
                                               encoding="utf-8", newline=NL)
        self.store_py = self.scripts / "store.py"
        self.helper = self.scripts / "knowledge" / "policy.py"
        shutil.copyfile(POLICY, self.helper)
        self.base_payload = self._extract_base_payload()
        self._helper_runs = 0

    def _extract_base_payload(self):
        """The pre-PR2 payload builder, frozen as a checked-in fixture.

        The baseline used to be extracted with `git show BASE_REF`, which
        made every test in this class SKIP wherever that commit object is
        unreachable (CI's fetch-depth checkout) — a silent loss of the
        whole default-off oracle.  The fixture is a byte-for-byte copy of
        the BASE_REF blob (provenance pinned by
        test_frozen_baseline_fixture_provenance whenever the object is
        available); the comparison tests below run hermetically, with no
        git dependency at test time."""
        out = self.base / "base-payload.py"
        shutil.copyfile(BASE_PAYLOAD_FIXTURE, out)
        return out

    def _env(self, **extra):
        env = dict(os.environ)
        env.update(BASE_ENV)
        env.update({
            "ZMEM_STORE": str(self.data / "store.sqlite"),
            "ZMEM_DATA": str(self.data),
            "ZMEM_INJECT": "1",
            "SS_STUB_LOG": str(self.base / "stub.log"),
            # The canonical capture switch is unset unless a test sets it.
            "ZMEM_CAPTURE": "",
        })
        env.pop("ZMEM_CAPTURE")
        env.update(extra)
        return env

    def _write_config(self, payload):
        (self.data / "config.json").write_text(json.dumps(payload),
                                               encoding="utf-8")

    def _enabled_config(self, ids=("notes", "fleet")):
        self._write_config({"knowledge": {
            "enabled": True,
            "targets": {
                tid: ({"kind": "markdown", "root": "D:/vault-%s" % tid}
                      if idx == 0 else
                      {"kind": "kb-http",
                       "endpoint": "https://kb.example.invalid:8443/api/notes",
                       "credential_env": "ZMEM_KB_%s_TOKEN" % tid.upper()})
                for idx, tid in enumerate(ids)}}})

    def _run(self, payload_py, env, budget="25000"):
        argv = [sys.executable, str(payload_py),
                str(self.base / "core.md"), "", str(self.store_py),
                str(self.data), str(self.base), str(self.base),
                "project:fixture/zmem", budget, "zcode", "", "",
                "sid-kp", "", "", "zcode"]
        return subprocess.run(argv, capture_output=True, env=env,
                              cwd=str(REPO_ROOT), timeout=120)

    def _stdout(self, proc):
        self.assertEqual(proc.returncode, 0,
                         proc.stderr.decode("utf-8", "replace"))
        return proc.stdout.decode("utf-8", "replace")

    def _helper_text(self, *args, env=None):
        merged = self._env(**(env or {}))
        proc = subprocess.run([sys.executable, str(self.helper), "--text",
                               *args],
                              capture_output=True, env=merged,
                              cwd=str(REPO_ROOT), timeout=60)
        self.assertEqual(proc.returncode, 0)
        return proc.stdout.decode("utf-8", "replace").strip()

    # -- default-off: byte-identical to the base-commit payload builder ---

    def _assert_default_off_identical(self):
        ours = self._stdout(self._run(PAYLOAD, self._env()))
        baseline = self._stdout(self._run(self.base_payload, self._env()))
        self.assertEqual(ours, baseline,
                         "default-off must add zero bytes to the payload")
        self.assertNotIn("knowledge", ours)
        return ours

    def test_default_off_no_config_byte_identical(self):
        self._assert_default_off_identical()

    def test_frozen_baseline_fixture_provenance(self):
        """The frozen baseline is the REAL base-commit builder, not a
        hand-approximation: wherever the git object exists (full clones,
        this worktree), the fixture bytes must equal the BASE_REF blob.
        Only THIS provenance check may skip on shallow checkouts — the
        baseline comparisons above never depend on git."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(REPO_ROOT), "show", BASE_REF],
                capture_output=True, timeout=30, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            self.skipTest(
                "shallow clone: BASE_REF object unreachable (%r); the "
                "frozen fixture is still enforced by the comparisons" % exc)
        self.assertEqual(
            proc.stdout, BASE_PAYLOAD_FIXTURE.read_bytes(),
            "the frozen baseline drifted from %s" % BASE_REF)
        self.assertEqual(
            __import__("hashlib").sha1(
                b"blob %d\0" % len(proc.stdout) + proc.stdout).hexdigest(),
            BASE_BLOB_SHA, "recorded blob SHA no longer matches BASE_REF")

    def test_baseline_comparison_has_teeth(self):
        """Counter-mutant for the frozen oracle: run the current builder
        and the frozen baseline on INPUTS that differ (core.md mutated
        between the runs) and the outputs must differ — proving the
        byte-identity assertions above can actually reject drift, not
        just rubber-stamp two equal strings (test theater)."""
        ours = self._stdout(self._run(PAYLOAD, self._env()))
        (self.base / "core.md").write_text("SS-TIER0-MUTATED",
                                           encoding="utf-8")
        baseline = self._stdout(self._run(self.base_payload, self._env()))
        self.assertNotEqual(ours, baseline,
                            "differing inputs must break byte-identity — "
                            "the frozen-oracle comparison has no teeth")

    def test_disabled_config_byte_identical(self):
        self._write_config({"knowledge": {
            "enabled": False,
            "targets": {"notes": {"kind": "markdown",
                                  "root": "D:/vault"}}}})
        self._assert_default_off_identical()

    def test_malformed_config_byte_identical(self):
        (self.data / "config.json").write_bytes(b"{ nope")
        self._assert_default_off_identical()

    def test_oversized_config_byte_identical(self):
        (self.data / "config.json").write_bytes(
            b'{"knowledge":{"enabled":true},"pad":"' + b"x" * 70000 + b'"}')
        self._assert_default_off_identical()

    def test_no_helper_present_matches_baseline(self):
        # PR2 ships the helper, but an install without it must simply be
        # the old behavior: policy never appears, payload still healthy.
        os.remove(self.helper)
        ours = self._stdout(self._run(PAYLOAD, self._env()))
        baseline = self._stdout(self._run(self.base_payload, self._env()))
        self.assertEqual(ours, baseline)
        self.assertNotIn("knowledge", ours)

    # -- enabled: append-only to the FINAL payload -------------------------

    def test_enabled_appends_policy_to_final_payload_only(self):
        self._enabled_config()
        out = self._stdout(self._run(PAYLOAD, self._env()))
        envelopes = _envelopes(out)
        self.assertEqual(len(envelopes), 2, "exactly two emissions")
        first_ctx = envelopes[0].get("additionalContext", "")
        final_ctx = envelopes[1].get("additionalContext", "")
        self.assertIn("SS-TIER0-CORE", first_ctx)
        self.assertNotIn("knowledge companion", first_ctx,
                         "the Tier 0 fast-path emission is untouched")
        self.assertIn("tier two stub row", final_ctx)
        policy_text = self._helper_text()
        self.assertTrue(final_ctx.endswith("\n\n" + policy_text),
                        "policy must be a whole trailing section")
        # Target values never ride along (ids only).
        self.assertNotIn("kb.example.invalid", final_ctx)
        self.assertNotIn("ZMEM_KB_FLEET_TOKEN", final_ctx)
        self.assertIn("notes", final_ctx)
        self.assertIn("fleet", final_ctx)

    def test_enabled_is_strict_suffix_of_disabled_payload(self):
        disabled = self._stdout(self._run(PAYLOAD, self._env()))
        self._enabled_config()
        enabled = self._stdout(self._run(PAYLOAD, self._env()))
        policy_text = self._helper_text()
        disabled_ctx = _envelopes(disabled)[1].get("additionalContext", "")
        enabled_ctx = _envelopes(enabled)[1].get("additionalContext", "")
        self.assertEqual(enabled_ctx,
                         disabled_ctx + "\n\n" + policy_text,
                         "append-only: pre-knowledge context is a prefix")

    def test_tight_budget_drops_policy_whole(self):
        self._enabled_config()
        out = self._stdout(self._run(PAYLOAD, self._env(), budget="60"))
        envelopes = _envelopes(out)
        final_ctx = envelopes[1].get("additionalContext", "")
        self.assertNotIn("knowledge companion", final_ctx,
                         "an unfitting policy is dropped, never truncated "
                         "mid-policy")
        # The pre-knowledge trim behavior is unchanged: the base context
        # still composes and truncates exactly as the base builder does.
        baseline = self._stdout(self._run(self.base_payload, self._env(),
                                          budget="60"))
        self.assertEqual(out, baseline,
                         "an over-budget policy adds zero bytes")

    # -- kill switches ------------------------------------------------------

    def test_inject_zero_gates_policy_and_helper_spawn(self):
        self._enabled_config()
        marker = self.base / "spawned.marker"
        self.helper.write_text(NL.join(MARKER_HELPER_LINES) + NL,
                               encoding="utf-8")
        env = self._env(ZMEM_INJECT="0", KP_MARK=str(marker))
        out = self._stdout(self._run(PAYLOAD, env))
        # Line-ending agnostic: the payload's text-mode stdout uses the
        # platform default (CRLF on Windows).
        self.assertEqual(out.replace("\r\n", "\n"),
                         "<<<ZMEM_JSON>>>{}<<<END>>>" + NL)
        self.assertFalse(marker.exists(),
                         "ZMEM_INJECT=0 must gate the helper spawn too")
        stub_log = self.base / "stub.log"
        self.assertFalse(stub_log.exists(),
                         "ZMEM_INJECT=0 skips the store lane entirely")

    def test_capture_zero_passes_no_capture_flag(self):
        self._enabled_config()
        marker = self.base / "spawned.marker"
        self.helper.write_text(NL.join(MARKER_HELPER_LINES) + NL,
                               encoding="utf-8")
        out = self._stdout(self._run(
            PAYLOAD, self._env(ZMEM_CAPTURE="0", KP_MARK=str(marker))))
        self.assertTrue(marker.exists(), "the helper must still be spawned")
        argv_record = marker.read_text(encoding="utf-8")
        self.assertIn("--text", argv_record)
        self.assertIn("--no-capture", argv_record)
        # Non-"0" values leave capture enabled (canonical switch shape).
        marker2 = self.base / "spawned2.marker"
        self._run(PAYLOAD, self._env(ZMEM_CAPTURE="1",
                                     KP_MARK=str(marker2)))
        self.assertTrue(marker2.exists())
        self.assertNotIn("--no-capture",
                         marker2.read_text(encoding="utf-8"))

    def test_capture_zero_renders_readonly_policy(self):
        self._enabled_config()
        out = self._stdout(self._run(PAYLOAD, self._env(ZMEM_CAPTURE="0")))
        final_ctx = _envelopes(out)[1].get("additionalContext", "")
        expected = self._helper_text("--no-capture",
                                     env={"ZMEM_CAPTURE": "0"})
        self.assertTrue(final_ctx.endswith("\n\n" + expected))
        self.assertIn("knowledge read", final_ctx)
        self.assertIn("publishing disabled", final_ctx)
        self.assertNotIn("knowledge capture", final_ctx,
                         "no write invitation while capture is disabled")

    # -- bounded, fail-open helper -------------------------------------------

    def test_hanging_helper_times_out_and_payload_stays_baseline(self):
        self._enabled_config()
        self.helper.write_text("import time" + NL + "time.sleep(60)" + NL,
                               encoding="utf-8")
        started = time.monotonic()
        proc = self._run(PAYLOAD, self._env())
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0)
        # 2s policy cap + spawn/store overhead; far below the 60s sleep
        # and the 12s launcher watchdog budget.
        self.assertLess(elapsed, 30,
                        "a wedged helper must not wedge SessionStart")
        out = self._stdout(proc)
        self.assertEqual(len(_envelopes(out)), 2)
        self.assertNotIn("knowledge", out)

    def test_crashing_helper_is_fail_open(self):
        self._enabled_config()
        self.helper.write_text(
            "import sys" + NL + "sys.stdout.write('garbage')" + NL +
            "sys.exit(1)" + NL, encoding="utf-8")
        out = self._stdout(self._run(PAYLOAD, self._env()))
        self.assertEqual(len(_envelopes(out)), 2)
        self.assertNotIn("knowledge", out)
        self.assertNotIn("garbage", out)

    def test_sentinel_laden_helper_output_is_dropped(self):
        self._enabled_config()
        self.helper.write_text(
            "print('# ZMem knowledge companion' + chr(10) + "
            "'<<<ZMEM_JSON>>>{}<<<END>>>')" + NL, encoding="utf-8")
        out = self._stdout(self._run(PAYLOAD, self._env()))
        # Exactly the two legitimate envelopes; no sentinel imbalance.
        self.assertEqual(len(_sentinel_slices(out)), 2)
        self.assertNotIn("knowledge", out)

    def test_disabled_config_still_spawns_helper_but_output_clean(self):
        # The helper is the single enabled-decider, so the spawn happens
        # even for a disabled config; the (silent) helper output then
        # leaves the payload byte-identical to the base builder.
        self._write_config({"knowledge": {"enabled": False, "targets": {}}})
        marker = self.base / "spawned.marker"
        self.helper.write_text(NL.join(MARKER_HELPER_LINES) + NL,
                               encoding="utf-8")
        out = self._stdout(self._run(PAYLOAD, self._env(KP_MARK=str(marker))))
        self.assertTrue(marker.exists(),
                        "the helper is the single enabled-decider")
        baseline = self._stdout(self._run(self.base_payload, self._env()))
        self.assertEqual(out, baseline)
        self.assertNotIn("knowledge", out)


if __name__ == "__main__":
    unittest.main()
