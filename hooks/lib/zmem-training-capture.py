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
from contextlib import ExitStack
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence


_MAX_INPUT_BYTES = 64 * 1024
_MAX_OBSERVATION_BYTES = 4_000
_STATE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
_SESSION_SCOPE_MAX_NAMESPACES = 8
_LOCK_TIMEOUT_SECONDS = 0.8
_DEFAULT_GOVERNANCE_SOURCE = "configured_local_policy"
_USE_PRIVATE_STANDALONE_BOOTSTRAP = False
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


def _identity_text(mapping: Mapping[str, Any], *names: str, limit: int) -> str:
    """Read an identity field without prefix truncation or fallback aliasing."""
    for name in names:
        value = mapping.get(name)
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if not candidate:
            continue
        if len(candidate.encode("utf-8")) > limit:
            return ""
        return candidate
    return ""


def _content_text(mapping: Mapping[str, Any], *names: str) -> tuple[bool, str | None]:
    """Return complete input for the store redactor, never a clipped prefix."""
    for name in names:
        value = mapping.get(name)
        if not isinstance(value, str):
            continue
        if len(value.encode("utf-8")) > _MAX_INPUT_BYTES:
            return False, None
        return True, value
    return True, None


def _has_oversized_identity(payload: Mapping[str, Any]) -> bool:
    for names, limit in (
        (("host",), 80),
        (("session_id", "sessionId", "namespace"), 512),
        (("task_id", "taskId", "host_task_id", "hostTaskId"), 512),
        (("capture_key", "captureKey", "turn_id", "turnId"), 512),
    ):
        for name in names:
            value = payload.get(name)
            if isinstance(value, str) and value.strip() and len(value.strip().encode("utf-8")) > limit:
                return True
    return False


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
    # State keys are store-issued correlation digests, never host identifiers.
    digest = _text(session_id, limit=128)
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = hashlib.sha256(digest.encode("utf-8")).hexdigest()
    return _data_dir(env) / "training-capture" / f"{digest}.json"


def _lock_path(session_key: str, env: Mapping[str, str] | None = None) -> Path:
    return _data_dir(env) / "training-capture" / "locks" / f"{session_key}.lock"


class _SessionLock:
    """A stable, process-scoped lock file that is never removed on release."""

    def __init__(self, session_key: str, env: Mapping[str, str],
                 deadline: float | None = None) -> None:
        self.path = _lock_path(session_key, env)
        self.deadline = deadline
        self.handle: Any | None = None
        self.locked = False

    def __enter__(self) -> "_SessionLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        deadline = self.deadline if self.deadline is not None else time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if time.monotonic() >= deadline:
                    try:
                        self.handle.close()
                    except OSError:
                        pass
                    self.handle = None
                    raise TimeoutError("capture_lock_timeout")
                time.sleep(0.01)
                continue
            self.locked = True
            try:
                # LK_NBLCK needs a byte at offset zero.  On Windows a held
                # byte-range lock denies another handle's read, so initialize
                # an empty stable file only after acquiring that byte.
                if os.fstat(self.handle.fileno()).st_size == 0:
                    self.handle.seek(0)
                    self.handle.write(b"0")
                    self.handle.flush()
                return self
            except BaseException:
                self.__exit__(None, None, None)
                raise

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self.handle is None:
            return
        try:
            if self.locked:
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None
            self.locked = False


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


def _session_scope_key(fields: Mapping[str, str]) -> str:
    """A hashed index for the start namespace of one host session."""
    canonical = json.dumps({"host": fields["host"], "session_id": fields["session_id"]},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_session_scope_keys(fields: Mapping[str, str], env: Mapping[str, str]) -> list[str]:
    path = _state_path(_session_scope_key(fields), env)
    try:
        if time.time() - path.stat().st_mtime > _STATE_MAX_AGE_SECONDS:
            path.unlink(missing_ok=True)
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        values = value.get("session_keys") if isinstance(value, dict) else []
        if not isinstance(values, list):
            return []
        session_keys = []
        for candidate in values[:_SESSION_SCOPE_MAX_NAMESPACES]:
            session_key = _text(candidate, limit=64).strip()
            if re.fullmatch(r"[0-9a-f]{64}", session_key) and session_key not in session_keys:
                session_keys.append(session_key)
        return session_keys
    except (OSError, ValueError, TypeError, OverflowError):
        return []


def _write_session_scope(fields: Mapping[str, str], session_key: str,
                         env: Mapping[str, str]) -> None:
    path = _state_path(_session_scope_key(fields), env)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            session_keys = _read_session_scope_keys(fields, env)
            if session_key not in session_keys:
                session_keys.append(session_key)
            if len(session_keys) > _SESSION_SCOPE_MAX_NAMESPACES:
                raise ValueError("capture_scope_limit")
            json.dump({"session_keys": session_keys}, handle, separators=(",", ":"))
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _clear_session_scope(fields: Mapping[str, str], env: Mapping[str, str]) -> None:
    try:
        _state_path(_session_scope_key(fields), env).unlink(missing_ok=True)
    except OSError as exc:
        raise RuntimeError("capture_scope_clear_failed") from exc


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


def _api_from_modules(schema: Any, capture: Any) -> dict[str, Any]:
    """Expose the exact capture surface shared by normal and private imports."""
    return {
        "connect": schema.connect,
        "prepare": schema._prepare_store,
        "start": capture.start_training_capture,
        "start_correlated": capture.start_correlated_training_capture,
        "observe": capture.append_training_capture_observation,
        "observe_correlated": capture.append_correlated_training_capture_observation,
        "snapshot": capture.record_training_delivery_snapshot,
        "snapshot_correlated": capture.record_correlated_training_delivery_snapshot,
        "clear_correlated": capture.clear_correlated_training_session,
        "clear_session_keys": capture._clear_training_capture_session_keys,
        "identity_keys": capture.training_capture_identity_keys,
    }


def _private_storelib_child(name: str, path: Path, package: Any) -> Any:
    """Load one real child module and give the private package normal attributes."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load {name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    setattr(package, name.rpartition(".")[2], module)
    return module


def _load_private_standalone_api() -> dict[str, Any] | None:
    """Load real capture modules without the unrelated compatibility package init.

    This applies only to this executable helper's short-lived process.  Imported
    adapters and every normal store client retain their ordinary package import.
    On any failure the entire private package prefix is removed before normal
    import fallback, preventing a compatibility-less shell from leaking there.
    """
    scripts = _scripts_dir()
    if scripts is None:
        return None
    storelib = scripts / "storelib"
    schema_path = storelib / "schema.py"
    capture_path = storelib / "training_capture.py"
    existing_prefix = tuple(name for name in sys.modules
                            if name == "storelib" or name.startswith("storelib."))
    # Never replace a parent or orphaned child from an embedding process.
    if existing_prefix or not schema_path.is_file() or not capture_path.is_file():
        return None
    inserted_path = str(scripts)
    if inserted_path not in sys.path:
        sys.path.insert(0, inserted_path)
    before = {name for name in sys.modules if name == "storelib" or name.startswith("storelib.")}
    try:
        package_spec = importlib.machinery.ModuleSpec("storelib", loader=None, is_package=True)
        package_spec.submodule_search_locations = [str(storelib)]
        package = importlib.util.module_from_spec(package_spec)
        package.__path__ = list(package_spec.submodule_search_locations)
        sys.modules["storelib"] = package
        schema = _private_storelib_child("storelib.schema", schema_path, package)
        capture = _private_storelib_child("storelib.training_capture", capture_path, package)
        if (Path(schema.__file__).resolve() != schema_path.resolve()
                or Path(capture.__file__).resolve() != capture_path.resolve()
                or getattr(package, "schema", None) is not schema
                or getattr(package, "training_capture", None) is not capture):
            raise ImportError("private storelib identity check failed")
        return _api_from_modules(schema, capture)
    except Exception:
        for name in tuple(sys.modules):
            if ((name == "storelib" or name.startswith("storelib."))
                    and name not in before):
                sys.modules.pop(name, None)
        return None


def _load_api() -> dict[str, Any]:
    if _USE_PRIVATE_STANDALONE_BOOTSTRAP:
        private = _load_private_standalone_api()
        if private is not None:
            return private
    scripts = _scripts_dir()
    if scripts is None:
        raise RuntimeError("zmem scripts directory unavailable")
    inserted = str(scripts)
    if inserted not in sys.path:
        sys.path.insert(0, inserted)
    from storelib import schema  # type: ignore
    from storelib import training_capture  # type: ignore
    return _api_from_modules(schema, training_capture)


def _connection(api: Mapping[str, Any]):
    conn = api["connect"]()
    prepare = api.get("prepare")
    if callable(prepare):
        prepare(conn)
    return conn


def _lock_timeout() -> dict[str, Any]:
    """Emit a non-sensitive host diagnostic while preserving fail-open output."""
    print("capture_lock_timeout", file=sys.stderr)
    return {}


def _session(payload: Mapping[str, Any]) -> str:
    return _first_text(payload, "session_id", "sessionId", limit=512)


def _identity_fields(payload: Mapping[str, Any], *, require_turn: bool) -> dict[str, str]:
    fields = {
        "host": _identity_text(payload, "host", limit=80).lower(),
        "session_id": _identity_text(payload, "session_id", "sessionId", limit=512),
        "namespace": _identity_text(payload, "namespace", limit=512),
        "task_id": _identity_text(payload, "task_id", "taskId", "host_task_id", "hostTaskId", limit=512),
        "turn_id": _identity_text(payload, "capture_key", "captureKey", "turn_id", "turnId", limit=512),
    }
    required = ("host", "session_id", "namespace") + (("task_id", "turn_id") if require_turn else ())
    return fields if all(fields[name] for name in required) else {}


def _identity_keys(payload: Mapping[str, Any], api: Mapping[str, Any], *, require_turn: bool) -> tuple[str, str, dict[str, str]] | None:
    fields = _identity_fields(payload, require_turn=require_turn)
    helper = api.get("identity_keys")
    if not fields or not callable(helper):
        return None
    try:
        correlation_key, session_key = helper(
            host=fields["host"], session_id=fields["session_id"],
            namespace=fields["namespace"], task_id=fields["task_id"],
            turn_id=fields["turn_id"], require_turn=require_turn,
        )
    except Exception:
        return None
    if not isinstance(session_key, str) or not re.fullmatch(r"[0-9a-f]{64}", session_key):
        return None
    if require_turn and (not isinstance(correlation_key, str) or not re.fullmatch(r"[0-9a-f]{64}", correlation_key)):
        return None
    return correlation_key, session_key, fields


def _start(payload: Mapping[str, Any], env: Mapping[str, str],
           api: Mapping[str, Any], *, persist_sidecar: bool = True) -> dict[str, Any]:
    # A correlated start has a complete durable tuple.  Standalone starts are
    # intentionally unmapped but still require a complete scope for the closed
    # session check in the store transaction.
    if _has_oversized_identity(payload):
        return {}
    session_id = _identity_text(payload, "session_id", "sessionId", limit=512)
    host = _identity_text(payload, "host", limit=80).lower()
    namespace = _identity_text(payload, "namespace", limit=512)
    if not host:
        return {}
    prompt_ok, prompt = _content_text(payload, "prompt")
    response_ok, response = _content_text(payload, "assistant_response")
    if not prompt_ok or not response_ok:
        return {}
    governance = _governance(env)
    kwargs = {
        "host": host, "session_id": session_id or None, "namespace": namespace or None,
        "host_task_id": _first_text(payload, "host_task_id", "hostTaskId", limit=512) or None,
        "cwd": _first_text(payload, "cwd", limit=4096) or None,
        "prompt": prompt,
        "assistant_response": response,
        **governance,
    }
    identity = _identity_keys(payload, api, require_turn=persist_sidecar)
    if identity is None:
        # Preserve the automatic, metadata-only partial contract for hosts that
        # did not provide enough identity to correlate a turn.  Content-bearing
        # fields are dropped because no later callback can prove their
        # association.  No sidecar or mapping is created on this path.
        kwargs["prompt"] = None
        kwargs["assistant_response"] = None
        try:
            conn = _connection(api)
            try:
                row = api["start"](conn, **kwargs)
            finally:
                conn.close()
        except (OSError, ValueError):
            return {}
        capture_id = _valid_capture_id(row.get("capture_id") if isinstance(row, dict) else "")
        if not capture_id:
            return {}
        return {
            "capture_id": capture_id,
            "state": _text(row.get("state") if isinstance(row, dict) else "", limit=64),
            "redaction_status": _text(row.get("redaction_status") if isinstance(row, dict) else "", limit=64),
        }
    correlation_key, session_key, fields = identity
    try:
        lock_deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        with _SessionLock(_session_scope_key(fields), env, lock_deadline):
            # Record the opaque scope before its database transaction.  A
            # crash may leave an empty scope to clear, but cannot strand a
            # successfully committed correlation outside session-end cleanup.
            if persist_sidecar:
                _write_session_scope(fields, session_key, env)
            with _SessionLock(session_key, env, lock_deadline):
                conn = _connection(api)
                try:
                    if persist_sidecar:
                        row = api["start_correlated"](
                            conn, task_id=fields["task_id"], turn_id=fields["turn_id"], **kwargs,
                        )
                    else:
                        row = api["start"](conn, **kwargs)
                finally:
                    conn.close()
                capture_id = _valid_capture_id(
                    row.get("capture_id") if isinstance(row, dict) else ""
                )
                if not capture_id:
                    return {}
                if persist_sidecar:
                    previous = _read_state_record(correlation_key, env)
                    generation = (
                        previous.get("generation")
                        if previous.get("capture_id") == capture_id else str(uuid.uuid4())
                    )
                    _write_state(correlation_key, capture_id, env, generation=generation)
                return {
                    "capture_id": capture_id,
                    "state": _text(row.get("state") if isinstance(row, dict) else "", limit=64),
                    "redaction_status": _text(
                        row.get("redaction_status") if isinstance(row, dict) else "", limit=64
                    ),
                }
    except TimeoutError:
        return _lock_timeout()
    except (OSError, ValueError):
        return {}


def _observation_kind(payload: Mapping[str, Any]) -> str:
    kind = _first_text(payload, "observation_kind", "observationKind", limit=128)
    return kind if kind in _OBSERVATION_KINDS else "post_tool"


def _observe(payload: Mapping[str, Any], env: Mapping[str, str],
              api: Mapping[str, Any]) -> dict[str, Any]:
    identity = _identity_keys(payload, api, require_turn=True)
    if identity is None:
        return {}
    _correlation_key, session_key, fields = identity
    observation = payload.get("observation")
    if observation is None:
        observation = payload.get("meta", payload)
    encoded = _bounded_json(observation)
    try:
        with _SessionLock(session_key, env):
            conn = _connection(api)
            try:
                return api["observe_correlated"](
                    conn, host=fields["host"], session_id=fields["session_id"],
                    namespace=fields["namespace"], task_id=fields["task_id"],
                    turn_id=fields["turn_id"], observation_kind=_observation_kind(payload),
                    payload=encoded,
                )
            finally:
                conn.close()
    except TimeoutError:
        return _lock_timeout()
    except (OSError, ValueError):
        return {}


def _snapshot(payload: Mapping[str, Any], env: Mapping[str, str],
              api: Mapping[str, Any]) -> dict[str, Any]:
    identity = _identity_keys(payload, api, require_turn=True)
    if identity is None:
        return {}
    correlation_key, session_key, fields = identity
    rendered = payload.get("rendered")
    if not isinstance(rendered, str):
        return {}
    effective_ops = payload.get("effective_ops")
    if not isinstance(effective_ops, list):
        effective_ops = []
    if len(effective_ops) > 128 or not all(isinstance(item, str) for item in effective_ops):
        return {}
    try:
        with _SessionLock(session_key, env):
            before = _read_state_record(correlation_key, env)
            conn = _connection(api)
            try:
                row = api["snapshot_correlated"](
                    conn, host=fields["host"], session_id=fields["session_id"],
                    namespace=fields["namespace"], task_id=fields["task_id"],
                    turn_id=fields["turn_id"], rendered=rendered,
                    effective_ops=effective_ops,
                    transform_version=_first_text(payload, "transform_version", limit=512) or "v1",
                )
            finally:
                conn.close()
            delivery_snapshot_id = _valid_capture_id(
                row.get("delivery_snapshot_id") if isinstance(row, dict) else ""
            )
            capture_id = _valid_capture_id(row.get("capture_id") if isinstance(row, dict) else "")
            # A detached snapshot may finish after a newer start has replaced
            # this sidecar.  Preserve delivery identity only for the same
            # durable correlation generation, while the session lock is held.
            after = _read_state_record(correlation_key, env)
            if (delivery_snapshot_id and before and after.get("capture_id") == before.get("capture_id")
                    and after.get("generation") == before.get("generation")):
                _write_state(
                    correlation_key, before["capture_id"], env,
                    generation=before["generation"], delivery_snapshot_id=delivery_snapshot_id,
                )
            return {
                "capture_id": capture_id,
                "delivery_snapshot_id": delivery_snapshot_id,
                "state": _text(row.get("state") if isinstance(row, dict) else "", limit=64),
            }
    except TimeoutError:
        return _lock_timeout()
    except (OSError, ValueError):
        return {}


def _clear_correlated(payload: Mapping[str, Any], env: Mapping[str, str],
                      api: Mapping[str, Any]) -> dict[str, Any]:
    identity = _identity_keys(payload, api, require_turn=False)
    if identity is None:
        return {}
    _correlation_key, session_key, fields = identity

    def remove_sidecars(keys: Sequence[str]) -> None:
        for key in keys:
            try:
                _state_path(key, env).unlink(missing_ok=True)
            except OSError as exc:
                raise RuntimeError("capture_sidecar_clear_failed") from exc

    try:
        lock_deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        with _SessionLock(_session_scope_key(fields), env, lock_deadline):
            trusted_session_keys = _read_session_scope_keys(fields, env)
            if trusted_session_keys:
                # Scope locks are acquired in digest order after the stable
                # host/session index lock, matching correlated-start order.
                with ExitStack() as locks:
                    for trusted_key in sorted(trusted_session_keys):
                        locks.enter_context(_SessionLock(trusted_key, env, lock_deadline))
                    conn = _connection(api)
                    try:
                        api["clear_session_keys"](
                            conn, session_keys=trusted_session_keys,
                            remove_sidecars=remove_sidecars,
                        )
                    finally:
                        conn.close()
                _clear_session_scope(fields, env)
                return {}
            with _SessionLock(session_key, env, lock_deadline):
                conn = _connection(api)
                try:
                    api["clear_correlated"](
                        conn, host=fields["host"], session_id=fields["session_id"],
                        namespace=fields["namespace"], remove_sidecars=remove_sidecars,
                    )
                finally:
                    conn.close()
            _clear_session_scope(fields, env)
        return {}
    except TimeoutError:
        return {"error": "capture_busy"}
    except (OSError, ValueError):
        return {}


def run_action(payload: Mapping[str, Any], *, env: Mapping[str, str] | None = None,
               api: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Run one adapter action; all host-facing errors are fail-open."""
    values = env if env is not None else os.environ
    try:
        if _text(values.get("ZMEM_CAPTURE"), limit=32).strip() == "0":
            return {}
        action = _first_text(payload, "action", limit=32)
        loaded = api if api is not None else _load_api()
        if action == "clear":
            return _clear_correlated(payload, values, loaded)
        if action not in {"start", "start_standalone", "observe", "snapshot"}:
            return {}
        if action in {"start", "start_standalone"}:
            return _start(
                payload, values, loaded,
                persist_sidecar=action == "start",
            )
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
    _USE_PRIVATE_STANDALONE_BOOTSTRAP = True
    raise SystemExit(main())
