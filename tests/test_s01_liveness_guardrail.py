"""Guardrail suite (S01 / issue #262): maintenance-lock holder liveness.

Companion to the frozen acceptance checks in
test_s01_maintenance_lock_liveness.py. Where the frozen checks pin the two
user-visible bug shapes (dead holder, empty backdated file), THIS suite pins
the fix's safety machinery and fail-closed boundaries:

  - ProcessAliveTest        host.process_alive semantics (alive / dead /
                            unknown-never-crashes, pid domain guard).
  - ReadLockHolderPidTest   the tri-state parser: (token, pid) / ("", None)
                            for stripped-empty / None for missing, unreadable,
                            binary junk, malformed text, out-of-domain pids.
  - BreakPhantomLockSafetyTest  the identity-checked break: content mismatch
                            never renames; the empty limb respects the
                            under-claim min-age gate; the token limb ignores
                            age by design; whitespace-only files break via
                            the strip-consistent empty marker.
  - ProbeDecisionTest       the probe's classification and clamp (in-process):
                            grace clamp, phantom verdicts per shape, and the
                            fail-closed liveness-unknown limb.
  - EndToEndPhantomRecoveryTest  real-CLI pins: a fresh dead token clears
                            without the stale wait; malformed / huge-pid /
                            binary-junk files refuse CLEANLY (today's message,
                            no traceback) even when old; the grace env knob
                            boundary; restore and purge proceed past a phantom.

House rules as in the frozen check file: the real ~/.zmem store is never
touched — the in-process import is pinned to a throwaway dir at module top
(BEFORE the storelib import: storelib freezes STORE_PATH from the ambient
env), and every subprocess gets its own freshly pinned env. The maintenance
wait is shortened to 0.3 s for the in-process refusal pins only; subprocess
tests pop that knob so the frozen 5.0 s default (and today's exact refusal
message) governs.

Run: python tests/test_s01_liveness_guardrail.py
"""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"

MAINTENANCE_LOCK_NAME = ".zmem-maintenance.lock"
STORE_FILE_NAME = "store.sqlite"

# --- module-level pin (must precede the storelib import) --------------------
_INPROC_TMP = tempfile.mkdtemp(prefix="zmem-s01-guard-inproc-")
atexit.register(shutil.rmtree, _INPROC_TMP, True)
os.environ["ZMEM_STORE"] = os.path.join(_INPROC_TMP, STORE_FILE_NAME)
os.environ["ZMEM_DATA"] = _INPROC_TMP
os.environ["ZMEM_AUTO_REKEY"] = "0"
os.environ["ZMEM_MAINTENANCE_WAIT_SECONDS"] = "0.3"
# Hermeticity (cubic F-8c): the in-process class classifies by grace age, so
# an ambient GRACE/POLL override from a developer shell would skew the pins.
# Pop both so the frozen defaults (grace 10 s, poll 0.05 s) govern the import.
os.environ.pop("ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS", None)
os.environ.pop("ZMEM_MAINTENANCE_POLL_SECONDS", None)
sys.path.insert(0, str(SCRIPTS_DIR))

import host  # noqa: E402
import storelib.schema as schema  # noqa: E402


def _pinned_env(tmp: Path, **overrides) -> dict:
    """Child env pinned to a throwaway store under ``tmp``; maintenance knobs
    are reset to frozen defaults unless explicitly overridden."""
    env = dict(os.environ)
    env["ZMEM_STORE"] = str(tmp / STORE_FILE_NAME)
    env["ZMEM_DATA"] = str(tmp)
    env["ZMEM_AUTO_REKEY"] = "0"
    for knob in (
        "ZMEM_MAINTENANCE_LOCK_STALE_SECONDS",
        "ZMEM_MAINTENANCE_WAIT_SECONDS",
        "ZMEM_MAINTENANCE_POLL_SECONDS",
        "ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS",
    ):
        env.pop(knob, None)
    for key, value in overrides.items():
        env[key] = str(value)
    return env


def _run_add(tmp: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(STORE_PY),
            "add",
            "--namespace",
            "project:s01guard",
            "--type",
            "lesson",
            "--content",
            "guard probe row",
            "--signal",
            "test",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _spawn_killed_holder(tmp: Path, token_tail: str = "cafebabe") -> tuple[int, str]:
    """Create a REAL dead holder via the genuine primitive: a child takes the
    maintenance lock, reports ready, and is killed. Returns (pid, token)."""
    lock_path = tmp / MAINTENANCE_LOCK_NAME
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys\n"
            "import time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "import host\n"
            "from pathlib import Path\n"
            "token = host.acquire_lock(Path(sys.argv[2]), 600)\n"
            "print('ready', token is not None, flush=True)\n"
            "time.sleep(600)\n",
            str(SCRIPTS_DIR),
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = child.stdout.readline()
        assert line.startswith("ready") and "True" in line, line
        token = lock_path.read_text(encoding="utf-8").strip()
        assert token, "child lock file carries no token"
    finally:
        # Never leak a live sleeper on an assertion failure (cubic F-8d).
        if child.poll() is None:
            child.kill()
        child.wait()
        if child.stdout is not None:
            child.stdout.close()
    return child.pid, token


def _plant(tmp: Path, payload: bytes, age_seconds: float | None = None) -> Path:
    lock_path = tmp / MAINTENANCE_LOCK_NAME
    lock_path.write_bytes(payload)
    if age_seconds is not None:
        old = time.time() - age_seconds
        os.utime(lock_path, (old, old))
    return lock_path


class ProcessAliveTest(unittest.TestCase):
    """host.process_alive: the fail-closed liveness primitive."""

    def test_self_pid_reports_alive(self):
        self.assertIs(host.process_alive(os.getpid()), True)

    def test_killed_child_reports_dead(self):
        tmp = Path(tempfile.mkdtemp(prefix="zmem-s01-guard-pa-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        pid, _token = _spawn_killed_holder(tmp)
        self.assertIs(host.process_alive(pid), False)

    def test_killed_child_with_live_handle_reports_dead(self):
        # Windows keeps the process OBJECT alive while any handle is open —
        # the killer's Popen holds one through kill()+wait(). The killed
        # holder is still dead: GetExitCodeProcess must distinguish, exactly
        # the shape a hook-runner timeout kill produces. (This pin is the
        # frozen check C2's failure mode, kept at unit level.)
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            child.kill()
            child.wait()
            self.assertIs(host.process_alive(child.pid), False)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_out_of_domain_and_garbage_are_unknown_not_crashes(self):
        self.assertIsNone(host.process_alive(0))
        self.assertIsNone(host.process_alive(-5))
        self.assertIsNone(host.process_alive(10**20))
        self.assertIsNone(host.process_alive("not-a-pid"))
        self.assertIsNone(host.process_alive(None))


class ReadLockHolderPidTest(unittest.TestCase):
    """The tri-state parser: holder / stripped-empty / no-holder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-s01-guard-rd-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _read(self, payload: bytes):
        return host.read_lock_holder_pid(_plant(self.tmp, payload))

    def test_missing_file_is_no_holder(self):
        self.assertIsNone(host.read_lock_holder_pid(self.tmp / "nope.lock"))

    def test_stripped_empty_file_is_the_empty_limb(self):
        self.assertEqual(self._read(b""), ("", None))

    def test_whitespace_only_file_is_the_empty_limb(self):
        self.assertEqual(self._read(b"  \n\t"), ("", None))

    def test_binary_junk_is_none_without_crashing(self):
        self.assertIsNone(self._read(b"\xff\xfe\x00binary-junk"))

    def test_malformed_text_token_is_none(self):
        self.assertIsNone(self._read(b"not-a-pid:abc"))

    def test_huge_pid_token_is_none(self):
        self.assertIsNone(self._read(b"99999999999:abcdef"))

    def test_unicode_digit_head_is_none_not_crash(self):
        # "²".isdigit() is True but int("²") raises ValueError: a foreign
        # lock file must degrade to no-holder, never crash (review R1).
        self.assertIsNone(self._read("²:abc".encode("utf-8")))

    def test_over_long_digit_head_is_none_not_crash(self):
        # A digit run beyond sys.get_int_max_str_digits() (4300 on 3.11)
        # makes int() raise despite isdigit() being true.
        self.assertIsNone(self._read(b"9" * 5000 + b":abc"))

    def test_arabic_indic_digit_head_is_none(self):
        # "٣".isdigit() is True AND int("٣")==3 SUCCEEDS: without the
        # isascii() gate a foreign lock would probe an unrelated real pid
        # (review swarm F1 / cubic F-4).
        self.assertIsNone(self._read("٣:abc".encode("utf-8")))

    def test_oversized_lock_file_is_none(self):
        # A hostile multi-KB blob at the lock path must not be read into the
        # parser (bounded-read gate, MAX_LOCK_READ_BYTES).
        self.assertIsNone(self._read(b"x" * 8192 + b":abc"))

    def test_directory_at_lock_path_is_none(self):
        # Non-regular lock "files" (dir/fifo/symlink family) classify as
        # no-recorded-holder via the lstat+S_ISREG gate.
        (self.tmp / MAINTENANCE_LOCK_NAME).mkdir()
        self.assertIsNone(
            host.read_lock_holder_pid(self.tmp / MAINTENANCE_LOCK_NAME)
        )

    def test_valid_token_parses(self):
        self.assertEqual(
            self._read(b"424242:abcdef0123456789"),
            ("424242:abcdef0123456789", 424242),
        )


class BreakPhantomLockSafetyTest(unittest.TestCase):
    """The identity-checked break never touches a lock it cannot prove."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-s01-guard-brk-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_token_mismatch_is_never_renamed(self):
        lock = _plant(self.tmp, b"111111:aaaa")
        self.assertFalse(host.break_phantom_lock(lock, "222222:bbbb"))
        self.assertTrue(lock.exists())
        self.assertEqual(lock.read_text(encoding="utf-8").strip(), "111111:aaaa")

    def test_fresh_empty_file_within_min_age_is_not_broken(self):
        lock = _plant(self.tmp, b"")
        self.assertFalse(host.break_phantom_lock(lock, "", min_age_seconds=10.0))
        self.assertTrue(lock.exists())

    def test_old_empty_file_past_min_age_breaks(self):
        lock = _plant(self.tmp, b"", age_seconds=30.0)
        self.assertTrue(host.break_phantom_lock(lock, "", min_age_seconds=10.0))
        self.assertFalse(lock.exists())

    def test_dead_token_breaks_regardless_of_min_age(self):
        # The token limb's identity witness is the CONTENT (a live successor's
        # pid:uuid can never equal the classified dead token), so no age gate
        # applies — a just-killed holder must clear immediately.
        lock = _plant(self.tmp, b"999999:deadbeef")
        self.assertTrue(
            host.break_phantom_lock(lock, "999999:deadbeef", min_age_seconds=10.0)
        )
        self.assertFalse(lock.exists())

    def test_whitespace_only_old_file_breaks_via_empty_marker(self):
        lock = _plant(self.tmp, b"   \n", age_seconds=30.0)
        self.assertTrue(host.break_phantom_lock(lock, "", min_age_seconds=10.0))
        self.assertFalse(lock.exists())

    def test_content_changed_after_rename_is_put_back(self):
        # The put-back limb: if the moved-aside file's content no longer
        # matches the classified token at confirm time (a successor installed
        # content between our pre-read and our rename), the file must be
        # restored, not dropped. Patched two-phase read: pre-read matches,
        # confirm read disagrees (cubic F-8a).
        lock = _plant(self.tmp, b"999999:aaa")
        real_read_text = Path.read_text
        calls = {"n": 0}

        def two_phase_read(self_path, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_read_text(self_path, *args, **kwargs)
            return "777777:bbb"

        with mock.patch.object(Path, "read_text", two_phase_read):
            self.assertFalse(host.break_phantom_lock(lock, "999999:aaa"))
        self.assertTrue(lock.exists())
        self.assertEqual(
            lock.read_text(encoding="utf-8").strip(), "999999:aaa"
        )


class ProbeDecisionTest(unittest.TestCase):
    """In-process classification: clamp, verdicts, fail-closed unknown."""

    def test_grace_clamp(self):
        clamp = schema._clamped_maintenance_grace
        self.assertEqual(clamp(float("nan")), 10.0)
        self.assertEqual(clamp(float("inf")), 10.0)
        self.assertEqual(clamp(-5.0), 10.0)
        self.assertEqual(clamp(0.0), 10.0)
        self.assertEqual(clamp(0.5), 1.0)
        self.assertEqual(clamp(3.0), 3.0)
        self.assertEqual(clamp(99999.0), schema.MAINTENANCE_LOCK_STALE_SECONDS)

    def test_grace_clamp_floor_holds_when_stale_window_is_sub_second(self):
        # A sub-second ZMEM_MAINTENANCE_LOCK_STALE_SECONDS must not drag the
        # grace ceiling (and therefore the floor) below 1 s (cubic F-2).
        clamp = schema._clamped_maintenance_grace
        with mock.patch.object(
            schema, "MAINTENANCE_LOCK_STALE_SECONDS", 0.5
        ):
            self.assertEqual(clamp(3.0), 1.0)
            self.assertEqual(clamp(10.0), 1.0)

    def setUp(self):
        self.verdict = schema._inspect_maintenance_phantom

    def test_empty_old_is_phantom_with_empty_marker(self):
        lock = _plant(Path(_INPROC_TMP), b"", age_seconds=30.0)
        try:
            self.assertEqual(self.verdict(lock), (True, ""))
        finally:
            lock.unlink(missing_ok=True)

    def test_empty_fresh_is_not_phantom(self):
        lock = _plant(Path(_INPROC_TMP), b"")
        try:
            self.assertEqual(self.verdict(lock), (False, ""))
        finally:
            lock.unlink(missing_ok=True)

    def test_dead_token_is_phantom_with_its_exact_token(self):
        _pid, token = _spawn_killed_holder(Path(_INPROC_TMP))
        lock = Path(_INPROC_TMP) / MAINTENANCE_LOCK_NAME
        try:
            self.assertEqual(self.verdict(lock), (True, token))
        finally:
            lock.unlink(missing_ok=True)

    def test_live_token_is_not_phantom(self):
        lock = _plant(Path(_INPROC_TMP), f"{os.getpid()}:cafe".encode())
        try:
            self.assertEqual(self.verdict(lock), (False, ""))
        finally:
            lock.unlink(missing_ok=True)

    def test_unparseable_old_content_is_not_phantom(self):
        lock = _plant(Path(_INPROC_TMP), b"not-a-pid:abc", age_seconds=30.0)
        try:
            self.assertEqual(self.verdict(lock), (False, ""))
        finally:
            lock.unlink(missing_ok=True)

    def test_liveness_unknown_fails_closed(self):
        _pid, token = _spawn_killed_holder(Path(_INPROC_TMP))
        lock = Path(_INPROC_TMP) / MAINTENANCE_LOCK_NAME
        original = host.process_alive
        try:
            host.process_alive = lambda pid: None
            self.assertEqual(self.verdict(lock), (False, ""))
            # The full probe refuses with today's message when liveness is
            # uncertain (wait shortened to 0.3 s by the module pin).
            with self.assertRaises(RuntimeError) as ctx:
                schema._wait_for_maintenance_clear("add")
            self.assertIn("maintenance is active", str(ctx.exception))
        finally:
            host.process_alive = original
            lock.unlink(missing_ok=True)

    def test_probe_clears_quickly_on_a_dead_holder(self):
        _pid, _token = _spawn_killed_holder(Path(_INPROC_TMP))
        lock = Path(_INPROC_TMP) / MAINTENANCE_LOCK_NAME
        try:
            started = time.time()
            self.assertIsNone(schema._wait_for_maintenance_clear("recall"))
            self.assertLess(time.time() - started, 2.0)
            self.assertFalse(lock.exists())
        finally:
            lock.unlink(missing_ok=True)


class EndToEndPhantomRecoveryTest(unittest.TestCase):
    """Real-CLI pins: the class boundaries behave without crashing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-s01-guard-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def assert_clean_refusal(self, proc):
        """Old unparseable shapes keep today's fail-closed refusal, with no
        traceback leaking through the new pre-dispatch liveness path."""
        self.assertEqual(proc.returncode, 2, proc.stderr or proc.stdout)
        self.assertIn("maintenance is active", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertNotIn("Traceback", proc.stdout)
        self.assertFalse((self.tmp / STORE_FILE_NAME).exists())

    def test_fresh_dead_token_clears_without_stale_wait(self):
        _pid, token = _spawn_killed_holder(self.tmp)  # fresh mtime by design
        self.assertEqual(token.count(":"), 1)
        proc = _run_add(self.tmp, _pinned_env(self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue((self.tmp / STORE_FILE_NAME).exists())

    def test_malformed_old_token_refuses_cleanly(self):
        _plant(self.tmp, b"not-a-pid:abc", age_seconds=30.0)
        self.assert_clean_refusal(_run_add(self.tmp, _pinned_env(self.tmp)))

    def test_huge_pid_old_token_refuses_cleanly(self):
        _plant(self.tmp, b"99999999999:abcdef", age_seconds=30.0)
        self.assert_clean_refusal(_run_add(self.tmp, _pinned_env(self.tmp)))

    def test_unicode_digit_head_old_refuses_cleanly(self):
        # isdigit()-true but int()-invalid content must keep today's clean
        # refusal, never a traceback on the pre-dispatch path (review R1).
        _plant(self.tmp, "²:abc".encode("utf-8"), age_seconds=30.0)
        self.assert_clean_refusal(_run_add(self.tmp, _pinned_env(self.tmp)))

    def test_over_long_digit_head_old_refuses_cleanly(self):
        _plant(self.tmp, b"9" * 5000 + b":abc", age_seconds=30.0)
        self.assert_clean_refusal(_run_add(self.tmp, _pinned_env(self.tmp)))

    def test_binary_junk_old_refuses_cleanly(self):
        _plant(self.tmp, b"\xff\xfe\x00binary-junk", age_seconds=30.0)
        self.assert_clean_refusal(_run_add(self.tmp, _pinned_env(self.tmp)))

    def test_grace_env_boundary(self):
        # Inside the (widened) grace window a stripped-empty file may still be
        # a live acquirer: blocked. Outside it: cleared.
        _plant(self.tmp, b"", age_seconds=10.0)
        blocked = _run_add(
            self.tmp, _pinned_env(self.tmp, ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS=30)
        )
        self.assertEqual(blocked.returncode, 2, blocked.stderr)
        self.assertIn("maintenance is active", blocked.stderr)
        self.assertFalse((self.tmp / STORE_FILE_NAME).exists())

        (self.tmp / MAINTENANCE_LOCK_NAME).unlink()
        _plant(self.tmp, b"", age_seconds=10.0)
        cleared = _run_add(
            self.tmp, _pinned_env(self.tmp, ZMEM_MAINTENANCE_LOCK_GRACE_SECONDS=5)
        )
        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertTrue((self.tmp / STORE_FILE_NAME).exists())

    def test_restore_proceeds_past_phantom_lock(self):
        src = self.tmp / "src"
        src.mkdir()
        seeded = _run_add(src, _pinned_env(src))
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        dst = self.tmp / "dst"
        dst.mkdir()
        _plant(dst, b"", age_seconds=30.0)
        proc = subprocess.run(
            [
                sys.executable,
                str(STORE_PY),
                "restore",
                "--from",
                str(src / STORE_FILE_NAME),
                "--force",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=_pinned_env(dst),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("restore REFUSED", proc.stderr)
        self.assertTrue((dst / STORE_FILE_NAME).exists())

    def test_purge_proceeds_past_phantom_lock(self):
        seeded = _run_add(self.tmp, _pinned_env(self.tmp))
        self.assertEqual(seeded.returncode, 0, seeded.stderr)
        _plant(self.tmp, b"", age_seconds=30.0)
        proc = subprocess.run(
            [
                sys.executable,
                str(STORE_PY),
                "purge",
                "--id",
                "nonexistent-zzz-guard",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env=_pinned_env(self.tmp),
        )
        self.assertNotEqual(proc.returncode, 4, proc.stderr)
        # The gate under test is specifically the MAINTENANCE refusal; the
        # unknown-id refusal legitimately reuses the "purge REFUSED" prefix.
        self.assertNotIn("another maintenance operation", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
