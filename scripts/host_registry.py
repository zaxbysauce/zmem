"""Strict codecs for host plugin-install registries.

Claude Code and ZCode use different versions of ``installed_plugins.json``.
Only the shapes used by those hosts are accepted here.  Keeping the codec
small and deliberately conservative is important: these files are host
state, so silently accepting a new shape could make a refresh overwrite data
that the host no longer understands.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class RegistrySchemaError(ValueError):
    """Raised when a registry is not one of the supported host shapes."""


_CLAUDE_HOST = "claude"
_ZCODE_HOST = "zcode"
_CLAUDE_VERSION = 2
_ZCODE_VERSION = 1
_ZMEM_CLAUDE_KEY = "zmem@zmem"
_ZMEM_ZCODE_NAME = "zmem"
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _schema_error(message: str) -> RegistrySchemaError:
    return RegistrySchemaError(f"unsupported host registry schema: {message}")


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value!r}")


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _schema_error(f"{name} must be an object")
    return value


def _require_version(data: dict[str, Any], expected: int) -> None:
    version = data.get("version")
    # bool is an int subclass, but true/false are not registry versions.
    if type(version) is not int or version != expected:
        raise _schema_error(f"version must be exactly {expected}")


def _validate_claude(data: Any) -> None:
    data = _require_mapping(data, "registry")
    _require_version(data, _CLAUDE_VERSION)
    if "plugins" not in data:
        raise _schema_error("Claude v2 registry is missing plugins")
    plugins = data["plugins"]
    if not isinstance(plugins, dict):
        raise _schema_error("Claude v2 plugins must be an object")
    for plugin_name, records in plugins.items():
        if not isinstance(plugin_name, str):
            raise _schema_error("Claude v2 plugin names must be strings")
        if not isinstance(records, list):
            raise _schema_error(
                f"Claude v2 records for {plugin_name!r} must be an array"
            )
        if any(not isinstance(record, dict) for record in records):
            raise _schema_error(
                f"Claude v2 records for {plugin_name!r} must contain objects"
            )


def _validate_zcode(data: Any) -> None:
    data = _require_mapping(data, "registry")
    _require_version(data, _ZCODE_VERSION)
    if "plugins" not in data:
        raise _schema_error("ZCode v1 registry is missing plugins")
    plugins = data["plugins"]
    if not isinstance(plugins, list):
        raise _schema_error("ZCode v1 plugins must be an array")
    if any(not isinstance(record, dict) for record in plugins):
        raise _schema_error("ZCode v1 plugins must contain objects")


def _validate_for_host(data: Any, host: str) -> str:
    if host == _CLAUDE_HOST:
        _validate_claude(data)
        return "v2"
    if host == _ZCODE_HOST:
        _validate_zcode(data)
        return "v1"
    raise RegistrySchemaError(f"unsupported host registry: {host!r}")


def _validate_any_supported(data: Any) -> str:
    """Validate a document for the host-independent writer.

    Version numbers distinguish the two supported top-level shapes, so the
    writer can validate without being passed a host name.
    """
    data = _require_mapping(data, "registry")
    version = data.get("version")
    if type(version) is int and version == _CLAUDE_VERSION:
        _validate_claude(data)
        return "v2"
    if type(version) is int and version == _ZCODE_VERSION:
        _validate_zcode(data)
        return "v1"
    raise _schema_error("version is neither supported Claude v2 nor ZCode v1")


def load_host_registry(path: Path, host: str) -> tuple[str, object]:
    """Load and validate a Claude or ZCode installed-plugin registry.

    Returns ``("v2", document)`` for Claude and ``("v1", document)`` for
    ZCode.  The returned document is the parsed JSON object and is safe for a
    caller to pass to :func:`update_host_registry`.
    """
    if host not in {_CLAUDE_HOST, _ZCODE_HOST}:
        raise RegistrySchemaError(f"unsupported host registry: {host!r}")
    registry_path = Path(path)
    try:
        text = registry_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RegistrySchemaError(
            f"cannot read registry {registry_path}: {exc}"
        ) from exc
    try:
        data = json.loads(text, parse_constant=_reject_non_json_constant)
    except (json.JSONDecodeError, TypeError, UnicodeError, ValueError) as exc:
        raise _schema_error(f"{registry_path} is not valid JSON") from exc
    schema_version = _validate_for_host(data, host)
    # JSON parsing already creates a fresh object.  Returning a deep copy makes
    # the ownership guarantee explicit and protects callers if the parser is
    # ever changed to use a shared decode object.
    return schema_version, copy.deepcopy(data)


def update_host_registry(
    data: object,
    *,
    host: str,
    version: str,
    git_commit_sha: str,
    install_path: Path,
) -> object:
    """Return a deep-copied registry with existing zmem records updated.

    No records are inserted or reordered.  Every unrelated key and record is
    retained byte-for-byte in meaning, while existing zmem records receive
    the new install path, release version, and checkout commit SHA.
    """
    _validate_for_host(data, host)
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        raise ValueError("version must be a valid semantic version")
    if not isinstance(git_commit_sha, str) or not _COMMIT_SHA_RE.fullmatch(
        git_commit_sha
    ):
        raise ValueError("git_commit_sha must be exactly 40 lowercase hex characters")
    if not isinstance(install_path, (Path, str, os.PathLike)):
        raise TypeError("install_path must be a path")

    registry = _require_mapping(data, "registry")
    updated = copy.deepcopy(registry)
    updated_install_path = str(install_path)
    if host == _CLAUDE_HOST:
        plugins = updated["plugins"]
        records = plugins.get(_ZMEM_CLAUDE_KEY, [])
        if not records:
            raise _schema_error("Claude v2 registry contains no zmem record")
        for record in records:
            record["installPath"] = updated_install_path
            record["version"] = version
            record["gitCommitSha"] = git_commit_sha
    else:
        found = False
        for record in updated["plugins"]:
            if record.get("name") == _ZMEM_ZCODE_NAME:
                found = True
                record["installPath"] = updated_install_path
                record["version"] = version
                record["gitCommitSha"] = git_commit_sha
        if not found:
            raise _schema_error("ZCode v1 registry contains no zmem record")
    return updated


def _ensure_destination_parent(destination: Path) -> None:
    """Create missing parents without traversing a symlink component."""
    if destination.is_symlink():
        raise OSError(f"registry destination is a symlink: {destination}")

    candidate = Path(os.path.abspath(str(destination)))
    while True:
        if os.path.lexists(str(candidate)) and candidate.is_symlink():
            if candidate == destination:
                raise OSError(f"registry destination is a symlink: {destination}")
            raise OSError(f"registry destination parent is a symlink: {candidate}")
        if candidate.parent == candidate:
            break
        candidate = candidate.parent

    missing: list[Path] = []
    current = destination.parent
    while not os.path.lexists(str(current)):
        missing.append(current)
        parent = current.parent
        if parent == current:
            raise OSError(f"cannot resolve registry destination parent: {destination}")
        current = parent

    if current.is_symlink():
        raise OSError(f"registry destination parent is a symlink: {current}")
    if not current.is_dir():
        raise OSError(f"registry destination parent is not a directory: {current}")

    for parent in reversed(missing):
        try:
            parent.mkdir()
        except FileExistsError:
            # A concurrent creator may have supplied the component.  Recheck
            # it rather than following a newly planted symlink.
            pass
        if parent.is_symlink():
            raise OSError(f"registry destination parent is a symlink: {parent}")
        if not parent.is_dir():
            raise OSError(f"registry destination parent is not a directory: {parent}")


def write_host_registry(path: Path, data: object) -> None:
    """Atomically write a supported registry as compact UTF-8 JSON.

    The temporary file is created beside the destination, flushed and fsynced,
    then installed with ``os.replace``.  The serialized document contains no
    formatting newlines and exactly one final LF.
    """
    _validate_any_supported(data)
    destination = Path(path)
    _ensure_destination_parent(destination)
    payload = (
        json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ) + "\n"
    ).encode("utf-8")
    temp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=str(destination.parent),
            delete=False,
        ) as temporary:
            temp_name = temporary.name
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temp_name, destination)
        temp_name = None
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


__all__ = [
    "RegistrySchemaError",
    "load_host_registry",
    "update_host_registry",
    "write_host_registry",
]
