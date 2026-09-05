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
                # Ground the pass in the bg log itself, not just the canary's
                # own verdict line: the seeded row id must appear in the fresh
                # decision line's ids=[...] (closes the fire-and-print-vacuous
                # gap the implementation reviewer flagged).
                m = re.search(r"row_id=([0-9a-f-]{36})", proc.stdout)
                bg = (d / "zmem-bg.log").read_text(encoding="utf-8")
                self.assertIn(m.group(1), bg)

    def test_decision_line_carries_reason_and_session(self):
        d = self._data_dir("reason")
        proc = run_canary("--host", "claude", "--self-test", "--data-dir", str(d))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        bg = (d / "zmem-bg.log").read_text(encoding="utf-8")
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


class CanaryHelperUnitTest(unittest.TestCase):
    """Helper-level tests (in-process module load) for assertion logic that
    subprocess e2e runs cannot reach deterministically."""

    def _load(self):
        return load_canary_module()

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
            # mismatch). Appending at drive time keeps the freshness
            # heuristic honest (pre_size snapshot happens before this).
            with (Path(args.data_dir) / "zmem-bg.log").open("a", encoding="utf-8") as fh:
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


if __name__ == "__main__":
    unittest.main()
