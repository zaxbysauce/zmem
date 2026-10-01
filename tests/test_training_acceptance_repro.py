"""Run each issue #135 acceptance replay in its own subprocess.

The validators and portable fixtures live under ``tests/fixtures/training/repro``
so CI never needs the issue-trace working files.  Each test intentionally
executes one validator independently and checks its stable ``ACn_OK`` marker.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPRO = ROOT / "tests" / "fixtures" / "training" / "repro"
_WRAPPER_FAILURE_EXIT = 86
_WRAPPER_FAILURE_MARKER = "ZMEM_VALIDATOR_CONTAINMENT_FAILURE:"
_CLEANUP_SECONDS = 1.0
_DRAIN_SECONDS = 1.0

_WINDOWS_JOB_WRAPPER = r'''
import ctypes, os, subprocess, sys
from ctypes import wintypes
MARKER = "ZMEM_VALIDATOR_CONTAINMENT_FAILURE:"
EXIT = 86
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
job = wintypes.HANDLE(int(sys.argv[1]))
try:
    if not kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess()):
        raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
    if not kernel32.CloseHandle(job):
        raise OSError(ctypes.get_last_error(), "CloseHandle failed")
    child = subprocess.Popen([sys.executable, sys.argv[2]], cwd=sys.argv[3], close_fds=True)
except Exception as exc:
    try:
        kernel32.CloseHandle(job)
    except Exception:
        pass
    print(MARKER + repr(exc), file=sys.stderr, flush=True)
    raise SystemExit(EXIT)
raise SystemExit(child.wait())
'''


class _PipeReader:
    """Daemon byte reader: bounded joins never force-close a blocked read."""

    def __init__(self, stream: object) -> None:
        self.chunks: list[bytes] = []
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _read(self, stream: object) -> None:
        try:
            while True:
                chunk = stream.read(8192)  # type: ignore[attr-defined]
                if not chunk:
                    return
                self.chunks.append(chunk)
        except OSError:
            return

    def finish(self) -> tuple[str, bool]:
        self.thread.join(_DRAIN_SECONDS)
        return b"".join(self.chunks).decode("utf-8", errors="replace"), self.thread.is_alive()


class TrainingAcceptanceReproTests(unittest.TestCase):
    @staticmethod
    def _create_windows_job() -> int:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, class 9, with KILL_ON_JOB_CLOSE.
        class Basic(ctypes.Structure):
            _fields_ = [("per_process", ctypes.c_longlong), ("per_job", ctypes.c_longlong),
                       ("flags", ctypes.c_ulong), ("min_ws", ctypes.c_size_t),
                       ("max_ws", ctypes.c_size_t), ("active", ctypes.c_ulong),
                       ("affinity", ctypes.c_size_t), ("priority", ctypes.c_ulong),
                       ("schedule", ctypes.c_ulong)]
        class Counters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes"
            )]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("counters", Counters),
                       ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                       ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
        limits = Extended()
        limits.basic.flags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject.argtypes = (
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong
        )
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise OSError(error, "SetInformationJobObject failed")
        return int(job)

    @staticmethod
    def _terminate_windows_job(job: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.TerminateJobObject.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.TerminateJobObject.restype = ctypes.c_int
        if not kernel32.TerminateJobObject(job, 1):
            raise OSError(ctypes.get_last_error(), "TerminateJobObject failed")

    @staticmethod
    def _close_windows_handle(handle: int) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_int
        if not kernel32.CloseHandle(handle):
            raise OSError(ctypes.get_last_error(), "CloseHandle failed")

    @staticmethod
    def _terminate_posix_group(process: subprocess.Popen[bytes]) -> None:
        try:
            os.killpg(process.pid, 15)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + _CLEANUP_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        try:
            os.killpg(process.pid, 9)
        except ProcessLookupError:
            pass

    @staticmethod
    def _wait_for_process(process: subprocess.Popen[bytes], seconds: float) -> int | None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            status = process.poll()
            if status is not None:
                return status
            time.sleep(0.01)
        return process.poll()

    def _run_validator(
        self,
        name: str,
        marker: str,
        *,
        timeout_seconds: float = 240,
        validator_path: Path | None = None,
    ) -> None:
        path = validator_path or (REPRO / name)
        process: subprocess.Popen[bytes] | None = None
        job: int | None = None
        stdout_reader: _PipeReader | None = None
        stderr_reader: _PipeReader | None = None
        timed_out = False
        containment_error: str | None = None
        stdout = stderr = ""
        stdout_abandoned = stderr_abandoned = False
        try:
            with tempfile.TemporaryDirectory(prefix="zmem-135-validator-") as scratch:
                env = os.environ.copy()
                env.update({"TMPDIR": scratch, "TMP": scratch, "TEMP": scratch})
                try:
                    if os.name == "nt":
                        job = self._create_windows_job()
                        os.set_handle_inheritable(job, True)
                        try:
                            process = subprocess.Popen(
                                [sys.executable, "-c", _WINDOWS_JOB_WRAPPER, str(job), str(path), str(ROOT)],
                                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=False,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                            )
                        finally:
                            # A restoration failure still enters the lifecycle teardown below.
                            os.set_handle_inheritable(job, False)
                    else:
                        process = subprocess.Popen(
                            [sys.executable, str(path)], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                        )
                except (OSError, subprocess.SubprocessError) as exc:
                    containment_error = repr(exc)
                try:
                    if containment_error is None and process is not None:
                        stdout_reader = _PipeReader(process.stdout)
                        stderr_reader = _PipeReader(process.stderr)
                        status = self._wait_for_process(process, timeout_seconds)
                        timed_out = status is None
                finally:
                    if process is not None:
                        try:
                            if os.name == "nt":
                                assert job is not None
                                self._terminate_windows_job(job)
                            else:
                                self._terminate_posix_group(process)
                        except OSError as exc:
                            containment_error = repr(exc)
                        self._wait_for_process(process, _CLEANUP_SECONDS)
                        if stdout_reader is not None:
                            try:
                                stdout, stdout_abandoned = stdout_reader.finish()
                                if not stdout_abandoned:
                                    process.stdout.close()
                            except Exception as exc:
                                containment_error = repr(exc)
                        else:
                            try:
                                process.stdout.close()
                            except OSError as exc:
                                containment_error = repr(exc)
                        if stderr_reader is not None:
                            try:
                                stderr, stderr_abandoned = stderr_reader.finish()
                                if not stderr_abandoned:
                                    process.stderr.close()
                            except Exception as exc:
                                containment_error = repr(exc)
                        else:
                            try:
                                process.stderr.close()
                            except OSError as exc:
                                containment_error = repr(exc)
                    if job is not None:
                        try:
                            self._close_windows_handle(job)
                        except OSError as exc:
                            containment_error = repr(exc)
                        finally:
                            job = None
                if process is None:
                    stdout = stderr = ""
                    stdout_abandoned = stderr_abandoned = False
                if stdout_abandoned or stderr_abandoned:
                    stderr += "\n[validator output drain exceeded bound; readers abandoned]"
            # The parent-owned scratch is cleaned before either result surface is reported.
        finally:
            if job is not None:
                try:
                    self._close_windows_handle(job)
                except OSError:
                    pass
        if containment_error is not None:
            self.fail(f"{name} validator containment failure: {containment_error}")
        assert process is not None
        if process.returncode == _WRAPPER_FAILURE_EXIT and _WRAPPER_FAILURE_MARKER in stderr:
            self.fail(f"{name} validator containment failure: {stderr}")
        if timed_out:
            self.fail(
                f"{name} validator timed out after {timeout_seconds:g} seconds; "
                f"partial stdout:\n{stdout}\npartial stderr:\n{stderr}"
            )
        output = stdout + stderr
        self.assertEqual(process.returncode, 0, output)
        self.assertIn(marker, output, output)

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if os.name == "nt":
            kernel32 = ctypes.windll.kernel32
            kernel32.OpenProcess.argtypes = (ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong)
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.GetExitCodeProcess.argtypes = (
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)
            )
            kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            try:
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == 259  # STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def test_timeout_reports_partial_output_reaps_descendant_and_removes_scratch(self) -> None:
        with tempfile.TemporaryDirectory(prefix="training-timeout-repro-") as raw:
            temp_root = Path(raw)
            pid_path = temp_root / "pids.json"
            scratch_path = temp_root / "scratch.txt"
            validator = temp_root / "timeout-validator.py"
            validator.write_text(
                "import json, os, pathlib, subprocess, sys, tempfile, time\n"
                "with tempfile.TemporaryDirectory(prefix='validator-child-'):\n"
                "    descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"    pathlib.Path({str(pid_path)!r}).write_text(json.dumps({{'validator': os.getpid(), 'descendant': descendant.pid}}), encoding='utf-8')\n"
                f"    pathlib.Path({str(scratch_path)!r}).write_text(tempfile.gettempdir(), encoding='utf-8')\n"
                "    print('timeout partial stdout', flush=True)\n"
                "    print('timeout partial stderr', file=sys.stderr, flush=True)\n"
                "    time.sleep(30)\n",
                encoding="utf-8",
            )

            started = time.monotonic()
            with self.assertRaises(AssertionError) as failure:
                self._run_validator(
                    validator.name,
                    "never-emitted-marker",
                    timeout_seconds=5.0,
                    validator_path=validator,
                )
            elapsed = time.monotonic() - started

            message = str(failure.exception)
            self.assertIn(
                "timeout-validator.py validator timed out after 5 seconds", message
            )
            self.assertIn("timeout partial stdout", message)
            self.assertIn("timeout partial stderr", message)
            self.assertTrue(pid_path.is_file())
            pids = json.loads(pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + _CLEANUP_SECONDS
            while any(self._pid_exists(pids[key]) for key in ("validator", "descendant")) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(any(self._pid_exists(pids[key]) for key in ("validator", "descendant")))
            self.assertFalse(Path(scratch_path.read_text(encoding="utf-8")).exists())
            self.assertLess(elapsed, 5.0 + (_CLEANUP_SECONDS * 4) + 4.0)

    def test_normal_validator_completion_reaps_lingering_descendant(self) -> None:
        with tempfile.TemporaryDirectory(prefix="training-normal-repro-") as raw:
            temp_root = Path(raw)
            pid_path = temp_root / "pids.json"
            scratch_path = temp_root / "scratch.txt"
            validator = temp_root / "normal-validator.py"
            validator.write_text(
                "import json, os, pathlib, subprocess, sys, tempfile\n"
                "descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(json.dumps({{'validator': os.getpid(), 'descendant': descendant.pid}}), encoding='utf-8')\n"
                f"pathlib.Path({str(scratch_path)!r}).write_text(tempfile.gettempdir(), encoding='utf-8')\n"
                "print('NORMAL_DESCENDANT_OK', flush=True)\n",
                encoding="utf-8",
            )
            self._run_validator(validator.name, "NORMAL_DESCENDANT_OK", timeout_seconds=5.0, validator_path=validator)
            pids = json.loads(pid_path.read_text(encoding="utf-8"))
            deadline = time.monotonic() + _CLEANUP_SECONDS
            while self._pid_exists(pids["descendant"]) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(self._pid_exists(pids["descendant"]))
            self.assertFalse(Path(scratch_path.read_text(encoding="utf-8")).exists())

    def test_nonzero_validator_status_is_reported(self) -> None:
        with tempfile.TemporaryDirectory(prefix="training-nonzero-repro-") as raw:
            validator = Path(raw) / "nonzero-validator.py"
            validator.write_text("import sys\nprint('native nonzero marker')\nraise SystemExit(7)\n", encoding="utf-8")
            with self.assertRaises(AssertionError) as failure:
                self._run_validator(validator.name, "unused", timeout_seconds=5.0, validator_path=validator)
            self.assertIn("native nonzero marker", str(failure.exception))
            self.assertIn("7 != 0", str(failure.exception))

    @unittest.skipUnless(os.name == "nt", "Windows Job containment only")
    def test_windows_containment_setup_failure_is_explicit(self) -> None:
        with patch.object(self, "_create_windows_job", side_effect=OSError("job denied")):
            with self.assertRaisesRegex(AssertionError, "containment failure"):
                self._run_validator("missing-validator.py", "unused", timeout_seconds=1.0)

    @unittest.skipUnless(os.name == "nt", "Windows Job containment only")
    def test_windows_post_launch_handle_restore_failure_still_terminates_job(self) -> None:
        real_set_inheritable = os.set_handle_inheritable

        def fail_restore(handle: int, inheritable: bool) -> None:
            if not inheritable:
                raise OSError("restore denied")
            real_set_inheritable(handle, inheritable)

        with patch.object(os, "set_handle_inheritable", side_effect=fail_restore):
            with patch.object(
                self, "_terminate_windows_job", wraps=self._terminate_windows_job
            ) as terminate:
                with self.assertRaisesRegex(AssertionError, "containment failure"):
                    self._run_validator("missing-validator.py", "unused", timeout_seconds=1.0)
        terminate.assert_called_once()

    def test_ac1_cli_surfaces(self) -> None:
        self._run_validator(
            "C1_cli_surfaces.py",
            "AC1_OK_suite_read_only_snapshot_binding_and_ledger_preserved",
        )

    def test_ac2_complete_capture(self) -> None:
        self._run_validator(
            "C2_complete_capture.py", "AC2_OK_complete_sft_row_contract_and_redaction"
        )

    def test_ac3_preferences(self) -> None:
        self._run_validator(
            "C3_preferences.py", "AC3_OK_explicit_correction_preferences_only"
        )

    def test_ac4_label_thresholds(self) -> None:
        self._run_validator(
            "C4_label_thresholds.py", "AC4_OK_label_thresholds_and_trust_floor"
        )

    def test_ac5_split_dedup(self) -> None:
        self._run_validator(
            "C5_split_dedup.py", "AC5_OK_lineage_split_exact_dedup_and_deletion_map"
        )

    def test_ac6_refusal_atomicity(self) -> None:
        self._run_validator(
            "C6_refusal_atomicity.py",
            "AC6_OK_missing_governance_refusal_without_exportable_partial",
        )

    def test_ac7_quarantine(self) -> None:
        self._run_validator(
            "C7_quarantine.py", "AC7_OK_quarantine_opt_in_redacted_bounded"
        )

    def test_ac8_snapshot_binding(self) -> None:
        self._run_validator(
            "C8_snapshot_binding.py",
            "AC8_OK_emitted_ack_completion_binding_and_replay",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
