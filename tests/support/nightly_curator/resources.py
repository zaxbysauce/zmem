"""Resource, isolation, and privacy primitives for the nightly-curator evaluation (#377).

Single source of truth for:
- child_env(): FIXED-ALLOWLIST environment builder (nothing inherited beyond the
  allowlist + explicit pins) — plan-critic R2-5.
- assert_contained(): realpath containment for every scratch path the harness touches.
- Budget + run_bounded(): finite wall-clock cap with process-tree kill (plan-critic
  R2-4); stdout/stderr are CAPPED on the returned strings (post-hoc — communicate()
  buffers transiently, so the bound applies to what the harness reads, not to
  transient pipe buffering). ACCURATE SCOPE: run_bounded bounds the strings it
  returns, not transient buffering.
- Memory: the ENFORCING mechanism is the harness's process-tree RSS monitor plus
  explicit termination (MemoryLimiter.mechanism_status defaults to "monitor-only").
  A Windows Job Object memory limit is attempted opportunistically and its outcome
  is REPORTED (job-honored / inert-on-host / unavailable-on-host / unknown-on-host),
  never claimed as the enforcement mechanism. No POSIX RLIMIT_AS is applied.
- redact(): every logged/report surface routes through redaction.redact_training_text.
- Consent gating for any remote writer transmission (default OFF; spy-assertable).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = _REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from redaction import redact_training_text  # noqa: E402

# Environment keys a child may ALWAYS see (Windows needs several of these for
# subprocess/python to function at all). Everything else must come from `extra`.
_ENV_ALLOWLIST = (
    "SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "PATH",
    "TEMP", "TMP", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    "OS", "USERDOMAIN", "COMPUTERNAME", "LANG", "LC_ALL", "PYTHONUTF8",
    "PYTHONIOENCODING",
)

# Behavior-freezing pins (defaults match production constants; see consolidate.py:47/49,
# write.py DEDUP_SIMILARITY_THRESHOLD, evidence.py retention, beliefs.py:877).
_ZMEM_PINS = {
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS": "7.0",
    "ZMEM_CONSOLIDATE_GROWTH_THRESHOLD": "0.20",
    "ZMEM_DEDUP_THRESHOLD": "0.85",
    "ZMEM_ORGANIZE_IDLE_HOURS": "0",
    "ZMEM_ORGANIZE_EPISODE_BOUND": "256",
}

# Keys that must NEVER cross into a harness child even if ambient.
_ZMEM_STRIP = (
    "ZMEM_BELIEF_ADAPTER_ACTIONS", "ZMEM_EMBED_PROFILE", "ZMEM_EMBED_PROFILE_DIR",
    "ZMEM_BACKUP_DIR", "ZMEM_REMOTE_URL", "ZMEM_HUB_URL", "ZMEM_ALLOW_REAL_STORE",
    "ZMEM_NLI_MAX_CALLS", "ZMEM_NLI_MAX_SECONDS", "ZMEM_NLI_COMMAND",
    "ZMEM_EVIDENCE_TTL_DAYS", "ZMEM_EVIDENCE_CAP", "ZMEM_TEST_REAL_MODELS_DIR",
    "ZMEM_DATA", "ZMEM_STORE",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "CLAUDE_CONFIG_DIR",
)


def redact(text: str) -> str:
    """Route every harness log/report surface through the production redactor."""
    out, _ = redact_training_text(text)
    return out


def child_env(scratch: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a child environment from a FIXED allowlist + explicit pins (R2-5).

    scratch becomes HOME/USERPROFILE; ZMEM_STORE/ZMEM_DATA point INSIDE scratch;
    ZMEM_MODELS_DIR points at a nonexistent dir so no model can load.
    """
    scratch = Path(scratch)
    env: dict[str, str] = {}
    for key in _ENV_ALLOWLIST:
        if key in os.environ:
            env[key] = os.environ[key]
    env["HOME"] = str(scratch)
    env["USERPROFILE"] = str(scratch)
    env["PYTHONUTF8"] = "1"
    env.update(_ZMEM_PINS)
    env["ZMEM_STORE"] = str(scratch / "store.sqlite")
    env["ZMEM_DATA"] = str(scratch)
    env["ZMEM_MODELS_DIR"] = str(scratch / "no-such-models")
    if extra:
        env.update(extra)
    return env


def module_env(scratch: Path) -> None:
    """Pin THIS process's environment the same way (called at module import, before
    any storelib import; see the plan's module-level pin)."""
    scratch = Path(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    for key in _ZMEM_STRIP:
        os.environ.pop(key, None)
    for key in _ENV_ALLOWLIST:
        pass  # inherited keys stay; behavior pins below override semantics
    os.environ.update(_ZMEM_PINS)
    os.environ["ZMEM_STORE"] = str(scratch / "store.sqlite")
    os.environ["ZMEM_DATA"] = str(scratch)
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    os.environ["ZMEM_MODELS_DIR"] = str(scratch / "no-such-models")


def assert_contained(paths: list[Path], scratch: Path, what: str = "path") -> None:
    """Every path the harness produces must realpath-contain inside scratch."""
    root = Path(scratch).resolve()
    for p in paths:
        rp = Path(p).resolve()
        if root != rp and root not in rp.parents:
            raise AssertionError(
                f"containment violation ({what}): {p} resolves outside scratch {scratch}"
            )


@dataclass
class Budget:
    """Finite caps ENFORCED by run_bounded for one bounded child run (AC7):
    wall-clock timeout, stdout/stderr byte caps, input byte cap, row cap.
    (Request counting and OS-level memory capping are NOT wired into
    run_bounded — see MemoryLimiter for the harness-monitored memory path.)"""

    timeout_s: float = 120.0
    max_output_bytes: int = 1_000_000
    max_input_bytes: int = 64_000
    max_rows: int = 200
    label: str = "child"

    def check_input(self, data: bytes) -> None:
        if len(data) > self.max_input_bytes:
            raise AssertionError(
                f"budget({self.label}): input {len(data)}B exceeds cap {self.max_input_bytes}B"
            )


def _kill_tree(proc: subprocess.Popen) -> None:
    """Best-effort process-tree termination per platform (no claim of job-kill).

    POSIX: every harness child is spawned with start_new_session=True (its own
    process group), so killpg targets ONLY the child's group. The self-group
    guard below is belt-and-braces: a child that somehow shares OUR process
    group must never cause killpg to SIGKILL the harness itself (the exact
    failure that silently cancelled the ubuntu CI lane mid-suite: killing the
    shared group killed the pytest runner, leaving a step-less 'cancelled' job).
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True, timeout=15,
            )
        else:
            import signal
            child_group = os.getpgid(proc.pid)
            if child_group != os.getpgid(os.getpid()):
                os.killpg(child_group, signal.SIGKILL)
    except Exception:
        pass
    finally:
        try:
            proc.kill()
        except Exception:
            pass


def run_bounded(
    argv: list[str],
    budget: Budget,
    env: dict[str, str],
    cwd: Path,
    stdin_data: bytes | None = None,
) -> dict[str, Any]:
    """Run one subprocess under the budget: timeout + tree-kill + output cap.

    Returns {returncode, stdout, stderr, stderr_truncated, timed_out,
    output_truncated, duration_s}. RETURNED output beyond the cap is TRUNCATED
    and flagged, never silently dropped. BOUNDING SCOPE (owner-review R-5):
    communicate() buffers each stream in harness memory before the cap is
    applied, so the cap bounds the RETURNED strings, not the child's peak
    output rate or the harness's transient buffering; a genuinely streaming
    bound would require incremental reads and is out of scope here.
    """
    import time

    if stdin_data is not None:
        budget.check_input(stdin_data)
    start = time.monotonic()
    proc = subprocess.Popen(
        [str(a) for a in argv],
        cwd=str(cwd),
        env=env,
        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=(os.name != "nt"),  # POSIX: own group so _kill_tree
        # can never SIGKILL the harness's own process group (the ubuntu CI
        # cancellation: the timeout probe killed the shared pytest group).
    )
    timed_out = False
    out = b""
    err = b""
    try:
        out, err = proc.communicate(input=stdin_data, timeout=budget.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except Exception:
            out, err = b"", b""
    duration = time.monotonic() - start
    truncated = len(out) > budget.max_output_bytes
    if truncated:
        out = out[: budget.max_output_bytes]
    # Owner-review R-5: stderr is capped identically to stdout in the RETURNED
    # dict (both streams are buffered by communicate() before this point —
    # see the BOUNDING SCOPE note in the docstring).
    err_truncated = len(err) > budget.max_output_bytes
    if err_truncated:
        err = err[: budget.max_output_bytes]
    return {
        "returncode": proc.returncode,
        "stdout": out.decode("utf-8", errors="replace"),
        "stderr": err.decode("utf-8", errors="replace"),
        "stderr_truncated": err_truncated,
        "timed_out": timed_out,
        "output_truncated": truncated,
        "duration_s": round(duration, 3),
    }


# ---------------------------------------------------------------------------
# Memory enforcement (R3-4 accurate semantics)
# ---------------------------------------------------------------------------

_IS_WINDOWS = os.name == "nt"


class MemoryLimiter:
    """Process/job memory enforcement with HONEST mechanism accounting.

    THE ENFORCING MECHANISM ON EVERY PLATFORM is this harness's tree-RSS
    monitor + explicit tree termination (monitor_and_terminate); the harness
    owns the kill decision and no automatic-termination claim is made.

    Windows: a Job Object commit-memory limit is additionally ATTEMPTED. On
    hosts where the job mechanism is honored (mechanism_status="job-honored"),
    a child committing past the limit FAILS ITS ALLOCATION (MemoryError). Some
    sandboxed hosts virtualize the job APIs into no-ops (SetInformationJobObject
    returns success but Query does not echo the limit and KILL_ON_CLOSE has no
    behavioral effect — observed on this box, trace 377 Phase 4): recorded as
    "inert-on-host".

    POSIX: no OS-level cap is wired into child spawning; enforcement is the
    monitored explicit termination alone (mechanism_status stays
    "monitor-only").
    """

    def __init__(self, max_memory_mb: int, label: str = "job"):
        self.max_memory_mb = max_memory_mb
        self.label = label
        self._job: Any = None
        # Truthful by default (owner-review): the ENFORCING mechanism on every
        # platform is this harness's tree-RSS monitor + explicit termination;
        # the OS job attempt below is best-effort and may be inert.
        self.mechanism_status = "monitor-only"
        if _IS_WINDOWS:
            self._setup_job()

    def _setup_job(self) -> None:  # pragma: no cover - Windows-specific
        import ctypes
        import struct

        k32 = ctypes.windll.kernel32
        job = k32.CreateJobObjectW(None, None)
        if not job:
            self.mechanism_status = "unavailable-on-host"
            return
        LIMIT_JOB_MEMORY = 0x00000200
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        commit_limit = self.max_memory_mb * 1024 * 1024
        # Layout (x64, verified via ctypes sizeof/offsetof in trace probes):
        # LimitFlags @56, JobMemoryLimit @120, sizeof == 144.
        buf = bytearray(144)
        struct.pack_into("<I", buf, 56, LIMIT_JOB_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        struct.pack_into("<q", buf, 120, commit_limit)
        info = ctypes.create_string_buffer(bytes(buf), 144)
        ok = k32.SetInformationJobObject(job, 9, info, 144)
        if not ok:
            self.mechanism_status = "unavailable-on-host"
            return
        q = ctypes.create_string_buffer(144)
        if not k32.QueryInformationJobObject(job, 9, q, 144, None):
            self.mechanism_status = "unknown-on-host"
        elif struct.unpack_from("<q", q.raw, 120)[0] != commit_limit:
            # the host silently ignores the limit (sandboxed job virtualization)
            self.mechanism_status = "inert-on-host"
        else:
            self.mechanism_status = "job-honored"
        self._job = job

    def attach(self, proc: subprocess.Popen) -> None:
        """Assign an already-started child to the job (Windows). NOTE: the
        assignment happens after Popen returns, so the child's earliest
        allocations precede the limit; enforcement that must cover the very
        first allocations needs the harness monitor regardless."""
        if _IS_WINDOWS and self._job is not None:
            import ctypes
            ctypes.windll.kernel32.AssignProcessToJobObject(
                self._job, int(proc._handle)
            )

    def monitor_and_terminate(
        self, proc: subprocess.Popen, poll_s: float = 0.1, hard_deadline_s: float = 60.0,
    ) -> dict[str, Any]:
        """Harness-owned enforcement: poll the child TREE's summed RSS; terminate
        the tree the moment it exceeds the cap (explicit kill — R3-4's aggregate
        mechanism; a venv trampoline's allocating child is inside the tree)."""
        import time as _time
        peak = 0
        killed = False
        start = _time.monotonic()
        while proc.poll() is None:
            rss = measure_tree_rss_mb(proc.pid)
            if rss is not None:
                peak = max(peak, rss)
                if rss > self.max_memory_mb:
                    _kill_tree(proc)
                    killed = True
                    break
            if _time.monotonic() - start > hard_deadline_s:
                _kill_tree(proc)
                killed = True
                break
            _time.sleep(poll_s)
        try:
            out, err = proc.communicate(timeout=15)
        except Exception:
            out, err = b"", b""
        return {
            "killed": killed, "peak_rss_mb": peak, "rc": proc.returncode,
            "stdout": (out or b"").decode("utf-8", errors="replace"),
            "stderr": (err or b"").decode("utf-8", errors="replace"),
        }

    def terminate(self) -> None:
        """EXPLICIT termination (R3-4: the harness owns the kill decision)."""
        if _IS_WINDOWS and self._job is not None:
            import ctypes
            ctypes.windll.kernel32.TerminateJobObject(self._job, 1)

    def close(self) -> None:
        if _IS_WINDOWS and self._job is not None:
            import ctypes
            ctypes.windll.kernel32.CloseHandle(self._job)
            self._job = None


def measure_rss_mb(pid: int | None = None) -> int | None:
    """Measure resident memory where the platform allows; honest None otherwise.

    NOTE: on Windows, a venv's python.exe is a TRAMPOLINE whose real allocating
    process is a child — for enforcement use measure_tree_rss_mb (tree sum)."""
    if _IS_WINDOWS:
        try:
            target = pid or os.getpid()
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {target}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip()
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if len(parts) >= 5 and parts[1].isdigit() and int(parts[1]) == target:
                    kb = parts[4].replace(",", "").replace(" K", "")
                    if kb.isdigit():
                        return int(kb) // 1024
            return None
        except Exception:
            return None
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return usage.ru_maxrss // 1024
    except Exception:
        return None


def measure_tree_rss_mb(pid: int) -> int | None:
    """Sum resident memory of `pid` AND its descendants (Windows: Toolhelp32
    snapshot for the PPID map + GetProcessMemoryInfo per process; POSIX: sum of
    /proc/<pid>/status VmRSS over the tree). Aggregate tree memory is what the
    memory budget enforces (a venv trampoline's child would otherwise be
    invisible to single-PID polling)."""
    if _IS_WINDOWS:
        try:
            import ctypes
            import ctypes.wintypes as wt

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD),
                    ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wt.DWORD),
                    ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wt.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260),
                ]

            class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
                _fields_ = [
                    ("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            k32 = ctypes.windll.kernel32
            psapi = ctypes.windll.psapi
            TH32CS_SNAPPROCESS = 0x2
            snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            if snap == -1 or snap == 0xFFFFFFFF:
                return None
            try:
                entry = PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                children: dict[int, list[int]] = {}
                if k32.Process32FirstW(snap, ctypes.byref(entry)):
                    while True:
                        children.setdefault(entry.th32ParentProcessID,
                                            []).append(entry.th32ProcessID)
                        entry = PROCESSENTRY32W()
                        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                        if not k32.Process32NextW(snap, ctypes.byref(entry)):
                            break
                tree = {pid}
                frontier = [pid]
                while frontier:
                    nxt = []
                    for p in frontier:
                        for c in children.get(p, []):
                            if c not in tree:
                                tree.add(c)
                                nxt.append(c)
                    frontier = nxt
                total = 0
                for p in tree:
                    h = k32.OpenProcess(0x0410, False, p)  # QUERY_LIMITED_INFORMATION
                    if not h:
                        continue
                    try:
                        pmc = PROCESS_MEMORY_COUNTERS()
                        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
                        if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc),
                                                      ctypes.sizeof(pmc)):
                            total += pmc.WorkingSetSize
                    finally:
                        k32.CloseHandle(h)
                return total // (1024 * 1024)
            finally:
                k32.CloseHandle(snap)
        except Exception:
            return None
    try:
        total = 0
        frontier = [pid]
        seen = {pid}
        while frontier:
            nxt = []
            for p in frontier:
                try:
                    status = Path(f"/proc/{p}/status").read_text()
                    for line in status.splitlines():
                        if line.startswith("VmRSS:"):
                            total += int(line.split()[1]) // 1024
                    for line in Path(f"/proc/{p}/task/{p}/children").read_text().split():
                        c = int(line)
                        if c not in seen:
                            seen.add(c)
                            nxt.append(c)
                except OSError:
                    continue
            frontier = nxt
        return total
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Remote-transmission consent (AC7)
# ---------------------------------------------------------------------------

GENERAL_WRITER_ENV = "ZMEM_CURATOR_GENERAL_WRITER"
TRANSMIT_CONSENT_ENV = "ZMEM_CURATOR_TRANSMIT_CONSENT"
TENCANT_WRITER_ENV = "ZMEM_CURATOR_TENCENT_WRITER"
LOCAL_ENDPOINT_ENV = "ZMEM_CURATOR_LOCAL_ENDPOINT"


class TransmissionGate:
    """Remote transmission only behind explicit consent; a spy can prove the
    guarded callable is never invoked without it."""

    def __init__(self, consent_env: dict[str, str] | None = None):
        env = consent_env if consent_env is not None else os.environ
        self._allowed = env.get(TRANSMIT_CONSENT_ENV, "") == "1"
        self.invocations: list[str] = []

    @property
    def allowed(self) -> bool:
        return self._allowed

    def guard(self, name: str, fn, *args, **kwargs):
        if not self._allowed:
            return {"status": "unavailable", "reason": "transmission_consent_not_granted"}
        self.invocations.append(name)
        return fn(*args, **kwargs)
