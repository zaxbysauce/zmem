"""tests for scripts/host_canary.py — the per-host injection canary (issue #108).

Every test drives the canary as a subprocess with an explicit --data-dir under a
temp dir. The module-level ZMEM_STORE/ZMEM_DATA pins below double as ambient
DECOYS for every child run: the canary must strip them and resolve its own
fixture, which is exactly the AC4 never-touch-the-real-store contract.

Runs standalone: python tests/test_host_canary.py
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CANARY = REPO_ROOT / "scripts" / "host_canary.py"

_TMP = tempfile.mkdtemp(prefix="zmem-canary-test-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "decoy-store.sqlite")
os.environ["ZMEM_DATA"] = os.path.join(_TMP, "decoy-data")
for _v in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "ZMEM_INJECT", "ZMEM_NAMESPACE"):
    os.environ.pop(_v, None)

UUID_RE = re.compile(r"row_id=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


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
