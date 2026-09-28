#!/usr/bin/env python3
"""Fail-open host adapter for automatic issue #135 partial captures.

The host hooks only know about callback payloads.  This adapter is the narrow
boundary that turns those payloads into calls to ``storelib.training_capture``.
It deliberately imports only the partial-capture and delivery-snapshot APIs:
acknowledgement and completion belong to a later trusted workflow and are not
available on this host path.

The adapter keeps a short-lived, hashed per-turn sidecar containing the store
capture id when the host supplies a stable turn key.  Hosts that do
not supply one still create fresh partials, but their later callbacks cannot be
associated automatically.  The id is a local correlation value; it is never
used as a host task id and never appears in a host response.  All failures
return an empty object and exit zero so a capture problem cannot block a host
callback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


_MAX_INPUT_BYTES = 64 * 1024
_MAX_OBSERVATION_BYTES = 4_000
_STATE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
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


def _valid_capture_id(value: object) -> str:
    text = _text(value, limit=80)
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, TypeError, ValueError):
        return ""
    return str(parsed) if str(parsed) == text.lower() else ""


def _read_state_record(state_key: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    if not state_key:
        return {}
    path = _state_path(state_key, env)
    try:
        if time.time() - path.stat().st_mtime > _STATE_MAX_AGE_SECONDS:
            path.unlink(missing_ok=True)
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            return {}
        capture_id = _valid_capture_id(value.get("capture_id"))
        generation = _valid_capture_id(value.get("generation"))
        if not capture_id or not generation:
            return {}
        delivery_snapshot_id = _valid_capture_id(value.get("delivery_snapshot_id"))
        result = {"capture_id": capture_id, "generation": generation}
        if delivery_snapshot_id:
            result["delivery_snapshot_id"] = delivery_snapshot_id
        return result
    except (OSError, ValueError, TypeError, OverflowError):
        return {}


def _read_state(state_key: str, env: Mapping[str, str] | None = None) -> str:
    """Return only the store-owned capture id for compatibility callers."""
    return _read_state_record(state_key, env).get("capture_id", "")


def _write_state(state_key: str, capture_id: str, env: Mapping[str, str] | None = None,
                 *, generation: str | None = None, delivery_snapshot_id: str = "") -> None:
    generation = generation or str(uuid.uuid4())
    path = _state_path(state_key, env)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            state = {"capture_id": capture_id, "generation": generation}
            if delivery_snapshot_id:
                state["delivery_snapshot_id"] = delivery_snapshot_id
            json.dump(state, handle, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


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
    """Choose an immutable per-turn sidecar key.

    Session ids identify a conversation, not a turn.  A sidecar is therefore
    written only when the host supplies an explicit turn identity.
    Keyless starts still create a fresh partial, but later callbacks cannot
    attach to it through a session or host fallback.
    """
    explicit = _first_text(
        payload, "capture_key", "captureKey", "turn_id", "turnId",
        limit=512,
    )
    if not explicit:
        return ""
    session_id = _session(payload)
    return f"{session_id}:turn:{explicit}" if session_id else f"turn:{explicit}"


def _correlated_capture_id(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """Resolve only the store-issued id bound to an explicit sidecar key.

    A callback supplied capture_id is a consistency check, never an authority.
    A valid but mismatched id fails closed; malformed values are ignored so a
    keyed host callback can still use its store-owned sidecar correlation.
    """
    state_key = _state_key(payload)
    if not state_key:
        return ""
    stored = _read_state_record(state_key, env)
    if not stored:
        return ""
    supplied = payload.get("capture_id")
    supplied_text = _text(supplied, limit=80)
    supplied_id = _valid_capture_id(supplied)
    if supplied_text and supplied_id and supplied_id != stored["capture_id"]:
        return ""
    return stored["capture_id"]


def _start(payload: Mapping[str, Any], env: Mapping[str, str],
           api: Mapping[str, Any]) -> dict[str, Any]:
    state_key = _state_key(payload)
    existing = _read_state_record(state_key, env) if state_key else {}
    if existing:
        return {
            "capture_id": existing["capture_id"],
            "state": "partial",
            "redaction_status": "",
        }
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
    capture_id = _valid_capture_id(
        row.get("capture_id") if isinstance(row, dict) else ""
    )
    if not capture_id:
        return {}
    generation = str(uuid.uuid4())
    if state_key:
        _write_state(state_key, capture_id, env, generation=generation)
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
    capture_id = _correlated_capture_id(payload, env)
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
    state_key = _state_key(payload)
    before = _read_state_record(state_key, env)
    capture_id = _correlated_capture_id(payload, env)
    if not capture_id or not before:
        return {}
    rendered = payload.get("rendered")
    if not isinstance(rendered, str):
        return {}
    effective_ops = payload.get("effective_ops")
    if not isinstance(effective_ops, list):
        effective_ops = []
    if len(effective_ops) > 128 or not all(isinstance(item, str) for item in effective_ops):
        return {}
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
    delivery_snapshot_id = _valid_capture_id(
        row.get("delivery_snapshot_id") if isinstance(row, dict) else ""
    )
    # A detached snapshot may finish after a newer start has replaced this
    # sidecar.  Only preserve its immutable delivery identity when the state
    # still names the same generation and store-issued capture id.
    after = _read_state_record(state_key, env)
    if (delivery_snapshot_id and after.get("capture_id") == before["capture_id"]
            and after.get("generation") == before["generation"]):
        _write_state(
            state_key,
            before["capture_id"],
            env,
            generation=before["generation"],
            delivery_snapshot_id=delivery_snapshot_id,
        )
    return {
        "capture_id": capture_id,
        "delivery_snapshot_id": delivery_snapshot_id,
        "state": _text(row.get("state") if isinstance(row, dict) else "", limit=64),
    }


def run_action(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None,
               api: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run one adapter action; all host-facing errors are fail-open."""
    values = env if env is not None else os.environ
    try:
        if _text(values.get("ZMEM_CAPTURE"), limit=32).strip() == "0":
            return {}
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
