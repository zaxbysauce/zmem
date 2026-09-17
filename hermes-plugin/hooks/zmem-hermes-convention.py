#!/usr/bin/env python3
"""Hermes ``post_tool_call`` compatibility hook.

Hermes treats this hook as observational, so it always emits ``{}``.  The
hook keeps the legacy convention/failure signal behavior by handing the
operation to zmem's internal store CLI.  Keeping SQLite and ring operations in
that process is important: the host hook must not import the database helper
package or access store state directly while another writer owns the database.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Keep the common resolver import available for the standalone hook layout and
# its local-filesystem policy.  Store access itself is delegated to store.py.
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
if _HOOK_DIR not in sys.path:
    sys.path.insert(0, _HOOK_DIR)
try:
    from _zmem_hook_common import assert_local_fs as _assert_local_fs  # noqa: E402,F401
except ModuleNotFoundError:
    # A documented copy install contains only ``hermes-plugin``.  Probe the
    # configured checkout's hooks directory for the shared resolver helper;
    # the hook still performs no store access itself.
    _configured_root = Path(os.environ.get("ZMEM_HOME", "")).expanduser()
    _configured_hook_dirs = (
        _configured_root / "hermes-plugin" / "hooks",
        _configured_root / "hooks",
    )
    for _configured_hooks in _configured_hook_dirs:
        if (_configured_hooks / "_zmem_hook_common.py").is_file():
            sys.path.insert(0, str(_configured_hooks))
            break
    try:
        from _zmem_hook_common import assert_local_fs as _assert_local_fs  # type: ignore  # noqa: E402,F401
    except ModuleNotFoundError:
        def _assert_local_fs(path: Path) -> bool:
            text = str(path)
            return not (text.startswith("\\\\") or text.startswith("//"))

_MAX_INPUT_BYTES = 64 * 1024
_STORE_TIMEOUT_S = 5.0


def _resolve_store_path() -> Path:
    """Resolve the authoritative store path for compatibility callers.

    This function remains a resolver only.  It deliberately does not open the
    path; all reads/writes happen inside the internal CLI process.
    """
    rel = Path("skills") / "memory" / "scripts"
    candidates = [
        Path(__file__).resolve().parents[2] / rel,
        Path(os.environ.get("ZMEM_HOME", "")).expanduser() / rel,
    ]
    for scripts_dir in candidates:
        if (scripts_dir / "host.py").is_file():
            sys.path.insert(0, str(scripts_dir))
            try:
                import host  # type: ignore  # noqa: F811

                return host.resolve_store_path()
            except Exception:
                pass
    explicit = os.environ.get("ZMEM_STORE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    for var in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        value = os.environ.get(var, "").strip()
        if value:
            return Path(value).expanduser() / "store.sqlite"
    return Path.home() / ".zmem" / "store.sqlite"


def _resolve_store_py() -> Path | None:
    """Find the internal store CLI for in-tree and copied plugin installs."""
    rel = Path("skills") / "memory" / "scripts" / "store.py"
    candidates = [
        Path(__file__).resolve().parents[2] / rel,
        Path(os.environ.get("ZMEM_HOME", "")).expanduser() / rel,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _python_bin() -> str:
    return os.environ.get("ZMEM_PYTHON", sys.executable or "python")


def _compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _read_payload() -> dict[str, Any]:
    """Read one bounded JSON object, dropping malformed/oversized input."""
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            return {}
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _valid_id(value: Any) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return str(uuid.uuid4())
    return str(parsed)


def _edit_path(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    for key in ("path", "file_path", "notebook_path", "target_file", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("edits", "files"):
        entries = args.get(key)
        if isinstance(entries, list):
            for entry in entries:
                path = _edit_path(entry)
                if path:
                    return path
    return ""


def _uri_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > 256 or any(char in value for char in "/\\\x00"):
        return ""
    return value


def _write_post_tool_evidence(
    payload: dict[str, Any],
    extra: dict[str, Any],
    clock: Callable[[], str] = _utc_now,
) -> bool:
    """Submit bounded post-tool evidence to the internal writer CLI.

    This is intentionally detached and fail-open.  A broken pipe, unavailable
    interpreter, or writer timeout must not affect the host turn.
    """
    if not isinstance(payload, dict) or not isinstance(extra, dict):
        return False
    required = ("tool_name", "args", "session_id", "task_id", "tool_call_id", "result", "duration_ms")
    if any(key not in payload for key in required):
        return False
    session = payload.get("session_id")
    task_id = _uri_id(payload.get("task_id"))
    tool_call_id = _uri_id(payload.get("tool_call_id"))
    tool_name = payload.get("tool_name")
    if (
        not isinstance(session, str) or not session.strip()
        or not isinstance(tool_name, str) or not task_id or not tool_call_id
    ):
        return False
    try:
        args = payload.get("args")
        result = payload.get("result")
        raw = _compact_json({"payload": payload, "extra": extra}).encode("utf-8")
        if len(raw) > _MAX_INPUT_BYTES:
            return False
        statuses = []
        for source in (extra, payload, result if isinstance(result, dict) else {}):
            value = source.get("status")
            if isinstance(value, str):
                statuses.append(value.strip().lower())
        failed = any(status in {"error", "failed", "failure"}
                     for status in statuses) or bool(
            extra.get("error") or extra.get("error_message")
            or payload.get("error") or payload.get("error_message")
            or payload.get("error_type")
        )
        normalized_tool = tool_name.strip().lower().replace("-", "_").replace(" ", "_")
        edit_names = {
            "edit", "edit_file", "write", "write_file", "writefile", "multi_edit",
            "notebookedit", "notebook_edit", "str_replace_editor",
        }
        edit_path = _edit_path(args)
        kind = "tool_failure" if failed else (
            "edit" if normalized_tool in edit_names and edit_path else "tool_call"
        )
        excerpt = _compact_json(
            {
                "tool_name": tool_name,
                "args": args,
                "result": result,
                "duration_ms": payload.get("duration_ms"),
            }
        )
        row = {
            "id": _valid_id(extra.get("evidence_id") or payload.get("evidence_id")),
            "session_id": session.strip(),
            "lane": "hermes-compat",
            "moment": "pretool",
            "kind": kind,
            "ts": clock(),
            "excerpt": excerpt,
            "ref_path": edit_path if kind == "edit" else f"hermes://{task_id}/{tool_call_id}",
            "ref_offset": None,
        }
        serialized = _compact_json(row).encode("utf-8")
        if len(serialized) > _MAX_INPUT_BYTES:
            return False
        store_py = _resolve_store_py()
        if store_py is None:
            return False
        env = os.environ.copy()
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        # Compatibility observation must retain the historical no-store/no-
        # migration behavior; the internal writer honors this guard.
        env["ZMEM_EVIDENCE_NO_CREATE"] = "1"
        payload_file = tempfile.TemporaryFile()
        payload_file.write(serialized + b"\n")
        payload_file.seek(0)
        kwargs: dict[str, Any] = {
            "stdin": payload_file,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "env": env,
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            kwargs["start_new_session"] = True
        try:
            child = subprocess.Popen(
                [_python_bin(), str(store_py), "evidence", "write"], **kwargs
            )
        finally:
            # Popen duplicates/inherits the file descriptor.  Closing this
            # parent handle immediately avoids a persistent raw-payload file;
            # the child owns its read handle for the bounded JSON only.
            payload_file.close()

        def _reap_child() -> None:
            try:
                child.wait(timeout=_STORE_TIMEOUT_S)
            except Exception:
                try:
                    child.kill()
                except Exception:
                    pass
                try:
                    child.wait(timeout=1.0)
                except Exception:
                    pass

        threading.Thread(
            target=_reap_child, name="zmem-hermes-evidence-reaper", daemon=True
        ).start()
        return True
    except Exception:
        return False


def _run_convention(payload: dict[str, Any], extra: dict[str, Any]) -> None:
    """Run the legacy convention operation through the store CLI."""
    try:
        store_py = _resolve_store_py()
        if store_py is None:
            return
        request = _compact_json({"payload": payload, "extra": extra}).encode("utf-8")
        if len(request) > _MAX_INPUT_BYTES:
            return
        env = os.environ.copy()
        env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        env["ZMEM_HERMES_CONVENTION_EXISTING_ONLY"] = "1"
        subprocess.run(
            [_python_bin(), str(store_py), "hermes-convention"],
            input=request + b"\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            timeout=_STORE_TIMEOUT_S,
            check=False,
        )
    except Exception:
        return


def _emit_empty() -> None:
    print("{}")


def main() -> int:
    envelope = _read_payload()
    if envelope:
        payload = envelope.get("payload", envelope)
        extra = envelope.get("extra", {})
        if isinstance(payload, dict) and isinstance(extra, dict):
            _write_post_tool_evidence(payload, extra)
            _run_convention(payload, extra)
    _emit_empty()
    return 0


if __name__ == "__main__":
    sys.exit(main())
