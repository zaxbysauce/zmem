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

from redaction import redact_secret_like_text


FENCE_BEGIN = "<<<ZMEM_UNTRUSTED_FENCE>>>"
FENCE_END = "<<<END_ZMEM_UNTRUSTED_FENCE>>>"
MAX_DESCRIPTOR_CHARS = 80
MAX_COMMAND_CHARS = 240

# Issue #511: the renderer now emits a PER-RENDER TOKEN
# (<<<ZMEM_UNTRUSTED_FENCE:<token>>>), so the strip must accept the
# token grammar as well as the legacy canonical literal — otherwise a
# token-form block survives the strip (PR #534 feedback F-04).
_FENCE_BEGIN_PREFIX = "<<<ZMEM_UNTRUSTED_FENCE"
_FENCE_END_PREFIX = "<<<END_ZMEM_UNTRUSTED_FENCE"
_FENCE_BLOCK = re.compile(
    re.escape(_FENCE_BEGIN_PREFIX) + r"(?::[0-9A-Za-z]{1,32})?"
    + re.escape(">>>")
    + r".*?"
    + re.escape(_FENCE_END_PREFIX) + r"(?::[0-9A-Za-z]{1,32})?"
    + re.escape(">>>"),
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


# issue #439: read-only search misses that should never cost the parent a
# reflection turn. Bash is trivial only when EVERY &&/;/| segment's first
# token is one of these readers AND nothing in the line can write or
# execute (no redirection/substitution characters, no write-capable or
# command-executing option -- including attached --opt=value forms).
_READ_ONLY_HEADS = frozenset(
    {"grep", "rg", "find", "ls", "head", "tail", "wc", "cat"})
_WRITE_EXEC_OPTION_PREFIXES = (
    "-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
    "-fprintf", "-fls", "-shred", "-truncate", "--pre", "-pre",
    "--pre-exec",
)
# A Read failure is a read-only miss only when the error names a missing
# path; permission/IO failures keep today's sidecar behavior (issue #439
# AC1/AC3 boundary, plan-critic round 2).
_NONEXISTENT_PATH_MARKERS = (
    "does not exist", "no such file", "not found", "enoent",
)


def classify_failure(detail: Mapping[str, object]) -> str:
    """Classify one failure detail as ``"trivial"``, ``"signal"``, or
    ``"other"`` (issue #439).

    Shared by the transcript path (zmem-subagent-reflect.sh) and the ZCode
    database path (storelib/mine.py verdict annotation) so both substrates
    agree per failure. Dependencies stay local to this module: no storelib
    import (importing storelib pulls the whole store package), runner
    detection is infer_signal's, and the reader/option sets live here.

    - signal: a recognized runner failure (infer_signal test/compile/lint
      on the whole command or any one segment);
    - trivial: a Grep/Glob error; a Read whose error names a nonexistent
      path; a Bash non-zero whose every segment is a bare reader command
      with no redirection, substitution, or write/exec option;
    - other: everything else (unrecognized runners like ``npx vitest
      run``, Edit/Write failures, conservative rejections). ``other``
      preserves today's behavior -- the sidecar still fires.
    """
    try:
        tool = _normalized(detail.get("tool"))
        command = "" if detail.get("command") is None else str(
            detail.get("command"))
        error = _normalized(detail.get("error"))
        # Structural conservatism (implementation-review r1 F3): a
        # command the segment rules cannot fully enumerate is NEVER
        # trivial. Multi-line commands (the detail carries the raw line
        # structure) and any single "&" (backgrounding — not the "&&"
        # separator) are rejected outright: a reader line followed by a
        # runner or destructive line must not be filtered.
        if command and ("\n" in command or "\r" in command
                        or re.search(r"(?<!&)&(?!&)", command)):
            return "other"
        segments = re.split(r"&&|;|\|", command) if command else []
        if infer_signal(command) in ("test", "compile", "lint"):
            return "signal"
        for segment in segments:
            if infer_signal(segment) in ("test", "compile", "lint"):
                return "signal"
        if tool in ("grep", "glob"):
            return "trivial"
        if tool == "read":
            # PRR-010a: the marker must not be satisfied by a path or a
            # permission/network diagnostic that merely CONTAINS the
            # phrase ("The network path was not found", "permission
            # denied, open '<path containing not found>'"). Require the
            # phrase outside any echoed path and reject diagnostic
            # families that are real failures.
            if any(marker in error for marker in _NONEXISTENT_PATH_MARKERS):
                if not any(word in error for word in
                           ("permission", "denied", "eacces", "eio",
                            "network path", "errno")):
                    path_text = _normalized(detail.get("path"))
                    if not (path_text and path_text in error
                            and error.index(path_text)
                            < min(error.index(m)
                                  for m in _NONEXISTENT_PATH_MARKERS
                                  if m in error)):
                        return "trivial"
            return "other"
        if tool == "bash" and command:
            if any(ch in command for ch in ("$", "`", ">", "<")):
                return "other"
            for segment in segments:
                try:
                    tokens = shlex.split(segment, posix=True)
                except (TypeError, ValueError):
                    return "other"
                if not tokens or tokens[0] not in _READ_ONLY_HEADS:
                    return "other"
                for token in tokens[1:]:
                    lowered = token.lower()
                    if any(lowered.startswith(prefix) for prefix in
                           _WRITE_EXEC_OPTION_PREFIXES):
                        return "other"
            return "trivial"
        return "other"
    except Exception:
        # Fail-open is the conservative direction: an unexpected shape
        # keeps today's behavior (sidecar written), never silence.
        return "other"


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
