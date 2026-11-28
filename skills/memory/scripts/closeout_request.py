"""Small, lock-protected state machine for the closeout request hooks.

The request and Stop hooks intentionally share this module: the sidecars live
beside the canonical store, not beside whichever plugin-data variable happened
to be visible to a particular hook invocation.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from typing import Iterator

from host import resolve_store_path
from knowledge.locking import try_lock_nb, unlock


_LOCK_ATTEMPTS = 20
_LOCK_DELAY_SECONDS = 0.05
_VALID_DISPOSITIONS = {"not_requested"}
_SIDECAR_READ_LIMIT = 16 * 1024


@dataclass(frozen=True)
class RequestState:
    """The active request as observed from its marker file."""

    nonce: str
    blocks: int


def _stem(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def ops_paths(session_id: str) -> dict[str, Path]:
    """Return the closeout sidecars under the canonical store's ``ops`` dir."""

    ops = resolve_store_path().parent / "ops"
    stem = _stem(session_id)
    return {
        "ops": ops,
        "requested": ops / f"{stem}.closeout-requested",
        "disposition": ops / f"{stem}.closeout-request-disposition.json",
        "lock": ops / f"{stem}.closeout-request.lock",
        "wrote": ops / f"{stem}.wrote",
    }


@contextmanager
def _locked(paths: dict[str, Path]) -> Iterator[None]:
    """Use the existing kernel byte-lock discipline with a finite retry budget."""

    paths["ops"].mkdir(parents=True, exist_ok=True)
    fd = os.open(str(paths["lock"]), os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        for attempt in range(_LOCK_ATTEMPTS):
            if try_lock_nb(fd):
                acquired = True
                break
            if attempt + 1 < _LOCK_ATTEMPTS:
                time.sleep(_LOCK_DELAY_SECONDS)
        if not acquired:
            raise TimeoutError("closeout request sidecar remained locked")
        yield
    finally:
        if acquired:
            unlock(fd)
        os.close(fd)


def _atomic_write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _encoded_request(nonce: str, blocks: int) -> str:
    # This stays text-shaped for hook and fixture compatibility.  Appending a
    # later ``blocks:`` line is understood as the current counter, rather than
    # invalidating an arm nonce written by an earlier implementation.
    return f"armed\nnonce:{nonce}\nblocks:{blocks}\n"


def _read_text_cap(path: Path) -> str | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = handle.read(_SIDECAR_READ_LIMIT + 1)
    except FileNotFoundError:
        return None
    return value if len(value) <= _SIDECAR_READ_LIMIT else None


def _read_request_path(path: Path) -> RequestState | None:
    raw = _read_text_cap(path)
    if raw is None:
        return None

    # Preserve historical ``armed\nblocks:N\n`` marker support.  Its content
    # hash is a synthetic nonce so a later writer cannot be cleared by a stale
    # gate which inspected an earlier legacy marker.
    if raw.splitlines()[:1] != ["armed"]:
        return None
    blocks = 0
    nonce = None
    for line in raw.splitlines()[1:]:
        if line.startswith("blocks:"):
            try:
                blocks = max(0, int(line.partition(":")[2]))
            except ValueError:
                return None
        elif line.startswith("nonce:"):
            candidate = line.partition(":")[2]
            if candidate:
                nonce = candidate
    if nonce:
        return RequestState(nonce=nonce, blocks=blocks)
    return RequestState(
        nonce="legacy-" + hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        blocks=blocks,
    )


def read_request(session_id: str) -> RequestState | None:
    paths = ops_paths(session_id)
    state = _read_request_path(paths["requested"])
    if state is not None and _disposition_for(paths) == state.nonce:
        # Release persists before unlink so a concurrent Stop hook does not
        # revive, delete, or increment the released arm.
        return None
    return state


def _is_explicit_request(prompt: str) -> bool:
    text = prompt.strip()
    if not text or text.startswith(("`", '"', "'", ">")):
        return False
    # A leading code fence or quoted example is documentation, not an action.
    if text.startswith("~~~") or text.startswith("```"):
        return False
    first = text.splitlines()[0].strip().lower()
    normalized = re.sub(r"\s+", " ", first)
    polite = re.match(
        r"^(?:could|would|can) you (?:please )?(?:run|perform|do) (?:a |the )?(?:session )?closeout(?: (?:for )?(?:this )?session)?\s*\??$",
        normalized,
    )
    if polite:
        return True
    if "?" in first:
        return False
    if re.match(r"^(?:do not|don't|dont|never)\b", normalized):
        # This is an explicit closeout instruction, despite the leading
        # negative: it means the session may not end without a closeout.
        return bool(
            re.match(
                r"^(?:do not|don't|dont|never)\s+(?:end|finish|wrap up)\b.*\bwithout\b.*\bcloseout\b",
                normalized,
            )
        )
    aliases = (
        r"(?:run|perform|do|start)\s+(?:the\s+)?(?:session\s+)?closeout"
        r"(?:\s+(?:for\s+)?(?:this\s+)?session)?(?:\s+now)?",
        r"closeout(?:\s+(?:this|the)\s+session(?:\s+now)?)?",
        r"wrap(?:\s+|-)?up(?:\s+(?:this|the)?\s*session(?:\s+with)?(?:\s+closeout)?)?",
        r"(?:end\s+of|end|finish)\s+(?:this|the)?\s*session(?:\s+with)?(?:\s+closeout)?",
        r"capture\s+(?:what\s+we\s+learned|the\s+session\s+lessons)",
    )
    return any(re.match(r"^(?:please\s+)?" + alias + r"(?:[.!,:;]|$)", normalized) for alias in aliases)


def arm_request(session_id: str, prompt: str) -> bool:
    """Apply a user prompt to request state; return whether an arm remains."""

    paths = ops_paths(session_id)
    explicit = _is_explicit_request(prompt)
    if not explicit:
        # The overwhelmingly common prompt has no arm. Do not create an ops
        # directory or persistent per-session lock just to prove that fact.
        observed = _read_request_path(paths["requested"])
        if observed is None:
            return False
        clear_if_matches(session_id, observed.nonce)
        return False
    with _locked(paths):
        current = _read_request_path(paths["requested"])
        released_nonce = _disposition_for(paths)
        if current is not None and released_nonce == current.nonce:
            current = None
        # Do not reset a live counter or nonce for repeated requests.
        if current is not None:
            return True
        if not paths["wrote"].is_file():
            return False
        _atomic_write(paths["requested"], _encoded_request(secrets.token_hex(16), 0))
        # A new nonce is now durable, so retiring an old terminal record cannot
        # create a window in which the old marker looks live again.
        if released_nonce is not None:
            paths["disposition"].unlink(missing_ok=True)
        return True


def clear_if_matches(session_id: str, nonce: str) -> bool:
    """Clear only the exact arm observed by a gate invocation."""

    paths = ops_paths(session_id)
    with _locked(paths):
        if _disposition_for(paths) == nonce:
            return False
        current = _read_request_path(paths["requested"])
        if current is None or current.nonce != nonce:
            return False
        paths["requested"].unlink(missing_ok=True)
        return True


def increment_blocks(session_id: str, nonce: str) -> int | None:
    """Atomically increment the observed arm, or return ``None`` if it changed."""

    paths = ops_paths(session_id)
    with _locked(paths):
        if _disposition_for(paths) == nonce:
            return None
        current = _read_request_path(paths["requested"])
        if current is None or current.nonce != nonce:
            return None
        blocks = current.blocks + 1
        _atomic_write(paths["requested"], _encoded_request(current.nonce, blocks))
        return blocks


def release(session_id: str) -> bool:
    """Record an operator release of the current request arm.

    An invocation-scoped ``ZMEM_SESSION`` must agree with ``session_id``.  The
    terminal disposition is nonce-bound and survives marker deletion, so the
    CLI can report a meaningful no-op/release outcome without treating it as
    acknowledgement state.
    """

    invocation_session = os.environ.get("ZMEM_SESSION")
    if invocation_session and invocation_session != session_id:
        return False
    paths = ops_paths(session_id)
    with _locked(paths):
        current = _read_request_path(paths["requested"])
        if current is None:
            return False
        if _disposition_for(paths) == current.nonce:
            return False
        _atomic_write(
            paths["disposition"],
            json.dumps(
                {"version": 1, "nonce": current.nonce, "disposition": "not_requested"},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        paths["requested"].unlink(missing_ok=True)
        return True


def disposition(session_id: str) -> str | None:
    """Return a validated terminal disposition without conflating it with ack."""

    paths = ops_paths(session_id)
    return _disposition_value(paths)


def _disposition_for(paths: dict[str, Path]) -> str | None:
    """Return the nonce of a well-formed terminal sidecar, if any."""

    raw = _read_text_cap(paths["disposition"])
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("disposition")
    nonce = parsed.get("nonce")
    if isinstance(value, str) and value in _VALID_DISPOSITIONS and isinstance(nonce, str) and nonce:
        return nonce
    return None


def _disposition_value(paths: dict[str, Path]) -> str | None:
    raw = _read_text_cap(paths["disposition"])
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("disposition")
    nonce = parsed.get("nonce")
    if isinstance(value, str) and value in _VALID_DISPOSITIONS and isinstance(nonce, str) and nonce:
        return value
    return None
