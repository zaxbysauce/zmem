"""Executable C1-C6 checks for issue #139.

The script is intentionally independent from a future ``storelib.source``
implementation.  It creates all stores and host records under a temporary
root, invokes the real CLI, and asserts the public result.  ``--repo-root``
is mandatory so no checkout path is guessed or hardcoded.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import Any
from unittest.mock import patch


class CheckFailure(AssertionError):
    pass


NAMESPACE = "project:issue139"
EVIDENCE_ID = "00000000-0000-4000-8000-000000000601"

# C6 binds to an independently acquired, offline Hermes checkout.  The source
# tree is deliberately not copied into zmem and this test never installs a
# package: CI must provision the exact checkout plus ruamel.yaml in the Python
# interpreter that runs this test.  Keeping that provision explicit prevents a
# host-installed, version-drifted Hermes package from silently becoming the
# acceptance provider.
_HERMES_PROVIDER_ROOT_ENV = "ZMEM_TEST_HERMES_PROVIDER_ROOT"
_HERMES_PROVIDER_REVISION = "00373b537616c96e0ca604b831890a113df07ac0"
_HERMES_STATE_SHA256 = "3f0995ca2bc122a16454d2ae59455806289695636b55b6ac4b64acf1cd577469"
_HERMES_RUAMEL_VERSION = "0.18.16"
_SQLITE_MAGIC = b"SQLite format 3\x00"
_HERMES_PROVIDER_MANIFEST = Path(__file__).resolve().parents[1] / "hermes-provider-manifest.json"


def _snapshot_files(root: Path) -> dict[str, str]:
    """Return a hash snapshot of files below one disposable fixture root."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_existing_shm_only_delta(
    before: dict[str, str], after: dict[str, str], *, label: str
) -> None:
    """Apply the selected logical read-only WAL contract to one source tree."""
    if set(before) != set(after):
        raise CheckFailure(
            f"{label} changed the source file set: before={sorted(before)} after={sorted(after)}"
        )
    changed = sorted(name for name in before if before[name] != after[name])
    if set(changed) - {"state.db-shm"}:
        raise CheckFailure(f"{label} changed non-coordination files: {changed}")
    if "state.db" not in before or "state.db-wal" not in before:
        raise CheckFailure(f"{label} lacks the required DB/WAL snapshot: {sorted(before)}")
    if before["state.db"] != after["state.db"] or before["state.db-wal"] != after["state.db-wal"]:
        raise CheckFailure(f"{label} changed state.db or state.db-wal")
    if "state.db-shm" not in before:
        raise CheckFailure(f"{label} allowed SHM coordination without a pre-existing sidecar")


def _hermes_provider_env() -> tuple[Path, dict[str, str]]:
    """Validate the offline provider binding and return its child environment.

    The fixture is intentionally a hard requirement for C6.  It must not
    download Hermes or ruamel at test time, and it must not accept an arbitrary
    installed provider merely because it happens to import on one developer
    machine.
    """
    root_text = os.environ.get(_HERMES_PROVIDER_ROOT_ENV, "")
    if not root_text:
        raise CheckFailure(
            f"C6 requires {_HERMES_PROVIDER_ROOT_ENV} to name the offline pinned Hermes checkout "
            f"({_HERMES_PROVIDER_REVISION}) and the current test Python to provide "
            f"ruamel.yaml=={_HERMES_RUAMEL_VERSION}; no runtime download is permitted"
        )
    root = Path(root_text).resolve()
    state_py = root / "hermes_state.py"
    if not state_py.is_file():
        raise CheckFailure(f"C6 Hermes provider root lacks hermes_state.py: {root}")
    if hashlib.sha256(state_py.read_bytes()).hexdigest() != _HERMES_STATE_SHA256:
        raise CheckFailure(
            "C6 Hermes provider source hash differs from pinned "
            f"{_HERMES_PROVIDER_REVISION}: {state_py}"
        )
    try:
        manifest = json.loads(_HERMES_PROVIDER_MANIFEST.read_text(encoding="utf-8"))
        expected_modules = manifest["runtime_module_sha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CheckFailure(f"C6 Hermes provider manifest is invalid: {_HERMES_PROVIDER_MANIFEST}") from exc
    if manifest.get("format") != 1 or manifest.get("revision") != _HERMES_PROVIDER_REVISION:
        raise CheckFailure("C6 Hermes provider manifest revision does not match the pinned source")
    if not isinstance(expected_modules, dict) or not expected_modules:
        raise CheckFailure("C6 Hermes provider manifest has no runtime dependency closure")
    required_modules = {
        "hermes_state.py",
        "hermes_state_holders.py",
        "hermes_state_messages.py",
        "hermes_state_sessions.py",
        "hermes_state_wal.py",
    }
    if not required_modules.issubset(expected_modules):
        raise CheckFailure("C6 Hermes provider manifest omits a required SessionDB dependency")
    for relative, expected_hash in expected_modules.items():
        if not isinstance(relative, str) or not isinstance(expected_hash, str):
            raise CheckFailure("C6 Hermes provider manifest has a malformed dependency entry")
        candidate = root / relative
        if not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest() != expected_hash:
            raise CheckFailure(f"C6 Hermes runtime dependency hash mismatch: {relative}")
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file() or hashlib.sha256(pyproject.read_bytes()).hexdigest() != manifest.get("pyproject_sha256"):
        raise CheckFailure("C6 Hermes provider dependency manifest differs from the pinned source")
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update({"PYTHONPATH": str(root), "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import pathlib, ruamel.yaml, hermes_state; "
                "assert ruamel.yaml.__version__ == %r, ruamel.yaml.__version__; "
                "assert pathlib.Path(hermes_state.__file__).resolve() == pathlib.Path(%r).resolve() / 'hermes_state.py'"
            ) % (_HERMES_RUAMEL_VERSION, str(root)),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        raise CheckFailure(
            "C6 pinned Hermes provider cannot import under the test interpreter; "
            "CI must prepare its offline venv before this acceptance lane.\n"
            f"stdout={probe.stdout!r}\nstderr={probe.stderr!r}"
        )
    return root, env


def _create_hermes_state_db(
    provider_root: Path, provider_env: dict[str, str], home: Path, *, session_id: str
) -> int:
    """Create an owned native state.db with the actual pinned SessionDB API."""
    home.mkdir(parents=True, exist_ok=True)
    program = r'''
import os
import sys
from pathlib import Path

provider_root, home, session_id = map(Path, sys.argv[1:4])
sys.path.insert(0, str(provider_root))
os.environ["HERMES_HOME"] = str(home)
import hermes_state

db = hermes_state.SessionDB(db_path=home / "state.db")
try:
    db.create_session(str(session_id), "hermes")
    db.append_message(str(session_id), "user", "hermes native prompt")
    anchor = db.append_message(str(session_id), "assistant", "hermes native answer")
finally:
    db.close()
print(anchor)
'''
    result = subprocess.run(
        [sys.executable, "-c", program, str(provider_root), str(home), session_id],
        env=provider_env,
        capture_output=True,
        text=True,
        timeout=45,
    )
    if result.returncode != 0:
        raise CheckFailure(
            "C6 actual pinned SessionDB fixture creation failed\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    try:
        return int(result.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError) as exc:
        raise CheckFailure(f"C6 provider returned no integer message anchor: {result.stdout!r}") from exc


def _prepare_hermes_wal_fixture(
    scratch: Path,
) -> tuple[Path, Path, int, sqlite3.Connection, dict[str, str], dict[str, str]]:
    """Build a live WAL fixture with pre-existing sidecars and a hostile JSONL bait.

    The returned SQLite connection deliberately remains open.  It preserves a
    real uncheckpointed WAL generation while the future resolver attaches.
    """
    provider_root, provider_env = _hermes_provider_env()
    home = scratch / "hermes-home"
    session_id = "hermes-session-139"
    anchor = _create_hermes_state_db(provider_root, provider_env, home, session_id=session_id)
    db_path = home / "state.db"
    writer = sqlite3.connect(db_path)
    try:
        mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        writer.execute("PRAGMA user_version=139")
        writer.commit()
        if str(mode).lower() != "wal":
            raise CheckFailure(f"C6 fixture did not enter WAL mode: {mode!r}")
        header = db_path.read_bytes()[:32]
        if header[:16] != _SQLITE_MAGIC or header[18] != 2:
            raise CheckFailure("C6 WAL fixture lacks the SQLite WAL header marker")
        expected_sidecars = (home / "state.db-wal", home / "state.db-shm")
        if not all(path.is_file() for path in expected_sidecars):
            raise CheckFailure(f"C6 fixture lacks required WAL sidecars: {expected_sidecars!r}")
        bait_dir = scratch / "hermes-jsonl-bait"
        bait_dir.mkdir()
        (bait_dir / "export.jsonl").write_text(
            '{"session_id":"hermes-session-139","text":"BAIT JSONL FALLBACK MUST NOT RENDER"}\n',
            encoding="utf-8",
        )
        resolver_env = dict(provider_env)
        resolver_env.update(
            {
                "HERMES_HOME": str(home),
                "ZMEM_HERMES_SESSIONS": str(bait_dir),
            }
        )
        return home, db_path, anchor, writer, resolver_env, _snapshot_files(home)
    except Exception:
        writer.close()
        raise


def _assert_hermes_resolver_uses_canonical_api(repo_root: Path) -> None:
    """Bind C6 to an assigned read-only canonical constructor in ``_hermes``.

    This AST seam check deliberately does not claim result-flow proof.  The
    native C6 fixture and the focused Hermes spy contract test the provider
    calls, returned data, anchors, close, and fail-closed behavior.
    """
    source_module = repo_root / "skills" / "memory" / "scripts" / "storelib" / "source.py"
    if not source_module.is_file():
        return
    try:
        tree = ast.parse(source_module.read_text(encoding="utf-8"), filename=str(source_module))
    except SyntaxError as exc:
        raise CheckFailure(f"C6 source resolver has invalid syntax: {exc}") from exc

    hermes_functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_hermes"
    ]
    if len(hermes_functions) != 1:
        raise CheckFailure("C6 Hermes resolver must define exactly one module-level _hermes function")
    hermes = hermes_functions[0]

    def scoped_nodes() -> list[ast.AST]:
        nodes: list[ast.AST] = []
        pending = list(hermes.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            nodes.append(node)
            pending.extend(ast.iter_child_nodes(node))
        return nodes

    constructors = []
    for node in scoped_nodes():
        if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        is_session_db = (isinstance(func, ast.Name) and func.id == "SessionDB") or (
            isinstance(func, ast.Attribute) and func.attr == "SessionDB"
        )
        if not is_session_db:
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        constructors.append(node.value)
    if len(constructors) != 1 or not any(
        keyword.arg == "read_only"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in constructors[0].keywords
    ):
        raise CheckFailure(
            "C6 Hermes _hermes must assign exactly one canonical SessionDB(..., read_only=True)"
        )


def _env(scratch: Path, **extra: str) -> dict[str, str]:
    strip = {
        "ZMEM_STORE",
        "ZMEM_DATA",
        "ZMEM_MODELS_DIR",
        "ZMEM_MODEL_AUTODOWNLOAD",
        "ZMEM_HOME",
        "ZMEM_NAMESPACE",
        "ZMEM_HOST",
        "ZMEM_SESSION",
        "ZMEM_TRANSCRIPT",
        "ZMEM_AGENT_TRANSCRIPT",
        "ZMEM_CODEX_MEMORY",
        "ZMEM_HERMES_SESSIONS",
        "ZMEM_ZCODE_DB",
        "HERMES_HOME",
        "PYTHONPATH",
    }
    env = {key: value for key, value in os.environ.items() if key not in strip}
    env.update(
        {
            "ZMEM_STORE": str(scratch / "store.sqlite"),
            "ZMEM_DATA": str(scratch / "data"),
            "ZMEM_MODELS_DIR": str(scratch / "missing-models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "ZMEM_EMBED_PROFILE": "fake",
            "ZMEM_NAMESPACE": NAMESPACE,
            "ZMEM_HOST": "claude",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    env.update(extra)
    return env


def _resolve_fixture_namespace(repo_root: Path, env: dict[str, str]) -> str:
    """Seed the owned namespace cache through the sole runtime producer.

    This runs only while building C1's disposable fixture.  The future
    ``source`` child must resolve the same namespace with ``ZMEM_NAMESPACE``
    absent and must not rewrite the cache seeded here.  Loading ``host.py`` by
    path avoids inheriting an unrelated test's host module; restore both the
    process environment and import path before returning.
    """
    scripts = repo_root / "skills" / "memory" / "scripts"
    host_path = scripts / "host.py"
    if not host_path.is_file():
        raise CheckFailure(f"C1 cannot load the canonical namespace producer: {host_path}")
    previous_env = dict(os.environ)
    previous_sys_path = sys.path[:]
    prior_storelib = {
        name: module
        for name, module in sys.modules.items()
        if name == "storelib" or name.startswith("storelib.")
    }
    try:
        sys.path.insert(0, str(scripts))
        with patch.dict(os.environ, env, clear=True):
            spec = importlib.util.spec_from_file_location("issue139_fixture_host", host_path)
            if spec is None or spec.loader is None:
                raise CheckFailure(f"C1 could not construct host module spec: {host_path}")
            host_module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(host_module)
            namespace = host_module.resolve_namespace(repo_root)
        if not isinstance(namespace, str) or not namespace.startswith("project:"):
            raise CheckFailure(f"C1 canonical namespace producer returned {namespace!r}")
        return namespace
    finally:
        sys.path[:] = previous_sys_path
        for name in list(sys.modules):
            if (name == "storelib" or name.startswith("storelib.")) and name not in prior_storelib:
                del sys.modules[name]
        sys.modules.update(prior_storelib)
        if dict(os.environ) != previous_env:
            os.environ.clear()
            os.environ.update(previous_env)
            raise CheckFailure("C1 fixture namespace setup did not restore the process environment")


def _store_path(repo_root: Path) -> Path:
    return repo_root / "skills" / "memory" / "scripts" / "store.py"


def _run(
    repo_root: Path,
    env: dict[str, str],
    *args: str,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_store_path(repo_root)), *args],
        cwd=repo_root,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=45,
    )


def _require_ok(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise CheckFailure(
            f"{label} exit={result.returncode}\n"
            f"--- stdout ---\n{result.stdout}"
            f"--- stderr ---\n{result.stderr}"
        )


def _json_stdout(result: subprocess.CompletedProcess[str], label: str) -> Any:
    _require_ok(result, label)
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise CheckFailure(f"{label} did not emit JSON: {result.stdout!r}") from exc


def _init_store(repo_root: Path, env: dict[str, str]) -> None:
    _require_ok(_run(repo_root, env, "init"), "store init")


def _add_memory(
    repo_root: Path,
    env: dict[str, str],
    *,
    source_ref: str,
    content: str,
    namespace: str = NAMESPACE,
) -> str:
    result = _run(
        repo_root,
        env,
        "add",
        "--namespace",
        namespace,
        "--type",
        "lesson",
        "--content",
        content,
        "--source-ref",
        source_ref,
        "--signal",
        "test",
        "--confidence",
        "0.9",
        "--json",
    )
    _require_ok(result, "store add")
    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("id"):
            return str(payload["id"])
    raise CheckFailure(f"store add emitted no memory id: {result.stdout!r}")


def _write_evidence(repo_root: Path, env: dict[str, str], payload: dict[str, Any]) -> None:
    result = _run(
        repo_root,
        env,
        "evidence",
        "write",
        input_text=json.dumps(payload, ensure_ascii=False),
    )
    _require_ok(result, "evidence write")
    if result.stdout.strip() != str(payload["id"]):
        raise CheckFailure(f"evidence write returned unexpected id: {result.stdout!r}")


def _write_zcode_db(path: Path) -> None:
    """Create the smallest disposable copy of the verified ZCode layout.

    The live layout is a structured SQLite source with ``session``,
    ``message``, and ``part`` rows.  This fixture intentionally keeps the
    payloads tiny and uses only the columns the intake probe verified: stable
    IDs, session links, millisecond timestamps, sequence values, and JSON
    payloads.  It is a native-source fixture; it is not a JSONL substitute.
    """
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                parent_id TEXT,
                slug TEXT,
                directory TEXT,
                path TEXT,
                title TEXT,
                version TEXT,
                time_created INTEGER,
                time_updated INTEGER
            );
            CREATE TABLE message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                time_created INTEGER,
                time_updated INTEGER,
                sequence INTEGER,
                data TEXT
            );
            CREATE TABLE part (
                id TEXT PRIMARY KEY,
                message_id TEXT,
                session_id TEXT NOT NULL,
                time_created INTEGER,
                time_updated INTEGER,
                sequence INTEGER,
                data TEXT
            );
            """
        )
        session_id = "zcode-session-139"
        conn.execute(
            "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                "fixture-project",
                None,
                "fixture-session",
                "C:/fixture/project",
                "C:/fixture/project/session",
                "Issue 139 native fixture",
                "fixture",
                1788998401000,
                1788998403000,
            ),
        )
        messages = (
            (
                "zcode-message-001",
                session_id,
                1788998401000,
                1788998401000,
                1,
                json.dumps({"role": "user", "time": "2026-09-10T00:00:01Z"}),
            ),
            (
                "zcode-message-002",
                session_id,
                1788998402000,
                1788998402000,
                2,
                json.dumps({"role": "assistant", "time": "2026-09-10T00:00:02Z"}),
            ),
        )
        conn.executemany("INSERT INTO message VALUES (?,?,?,?,?,?)", messages)
        conn.execute(
            "INSERT INTO part VALUES (?,?,?,?,?,?,?)",
            (
                "zcode-part-002",
                "zcode-message-002",
                session_id,
                1788998402000,
                1788998402000,
                1,
                json.dumps({"type": "text", "text": "zcode native turn"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _associate(env: dict[str, str], memory_id: str, evidence_id: str) -> None:
    conn = sqlite3.connect(env["ZMEM_STORE"])
    try:
        conn.execute(
            "INSERT INTO memory_evidence(memory_id, evidence_id) VALUES (?, ?)",
            (memory_id, evidence_id),
        )
        conn.commit()
    finally:
        conn.close()


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.exists():
        return result
    if root.is_file():
        result[str(root.relative_to(root.parent))] = hashlib.sha256(root.read_bytes()).hexdigest()
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _assert_canonical_store_coordination_only(
    before: dict[str, str], after: dict[str, str], *, scratch: Path, store_path: Path
) -> None:
    """Apply the intentionally narrow normal-read exception for zmem's own DB.

    A closed WAL-mode canonical store can have no sidecars. SQLite's normal
    ``mode=ro`` attach then creates a zero-byte WAL and a shared-memory reader
    index.  This exception is exclusive to the exact canonical store path; it
    does not apply to transcripts, caches, queues, or external native hosts.
    """
    scratch = scratch.resolve()
    store_path = store_path.resolve()
    try:
        main = str(store_path.relative_to(scratch))
    except ValueError as exc:
        raise CheckFailure(f"C1 canonical store is outside its fixture root: {store_path}") from exc
    sidecars = {f"{main}-wal", f"{main}-shm"}
    changed = {
        name
        for name in set(before) & set(after)
        if before[name] != after[name]
    }
    created = set(after) - set(before)
    deleted = set(before) - set(after)
    if (changed | created | deleted) - sidecars:
        raise CheckFailure(
            "C1 changed a non-canonical source file: "
            f"changed={sorted(changed)} created={sorted(created)} deleted={sorted(deleted)}"
        )
    if main not in before or before.get(main) != after.get(main):
        raise CheckFailure("C1 changed the canonical store database bytes")
    if deleted:
        raise CheckFailure(f"C1 deleted a canonical SQLite file: {sorted(deleted)}")
    for relative in sidecars:
        path = scratch / relative
        if relative in before and (path.is_symlink() or not path.is_file()):
            raise CheckFailure(f"C1 pre-existing canonical sidecar is unsafe: {path}")
        if relative in after and (path.is_symlink() or not path.is_file()):
            raise CheckFailure(f"C1 created a non-regular canonical sidecar: {path}")
    wal = f"{main}-wal"
    if wal in before and before[wal] != after.get(wal):
        raise CheckFailure("C1 changed a pre-existing canonical WAL")
    if wal in created:
        wal_path = scratch / wal
        if wal_path.stat().st_size != 0 or after[wal] != hashlib.sha256(b"").hexdigest():
            raise CheckFailure("C1 created a non-empty canonical WAL")


def _assert_canonical_sidecar_refusal(
    repo_root: Path, *, scratch: Path, transcript: Path
) -> None:
    """A directory named like a canonical SHM sidecar must fail before SQLite opens."""
    unsafe = scratch / "unsafe-canonical-store"
    unsafe.mkdir()
    env = _env(
        unsafe,
        ZMEM_TRANSCRIPT=str(transcript),
        ZMEM_MODEL_AUTODOWNLOAD="0",
    )
    _init_store(repo_root, env)
    memory_id = _add_memory(
        repo_root,
        env,
        source_ref=transcript.name,
        content="C1 non-regular canonical sidecar refusal fixture",
    )
    sidecar = Path(env["ZMEM_STORE"] + "-shm")
    sidecar.mkdir()
    before = _snapshot(unsafe)
    _assert_refused(
        _run(repo_root, env, "source", "--id", memory_id, "--context", "0"),
        "C1 non-regular canonical sidecar",
    )
    if before != _snapshot(unsafe) or not sidecar.is_dir() or any(sidecar.iterdir()):
        raise CheckFailure("C1 non-regular canonical sidecar refusal changed the owned fixture")


def _assert_refused(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 1:
        raise CheckFailure(
            f"{label} should exit 1, got {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}"
            f"--- stderr ---\n{result.stderr}"
        )
    if result.stdout.strip() and "excerpt" in result.stdout.lower():
        raise CheckFailure(f"{label} emitted an excerpt on refusal: {result.stdout!r}")
    if "source: refused" not in result.stderr:
        raise CheckFailure(f"{label} omitted the refusal marker: {result.stderr!r}")


def _claude_records(session_id: str = "claude-001") -> bytes:
    records = [
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-09-10T00:00:01Z",
            "message": {"role": "user", "content": [{"type": "text", "text": "first turn"}]},
        },
        {
            "type": "assistant",
            "sessionId": session_id,
            "timestamp": "2026-09-10T00:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "middle turn token sk-test-00000000000000000000",
                    }
                ],
            },
        },
        {
            "type": "user",
            "sessionId": session_id,
            "timestamp": "2026-09-10T00:00:03Z",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-001",
                        "content": "tool result",
                    }
                ],
            },
        },
    ]
    return ("".join(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n" for item in records)).encode("utf-8")


def _source_show(repo_root: Path, env: dict[str, str], memory_id: str, context: int = 2):
    return _json_stdout(
        _run(repo_root, env, "source", "--id", memory_id, "--context", str(context)),
        "source show",
    )


def check_c1(repo_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="zmem-139-c1-") as raw:
        scratch = Path(raw)
        fixture_dir = scratch / "fixtures"
        fixture_dir.mkdir()
        transcript = fixture_dir / "claude-session.jsonl"
        transcript.write_bytes(_claude_records())
        data = scratch / "data"
        queue = data / "queue"
        cache = scratch / "cache"
        queue.mkdir(parents=True)
        cache.mkdir()
        (queue / "sentinel.jsonl").write_text("queue sentinel\n", encoding="utf-8")
        (cache / "sentinel.bin").write_bytes(b"cache sentinel")
        network_count = scratch / "network-count"
        site = scratch / "sitecustomize.py"
        site.write_text(
            "import os\n"
            "import socket\n"
            "from pathlib import Path\n"
            "_connect = socket.socket.connect\n"
            "def _blocked(self, address):\n"
            "    p = Path(os.environ['ZMEM_NETWORK_COUNT'])\n"
            "    p.write_text(p.read_text() + '1' if p.exists() else '1', encoding='ascii')\n"
            "    raise OSError('network disabled by acceptance fixture')\n"
            "socket.socket.connect = _blocked\n",
            encoding="utf-8",
        )
        env = _env(
            scratch,
            ZMEM_TRANSCRIPT=str(transcript),
            ZMEM_NETWORK_COUNT=str(network_count),
            PYTHONPATH=str(scratch),
        )
        _init_store(repo_root, env)
        resolved_namespace = _resolve_fixture_namespace(repo_root, env)
        namespace_cache = scratch / "namespace-cache"
        if not namespace_cache.is_dir() or len(list(namespace_cache.glob("*.json"))) != 1:
            raise CheckFailure("C1 fixture setup did not create exactly one owned namespace cache entry")
        memory_id = _add_memory(
            repo_root,
            env,
            source_ref=transcript.name,
            content="C1 read-only provenance fixture",
            namespace=resolved_namespace,
        )
        source_env = dict(env)
        source_env.pop("ZMEM_NAMESPACE", None)
        before = _snapshot(scratch)
        namespace_cache_before = _snapshot(namespace_cache)
        shown = _source_show(repo_root, source_env, memory_id, context=0)
        after = _snapshot(scratch)
        if namespace_cache_before != _snapshot(namespace_cache):
            raise CheckFailure("C1 source rewrote the namespace cache while resolving its implicit namespace")
        _assert_canonical_store_coordination_only(
            before, after, scratch=scratch, store_path=Path(env["ZMEM_STORE"])
        )
        if network_count.exists() and network_count.read_text(encoding="ascii"):
            raise CheckFailure(f"C1 attempted network calls: {network_count.read_text()!r}")
        if shown.get("excerpt") in (None, ""):
            raise CheckFailure(f"C1 source returned no excerpt: {shown!r}")
        if shown.get("namespace") != resolved_namespace:
            raise CheckFailure(
                "C1 source implicit namespace disagrees with the canonical producer: "
                f"expected={resolved_namespace!r} shown={shown.get('namespace')!r}"
            )
        _assert_canonical_sidecar_refusal(repo_root, scratch=scratch, transcript=transcript)


def check_c2(repo_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="zmem-139-c2-") as raw:
        scratch = Path(raw)
        fixture_dir = scratch / "fixtures"
        fixture_dir.mkdir()
        transcript = fixture_dir / "claude-session.jsonl"
        transcript_bytes = _claude_records()
        transcript.write_bytes(transcript_bytes)
        fallback = fixture_dir / "fallback-session.jsonl"
        fallback.write_text('{"type":"user","text":"fallback only"}\n', encoding="utf-8")
        env = _env(scratch, ZMEM_TRANSCRIPT=str(transcript))
        _init_store(repo_root, env)
        memory_id = _add_memory(
            repo_root,
            env,
            source_ref=fallback.name,
            content="C2 evidence precedence fixture",
        )
        middle_offset = transcript_bytes.find(b'"timestamp":"2026-09-10T00:00:02Z"')
        if middle_offset < 0:
            raise CheckFailure("C2 fixture lost the middle-turn anchor")
        _write_evidence(
            repo_root,
            env,
            {
                "id": EVIDENCE_ID,
                "session_id": "claude-001",
                "lane": "claude",
                "moment": "user_prompt",
                "kind": "turn",
                "ts": "2026-09-10T00:00:02Z",
                "excerpt": "middle turn",
                "ref_path": transcript.name,
                "ref_offset": middle_offset,
            },
        )
        _associate(env, memory_id, EVIDENCE_ID)
        shown = _source_show(repo_root, env, memory_id, context=2)
        expected_keys = {
            "memory_id", "namespace", "source_ref", "evidence_ids", "source_kind",
            "source_path", "session_id", "capture_time", "turn_start", "turn_end",
            "byte_start", "byte_end", "context_requested", "context_returned",
            "truncated", "truncated_reason", "excerpt_sha256", "excerpt",
        }
        if set(shown) != expected_keys:
            raise CheckFailure(f"C2 show keys drifted: {sorted(shown)}")
        if shown["memory_id"] != memory_id or shown["namespace"] != NAMESPACE:
            raise CheckFailure(f"C2 identity mismatch: {shown!r}")
        if shown["source_ref"] != fallback.name or shown["source_path"] != transcript.name:
            raise CheckFailure(f"C2 evidence did not win over source_ref: {shown!r}")
        if shown["evidence_ids"] != [EVIDENCE_ID] or shown["source_kind"] != "evidence":
            raise CheckFailure(f"C2 evidence metadata mismatch: {shown!r}")
        if shown["session_id"] != "claude-001" or shown["capture_time"] != "2026-09-10T00:00:02Z":
            raise CheckFailure(f"C2 current-schema evidence metadata missing: {shown!r}")
        if shown["context_requested"] != 2 or shown["context_returned"] != 3:
            raise CheckFailure(f"C2 turn window mismatch: {shown!r}")
        if shown["truncated"] is not True or shown["truncated_reason"] != "context_bound":
            raise CheckFailure(f"C2 context truncation mismatch: {shown!r}")
        if shown["byte_start"] != 0 or shown["byte_end"] != len(transcript_bytes):
            raise CheckFailure(f"C2 original UTF-8 offsets mismatch: {shown!r}")
        excerpt = shown["excerpt"]
        if not isinstance(excerpt, str) or "sk-test-00000000000000000000" in excerpt:
            raise CheckFailure(f"C2 secret was not redacted: {excerpt!r}")
        if "[REDACTED" not in excerpt:
            raise CheckFailure(f"C2 redaction marker missing: {excerpt!r}")
        digest = shown["excerpt_sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise CheckFailure(f"C2 excerpt digest is not SHA-256: {digest!r}")
        if digest != hashlib.sha256(excerpt.encode("utf-8")).hexdigest():
            raise CheckFailure("C2 excerpt digest does not cover displayed UTF-8 text")


def check_c3(repo_root: Path) -> None:
    refusal_cases = (
        ("missing", "missing-session.jsonl", "missing transcript"),
        ("malformed", "malformed.jsonl", "malformed record"),
        ("remote", "https://example.invalid/session.jsonl", "remote source"),
        ("unc", r"\\server\share\session.jsonl", "UNC source"),
    )
    with tempfile.TemporaryDirectory(prefix="zmem-139-c3-") as raw:
        scratch = Path(raw)
        fixture_dir = scratch / "fixtures"
        fixture_dir.mkdir()
        env = _env(scratch)
        _init_store(repo_root, env)
        for case, source_ref, label in refusal_cases:
            memory_id = _add_memory(
                repo_root,
                env,
                source_ref=source_ref,
                content=f"C3 {case} refusal fixture",
            )
            if case == "malformed":
                (fixture_dir / source_ref).write_text("not-json\n", encoding="utf-8")
                env["ZMEM_TRANSCRIPT"] = str(fixture_dir / source_ref)
            else:
                env.pop("ZMEM_TRANSCRIPT", None)
            _assert_refused(
                _run(repo_root, env, "source", "--id", memory_id, "--context", "0"),
                label,
            )
        outside = scratch.parent / f"zmem-139-c3-outside-{os.getpid()}.jsonl"
        outside.write_text('{"type":"user","text":"outside"}\n', encoding="utf-8")
        try:
            memory_id = _add_memory(
                repo_root,
                env,
                source_ref=str(outside),
                content="C3 out-of-root refusal fixture",
            )
            env["ZMEM_TRANSCRIPT"] = str(outside)
            _assert_refused(
                _run(repo_root, env, "source", "--id", memory_id, "--context", "0"),
                "out-of-root source",
            )
        finally:
            outside.unlink(missing_ok=True)

        raw_memories = fixture_dir / "raw_memories.md"
        raw_memories.write_text("# raw memory\n- never resolve this\n", encoding="utf-8")
        memory_id = _add_memory(
            repo_root,
            env,
            source_ref=raw_memories.name,
            content="C3 raw memories refusal fixture",
        )
        env["ZMEM_CODEX_MEMORY"] = str(raw_memories)
        env.pop("ZMEM_TRANSCRIPT", None)
        _assert_refused(
            _run(repo_root, env, "source", "--id", memory_id, "--context", "0"),
            "raw_memories.md",
        )

        # Two records in one supported Claude transcript share every anchor
        # available in the current evidence schema.  The resolver must refuse
        # instead of choosing by incidental line order or falling back to the
        # memory source_ref.
        ambiguous = fixture_dir / "ambiguous-session.jsonl"
        duplicate = {
            "type": "assistant",
            "sessionId": "claude-ambiguous",
            "timestamp": "2026-09-10T00:00:04Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "same turn"}]},
        }
        ambiguous.write_bytes(
            (json.dumps(duplicate, separators=(",", ":")) + "\n"
             + json.dumps(duplicate, separators=(",", ":")) + "\n").encode("utf-8")
        )
        env["ZMEM_TRANSCRIPT"] = str(ambiguous)
        memory_id = _add_memory(
            repo_root,
            env,
            source_ref=ambiguous.name,
            content="C3 ambiguous supported session fixture",
        )
        _write_evidence(
            repo_root,
            env,
            {
                "id": "00000000-0000-4000-8000-000000000603",
                "session_id": "claude-ambiguous",
                "lane": "claude",
                "moment": "user_prompt",
                "kind": "turn",
                "ts": "2026-09-10T00:00:04Z",
                "excerpt": "same turn",
                "ref_path": ambiguous.name,
                "ref_offset": None,
            },
        )
        _associate(env, memory_id, "00000000-0000-4000-8000-000000000603")
        _assert_refused(
            _run(repo_root, env, "source", "--id", memory_id, "--context", "0"),
            "ambiguous supported session",
        )

        invalid_utf8 = fixture_dir / "invalid-utf8.jsonl"
        invalid_utf8.write_bytes(b"\xff\xfe\n")
        memory_id = _add_memory(
            repo_root,
            env,
            source_ref=invalid_utf8.name,
            content="C3 redaction/decode refusal fixture",
        )
        env["ZMEM_TRANSCRIPT"] = str(invalid_utf8)
        _assert_refused(
            _run(repo_root, env, "source", "--id", memory_id, "--context", "0"),
            "redaction/decode failure",
        )

        malformed_context = _run(repo_root, env, "source", "--id", memory_id, "--context", "21")
        if malformed_context.returncode != 2 or malformed_context.stderr != "store.py: error: --context must be between 0 and 20\n":
            raise CheckFailure(
                f"C3 invalid context contract drifted: exit={malformed_context.returncode} "
                f"stderr={malformed_context.stderr!r}"
            )
        empty_needle = _run(
            repo_root,
            env,
            "source",
            "scan",
            "--id",
            memory_id,
            "--needle",
            "",
        )
        if empty_needle.returncode != 2 or empty_needle.stderr != "store.py: error: --needle must not be empty\n":
            raise CheckFailure(
                f"C3 empty needle contract drifted: exit={empty_needle.returncode} "
                f"stderr={empty_needle.stderr!r}"
            )


def check_c4(repo_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="zmem-139-c4-") as raw:
        scratch = Path(raw)
        fixture_dir = scratch / "fixtures"
        fixture_dir.mkdir()
        codex = fixture_dir / "MEMORY.md"
        codex.write_text(
            "# Reusable Knowledge\n"
            + "".join(f"- codex literal .* marker {index}\n" for index in range(55)),
            encoding="utf-8",
        )
        (fixture_dir / "unrelated.jsonl").write_text(".* outside resolved session\n", encoding="utf-8")
        env = _env(scratch, ZMEM_CODEX_MEMORY=str(codex))
        _init_store(repo_root, env)
        memory_id = _add_memory(
            repo_root,
            env,
            source_ref=codex.name,
            content="C4 literal scan fixture",
        )
        result = _json_stdout(
            _run(repo_root, env, "source", "scan", "--id", memory_id, "--needle", ".*"),
            "source scan",
        )
        expected_keys = {
            "memory_id", "session_id", "source_path", "needle", "matches",
            "match_count", "truncated", "truncated_reason",
        }
        if set(result) != expected_keys:
            raise CheckFailure(f"C4 scan keys drifted: {sorted(result)}")
        if result["memory_id"] != memory_id or result["source_path"] != codex.name:
            raise CheckFailure(f"C4 scan identity mismatch: {result!r}")
        if result["needle"] != ".*" or result["match_count"] != 55:
            raise CheckFailure(f"C4 literal match count mismatch: {result!r}")
        if len(result["matches"]) != 50 or result["truncated"] is not True:
            raise CheckFailure(f"C4 match cap mismatch: {result!r}")
        if result["truncated_reason"] != "match_limit":
            raise CheckFailure(f"C4 truncation reason mismatch: {result!r}")
        for match in result["matches"]:
            if match["byte_end"] - match["byte_start"] != 2:
                raise CheckFailure(f"C4 treated literal .* as regex: {match!r}")


def check_c5(repo_root: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="zmem-139-c5-") as raw:
        scratch = Path(raw)
        env = _env(scratch)
        _init_store(repo_root, env)
        memory_id = _add_memory(
            repo_root,
            env,
            source_ref="session:c5",
            content="provenance hint acceptance fixture",
        )
        explicit = _run(
            repo_root,
            env,
            "recall",
            "--query",
            "provenance",
            "--namespace",
            NAMESPACE,
            "--no-hybrid",
            "--no-mmr",
            "--link-hops",
            "0",
            "--link-budget",
            "0",
        )
        _require_ok(explicit, "explicit recall")
        hint = f"-> source {memory_id} (store.py source --id {memory_id})"
        if explicit.stdout.count(hint) != 1:
            raise CheckFailure(
                f"C5 explicit recall must contain one source hint, got {explicit.stdout!r}"
            )
        passive = _run(
            repo_root,
            env,
            "recall",
            "--query",
            "provenance",
            "--namespace",
            NAMESPACE,
            "--for-injection",
            "--session-id",
            "c5-passive",
            "--moment",
            "user_prompt",
            "--lane",
            "claude",
            "--no-hybrid",
            "--no-mmr",
            "--link-hops",
            "0",
            "--link-budget",
            "0",
        )
        _require_ok(passive, "passive recall")
        if "-> source " in passive.stdout:
            raise CheckFailure(f"C5 passive recall leaked the explicit source hint: {passive.stdout!r}")

        # The renderer is the shared hook fence seam.  Compare the old call
        # shape to the explicit false opt-in, proving source_hint=False is a
        # byte-preserving default for passive callers.
        scripts = repo_root / "skills" / "memory" / "scripts"
        sys.path.insert(0, str(scripts))
        with patch.dict(os.environ, env, clear=True):
            import storelib  # imported only after the temp env is installed

            row = {
                "id": memory_id,
                "namespace": NAMESPACE,
                "type": "lesson",
                "content": "provenance hint acceptance fixture",
                "tags": "",
                "confidence": 0.9,
                "signal": "test",
                "source_ref": "session:c5",
                "stale": False,
                "_stale_note": "",
            }
            before = storelib._format_fenced_recall(
                [row], header="c5", legacy_injection_wire=True
            )
            after = storelib._format_fenced_recall(
                [row], header="c5", legacy_injection_wire=True, source_hint=False
            )
        if before != after:
            raise CheckFailure("C5 passive renderer bytes changed with source_hint=False")


def check_c6(repo_root: Path) -> None:
    """Exercise file hosts plus the approved native DB null-offset contract.

    Claude and curated Codex retain original file offsets.  The verified
    ZCode SQLite layout has stable source/session/message identifiers but no
    original UTF-8 transcript stream, so its successful result must carry
    explicit null byte offsets and the exact native anchors.  Hermes has the
    same native-anchor policy, but its arm additionally binds a real pinned
    SessionDB, rejects its JSONL export bait while that canonical DB exists,
    permits only an existing-SHM coordination delta, and proves refusal before
    a missing-sidecar or import failure can open a database.  A separate
    evidence-pinned JSONL export is accepted only with no canonical DB.
    """
    with tempfile.TemporaryDirectory(prefix="zmem-139-c6-") as raw:
        scratch = Path(raw)
        fixture_dir = scratch / "fixtures"
        fixture_dir.mkdir()
        claude = fixture_dir / "claude-session.jsonl"
        claude.write_bytes(_claude_records("claude-139"))
        codex = fixture_dir / "MEMORY.md"
        codex.write_text(
            "# Reusable Knowledge\n- codex supported host fixture\n",
            encoding="utf-8",
        )
        env = _env(scratch)
        _init_store(repo_root, env)
        cases = (
            ("claude-session.jsonl", "claude-139", "claude", claude),
            ("MEMORY.md", None, "codex", codex),
        )
        for source_ref, expected_session, host, host_input in cases:
            for key in (
                "ZMEM_TRANSCRIPT",
                "ZMEM_AGENT_TRANSCRIPT",
                "ZMEM_CODEX_MEMORY",
                "ZMEM_HERMES_SESSIONS",
            ):
                env.pop(key, None)
            env["ZMEM_TRANSCRIPT" if host == "claude" else "ZMEM_CODEX_MEMORY"] = str(host_input)
            memory_id = _add_memory(
                repo_root,
                env,
                source_ref=source_ref,
                content=f"C6 {host} supported host fixture",
            )
            shown = _source_show(repo_root, env, memory_id, context=0)
            if shown["source_path"] != source_ref:
                raise CheckFailure(f"C6 {host} path mismatch: {shown!r}")
            if shown["source_kind"] not in {
                "evidence", "claude_transcript", "codex_session",
                "zcode_session", "hermes_session",
            }:
                raise CheckFailure(f"C6 {host} source kind mismatch: {shown!r}")
            if expected_session is not None and shown["session_id"] != expected_session:
                raise CheckFailure(f"C6 {host} session mismatch: {shown!r}")

        raw_memories = fixture_dir / "raw_memories.md"
        raw_memories.write_text("# raw\n- no bulk memory\n", encoding="utf-8")
        memory_id = _add_memory(
            repo_root,
            env,
            source_ref=raw_memories.name,
            content="C6 raw memories refusal fixture",
        )
        env["ZMEM_CODEX_MEMORY"] = str(raw_memories)
        _assert_refused(
            _run(repo_root, env, "source", "--id", memory_id, "--context", "0"),
            "C6 raw_memories.md",
        )

        zcode_db = scratch / "zcode" / "db.sqlite"
        zcode_db.parent.mkdir()
        _write_zcode_db(zcode_db)
        for key in (
            "ZMEM_TRANSCRIPT",
            "ZMEM_AGENT_TRANSCRIPT",
            "ZMEM_CODEX_MEMORY",
            "ZMEM_HERMES_SESSIONS",
        ):
            env.pop(key, None)
        env["ZMEM_ZCODE_DB"] = str(zcode_db)
        native_source = "zcode:session/zcode-session-139"
        memory_id = _add_memory(
            repo_root,
            env,
            source_ref=native_source,
            content="C6 ZCode native SQLite fixture",
        )
        _write_evidence(
            repo_root,
            env,
            {
                "id": "00000000-0000-0000-0000-000000000604",
                "session_id": "zcode-session-139",
                "lane": "zcode",
                "moment": "user_prompt",
                "kind": "turn",
                "ts": "2026-09-10T00:00:02Z",
                "excerpt": "zcode native turn",
                "ref_path": native_source,
                "ref_offset": None,
            },
        )
        _associate(env, memory_id, "00000000-0000-0000-0000-000000000604")
        shown = _source_show(repo_root, env, memory_id, context=0)
        if shown["source_kind"] != "zcode_session":
            raise CheckFailure(f"C6 ZCode source kind mismatch: {shown!r}")
        if shown["source_path"] != native_source:
            raise CheckFailure(f"C6 ZCode source identifier mismatch: {shown!r}")
        if shown["session_id"] != "zcode-session-139":
            raise CheckFailure(f"C6 ZCode session identifier mismatch: {shown!r}")
        if shown["turn_start"] != "zcode-message-002" or shown["turn_end"] != "zcode-message-002":
            raise CheckFailure(f"C6 ZCode message anchor mismatch: {shown!r}")
        if shown["byte_start"] is not None or shown["byte_end"] is not None:
            raise CheckFailure(f"C6 ZCode native offsets must be explicit nulls: {shown!r}")
        if "zcode native turn" not in shown["excerpt"]:
            raise CheckFailure(f"C6 ZCode native excerpt missing: {shown!r}")

        # Native Hermes arm.  The provider source and dependency are supplied
        # by the explicit offline CI lane documented at _hermes_provider_env;
        # neither is installed or downloaded by this test.  Keep a stdlib
        # SQLite writer alive so the provider reads a genuine WAL generation
        # with pre-existing -wal/-shm files.
        _assert_hermes_resolver_uses_canonical_api(repo_root)
        hermes_home, hermes_db, anchor_id, wal_writer, hermes_env, before = _prepare_hermes_wal_fixture(scratch)
        try:
            for key in (
                "ZMEM_TRANSCRIPT",
                "ZMEM_AGENT_TRANSCRIPT",
                "ZMEM_CODEX_MEMORY",
                "ZMEM_ZCODE_DB",
            ):
                hermes_env.pop(key, None)
            hermes_env.update(env)
            hermes_env["HERMES_HOME"] = str(hermes_home)
            hermes_env["ZMEM_HERMES_SESSIONS"] = str(scratch / "hermes-jsonl-bait")
            native_source = "hermes:state.db/session/hermes-session-139"
            memory_id = _add_memory(
                repo_root,
                hermes_env,
                source_ref=native_source,
                content="C6 Hermes native SQLite fixture",
            )
            _write_evidence(
                repo_root,
                hermes_env,
                {
                    "id": "00000000-0000-0000-0000-000000000605",
                    "session_id": "hermes-session-139",
                    "lane": "hermes-provider",
                    "moment": "user_prompt",
                    "kind": "turn",
                    "ts": "2026-09-10T00:00:02Z",
                    "excerpt": "hermes native answer",
                    "ref_path": native_source,
                    "ref_offset": None,
                },
            )
            _associate(env, memory_id, "00000000-0000-0000-0000-000000000605")
            shown = _source_show(repo_root, hermes_env, memory_id, context=0)
            if shown["source_kind"] != "hermes_session":
                raise CheckFailure(f"C6 Hermes source kind mismatch: {shown!r}")
            if shown["source_path"] != native_source or shown["session_id"] != "hermes-session-139":
                raise CheckFailure(f"C6 Hermes native source identity mismatch: {shown!r}")
            if shown["turn_start"] != anchor_id or shown["turn_end"] != anchor_id:
                raise CheckFailure(f"C6 Hermes native message anchor mismatch: {shown!r}")
            if shown["byte_start"] is not None or shown["byte_end"] is not None:
                raise CheckFailure(f"C6 Hermes native offsets must be explicit nulls: {shown!r}")
            if "hermes native answer" not in shown["excerpt"]:
                raise CheckFailure(f"C6 Hermes native excerpt missing: {shown!r}")
            if "BAIT JSONL FALLBACK MUST NOT RENDER" in shown["excerpt"]:
                raise CheckFailure(f"C6 Hermes used JSONL instead of native SessionDB: {shown!r}")
            _assert_existing_shm_only_delta(before, _snapshot_files(hermes_home), label="C6 Hermes WAL read")

            # A present DB coupled with a provider import failure must be a
            # visible refusal.  A deliberately failing import module is only a
            # fault injector; it is never presented as a Hermes implementation.
            import_failure = scratch / "hermes-provider-import-failure"
            import_failure.mkdir()
            (import_failure / "hermes_state.py").write_text(
                "raise ImportError('C6 fixture: provider import unavailable')\n", encoding="utf-8"
            )
            failure_env = dict(hermes_env)
            failure_env["PYTHONPATH"] = str(import_failure)
            failure_before = _snapshot_files(hermes_home)
            _assert_refused(
                _run(repo_root, failure_env, "source", "--id", memory_id, "--context", "0"),
                "C6 Hermes provider import failure",
            )
            if failure_before != _snapshot_files(hermes_home):
                raise CheckFailure("C6 Hermes provider import failure opened or changed the source")

            # Keep an actual provider state database locked by another SQLite
            # connection.  The pinned SessionDB constructor reaches its FTS
            # probe and raises ``OperationalError: database is locked`` here;
            # a future resolver may preflight and refuse earlier, but it must
            # still report this distinct present-DB open failure with no export
            # fallback or source mutation.
            provider_root, provider_env = _hermes_provider_env()
            open_failure_home = scratch / "hermes-home-open-failure"
            _create_hermes_state_db(
                provider_root, provider_env, open_failure_home, session_id="hermes-open-failure"
            )
            open_failure_db = open_failure_home / "state.db"
            open_holder = sqlite3.connect(open_failure_db, timeout=0.0, isolation_level=None)
            try:
                mode = open_holder.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
                if str(mode).lower() != "delete":
                    raise CheckFailure(f"C6 provider-open fixture did not enter DELETE mode: {mode!r}")
                open_holder.execute("BEGIN EXCLUSIVE")
                open_failure_env = dict(hermes_env)
                open_failure_env["HERMES_HOME"] = str(open_failure_home)
                open_before = _snapshot_files(open_failure_home)
                _assert_refused(
                    _run(repo_root, open_failure_env, "source", "--id", memory_id, "--context", "0"),
                    "C6 Hermes provider open failure",
                )
                if open_before != _snapshot_files(open_failure_home):
                    raise CheckFailure("C6 Hermes provider open failure changed the source")
            finally:
                open_holder.rollback()
                open_holder.close()

            # The canonical provider itself will create missing WAL sidecars
            # under mode=ro.  A resolver must detect this unsafe state before
            # importing/opening it, then refuse rather than returning the
            # permitted JSONL export bait.
            missing_home = scratch / "hermes-home-missing-sidecars"
            missing_home.mkdir()
            missing_db = missing_home / "state.db"
            shutil.copy2(hermes_db, missing_db)
            if (missing_home / "state.db-wal").exists() or (missing_home / "state.db-shm").exists():
                raise CheckFailure("C6 missing-sidecar fixture unexpectedly has WAL sidecars")
            missing_env = dict(hermes_env)
            missing_env["HERMES_HOME"] = str(missing_home)
            missing_before = _snapshot_files(missing_home)
            _assert_refused(
                _run(repo_root, missing_env, "source", "--id", memory_id, "--context", "0"),
                "C6 Hermes missing WAL sidecars",
            )
            missing_after = _snapshot_files(missing_home)
            if missing_before != missing_after:
                raise CheckFailure(
                    "C6 Hermes missing-sidecar refusal created or changed source files: "
                    f"before={missing_before} after={missing_after}"
                )
        finally:
            wal_writer.close()

        # JSONL export is an acceptance-bearing fallback only when the
        # canonical Hermes DB is absent.  Its role/content records use the
        # existing bounded Hermes-session parser shape; evidence supplies the
        # session/time anchor.  Do not reuse the native arm's bait: that bait
        # proves the stronger present-DB no-substitution contract above.
        fallback_home = scratch / "hermes-home-absent"
        fallback_home.mkdir()
        if (fallback_home / "state.db").exists():
            raise CheckFailure("C6 JSONL fallback fixture unexpectedly has a canonical Hermes DB")
        fallback_dir = scratch / "hermes-jsonl-fallback"
        fallback_dir.mkdir()
        fallback = fallback_dir / "hermes-session.jsonl"
        fallback_bytes = (
            b'{"session_id":"hermes-jsonl-fallback-139","timestamp":"2026-09-10T00:00:01Z","role":"user","content":"hermes JSONL fallback prompt"}\n'
            b'{"session_id":"hermes-jsonl-fallback-139","timestamp":"2026-09-10T00:00:02Z","role":"assistant","content":"hermes JSONL fallback answer"}\n'
            b'{"session_id":"hermes-jsonl-unrelated-139","timestamp":"2026-09-10T00:00:03Z","role":"assistant","content":"unrelated export record"}\n'
        )
        fallback.write_bytes(fallback_bytes)
        fallback_offset = fallback_bytes.find(b'"timestamp":"2026-09-10T00:00:02Z"')
        if fallback_offset < 0:
            raise CheckFailure("C6 JSONL fallback fixture lost the evidence anchor")
        fallback_env = dict(hermes_env)
        fallback_env["HERMES_HOME"] = str(fallback_home)
        fallback_env["ZMEM_HERMES_SESSIONS"] = str(fallback_dir)
        fallback_memory = _add_memory(
            repo_root,
            fallback_env,
            source_ref=fallback.name,
            content="C6 Hermes JSONL fallback fixture",
        )
        fallback_evidence = "00000000-0000-0000-0000-000000000606"
        _write_evidence(
            repo_root,
            fallback_env,
            {
                "id": fallback_evidence,
                "session_id": "hermes-jsonl-fallback-139",
                "lane": "hermes-provider",
                "moment": "user_prompt",
                "kind": "turn",
                "ts": "2026-09-10T00:00:02Z",
                "excerpt": "hermes JSONL fallback answer",
                "ref_path": fallback.name,
                "ref_offset": fallback_offset,
            },
        )
        _associate(fallback_env, fallback_memory, fallback_evidence)
        fallback_shown = _source_show(repo_root, fallback_env, fallback_memory, context=0)
        if fallback_shown["source_kind"] != "hermes_session":
            raise CheckFailure(f"C6 JSONL fallback source kind mismatch: {fallback_shown!r}")
        if fallback_shown["source_path"] != fallback.name:
            raise CheckFailure(f"C6 JSONL fallback path mismatch: {fallback_shown!r}")
        if fallback_shown["session_id"] != "hermes-jsonl-fallback-139":
            raise CheckFailure(f"C6 JSONL fallback session mismatch: {fallback_shown!r}")
        if "hermes JSONL fallback answer" not in fallback_shown["excerpt"]:
            raise CheckFailure(f"C6 JSONL fallback excerpt mismatch: {fallback_shown!r}")
        if fallback_shown["byte_start"] is None or fallback_shown["byte_end"] is None:
            raise CheckFailure(f"C6 JSONL fallback must retain file byte offsets: {fallback_shown!r}")


CHECKS = {
    "C1": check_c1,
    "C2": check_c2,
    "C3": check_c3,
    "C4": check_c4,
    "C5": check_c5,
    "C6": check_c6,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=sorted(CHECKS))
    parser.add_argument("--repo-root", required=True, type=Path)
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    try:
        CHECKS[args.check](repo_root)
    except Exception as exc:
        print(f"{args.check}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"{args.check}: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
