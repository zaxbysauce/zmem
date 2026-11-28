"""Shared isolated-store helpers for issue #546 acceptance specs."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS / "store.py"
DOCTOR_PY = SCRIPTS / "doctor.py"
_AMBIENT = {
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_INJECT",
    "ZMEM_REMOTE_URL", "ZMEM_SCHEMA_LOCK_WAIT_SECONDS",
}
_MAX_OUTPUT_BYTES = 256 * 1024


def store_env(store: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _AMBIENT}
    env.update({
        "ZMEM_STORE": str(store),
        "ZMEM_DATA": str(store.parent),
        "ZMEM_MODELS_DIR": str(store.parent / "models-absent"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_INJECT": "0",
    })
    return env


def _run_cli(store, script, args, timeout):
    command = [sys.executable, "-X", "utf8", str(script), *args]
    with subprocess.Popen(
        command, cwd=REPO_ROOT, env=store_env(store),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as child, ThreadPoolExecutor(max_workers=2) as readers:
        def read_stream(stream):
            data = bytearray()
            while chunk := stream.read1(8192):
                if len(data) + len(chunk) > _MAX_OUTPUT_BYTES:
                    child.kill()
                    raise AssertionError("CLI output exceeded the test cap")
                data.extend(chunk)
            return data.decode("utf-8", errors="replace")

        stdout = readers.submit(read_stream, child.stdout)
        stderr = readers.submit(read_stream, child.stderr)
        try:
            status = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
            raise
        return subprocess.CompletedProcess(command, status, stdout.result(), stderr.result())


def run_store(store: Path, *args: str, timeout: float = 60.0) -> subprocess.CompletedProcess[str]:
    return _run_cli(store, STORE_PY, args, timeout)


def init_store(store: Path) -> None:
    result = run_store(store, "init")
    assert result.returncode == 0, (result.stdout, result.stderr)


def connect(store: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(store))
    conn.row_factory = sqlite3.Row
    return conn


def insert_memory(conn: sqlite3.Connection, memory_id: str, content: str, content_norm: str | None) -> None:
    conn.execute(
        "INSERT INTO memory (id, namespace, type, content, tags, source_ref, "
        "content_norm, superseded_at, ingestion_ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (memory_id, "project:issue546", "fact", content, "", "issue546:test",
         content_norm, None, "2026-11-27T00:00:00Z"),
    )


def doctor_text(store: Path) -> str:
    result = _run_cli(
        store, DOCTOR_PY,
        ("--format", "json", "--project", str(REPO_ROOT), "--repo-root", str(REPO_ROOT)),
        60,
    )
    assert result.returncode in (0, 1), (result.stdout, result.stderr)
    return result.stdout


def backup_snapshot(store: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    result = run_store(store, "backup", "--out-dir", str(out_dir))
    assert result.returncode == 0, (result.stdout, result.stderr)
    snapshots = sorted(out_dir.glob("*.sqlite"))
    assert snapshots, result.stdout
    return snapshots[-1]
