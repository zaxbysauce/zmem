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
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

_MAX_INPUT_BYTES = 64 * 1024
_STORE_TIMEOUT_S = 5.0
_EVIDENCE_INFLIGHT_MAX = 8
_EVIDENCE_INFLIGHT = threading.BoundedSemaphore(_EVIDENCE_INFLIGHT_MAX)


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
    explicit = os.environ.get("ZMEM_PYTHON", "").strip()
    if explicit:
        return explicit
    if sys.executable:
        return sys.executable
    candidates = ("python", "python3") if os.name == "nt" else ("python3", "python")
    return next((candidate for candidate in candidates if shutil.which(candidate)), candidates[0])


def _compact_json(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        text = text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        if len(text.encode("utf-8")) > _MAX_INPUT_BYTES:
            raise ValueError("JSON payload exceeds input bound")
        return text
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise ValueError("JSON payload is not safely serializable") from exc


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
    if isinstance(args, str):
        return _patch_path(args)
    if not isinstance(args, dict):
        return ""
    for key in ("path", "file_path", "filePath", "notebook_path", "notebookPath",
                "target_file", "targetFile", "filename"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("edits", "files", "changes"):
        entries = args.get(key)
        if isinstance(entries, list):
            for entry in entries:
                path = _edit_path(entry)
                if path:
                    return path
    for key in ("patch", "patch_text", "patchText", "diff", "input", "content"):
        path = _patch_path(args.get(key))
        if path:
            return path
    return ""


def _patch_path(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.replace("\r", "")
    for pattern in (
        r"^\s*\*\*\*\s+(?:Update|Add|Delete)\s+File:\s*(\S.*?)\s*$",
        r"^\s*\+\+\+\s+(?:b/)?([^\s]+)\s*$",
        r"^\s*---\s+(?:a/)?([^\s]+)\s*$",
    ):
        match = re.search(pattern, text, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


def _uri_id(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if not value or len(value) > 256 or any(char in value for char in "/\\\x00"):
        return ""
    return value


def _safe_ref_path(value: Any, limit: int = 4096) -> str:
    if not isinstance(value, str):
        return ""
    clean = re.sub(r"[\x00-\x1f\x7f\u2028\u2029]", " ", value).strip()
    if "://" not in clean and (clean.startswith(("/", "\\\\"))
                              or re.match(r"^[A-Za-z]:[\\\\/]", clean)):
        clean = clean.replace("\\", "/").rsplit("/", 1)[-1]
    return clean[:limit]


def _failure(value: Any, *, depth: int = 0, seen: set[int] | None = None) -> bool:
    if depth > 8 or value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in {
            "", "ok", "success", "succeeded", "completed", "complete"
        }
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, list):
        return bool(value)
    if not isinstance(value, dict):
        return False
    seen = seen or set()
    marker = id(value)
    if marker in seen:
        return False
    seen.add(marker)
    try:
        status = value.get("status")
        if isinstance(status, str) and status.strip().lower() not in {
            "", "ok", "success", "succeeded", "completed", "complete"
        }:
            return True
        for key in ("error", "error_message", "error_type", "failure"):
            candidate = value.get(key)
            if candidate not in (None, "", False, 0, [], {}) and _failure(
                candidate, depth=depth + 1, seen=seen
            ):
                return True
        return any(_failure(value.get(key), depth=depth + 1, seen=seen)
                   for key in ("result", "tool_result", "tool_output", "details", "cause"))
    finally:
        seen.remove(marker)


def _stable_id(extra: dict[str, Any], payload: dict[str, Any]) -> str:
    supplied = extra.get("evidence_id") or payload.get("evidence_id")
    try:
        if isinstance(supplied, str):
            return str(uuid.UUID(supplied))
    except (AttributeError, TypeError, ValueError):
        pass
    task_id = _uri_id(payload.get("task_id"))
    call_id = _uri_id(payload.get("tool_call_id"))
    if task_id and call_id:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"zmem-hermes:{task_id}:{call_id}"))
    return str(uuid.uuid4())


def _write_post_tool_evidence(
    payload: dict[str, Any],
    extra: dict[str, Any],
    clock: Callable[[], str] = _utc_now,
) -> bool:
    """Submit bounded post-tool evidence to the internal writer CLI.

    This is intentionally detached and fail-open.  A broken pipe, unavailable
    interpreter, or writer timeout must not affect the host turn.
    """
    if not isinstance(payload, dict):
        return False
    if not isinstance(extra, dict):
        extra = {}
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
                     for status in statuses) or _failure(
            result
        ) or bool(
            extra.get("error") or extra.get("error_message")
            or payload.get("error") or payload.get("error_message")
            or payload.get("error_type")
        )
        normalized_tool = tool_name.strip().lower().replace("-", "_").replace(" ", "_")
        edit_names = {
            "edit", "edit_file", "write", "write_file", "writefile", "multi_edit",
            "multiedit", "apply_patch", "applypatch", "patch_file", "patchfile",
            "notebookedit", "notebook_edit", "str_replace_editor", "strreplaceeditor",
        }
        edit_path = _safe_ref_path(_edit_path(args))
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
            "id": _stable_id(extra, payload),
            "session_id": session.strip(),
            "lane": "hermes-compat",
            "moment": "pretool",
            "kind": kind,
            "ts": clock(),
            "excerpt": excerpt,
            "ref_path": edit_path if kind == "edit" else (
                f"hermes://{quote(task_id, safe='')}/{quote(tool_call_id, safe='')}"
            ),
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
        if not _EVIDENCE_INFLIGHT.acquire(blocking=False):
            return False
        try:
            child = subprocess.Popen(
                [_python_bin(), str(store_py), "evidence", "write"], **kwargs
            )
        except Exception:
            _EVIDENCE_INFLIGHT.release()
            raise
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
            finally:
                try:
                    _EVIDENCE_INFLIGHT.release()
                except ValueError:
                    pass

        reaper = threading.Thread(
            target=_reap_child, name="zmem-hermes-evidence-reaper", daemon=True
        )
        try:
            reaper.start()
        except Exception:
            _EVIDENCE_INFLIGHT.release()
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
        if isinstance(payload, dict):
            if not isinstance(extra, dict):
                extra = {}
            _write_post_tool_evidence(payload, extra)
            _run_convention(payload, extra)
    _emit_empty()
    return 0


if __name__ == "__main__":
    sys.exit(main())
