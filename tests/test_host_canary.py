"""tests for scripts/host_canary.py — the per-host injection canary (issue #108).

Every test drives the canary as a subprocess with an explicit --data-dir under a
temp dir. The module-level ZMEM_STORE/ZMEM_DATA pins below double as ambient
DECOYS for every child run: the canary must strip them and resolve its own
fixture, which is exactly the AC4 never-touch-the-real-store contract.

Runs standalone: python tests/test_host_canary.py
"""

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CANARY = REPO_ROOT / "scripts" / "host_canary.py"

_TMP = tempfile.mkdtemp(prefix="zmem-canary-test-")
atexit.register(shutil.rmtree, _TMP, True)
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "decoy-store.sqlite")
os.environ["ZMEM_DATA"] = os.path.join(_TMP, "decoy-data")
for _v in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "ZMEM_INJECT", "ZMEM_NAMESPACE"):
    os.environ.pop(_v, None)

UUID_RE = re.compile(r"row_id=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

CANARY_LANES = (
    "hermes-gateway", "hermes-provider-mode", "hermes-compat-mode",
    "claude-compact", "codex-trust", "zcode-duplicate",
    "exec-form-claude", "exec-form-codex", "exec-form-zcode",
)
CANARY_SCHEMA = REPO_ROOT / "scripts" / "canary-schema.json"
CANARY_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "canary"
EXPECTED_LANES = CANARY_FIXTURES / "expected-lanes.json"
sys.path.insert(0, str(REPO_ROOT / "tests" / "support"))
import fake_executor  # noqa: E402 - tests/support seam module


class FakeExecutable:
    """A committed-bytes fake host binary: real file bytes so sha256 is
    calculated, never hard-coded (issue #96 contract)."""

    def __init__(self, name, image, version):
        self.name = name
        self.image = image
        self.version = version
        self.path = Path(tempfile.mkdtemp(prefix="zmem-fakeexe-")) / name
        self.path.write_bytes(image)
        atexit.register(shutil.rmtree, str(self.path.parent), True)

    @classmethod
    def from_fixture(cls, host):
        spec = json.loads(
            (CANARY_FIXTURES / "fake-executables.json")
            .read_text(encoding="utf-8"))[host]
        return cls("fake-%s" % host,
                   spec["image"].encode("utf-8"),
                   spec["version_stdout"].encode("utf-8"))

    def sha256(self):
        import hashlib
        return hashlib.sha256(self.path.read_bytes()).hexdigest()


def load_canary_module():
    """Load scripts/host_canary.py in-process for helper-level unit tests.

    Import is side-effect free (the module only parses args inside main), and
    the decoy env pins above keep any accidental store resolution on the
    fixture, never the real store."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("host_canary_under_test", CANARY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_canary(*args, extra_env=None, timeout=600):
    env = dict(os.environ)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(CANARY), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
        timeout=timeout,
    )


class CanarySelfTestTest(unittest.TestCase):
    def _data_dir(self, name):
        d = Path(tempfile.mkdtemp(prefix="zmem-canary-%s-" % name))
        self.addCleanup(shutil.rmtree, d, True)
        return d

    def test_self_test_pass_all_hosts(self):
        for host in ("claude", "codex", "zcode", "hermes"):
            with self.subTest(host=host):
                d = self._data_dir(host)
                proc = run_canary("--host", host, "--self-test", "--data-dir", str(d))
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("verdict=pass", proc.stdout)
                self.assertRegex(proc.stdout, UUID_RE.pattern)
                self.assertRegex(proc.stdout, r"drift=(matched|drifted|unknown)")
                self.assertIn("seeded id=", proc.stdout)
                # Ground the pass in the decision log itself, not just the
                # canary's own verdict line: the seeded row id must appear in
                # the fresh decision line's ids=[...] (closes the
                # fire-and-print-vacuous gap the implementation reviewer
                # flagged). #129: decision lines live in zmem-decisions.log.
                m = re.search(r"row_id=([0-9a-f-]{36})", proc.stdout)
                bg = (d / "zmem-decisions.log").read_text(encoding="utf-8")
                self.assertIn(m.group(1), bg)

    def test_compact_self_test_passes_and_grounded_on_compact_moment(self):
        # Issue #118 (AC2): the compaction lane drives precompact ->
        # postcompact -> session-start(source=compact) through the real
        # launcher and passes only when the query-aware compact branch
        # re-injects the seeded row.
        for host in ("claude", "codex"):
            with self.subTest(host=host):
                d = self._data_dir("compact-" + host)
                proc = run_canary("--host", host, "--compact-self-test",
                                  "--data-dir", str(d))
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("mode=compact-self-test", proc.stdout)
                self.assertIn("verdict=pass", proc.stdout)
                bg = (d / "zmem-decisions.log").read_text(encoding="utf-8")
                self.assertIn("moment=session_start_compact", bg)
                # Grounded on the COMPACT moment's line specifically: the
                # precompact drive also writes a decision line and grounding
                # on it would green-light a broken re-injection.
                compact_lines = [l for l in bg.splitlines()
                                 if "moment=session_start_compact" in l]
                self.assertTrue(compact_lines, "no compact-moment decision line")
                m = re.search(r"row_id=([0-9a-f-]{36})", proc.stdout)
                self.assertIn("ids=['%s']" % m.group(1), compact_lines[-1])

    def test_compact_self_test_still_runs_plain_self_test_lane(self):
        # The existing --self-test lane must be untouched by the compact
        # lane's addition (guard against flag cross-wiring).
        d = self._data_dir("plain")
        proc = run_canary("--host", "claude", "--self-test", "--data-dir", str(d))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("mode=self-test", proc.stdout)
        bg = (d / "zmem-decisions.log").read_text(encoding="utf-8")
        self.assertNotIn("moment=session_start_compact", bg)

    def test_decision_line_carries_reason_and_session(self):
        d = self._data_dir("reason")
        proc = run_canary("--host", "claude", "--self-test", "--data-dir", str(d))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        bg = (d / "zmem-decisions.log").read_text(encoding="utf-8")
        self.assertRegex(bg, r"zmem-hook status=\S+ reason=\S+")
        self.assertIn("sid=zmem-canary-selftest", bg)
        # The store the canary reports is the isolated fixture (AC4 invariant).
        # Compare resolved+case-normalized forms: on Windows CI the temp dir
        # arrives as an 8.3 short name (RUNNER~1) while the canary prints the
        # resolved long form (runneradmin) — a raw substring assert
        # false-fails there.
        fixture = os.path.normcase(str((d / "store.sqlite").resolve()))
        self.assertIn(fixture, os.path.normcase(proc.stdout))

    def test_no_seed_fails_no_row_id(self):
        d = self._data_dir("noseed")
        proc = run_canary("--host", "claude", "--self-test", "--no-seed",
                          "--data-dir", str(d))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("reason=no-row-id", proc.stdout)

    def test_missing_launcher_hook_not_fired(self):
        empty = self._data_dir("emptyroot")
        proc = run_canary("--host", "claude", "--self-test",
                          "--plugin-root", str(empty),
                          "--data-dir", str(self._data_dir("emptyrun")))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("reason=hook-not-fired", proc.stdout)

    def test_broken_hook_path_hook_not_fired(self):
        """AC3: copied tree with zmem-launch.js deleted — the rest intact."""
        broken = self._data_dir("brokenroot")
        shutil.copytree(REPO_ROOT / "hooks", broken / "hooks")
        (broken / "skills" / "memory").mkdir(parents=True)
        shutil.copytree(REPO_ROOT / "skills" / "memory" / "scripts",
                        broken / "skills" / "memory" / "scripts")
        (broken / "hooks" / "zmem-launch.js").unlink()
        proc = run_canary("--host", "claude", "--self-test",
                          "--plugin-root", str(broken),
                          "--data-dir", str(self._data_dir("brokenrun")))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("reason=hook-not-fired", proc.stdout)

    def test_skip_when_host_binary_absent(self):
        for host in ("codex", "hermes"):
            with self.subTest(host=host):
                d = self._data_dir("skip-" + host)
                proc = run_canary(
                    "--host", host, "--data-dir", str(d),
                    extra_env={"ZMEM_CANARY_HOST_BIN": os.path.join(_TMP, "absent-bin.exe")},
                )
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertIn("verdict=skip", proc.stdout)
                self.assertIn("reason=host-binary-absent", proc.stdout)

    def test_unsupported_live_session_reason_slug(self):
        """A host with a present binary but no known one-shot session form
        (zcode) must exit 5 with the honest reason slug, not
        hook-not-fired (final-critic finding)."""
        d = self._data_dir("unsupported")
        proc = run_canary(
            "--host", "zcode", "--data-dir", str(d),
            extra_env={"ZMEM_CANARY_HOST_BIN": sys.executable},
        )
        self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)
        self.assertIn("reason=host-session-unsupported", proc.stdout)

    def test_override_extensionless_path_resolves_via_pathext(self):
        """Windows: an override naming an extension-less path whose .exe
        sibling exists must resolve via PATHEXT (shutil.which cannot — it
        short-circuits dir-bearing names), not report the host binary
        absent (final-critic finding 2, extension-less repro)."""
        if os.name != "nt" or not sys.executable.lower().endswith(".exe"):
            self.skipTest("Windows PATHEXT repro only")
        exe = Path(sys.executable)
        stem = exe.with_suffix("")  # same dir, no extension
        d = self._data_dir("pathext")
        proc = run_canary(
            "--host", "zcode", "--data-dir", str(d),
            extra_env={"ZMEM_CANARY_HOST_BIN": str(stem)},
        )
        # Resolved binary + no session form => exit 5 unsupported. A skip
        # (exit 0, host-binary-absent) would mean PATHEXT probing failed.
        self.assertEqual(proc.returncode, 5, proc.stdout + proc.stderr)
        self.assertIn("reason=host-session-unsupported", proc.stdout)

    def test_probe_store_path_beats_ambient_decoys(self):
        d = self._data_dir("probe")
        proc = run_canary(
            "--host", "claude", "--probe-store-path", "--data-dir", str(d),
            extra_env={
                "ZMEM_STORE": os.path.join(_TMP, "decoy-store-2.sqlite"),
                "ZMEM_DATA": os.path.join(_TMP, "decoy-data-2"),
                "CLAUDE_PLUGIN_DATA": os.path.join(_TMP, "decoy-claude"),
                "ZCODE_PLUGIN_DATA": os.path.join(_TMP, "decoy-zcode"),
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("zmem-canary probe store=", proc.stdout)
        fixture = os.path.normcase(str((d / "store.sqlite").resolve()))
        self.assertIn(fixture, os.path.normcase(proc.stdout))
        self.assertNotIn("decoy", proc.stdout)

    def test_leaked_host_detect_vars_cannot_hijack(self):
        """F-002: ambient launcher host-detection vars from a surrounding
        host session must not beat the canary's chosen --host (the launcher's
        detectHost precedence would otherwise drive the wrong host/tree and
        still print verdict=pass)."""
        d = self._data_dir("leak")
        proc = run_canary(
            "--host", "claude", "--self-test", "--data-dir", str(d),
            extra_env={
                "ZMEM_HOST": "codex",
                "PLUGIN_ROOT": _TMP,
                "PLUGIN_DATA": _TMP,
                "CLAUDE_PLUGIN_ROOT": _TMP,
                "ZCODE_PLUGIN_ROOT": _TMP,
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("verdict=pass", proc.stdout)
        fixture = os.path.normcase(str((d / "store.sqlite").resolve()))
        self.assertIn(fixture, os.path.normcase(proc.stdout))

    def test_override_directory_skips_not_crashes(self):
        """F-004: a ZMEM_CANARY_HOST_BIN pointing at a directory must follow
        the absent-binary skip contract (exit 0), not crash with an uncaught
        PermissionError/NotADirectoryError from the spawn."""
        d = self._data_dir("diroverride")
        proc = run_canary(
            "--host", "codex", "--data-dir", str(d),
            extra_env={"ZMEM_CANARY_HOST_BIN": _TMP},
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("verdict=skip", proc.stdout)
        self.assertIn("reason=host-binary-absent", proc.stdout)

    def test_probe_without_data_dir_probes_default_and_prints_verdict(self):
        """F-007/F-013: --probe-store-path standalone (no --data-dir) must use
        the computed default isolation root instead of crashing with
        TypeError, and probe output must still end in the documented verdict
        line (mode=probe)."""
        proc = run_canary("--host", "claude", "--probe-store-path")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("zmem-canary probe store=", proc.stdout)
        self.assertIn("mode=probe", proc.stdout)
        self.assertIn("verdict=pass", proc.stdout)

    def test_live_host_error_rc_is_attributed(self):
        """F-006: a host binary that spawns but exits non-zero must surface
        its rc + stderr tail for attribution (while the decision line still
        decides the verdict) — not silently swallow both."""
        d = self._data_dir("rcattr")
        proc = run_canary(
            "--host", "codex", "--data-dir", str(d),
            extra_env={"ZMEM_CANARY_HOST_BIN": sys.executable},
        )
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("reason=hook-not-fired", proc.stdout)
        self.assertIn("host session exited rc=", proc.stderr)

    def _codex_tree(self, hooks_value):
        """A copyable plugin tree carrying a codex manifest with a chosen
        hooks value (for the manifest-contract precheck regression)."""
        root = self._data_dir("codex-tree")
        shutil.copytree(REPO_ROOT / "hooks", root / "hooks")
        (root / "skills" / "memory").mkdir(parents=True)
        shutil.copytree(REPO_ROOT / "skills" / "memory" / "scripts",
                        root / "skills" / "memory" / "scripts")
        (root / ".codex-plugin").mkdir()
        (root / ".codex-plugin" / "plugin.json").write_text(
            json.dumps({"hooks": hooks_value}), encoding="utf-8")
        return root

    def test_codex_manifest_precheck_blocks_unprefixed(self):
        """F-008: a codex manifest whose hooks path lost the ./ prefix must
        fail the canary (reason=codex-manifest-contract) instead of
        green-lighting hooks codex-cli >= 0.153.0 silently ignores."""
        root = self._codex_tree("hooks/hooks.codex.json")
        proc = run_canary("--host", "codex", "--self-test",
                          "--plugin-root", str(root),
                          "--data-dir", str(self._data_dir("precheck-bad")))
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("reason=codex-manifest-contract", proc.stdout)

    def test_codex_manifest_precheck_passes_prefixed(self):
        """F-008 control: a compliant ./ manifest drives the full self-test
        to verdict=pass — proving the precheck consults the manifest without
        breaking legitimate codex runs."""
        root = self._codex_tree("./hooks/hooks.codex.json")
        proc = run_canary("--host", "codex", "--self-test",
                          "--plugin-root", str(root),
                          "--data-dir", str(self._data_dir("precheck-ok")))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("verdict=pass", proc.stdout)

    def test_result_schema_requires_sha_version_and_hook_ids(self):
        """Issue #96 AC1: the strict result contract — exact key set,
        enum/nullability rules, conditional Hermes callback evidence, and the
        ``<event>:<command-basename>`` id rule, all enforced by the
        standard-library validator against scripts/canary-schema.json and the
        generated expected-lanes contract fixture."""
        mod = load_canary_module()
        schema = json.loads(CANARY_SCHEMA.read_text(encoding="utf-8"))
        self.assertTrue(EXPECTED_LANES.is_file(),
                        "expected-lanes.json must be generated and committed")
        expected = json.loads(EXPECTED_LANES.read_text(encoding="utf-8"))
        self.assertEqual(sorted(expected["lanes"]), sorted(CANARY_LANES))
        empty = {"path": "seed", "kind": "directory"}
        inventories = {name: {"before": [dict(empty)], "after": [dict(empty)]}
                       for name in schema["inventory_roots"]}

        def golden(**over):
            base = mod.build_result(
                "exec-form-zcode", "zcode", "fail", "version-unavailable",
                sha="a" * 64, version=None, command=["node"],
                inventories=json.loads(json.dumps(inventories)),
                manifest_hook_ids=["SessionStart:zmem-launch.js"],
                fired_hook_ids=[])
            base.update(over)
            return base

        # The golden result validates clean.
        with unittest.mock.patch.object(mod, "CANARY_SCHEMA_PATH",
                                        CANARY_SCHEMA):
            self.assertEqual(mod._validate_result_object(golden(), schema),
                             [])
            # Extra keys are rejected (mode alone is optional; a truly
            # unknown key is not).
            extra = golden()
            extra["surprise"] = "unexpected"
            problems = mod._validate_result_object(extra, schema)
            self.assertTrue(any("unexpected key" in p for p in problems),
                             problems)
            # Fixed nullability: a skip with a measured sha is invalid.
            skip = golden(verdict="skip", reason="executable-absent")
            problems = mod._validate_result_object(skip, schema)
            self.assertTrue(any("skip requires" in p for p in problems),
                             problems)
            # version-unavailable must carry version null.
            vu = golden(version="fake-zcode 96.1")
            problems = mod._validate_result_object(vu, schema)
            self.assertTrue(any("version-unavailable" in p for p in problems),
                             problems)
            # Conditional Hermes callback evidence: a hermes pass without it
            # is invalid; with it, valid.
            hermes = golden(lane="hermes-provider-mode", host="hermes",
                            verdict="pass", reason="hermes-delivery-verified",
                            mode="provider")
            problems = mod._validate_result_object(hermes, schema)
            self.assertTrue(any("callback_evidence" in p for p in problems),
                             problems)
            hermes["callback_evidence"] = ["pre_llm_call"]
            self.assertEqual(mod._validate_result_object(hermes, schema), [])
            # The <event>:<command-basename> id rule.
            badid = golden(manifest_hook_ids=["SessionStart"])
            problems = mod._validate_result_object(badid, schema)
            self.assertTrue(any("hook-id" in p for p in problems), problems)
            # manifest_hook_ids must be non-empty outside hermes lanes.
            noids = golden(manifest_hook_ids=[])
            problems = mod._validate_result_object(noids, schema)
            self.assertTrue(any("manifest_hook_ids must not be empty" in p
                                for p in problems), problems)
            # claude-compact requires compact_result in the enum.
            cc = golden(lane="claude-compact", host="claude",
                        verdict="fail", reason="compact-undetermined")
            problems = mod._validate_result_object(cc, schema)
            self.assertTrue(any("compact_result" in p for p in problems),
                            problems)
            cc["compact_result"] = "unknown"
            self.assertEqual(mod._validate_result_object(cc, schema), [])
            # zcode-duplicate requires one_copy with valid values.
            zd = golden(lane="zcode-duplicate", host="zcode",
                        verdict="fail", reason="duplicate-install")
            problems = mod._validate_result_object(zd, schema)
            self.assertTrue(any("one_copy" in p for p in problems), problems)
            zd["one_copy"] = {"verdict": "banana", "reason": "x"}
            problems = mod._validate_result_object(zd, schema)
            self.assertTrue(any("one_copy verdict/reason" in p
                                for p in problems), problems)
            zd["one_copy"] = {"verdict": "pass",
                              "reason": "single-copy-pass"}
            self.assertEqual(mod._validate_result_object(zd, schema), [])
            # Timestamp is end-anchored (malformed suffixes rejected).
            ts = golden()
            ts["timestamp"] = "2026-09-10T00:00:00Zextra"
            problems = mod._validate_result_object(ts, schema)
            self.assertTrue(any("timestamp" in p for p in problems),
                            problems)
            # Symlink targets must be POSIX-relative.
            esc = golden()
            esc["inventories"] = json.loads(json.dumps(inventories))
            esc["inventories"]["host-roots"]["after"] = [
                {"path": "escaped", "kind": "symlink",
                 "target": "../../etc/passwd"}]
            problems = mod._validate_result_object(esc, schema)
            self.assertTrue(any("POSIX-relative" in p for p in problems),
                            problems)
            # canary-data changes outside the schema allowlist are rejected.
            rogue = golden()
            rogue["inventories"] = json.loads(json.dumps(inventories))
            rogue["inventories"]["canary-data"]["after"] = [
                {"path": "zzz-rogue.bin", "kind": "file", "size": 1,
                 "sha256": "c" * 64}]
            problems = mod._validate_result_object(rogue, schema)
            self.assertTrue(any("outside the allowed set" in p
                                for p in problems), problems)


class CanaryHelperUnitTest(unittest.TestCase):
    """Helper-level tests (in-process module load) for assertion logic that
    subprocess e2e runs cannot reach deterministically."""

    def _load(self):
        return load_canary_module()

    def _data_dir(self, name):
        d = Path(tempfile.mkdtemp(prefix="zmem-canary-%s-" % name))
        self.addCleanup(shutil.rmtree, d, True)
        return d

    @staticmethod
    def _fake_runner(children_stdout=b"{}\n", version_stdout=b"", rc=0,
                     on_launch=None):
        """A hermetic CommandRunner for the lane tests: manifest children
        (zmem-launch.js argv) get children_stdout; host version invocations
        get version_stdout. on_launch observes every argv."""
        def _run(argv, *, input_bytes, env, cwd, deadline_s, deadline=None):
            if on_launch is not None:
                on_launch([str(a) for a in argv])
            joined = " ".join(str(a) for a in argv)
            out = (children_stdout if "zmem-launch.js" in joined
                   else version_stdout)
            return subprocess.CompletedProcess([str(a) for a in argv], rc,
                                               out, b"")
        return _run

    def test_claude_codex_zcode_exec_forms(self):
        """Issue #96 AC2/AC5: the exec-form lanes with FakeExecutable +
        FakeExecutor — every manifest-derived event id and exact child stdout
        bytes, the Codex structural trust state, and the two-root/one-root
        ZCode outcomes."""
        mod = self._load()
        # -- exec-form-claude: manifest children emit the translated object;
        #    the documented live-session surface emits the fake's version.
        fake = FakeExecutable.from_fixture("claude")
        seen = []

        def resolve(name):
            return str(fake.path) if name == "claude" else None

        result = mod.run_exec_form_lane(
            "claude", REPO_ROOT, self._data_dir("exec-claude"), None,
            resolve_executable=resolve,
            command_runner=self._fake_runner(
                children_stdout=b"{}\n",
                version_stdout=fake.version,
                on_launch=seen.append))
        self.assertEqual(result["verdict"], "pass", result["notes"])
        self.assertEqual(result["version"], "fake-claude 96.1")
        self.assertEqual(result["sha"], fake.sha256())
        manifest_ids = mod.derive_hook_ids("claude", REPO_ROOT)
        self.assertTrue(manifest_ids)
        self.assertEqual(result["manifest_hook_ids"], sorted(manifest_ids))
        for hid in manifest_ids:
            self.assertIn(hid, result["fired_hook_ids"])
        launches = [argv for argv in seen if "zmem-launch.js"
                    in " ".join(argv)]
        self.assertTrue(launches)
        for argv in launches:
            self.assertTrue(
                any(p.replace("\\", "/").endswith("hooks/zmem-launch.js")
                    for p in argv),
                "launcher path not substituted from the manifest: %r" % argv)
        # -- exec-form-zcode (no documented version surface): children pass
        #    but the lane records the schema-defined version-unavailable.
        fake_z = FakeExecutable.from_fixture("zcode")
        result = mod.run_exec_form_lane(
            "zcode", REPO_ROOT, self._data_dir("exec-zcode"), None,
            resolve_executable=lambda name: str(fake_z.path)
            if name == "zcode" else None,
            command_runner=self._fake_runner(children_stdout=b"{}\n"))
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["reason"], "version-unavailable")
        self.assertIsNone(result["version"])
        self.assertIsNotNone(result["sha"])
        # -- codex-trust: the fake codex executable emits the deterministic
        #    version token; the isolated hooks.state is operator-side consent
        #    state and is written BEFORE the lane runs (a state appearing
        #    mid-run is exactly what the inventory proof must flag).
        fake_cx = FakeExecutable.from_fixture("codex")

        def codex_runner():
            def _run(argv, *, input_bytes, env, cwd, deadline_s,
                     deadline=None):
                argv = [str(a) for a in argv]
                joined = " ".join(argv)
                if "exec" in joined and "--skip-git-repo-check" in joined:
                    return subprocess.CompletedProcess(argv, 0,
                                                       fake_cx.version, b"")
                return subprocess.CompletedProcess(argv, 0, b"{}\n", b"")
            return _run

        def write_state(data_dir, state_events):
            state_dir = Path(data_dir) / "operator-config" / "codex"
            state_dir.mkdir(parents=True, exist_ok=True)
            (state_dir / "hooks.state").write_text(
                json.dumps({"hooks": state_events}), encoding="utf-8")

        # A state carrying every manifest event => trusted.
        events = {event: {"trusted": True}
                  for event in mod._manifest_events(REPO_ROOT, "codex")}
        ok_dir = self._data_dir("codex-trust-ok")
        write_state(ok_dir, events)
        result = mod.run_codex_trust_lane(
            REPO_ROOT, ok_dir, None,
            codex_executable=str(fake_cx.path),
            command_runner=codex_runner())
        self.assertEqual(result["verdict"], "pass", result["notes"])
        self.assertEqual(result["fired_hook_ids"], [])
        self.assertEqual(result["version"], "fake-codex 96.1")
        # An absent state => untrusted-hook with derived missing ids.
        result = mod.run_codex_trust_lane(
            REPO_ROOT, self._data_dir("codex-trust-missing"), None,
            codex_executable=str(fake_cx.path),
            command_runner=codex_runner())
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["reason"], "untrusted-hook")
        self.assertEqual(sorted(result["fired_hook_ids"]),
                         sorted(mod.derive_hook_ids("codex", REPO_ROOT)))
        # -- zcode-duplicate: the injected runner appends a decision line per
        #    launcher drive; both roots firing is the duplicate condition and
        #    the one-root re-fire is the single-copy pass.
        def zdup_runner(fire):
            def _run(argv, *, input_bytes, env, cwd, deadline_s,
                     deadline=None):
                joined = " ".join(str(a) for a in argv)
                if "zmem-launch.js" in joined and fire:
                    log = Path(env["ZMEM_DATA"]) / "zmem-decisions.log"
                    with log.open("a", encoding="utf-8") as fh:
                        fh.write("[1700000000] zmem-hook status=injected "
                                 "reason=injected ids=['x'] all=['x'] "
                                 "sid=s\n")
                    return subprocess.CompletedProcess(
                        [str(a) for a in argv], 0, b"{}\n", b"")
                return subprocess.CompletedProcess(
                    [str(a) for a in argv], 0, b"96.1\n", b"")
            return _run

        result = mod.run_zcode_duplicate_lane(
            REPO_ROOT, self._data_dir("zdup"), None,
            command_runner=zdup_runner(True))
        self.assertEqual(result["verdict"], "fail", result["notes"])
        self.assertEqual(result["reason"], "duplicate-install")
        self.assertEqual(result["one_copy"]["reason"], "single-copy-pass")
        self.assertEqual(result["one_copy"]["verdict"], "pass")
        # Only one root firing => no duplicate condition.
        result = mod.run_zcode_duplicate_lane(
            REPO_ROOT, self._data_dir("zdup-one"), None,
            command_runner=zdup_runner(False))
        self.assertEqual(result["verdict"], "pass")
        self.assertEqual(result["reason"], "single-copy-pass")

    def test_fake_executor_deadline_cancels_without_late_write(self):
        """Issue #96 seam contract: the injected FakeExecutor implements the
        Scheduler surface (submit/advance/now) and the DeadlineExecutor
        surface — at the deadline run_command returns None, the child call is
        cancelled exactly once, cancelling the wrapper KILLS the real child
        (delegated _ChildCall.cancel), and a cancelled call can never write
        late (invoking it raises Cancelled)."""
        mod = self._load()

        # DeadlineExecutor surface: the first run hits its deadline; the
        # wrapped _ChildCall.cancel must fire, killing the real child.
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self.addCleanup(lambda: (child.kill(), child.wait(timeout=5)))
        executor = fake_executor.FakeExecutor(deadline_hits={1})
        out = mod.run_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            input_bytes=None, env=os.environ.copy(), cwd=".",
            deadline_s=30, deadline=executor)
        self.assertIsNone(out, "a deadline-hit child must yield None")
        self.assertEqual(len(executor.cancellations), 1)
        # The delegation proof: a real child handed through the SAME wrap
        # path is dead after cancel — not an orphan sleeping on.
        call = mod._ChildCall(child)
        wrapped = fake_executor.FakeCall(call)
        wrapped.cancel()
        self.assertTrue(wrapped.cancelled)
        rc = child.wait(timeout=5)
        self.assertNotEqual(rc, 0, "the cancelled child must be killed")

        # No-late-write: a cancelled FakeCall can never run again.
        with self.assertRaises(fake_executor.FakeCall.Cancelled):
            wrapped()

        # Scheduler surface: submit/advance/now drive deterministic firing.
        executor = fake_executor.FakeExecutor()
        fired = []
        executor.submit(lambda: fired.append("tick"), delay_s=5)
        executor.advance(3)
        self.assertEqual(executor.now(), 3)
        self.assertEqual(fired, [], "nothing fires before its delay")
        executor.advance(2)
        self.assertEqual(fired, ["tick"], "work fires exactly at the delay")

    def test_pinned_sha_and_lane_names(self):
        """Issue #96: the pinned Hermes sha default, the nine lane choices,
        Hermes version measurement, and the argparse exit-2 usage rules."""
        mod = self._load()
        hermes = json.loads(
            (CANARY_FIXTURES / "hermes" / "cdf4c76.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(hermes["sha"], "cdf4c76")
        self.assertEqual(hermes["version"], "0.21.1")
        self.assertEqual(sorted(mod.LANE_HOSTS), sorted(CANARY_LANES))
        self.assertEqual(len(mod.LANE_HOSTS), 9)
        # Exit 2: a Hermes lane without --hermes-root.
        with self.assertRaises(SystemExit) as ctx:
            mod.main(["--host", "hermes", "--lane", "hermes-gateway"])
        self.assertEqual(ctx.exception.code, 2)
        # Exit 2: an incompatible host/lane pair.
        with self.assertRaises(SystemExit) as ctx:
            mod.main(["--host", "claude", "--lane", "hermes-gateway",
                      "--hermes-root", str(REPO_ROOT)])
        self.assertEqual(ctx.exception.code, 2)
        # Exit 2: --result-json without --lane.
        with self.assertRaises(SystemExit) as ctx:
            mod.main(["--host", "claude",
                      "--result-json", "x.json"])
        self.assertEqual(ctx.exception.code, 2)
        # Hermes version measurement: a fake hermes root whose --version
        # output and git HEAD both measure; a sha mismatch is a structured
        # fail carrying the MEASURED version.
        fake_hermes = FakeExecutable("hermes", b"#!/bin/sh\n",
                                     b"Hermes 0.21.1\n")
        root = fake_hermes.path.parent

        def hermes_runner(measured_head, version_stdout):
            def _run(argv, *, input_bytes, env, cwd, deadline_s,
                     deadline=None):
                argv = [str(a) for a in argv]
                if argv[-1] == "--version":
                    return subprocess.CompletedProcess(argv, 0,
                                                       version_stdout, b"")
                if "rev-parse" in argv:
                    return subprocess.CompletedProcess(argv, 0,
                                                       measured_head, b"")
                return subprocess.CompletedProcess(argv, 0, b"{}\n", b"")
            return _run

        result = mod.run_hermes_lane(
            "hermes-gateway", root, self._data_dir("hermes-sha"), "cdf4c76",
            None, command_runner=hermes_runner("cdf4c76" + "0" * 32,
                                               fake_hermes.version))
        self.assertNotEqual(result["verdict"], "skip")
        self.assertEqual(result["hermes_version_measured"], "Hermes 0.21.1")
        result = mod.run_hermes_lane(
            "hermes-gateway", root, self._data_dir("hermes-sha-bad"),
            "cdf4c76",
            None, command_runner=hermes_runner("deadbeef" + "0" * 32,
                                               fake_hermes.version))
        self.assertEqual(result["verdict"], "fail")
        self.assertEqual(result["reason"], "sha-mismatch")
        self.assertEqual(result["hermes_version_measured"], "Hermes 0.21.1")

    def test_provider_and_compatibility_are_distinct(self):
        """Issue #96: the isolated provider config carries memory.provider:
        zmem, the compatibility config carries the pre_llm_call shell hook +
        hooks_auto_accept, the chat argv are the supported forms, and the
        bare-fence / <memory-context> wrapper boundary holds."""
        mod = self._load()
        fake_hermes = FakeExecutable("hermes", b"#!/bin/sh\n",
                                     b"Hermes 0.21.1\n")
        root = fake_hermes.path.parent
        head = "cdf4c76" + "0" * 32

        def hermes_runner(argv, *, input_bytes, env, cwd, deadline_s,
                          deadline=None):
            argv = [str(a) for a in argv]
            if argv[-1] == "--version":
                return subprocess.CompletedProcess(argv, 0,
                                                   fake_hermes.version, b"")
            if "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 0, head, b"")
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        data_dir = self._data_dir("hermes-modes")
        provider = mod.run_hermes_lane(
            "hermes-provider-mode", root, data_dir / "prov", "cdf4c76", None,
            command_runner=hermes_runner)
        compat = mod.run_hermes_lane(
            "hermes-compat-mode", root, data_dir / "compat", "cdf4c76", None,
            command_runner=hermes_runner)
        prov_cfg = (data_dir / "prov" / "hermes-home" / "config.yaml"
                    ).read_text(encoding="utf-8")
        compat_cfg = (data_dir / "compat" / "hermes-home" / "config.yaml"
                      ).read_text(encoding="utf-8")
        self.assertIn("memory:\n  provider: zmem\n", prov_cfg)
        self.assertNotIn("hooks_auto_accept", prov_cfg)
        self.assertNotIn("provider: zmem", compat_cfg)
        self.assertIn("pre_llm_call:", compat_cfg)
        self.assertIn("zmem-hermes-reflect.py", compat_cfg)
        self.assertIn("hooks_auto_accept: true", compat_cfg)
        self.assertEqual(provider["mode"], "provider")
        self.assertEqual(compat["mode"], "compatibility")
        self.assertIn("canary provider prompt", provider["command"])
        self.assertIn("canary compatibility prompt", compat["command"])
        for lane_result in (provider, compat):
            self.assertEqual(lane_result["callback_evidence"],
                             ["pre_llm_call"])
        # Verdict assertions: with the fake runner the chat runs cleanly but
        # no isolated-store delivery evidence exists, so both lanes are the
        # deterministic structured fail — not just "any verdict passes".
        self.assertEqual(provider["verdict"], "fail",
                         provider["notes"])
        self.assertEqual(provider["reason"], "hermes-delivery-unverified")
        self.assertEqual(compat["verdict"], "fail", compat["notes"])
        self.assertEqual(compat["reason"], "hermes-delivery-unverified")
        # The bare-fence boundary: the store-rendered fence itself never
        # carries the Hermes wrapper — wrapping is Hermes' own act.
        fence = ("<<<ZMEM_UNTRUSTED_FENCE>>>\n"
                 "# canary\n"
                 "<<<END_ZMEM_UNTRUSTED_FENCE>>>\n")
        self.assertNotIn("<memory-context>", fence)
        self.assertIn("<memory-context>",
                      "<memory-context>\n%s\n</memory-context>" % fence)

    def test_lane_changes_only_canary_inventory(self):
        """Issue #96 AC6: only the allowed canary-data paths change across a
        lane run; operator-config and host-roots stay byte-identical; a
        symlink escaping its declared root fails the lane."""
        mod = self._load()
        fake_z = FakeExecutable.from_fixture("zcode")
        data_dir = self._data_dir("inv")
        # Pre-seed an operator config and a host-roots copy BEFORE the lane
        # so the run must leave them untouched.
        (data_dir / "operator-config" / "codex").mkdir(parents=True)
        (data_dir / "operator-config" / "codex" / "hooks.state").write_text(
            "{}", encoding="utf-8")
        result = mod.run_exec_form_lane(
            "zcode", REPO_ROOT, data_dir, None,
            resolve_executable=lambda name: str(fake_z.path)
            if name == "zcode" else None,
            command_runner=self._fake_runner(children_stdout=b"{}\n"))
        inventories = result["inventories"]
        self.assertEqual(
            inventories["operator-config"]["before"],
            inventories["operator-config"]["after"])
        self.assertEqual(inventories["host-roots"]["before"],
                         inventories["host-roots"]["after"])
        allowed = {"store.sqlite", "store.sqlite-wal", "store.sqlite-shm",
                   "store.sqlite-journal", "zmem-decisions.log",
                   "zmem-bg.log"}
        before = {e["path"] for e in inventories["canary-data"]["before"]}
        for entry in inventories["canary-data"]["after"]:
            path = entry["path"]
            if entry["path"] in before and \
                    entry in inventories["canary-data"]["before"]:
                continue
            top = path.split("/")[0]
            if path in allowed or top in allowed or top == "ops":
                continue
            self.fail("unexpected canary-data change: %s" % path)
        # A symlink escaping its declared root fails the lane.
        escape = self._data_dir("inv-escape")
        (escape / "operator-config").mkdir(parents=True)
        (escape / "host-roots").mkdir(parents=True)
        if os.name == "nt":
            self.skipTest("symlink creation needs privileges on Windows")
        target = Path(tempfile.mkdtemp(prefix="zmem-escape-"))
        atexit.register(shutil.rmtree, target, True)
        os.symlink(str(target), str(escape / "host-roots" / "escaped"))
        result = mod.run_zcode_duplicate_lane(
            REPO_ROOT, escape, None, command_runner=self._fake_runner())
        notes = result["notes"]
        self.assertIn("symlink-escape", notes, notes)

    def test_ids_grounding_is_field_scoped(self):
        """F-001: the seeded row must ground ONLY via the ids=[...] field —
        a row present solely in the pre-gate all=[...] list must NOT pass
        (the gate/budget-filtered row is exactly the silent no-injection
        case the canary exists to catch)."""
        mod = self._load()
        row = "abc-123"
        grounded = ("[1700000000] zmem-hook status=injected reason=injected "
                    "ids=['%s'] all=['%s'] sid=s" % (row, row))
        all_only = ("[1700000000] zmem-hook status=silent reason=gated "
                    "ids=[] all=['%s'] sid=s" % row)
        other_only = ("[1700000000] zmem-hook status=injected reason=injected "
                      "ids=['other'] all=['other'] sid=s")
        no_ids = "[1700000000] zmem-hook status=silent reason=empty-pool sid=s"
        self.assertTrue(mod.line_ids_ground_row(grounded, row))
        self.assertFalse(mod.line_ids_ground_row(all_only, row),
                         "row only in all=[...] must not ground")
        self.assertFalse(mod.line_ids_ground_row(other_only, row))
        self.assertFalse(mod.line_ids_ground_row(no_ids, row))

    def test_fresh_decision_line_compares_bytes_not_chars(self):
        """F-012: freshness compares st_size (bytes) against the same domain —
        a pre-existing multi-byte UTF-8 log must not make a genuinely fresh
        line read as stale (chars < bytes)."""
        mod = self._load()
        d = Path(tempfile.mkdtemp(prefix="zmem-canary-fresh-"))
        self.addCleanup(shutil.rmtree, d, True)
        log = d / "zmem-bg.log"
        log.write_text("é" * 200 + "\n", encoding="utf-8")  # 200 chars
        pre_size = log.stat().st_size
        # The multi-byte property that broke the old chars-vs-bytes compare:
        # 200 é = 400 bytes (+ line ending), so bytes strictly exceed chars.
        self.assertGreater(pre_size, 200 * len("é".encode("utf-8")) - 1)
        line = ("[1700000001] zmem-hook status=injected reason=injected "
                "ids=['x'] all=['x'] sid=s\n")
        with log.open("a", encoding="utf-8") as fh:
            fh.write(line)
        self.assertEqual(mod.fresh_decision_line(log, pre_size), line.strip())
        # Stale domain: pre_size at-or-above the current size => None.
        self.assertIsNone(mod.fresh_decision_line(log, log.stat().st_size))
        self.assertIsNone(mod.fresh_decision_line(log, log.stat().st_size + 1))

    def test_seed_row_swallows_spawn_exceptions(self):
        """F-005: seed spawn failures (timeout, missing interpreter, locked
        store) must return None — main then emits the documented seed-failed
        verdict + exit 4 — never a bare traceback."""
        import subprocess as sp

        mod = self._load()
        for exc in (sp.TimeoutExpired(cmd="store.py", timeout=120),
                    FileNotFoundError("python"),
                    PermissionError("locked")):
            with self.subTest(exc=type(exc).__name__):
                with unittest.mock.patch.object(
                        mod.subprocess, "run", side_effect=exc):
                    self.assertIsNone(
                        mod.seed_row({}, REPO_ROOT, "ns", _TMP))

    def test_resolve_override_rejects_non_files_and_still_finds_which(self):
        """F-004/F-014: _resolve_override must refuse directories (skip
        contract, no spawn crash) on EVERY platform, and the shutil.which
        fallback must still resolve a bare executable name on every platform
        (the PATHEXT branch stays Windows-e2e-gated above)."""
        mod = self._load()
        self.assertIsNone(mod._resolve_override(_TMP))  # an existing directory
        resolved = mod._resolve_override(Path(sys.executable).name)
        self.assertIsNotNone(resolved, "bare interpreter name must resolve via which")

    def test_canary_fails_when_fresh_line_lacks_seeded_row(self):
        """F-011 consumer bite (critic NEW-02): a run where the hook fires a
        fresh decision line whose ids=[...] is NON-EMPTY but does NOT carry
        the seeded row must exit 3 via the ids-grounding consumer — deleting
        that consumer must turn this test red (the --no-seed e2e only pins
        the row_id-is-None branch)."""
        import contextlib
        import io

        mod = self._load()
        d = Path(tempfile.mkdtemp(prefix="zmem-canary-grounding-"))
        self.addCleanup(shutil.rmtree, d, True)
        line = ("[1700000000] zmem-hook status=injected reason=injected "
                "ids=['decoy-row-uuid'] all=['decoy-row-uuid'] sid=s\n")

        def fake_self_test(args, env, workdir):
            # Stand-in for the real drive: the hook "fires" and appends a
            # fresh decision line whose ids carry an UNRELATED post-gate row
            # (the ids-non-empty-without-seeded-id shape, e.g. a namespace
            # mismatch) to the #129 decisions log. Appending at drive time
            # keeps the freshness heuristic honest (pre_size snapshot happens
            # before this).
            with (Path(args.data_dir) / "zmem-decisions.log").open(
                    "a", encoding="utf-8") as fh:
                fh.write(line)
            # The rendered fence DOES carry the marker (seed succeeded) — so
            # the self-test marker check passes and the ONLY path to exit 3
            # is the ids-grounding consumer. Bite-proof: with the consumer
            # disabled this test returns 0 (pass) and fails.
            envelope = json.dumps(
                {"hookSpecificOutput": {"additionalContext":
                                        "ctx %s ctx" % mod.MARKER}})
            return 0, envelope + "\n"

        captured = []
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            with unittest.mock.patch.object(mod, "self_test", fake_self_test):
                with unittest.mock.patch.object(
                        mod, "verdict_line",
                        lambda *a: captured.append(a)):
                    rc = mod.main([
                        "--host", "claude", "--self-test",
                        "--data-dir", str(d),
                    ])
        self.assertEqual(rc, 3, "ungrounded ids must exit no-row-id")
        self.assertEqual(len(captured), 1, buf.getvalue())
        verdict = captured[0]
        self.assertEqual(verdict[2], "fail")
        self.assertEqual(verdict[3], "no-row-id")
        # row_id must be NON-None here: this is the grounding consumer, not
        # the --no-seed / seed-failure branch.
        self.assertIsNotNone(verdict[5])

    def test_canary_reads_decisions_log_when_both_logs_exist(self):
        """Issue #129 regression: with BOTH a stale zmem-bg.log (pre-split
        deployment leftover holding a fresh-looking decoy decision line) and
        a zmem-decisions.log (holding the real fresh line), the canary must
        ground its verdict on the DECISIONS file — reading the legacy file
        instead would exit 3 (decoy ids never ground the seeded row)."""
        import contextlib
        import io

        mod = self._load()
        d = Path(tempfile.mkdtemp(prefix="zmem-canary-bothlogs-"))
        self.addCleanup(shutil.rmtree, d, True)
        seeded_id = "seeded-row-fixed-0000"
        stale = "[1700000000] zmem-hook status=silent reason=empty-pool sid=old\n"
        # Identical stale prefixes in BOTH files so the pre_size snapshot
        # cannot bias which file's tail reads as fresh.
        (d / "zmem-bg.log").write_text(stale, encoding="utf-8")
        (d / "zmem-decisions.log").write_text(stale, encoding="utf-8")
        good = ("[1700000001] zmem-hook status=injected reason=injected "
                "ids=['%s'] all=['%s'] sid=zmem-canary-selftest\n"
                % (seeded_id, seeded_id))
        decoy = ("[1700000001] zmem-hook status=injected reason=injected "
                 "ids=['legacy-decoy-row'] all=['legacy-decoy-row'] sid=old\n")

        def fake_self_test(args, env, workdir):
            # The drive appends the REAL fresh line to the decisions log and
            # a same-second decoy to the legacy bg log — a canary still
            # reading zmem-bg.log picks the decoy and fails to ground.
            with (Path(args.data_dir) / "zmem-decisions.log").open(
                    "a", encoding="utf-8") as fh:
                fh.write(good)
            with (Path(args.data_dir) / "zmem-bg.log").open(
                    "a", encoding="utf-8") as fh:
                fh.write(decoy)
            envelope = json.dumps(
                {"hookSpecificOutput": {"additionalContext":
                                        "ctx %s ctx" % mod.MARKER}})
            return 0, envelope + "\n"

        captured = []
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            with unittest.mock.patch.object(mod, "seed_row",
                                            lambda *a: seeded_id):
                with unittest.mock.patch.object(mod, "self_test",
                                                fake_self_test):
                    with unittest.mock.patch.object(
                            mod, "verdict_line",
                            lambda *a: captured.append(a)):
                        rc = mod.main([
                            "--host", "claude", "--self-test",
                            "--data-dir", str(d),
                        ])
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertEqual(len(captured), 1, buf.getvalue())
        verdict = captured[0]
        self.assertEqual(verdict[2], "pass",
                         "the canary must ground on zmem-decisions.log when "
                         "both logs exist")
        self.assertEqual(verdict[5], seeded_id)


class ReadmeCanaryDocTest(unittest.TestCase):
    """AC5, strengthened pin: the canary is documented as the post-install
    verification step with its exit-code semantics (frozen check C5 pins the
    minimum; this pins the substance)."""

    def test_readme_documents_canary(self):
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("### Post-install canary", text)
        self.assertIn("scripts/host_canary.py", text)
        self.assertIn("--self-test", text)
        self.assertIn("--host", text)
        self.assertTrue(
            ("verdict=skip" in text) or ("host-binary-absent" in text),
            "README must document the skip semantics of the canary",
        )

    def test_negative_lane_is_structured_failure(self):
        """Issue #96 AC1/AC7 (docs): README and skills/memory/SKILL.md
        document the schema-valid structured fail semantics, the measured
        SHA/version values, the lane exit-code contract, and the nine exact
        artifact paths."""
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        skill = (REPO_ROOT / "skills" / "memory" / "SKILL.md").read_text(
            encoding="utf-8")
        for lane in CANARY_LANES:
            artifact = "canary/%s.json" % lane
            self.assertIn(artifact, readme,
                          "README must document the exact artifact path %s"
                          % artifact)
        for needle in ("--lane", "--validate-result", "structured",
                       "scripts/canary-schema.json"):
            self.assertIn(needle, readme, "README missing %r" % needle)
        self.assertIn("structured", skill,
                      "SKILL.md must document schema-valid fail results")
        self.assertIn("host_canary", skill)
        for needle in ("measured", "verdict"):
            self.assertIn(needle, readme,
                          "README must document measured verdict values")


if __name__ == "__main__":
    unittest.main()
