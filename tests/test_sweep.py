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


if __name__ == "__main__":
    unittest.main()
