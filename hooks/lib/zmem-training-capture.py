#!/usr/bin/env python3
"""Fail-open host adapter for automatic issue #135 partial captures.

The host hooks only know about callback payloads.  This adapter is the narrow
boundary that turns those payloads into calls to ``storelib.training_capture``.
It deliberately imports only the partial-capture and delivery-snapshot APIs:
acknowledgement and completion belong to a later trusted workflow and are not
available on this host path.

The adapter keeps a short-lived, hashed-session sidecar containing the store
capture id.  The id is a local correlation value; it is never used as a host
task id and never appears in a host response.  All failures return an empty
object and exit zero so a capture problem cannot block a host callback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


_MAX_INPUT_BYTES = 64 * 1024
_MAX_OBSERVATION_BYTES = 4_000
_DEFAULT_GOVERNANCE_SOURCE = "configured_local_policy"
_OBSERVATION_KINDS = frozenset({
    "pre_tool", "post_tool", "post_tool_failure", "stop", "user_prompt",
    "post_tool_call", "turn",
})


def _text(value: object, *, limit: int = 4096) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _first_text(mapping: Mapping[str, Any], *names: str, limit: int = 4096) -> str:
    for name in names:
        value = _text(mapping.get(name), limit=limit)
        if value:
            return value
    return ""


def _bounded_json(value: object, limit: int = _MAX_OBSERVATION_BYTES) -> str | None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))
    except (TypeError, ValueError, RecursionError, UnicodeError):
        return None
    raw = encoded.encode("utf-8")
    if len(raw) <= limit:
        return encoded
    # Keep the observation useful without handing a giant callback object to
    # the store.  The store applies its own redaction and final byte cap.
    return json.dumps({"truncated": True}, separators=(",", ":"))


def _scripts_dir() -> Path | None:
    relative = Path("skills") / "memory" / "scripts"
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / relative,
        Path(os.environ.get("ZMEM_HOME", "")).expanduser() / relative,
    ]
    for candidate in candidates:
        if (candidate / "store.py").is_file():
            return candidate
    return None


def _data_dir(env: Mapping[str, str] | None = None) -> Path:
    values = env if env is not None else os.environ
    store = _text(values.get("ZMEM_STORE"))
    if store:
        return Path(store).expanduser().parent
    for name in ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        value = _text(values.get(name))
        if value:
            return Path(value).expanduser()
    return Path.home() / ".zmem"


def _state_path(session_id: str, env: Mapping[str, str] | None = None) -> Path:
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _data_dir(env) / "training-capture" / f"{digest}.json"


def _read_state(session_id: str, env: Mapping[str, str] | None = None) -> str:
    if not session_id:
        return ""
    try:
        value = json.loads(_state_path(session_id, env).read_text(encoding="utf-8"))
        capture_id = value.get("capture_id") if isinstance(value, dict) else ""
        return _text(capture_id, limit=80)
    except (OSError, ValueError, TypeError):
        return ""


def _write_state(session_id: str, capture_id: str,
                 env: Mapping[str, str] | None = None) -> None:
    path = _state_path(session_id, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"capture_id": capture_id}, separators=(",", ":")),
                         encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _clear_state(session_id: str, env: Mapping[str, str] | None = None) -> None:
    if not session_id:
        return
    try:
        _state_path(session_id, env).unlink()
    except OSError:
        pass


def _governance(env: Mapping[str, str] | None = None) -> dict[str, str | None]:
    values = env if env is not None else os.environ
    # The absence of any one field is intentional: the store then records a
    # metadata-only partial.  Hooks never infer consent from host payloads.
    return {
        "consent_scope": _text(values.get("ZMEM_CAPTURE_CONSENT_SCOPE"), limit=512) or None,
        "content_license": _text(values.get("ZMEM_CAPTURE_CONTENT_LICENSE"), limit=512) or None,
        "redaction_policy_version": (
            _text(values.get("ZMEM_CAPTURE_REDACTION_POLICY_VERSION"), limit=512) or None
        ),
        "governance_source": (
            _text(values.get("ZMEM_CAPTURE_GOVERNANCE_SOURCE"), limit=512)
            or _DEFAULT_GOVERNANCE_SOURCE
        ),
    }


def _load_api() -> dict[str, Any]:
    scripts = _scripts_dir()
    if scripts is None:
        raise RuntimeError("zmem scripts directory unavailable")
    inserted = str(scripts)
    if inserted not in sys.path:
        sys.path.insert(0, inserted)
    from storelib import schema  # type: ignore
    from storelib.training_capture import (  # type: ignore
        append_training_capture_observation,
        record_training_delivery_snapshot,
        start_training_capture,
    )
    return {
        "connect": schema.connect,
        "prepare": schema._prepare_store,
        "start": start_training_capture,
        "observe": append_training_capture_observation,
        "snapshot": record_training_delivery_snapshot,
    }


def _connection(api: Mapping[str, Any]):
    conn = api["connect"]()
    prepare = api.get("prepare")
    if callable(prepare):
        prepare(conn)
    return conn


def _session(payload: Mapping[str, Any]) -> str:
    return _first_text(payload, "session_id", "sessionId", limit=512)


def _state_key(payload: Mapping[str, Any]) -> str:
    """Choose a local-only sidecar key when hosts omit a session id."""
    session_id = _session(payload)
    if session_id:
        return session_id
    host = _first_text(payload, "host", limit=80).lower() or "unknown"
    task_id = _first_text(payload, "host_task_id", "hostTaskId", "task_id", "taskId", limit=512)
    return f"{host}:task:{task_id}" if task_id else f"{host}:unknown"


def _start(payload: Mapping[str, Any], env: Mapping[str, str],
           api: Mapping[str, Any]) -> dict[str, Any]:
    session_id = _session(payload)
    host = _first_text(payload, "host", limit=80).lower()
    namespace = _first_text(payload, "namespace", limit=512)
    if not host:
        return {}
    governance = _governance(env)
    conn = _connection(api)
    try:
        row = api["start"](
            conn,
            host=host,
            session_id=session_id or None,
            namespace=namespace or None,
            host_task_id=_first_text(payload, "host_task_id", "hostTaskId", limit=512) or None,
            cwd=_first_text(payload, "cwd", limit=4096) or None,
            prompt=_first_text(payload, "prompt", limit=16_000) or None,
            assistant_response=_first_text(payload, "assistant_response", limit=16_000) or None,
            **governance,
        )
    finally:
        conn.close()
    capture_id = _text(row.get("capture_id") if isinstance(row, dict) else "", limit=80)
    if not capture_id:
        return {}
    _write_state(_state_key(payload), capture_id, env)
    return {
        "capture_id": capture_id,
        "state": _text(row.get("state") if isinstance(row, dict) else "", limit=64),
        "redaction_status": _text(
            row.get("redaction_status") if isinstance(row, dict) else "", limit=64
        ),
    }


def _observation_kind(payload: Mapping[str, Any]) -> str:
    kind = _first_text(payload, "observation_kind", "observationKind", limit=128)
    return kind if kind in _OBSERVATION_KINDS else "post_tool"


def _observe(payload: Mapping[str, Any], env: Mapping[str, str],
             api: Mapping[str, Any]) -> dict[str, Any]:
    capture_id = _read_state(_state_key(payload), env)
    if not capture_id:
        return {}
    observation = payload.get("observation")
    if observation is None:
        observation = payload.get("meta", payload)
    encoded = _bounded_json(observation)
    conn = _connection(api)
    try:
        return api["observe"](
            conn,
            capture_id,
            observation_kind=_observation_kind(payload),
            payload=encoded,
        )
    finally:
        conn.close()


def _snapshot(payload: Mapping[str, Any], env: Mapping[str, str],
              api: Mapping[str, Any]) -> dict[str, Any]:
    capture_id = _read_state(_state_key(payload), env)
    if not capture_id:
        return {}
    rendered = payload.get("rendered")
    if not isinstance(rendered, str):
        return {}
    effective_ops = payload.get("effective_ops")
    if not isinstance(effective_ops, list):
        effective_ops = []
    effective_ops = [item for item in effective_ops if isinstance(item, str)][:128]
    conn = _connection(api)
    try:
        row = api["snapshot"](
            conn,
            capture_id,
            rendered=rendered,
            effective_ops=effective_ops,
            transform_version=_first_text(payload, "transform_version", limit=512) or "v1",
        )
    finally:
        conn.close()
    return {
        "capture_id": capture_id,
        "delivery_snapshot_id": (
            _text(row.get("delivery_snapshot_id") if isinstance(row, dict) else "", limit=80)
        ),
        "state": _text(row.get("state") if isinstance(row, dict) else "", limit=64),
    }


def run_action(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None,
               api: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run one adapter action; all host-facing errors are fail-open."""
    values = env if env is not None else os.environ
    try:
        action = _first_text(payload, "action", limit=32)
        if action == "clear":
            _clear_state(_state_key(payload), values)
            return {}
        if action not in {"start", "observe", "snapshot"}:
            return {}
        loaded = api if api is not None else _load_api()
        if action == "start":
            return _start(payload, values, loaded)
        if action == "observe":
            return _observe(payload, values, loaded)
        return _snapshot(payload, values, loaded)
    except Exception:
        return {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--action", required=True)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
        if len(raw) > _MAX_INPUT_BYTES:
            raise ValueError("training capture input too large")
        payload = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(payload, dict):
            payload = {}
        payload["action"] = args.action
        print(json.dumps(run_action(payload), ensure_ascii=False,
                          separators=(",", ":")))
    except Exception:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
