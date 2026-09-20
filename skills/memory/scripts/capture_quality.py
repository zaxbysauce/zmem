"""Pure capture-policy helpers shared by the host adapters.

This module deliberately has no store or host dependencies.  The shell and
plugin entry points use these small functions before they parse payloads or
touch any persistent state, so the policy remains deterministic and easy to
exercise in isolation.
"""

from __future__ import annotations

import os
import re
import shlex
from typing import Mapping


FENCE_BEGIN = "<<<ZMEM_UNTRUSTED_FENCE>>>"
FENCE_END = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
MAX_DESCRIPTOR_CHARS = 80
MAX_COMMAND_CHARS = 240

_FENCE_BLOCK = re.compile(
    re.escape(FENCE_BEGIN) + r".*?" + re.escape(FENCE_END),
    flags=re.DOTALL,
)


def capture_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """Return whether capture work is enabled for *environ*.

    ``ZMEM_CAPTURE`` is intentionally a string switch.  Undefined, empty,
    whitespace, and every value other than a trimmed ``"0"`` leave capture
    enabled; this preserves the parent process value byte-for-byte for the
    launcher while making the disabled value unambiguous at each boundary.
    """

    values = os.environ if environ is None else environ
    return str(values.get("ZMEM_CAPTURE", "1")).strip() != "0"


def infer_signal(command: str, *, exit_code: int | None = None) -> str:
    """Map an exact recognized command prefix to its evidence signal.

    ``exit_code`` is accepted as part of the policy API, but signal quality is
    determined by the command family rather than the particular nonzero code.
    Invalid shell syntax and all unrecognized commands are deliberately
    ``none``.
    """

    del exit_code
    try:
        tokens = shlex.split(command or "", posix=True)
    except (TypeError, ValueError):
        return "none"

    if not tokens:
        return "none"
    if tokens[0] == "pytest":
        return "test"
    if tokens[:3] in (["python", "-m", "unittest"], ["python", "-m", "pytest"]):
        return "test"
    if tokens[:3] == ["python", "-m", "compileall"]:
        return "compile"
    if tokens[:2] in (["ruff", "check"], ["biome", "check"]):
        return "lint"
    return "none"


def _normalized(value: object, limit: int | None = None) -> str:
    """Normalize untrusted descriptor text to one bounded lower-case line."""

    text = "" if value is None else str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    normalized = text.lower()
    return normalized if limit is None else normalized[:limit]


def operation_descriptor(
    tool: str,
    command: str,
    path: str,
    error_type: str,
) -> dict[str, str]:
    """Return the stable, bounded descriptor used by capture prompts."""

    tool_key = " ".join(
        ("" if tool is None else str(tool))
        .replace("\r", " ")
        .replace("\n", " ")
        .split()
    ).lower()
    if tool_key == "bash":
        verb = "run"
    elif tool_key in {"edit", "write", "multiedit", "notebookedit"}:
        verb = "edit"
    else:
        verb = "call"

    normalized_path = _normalized(path)
    if normalized_path:
        basename = re.split(r"[\\/]", normalized_path)[-1] or "unknown"
    else:
        basename = "unknown"

    error = _normalized(error_type, MAX_DESCRIPTOR_CHARS) or "none"
    return {
        "tool": _normalized(tool, MAX_DESCRIPTOR_CHARS),
        "verb": verb,
        "basename": basename[:MAX_DESCRIPTOR_CHARS],
        "command": _normalized(command, MAX_COMMAND_CHARS),
        "error": error,
    }


def strip_zmem_fence(text: str) -> str:
    """Remove complete zmem fence blocks while preserving unmatched markers."""

    return _FENCE_BLOCK.sub("", text)
