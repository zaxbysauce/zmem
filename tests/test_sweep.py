"""Tests for the `sweep` command on store.py (issue #23 "Minor, related").

The capture/convention hooks write one per-session cooldown marker into the data
dir (`.capture-prompted-<session>` / `.convention-prompted-<session>`) and
nothing removed them, so they accumulated unboundedly. `sweep` prunes markers
older than a TTL (default 7 days) from EVERY directory the two hooks could write
into — they resolve their data dir on DIFFERENT chains, so a reaper that only
mirrored one would silently miss markers a config placed elsewhere.

This suite drives the REAL store.py CLI via subprocess — the same path the
SessionStart hook fires — against throwaway temp dirs, never the box store.

Run: python tests/test_sweep.py   (no pytest required — repo convention)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable

DATA_DIR_ENV_VARS = ("ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")


class SweepBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-sweep-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args, env_extra=None, env_remove=()):
        env = {**os.environ}
        for k in env_remove:
            env.pop(k, None)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            [PYTHON, str(STORE_PY), *args],
            env=env, capture_output=True, text=True, timeout=60,
        )

    def _mk(self, directory, name, age_days):
        """Create a marker file at directory/name with mtime = now - age_days."""
        p = Path(directory) / name
        p.write_text("1", encoding="utf-8")
        ts = time.time() - age_days * 86400
        os.utime(p, (ts, ts))
        return p


class SweepSingleDirTest(SweepBase):
    """Deterministic single-dir sweeps via --marker-dir (fully isolated)."""

    def _sweep(self, max_age_days=7, dry_run=False):
        args = ["sweep", "--marker-dir", str(self.tmp),
                "--max-age-days", str(max_age_days)]
        if dry_run:
            args.append("--dry-run")
        return self._run(*args)

    def test_stale_removed_fresh_kept(self):
        stale = self._mk(self.tmp, ".capture-prompted-old", 8)
        fresh = self._mk(self.tmp, ".convention-prompted-new", 1)
        r = self._sweep(max_age_days=7)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(stale.exists(), "stale marker should be pruned")
        self.assertTrue(fresh.exists(), "fresh marker must be kept")

    def test_max_age_days_zero_removes_all_sentinels(self):
        p = self._mk(self.tmp, ".capture-prompted-a", 0.5)
        r = self._sweep(max_age_days=0)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(p.exists())

    def test_nonfinite_or_negative_max_age_days_rejected(self):
        # PRR-001: NaN / -inf / -1 must be rejected (exit 2) and delete nothing,
        # because a NaN or future cutoff would prune EVERY sentinel including the
        # live session's freshly-written marker. 0 is valid (tested above).
        live = self._mk(self.tmp, ".capture-prompted-live", 0)
        stale = self._mk(self.tmp, ".convention-prompted-old", 30)
        for bad in ("nan", "inf", "-inf", "-1"):
            with self.subTest(value=bad):
                r = self._sweep(max_age_days=bad)
                self.assertEqual(r.returncode, 2, f"{bad!r} must be rejected")
                self.assertTrue(live.exists(), f"live marker deleted under {bad!r}")
                self.assertTrue(stale.exists(), f"stale marker deleted under {bad!r}")

    def test_dry_run_removes_nothing(self):
        p = self._mk(self.tmp, ".capture-prompted-old", 8)
        r = self._sweep(max_age_days=7, dry_run=True)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(p.exists(), "--dry-run must not delete")
        self.assertIn("would prune", r.stdout)

    def test_both_prefixes_swept(self):
        a = self._mk(self.tmp, ".capture-prompted-old1", 8)
        b = self._mk(self.tmp, ".convention-prompted-old2", 9)
        r = self._sweep(max_age_days=7)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(a.exists())
        self.assertFalse(b.exists())

    def test_unrelated_files_untouched(self):
        sentinel = self._mk(self.tmp, ".capture-prompted-old", 8)
        keep = [
            "store.sqlite", "notes.txt", ".some-other-dotfile", ".zmem-backup.lock",
            "sub/.convention-prompted-nested",
        ]
        for rel in keep:
            fp = self.tmp / rel
            fp.parent.mkdir(parents=True, exist_ok=True)
            self.tmp.mkdir(exist_ok=True)
            fp.write_text("x", encoding="utf-8")
        r = self._sweep(max_age_days=7)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(sentinel.exists())
        for rel in keep:
            self.assertTrue((self.tmp / rel).exists(), f"{rel} should be untouched")

    def test_fresh_live_session_marker_kept(self):
        # The load-bearing guarantee: a marker just written by the LIVE session
        # (mtime ~ now, far above any realistic cutoff) must never be pruned,
        # even with an aggressive TTL. The strict-< comparison (mtime >= cutoff →
        # keep) is what protects it.
        p = self._mk(self.tmp, ".capture-prompted-live", 0)  # mtime = now
        r = self._sweep(max_age_days=7)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(p.exists(), "live session marker must never be pruned")

    def test_missing_dir_is_noop(self):
        missing = self.tmp / "does-not-exist"
        r = self._run("sweep", "--marker-dir", str(missing), "--max-age-days", "7")
        self.assertEqual(r.returncode, 0)

    def test_idempotent_double_run(self):
        p = self._mk(self.tmp, ".capture-prompted-old", 8)
        r1 = self._sweep(max_age_days=7)
        r2 = self._sweep(max_age_days=7)
        self.assertEqual(r1.returncode, 0)
        self.assertEqual(r2.returncode, 0)
        self.assertFalse(p.exists())

    def test_path_with_spaces_swept(self):
        spaced = self.tmp / "dir with spaces"
        spaced.mkdir()
        p = self._mk(spaced, ".convention-prompted-old", 8)
        r = self._run("sweep", "--marker-dir", str(spaced), "--max-age-days", "7")
        self.assertEqual(r.returncode, 0)
        self.assertFalse(p.exists())


class SweepUnionChainTest(SweepBase):
    """sweep must reach EVERY directory the capture/convention hooks resolve.

    capture-failure uses ZMEM_DATA > ZCODE_PLUGIN_DATA > ~/.zmem;
    convention-capture uses dirname(ZMEM_STORE) > ZMEM_DATA > CLAUDE_PLUGIN_DATA >
    ZCODE_PLUGIN_DATA > host.py tail. To keep these tests deterministic and
    isolated from the real box store, the data-dir env vars are pinned to temp
    dirs and HOME/USERPROFILE are redirected into a throwaway home.
    """

    def _isolated_env(self, home, overrides):
        # Start from the ambient env, drop any data-dir vars, redirect home so
        # ~/.zmem resolves into the throwaway tree, then apply the overrides.
        env = {**os.environ}
        for k in DATA_DIR_ENV_VARS:
            env.pop(k, None)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        env.update(overrides)
        return env

    def test_env_resolution_uses_zmem_data(self):
        d = self.tmp / "data dir with space"
        d.mkdir(parents=True)
        p = self._mk(d, ".capture-prompted-old", 8)
        env = self._isolated_env(
            home=self.tmp / "fakehome",
            overrides={"ZMEM_DATA": str(d)},
        )
        r = self._run("sweep", "--max-age-days", "7", env_extra=env,
                      env_remove=DATA_DIR_ENV_VARS)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(p.exists(), "ZMEM_DATA marker must be swept")

    def test_union_sweeps_dirname_of_zmem_store(self):
        # convention-capture's chain resolves dirname(ZMEM_STORE); a marker
        # placed there must be pruned even when ZMEM_DATA is also set elsewhere.
        store_dir = self.tmp / "storedir"
        store_dir.mkdir()
        data_dir = self.tmp / "datadir"
        data_dir.mkdir()
        p_store = self._mk(store_dir, ".convention-prompted-old", 8)
        p_data = self._mk(data_dir, ".capture-prompted-old2", 9)
        env = self._isolated_env(
            home=self.tmp / "fakehome2",
            overrides={
                "ZMEM_STORE": str(store_dir / "store.sqlite"),
                "ZMEM_DATA": str(data_dir),
            },
        )
        r = self._run("sweep", "--max-age-days", "7", env_extra=env,
                      env_remove=DATA_DIR_ENV_VARS)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(p_store.exists(), "dirname(ZMEM_STORE) must be swept")
        self.assertFalse(p_data.exists(), "ZMEM_DATA must be swept")

    def test_env_default_ttl_via_zmem_sentinel_sweep_days(self):
        # PRR-006: the env-default TTL path (ZMEM_SENTINEL_SWEEP_DAYS) is what the
        # SessionStart hook exercises (it fires `sweep` with no --max-age-days).
        d = self.tmp / "envdefault"
        d.mkdir()
        stale = self._mk(d, ".capture-prompted-old", 3)
        fresh = self._mk(d, ".convention-prompted-new", 0)
        env = self._isolated_env(
            home=self.tmp / "fh3",
            overrides={"ZMEM_DATA": str(d), "ZMEM_SENTINEL_SWEEP_DAYS": "2"},
        )
        r = self._run("sweep", env_extra=env, env_remove=DATA_DIR_ENV_VARS)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(stale.exists(), "stale marker must be pruned by env TTL")
        self.assertTrue(fresh.exists(), "fresh marker must survive env TTL")

    def test_union_sweeps_claude_and_zcode_plugin_data(self):
        # PRR-007: convention-capture's chain also resolves CLAUDE_PLUGIN_DATA and
        # ZCODE_PLUGIN_DATA; markers placed there must be pruned too.
        claude_dir = self.tmp / "claude_data"
        zcode_dir = self.tmp / "zcode_data"
        claude_dir.mkdir()
        zcode_dir.mkdir()
        p_claude = self._mk(claude_dir, ".convention-prompted-old", 8)
        p_zcode = self._mk(zcode_dir, ".capture-prompted-old", 9)
        env = self._isolated_env(
            home=self.tmp / "fh4",
            overrides={
                "CLAUDE_PLUGIN_DATA": str(claude_dir),
                "ZCODE_PLUGIN_DATA": str(zcode_dir),
            },
        )
        r = self._run("sweep", "--max-age-days", "7", env_extra=env,
                      env_remove=DATA_DIR_ENV_VARS)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(p_claude.exists(), "CLAUDE_PLUGIN_DATA must be swept")
        self.assertFalse(p_zcode.exists(), "ZCODE_PLUGIN_DATA must be swept")

    def test_union_sweeps_home_relative_nodes(self):
        # PRR-007 (remainder): _sweep_candidate_dirs also adds home-relative
        # nodes that no env var covers — ~/.zmem (always), ~/.zcode/memory
        # (if it exists), and each ~/.zcode/cli/plugins/data/*zmem* dir (the
        # legacy plugin scan, filtered by name). All three must be swept, and
        # a non-zmem sibling under the scan root must be left alone (pins the
        # name filter). HOME/USERPROFILE are redirected into a throwaway tree
        # so the real box store is never touched.
        home = self.tmp / "fh6"
        zmem_dir = home / ".zmem"
        legacy_dir = home / ".zcode" / "memory"
        scan_root = home / ".zcode" / "cli" / "plugins" / "data"
        legacy_plugin = scan_root / "zmem-old"
        other_plugin = scan_root / "other-plugin"  # must NOT be swept
        for d in (zmem_dir, legacy_dir, legacy_plugin, other_plugin):
            d.mkdir(parents=True)
        p_zmem = self._mk(zmem_dir, ".capture-prompted-old", 8)
        p_legacy = self._mk(legacy_dir, ".convention-prompted-old", 9)
        p_plugin = self._mk(legacy_plugin, ".capture-prompted-old2", 10)
        # A sentinel-prefixed file in a NON-zmem plugin dir must survive: the
        # scan only adds dirs whose name contains "zmem" (store.py:3343).
        p_other = self._mk(other_plugin, ".convention-prompted-old", 11)
        env = self._isolated_env(home=home, overrides={})
        r = self._run("sweep", "--max-age-days", "7", env_extra=env,
                      env_remove=DATA_DIR_ENV_VARS)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(p_zmem.exists(), "~/.zmem node must be swept")
        self.assertFalse(p_legacy.exists(), "~/.zcode/memory node must be swept")
        self.assertFalse(p_plugin.exists(),
                         "~/.zcode/cli/plugins/data/*zmem* node must be swept")
        self.assertTrue(p_other.exists(),
                        "a non-zmem plugin dir must NOT be swept (name filter)")

    def test_unreadable_dir_fail_open(self):
        # PRR-008: a listdir OSError on a candidate dir must fail-open (exit 0),
        # not crash, and must not block sweeping OTHER candidate dirs. On POSIX a
        # chmod 000 dir is unreadable; on Windows chmod does not block the owner,
        # so skip there (the is_file/stat OSError catch is exercised by the
        # missing-dir and path-with-spaces tests).
        if os.name == "nt":
            self.skipTest("POSIX-only: Windows chmod does not deny the owner")
        locked = self.tmp / "locked"
        ok = self.tmp / "ok"
        locked.mkdir()
        ok.mkdir()
        ok_marker = self._mk(ok, ".capture-prompted-old", 8)
        os.chmod(locked, 0o000)
        try:
            env = self._isolated_env(
                home=self.tmp / "fh5",
                overrides={"ZMEM_DATA": str(locked), "CLAUDE_PLUGIN_DATA": str(ok)},
            )
            r = self._run("sweep", "--max-age-days", "7", env_extra=env,
                          env_remove=DATA_DIR_ENV_VARS)
            self.assertEqual(r.returncode, 0,
                             "unreadable dir must fail-open, not crash")
            # Fail-open must also CONTINUE sweeping: the other candidate dir's
            # stale marker is still pruned despite the locked sibling.
            self.assertFalse(ok_marker.exists(),
                             "sweep must continue past an unreadable dir")
        finally:
            # Restore so tearDown can rmtree the tree.
            os.chmod(locked, 0o755)


if __name__ == "__main__":
    unittest.main()
