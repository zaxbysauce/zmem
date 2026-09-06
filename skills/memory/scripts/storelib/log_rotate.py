"""Rotation (not truncation) for append-only telemetry logs (issue #129).

The background log and the decision log are append-only audit surfaces: the
decision log is the evidence substrate for the miss-rate join and the
false-injection counter, so its growth control must never destroy prior
content. The pre-#129 control was truncate-to-empty at the size cap — one
burst wiped every accumulated decision line (proven in the #129 trace:
294580 bytes / 2800 lines reduced to 88 bytes / 1 line by a single hook
drive). This module replaces that with bounded rotation.

Contract:
- ``rotate_on_append(path)`` is called BEFORE every append. When the file is
  over the cap, its active content becomes ``path.1`` and older segments
  shift toward ``path.N`` (the oldest beyond ``ZMEM_LOG_ROTATIONS`` is
  deleted). Each rotated segment is rewritten with a ``# zmem-seq=`` marker
  line at the top so a segment is self-describing even when copied out of
  the rotation family; parsers order segments by filename number, and
  marker lines never match decision-line regexes.
- Best-effort by contract: any OSError leaves the log exactly as it was and
  the caller appends anyway. Sustained rotation failure therefore degrades
  to unbounded growth, NEVER to evidence loss — the fail-open direction the
  hooks already use ("never let the audit log block the hook"), pointed the
  evidence-preserving way.
- Windows: renames use ``os.replace`` (atomic overwrite of an existing
  target). A concurrent writer mid-shift may have its line land in a
  rotated segment or be lost — the same torn-line tolerance the append-only
  log always had; writers open-append-close per line, so no long-lived
  in-process handles interact with the shift.

Env knobs (shared with the pre-#129 behavior so operator configs survive):
- ``ZMEM_BG_LOG_MAX_BYTES`` — cap per active file (default 262144, kept).
- ``ZMEM_LOG_ROTATIONS`` — kept segments (default 3; values < 1 fall back
  to the default, mirroring the cap-knob validation).
"""

from __future__ import annotations

import os
import re
import time

BG_LOG_DEFAULT_MAX_BYTES = 262144
DEFAULT_ROTATIONS = 3

_SEGMENT_RE = re.compile(r"^(?P<base>.+)\.(?P<num>[1-9][0-9]*)$")
_MARKER_PREFIX = "# zmem-seq="


def max_bytes(default: int = BG_LOG_DEFAULT_MAX_BYTES) -> int:
    """The per-file cap (ZMEM_BG_LOG_MAX_BYTES), validated like the
    pre-#129 hook helper: non-int or non-positive values fall back."""
    raw = os.environ.get("ZMEM_BG_LOG_MAX_BYTES", "")
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def rotations(default: int = DEFAULT_ROTATIONS) -> int:
    """How many rotated segments to keep (ZMEM_LOG_ROTATIONS)."""
    raw = os.environ.get("ZMEM_LOG_ROTATIONS", "")
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value > 0 else default


def split_segment(path) -> "tuple[str, int] | None":
    """``('<base>', <n>)`` for a rotated-segment name, else None.

    ``zmem-bg.log.2`` -> ``('zmem-bg.log', 2)``; ``zmem-bg.log`` -> None.
    """
    m = _SEGMENT_RE.match(str(path))
    if not m:
        return None
    return m.group("base"), int(m.group("num"))


def iter_segments(path):
    """All existing rotated segments of ``path``, by ascending segment
    number (``path.1`` first — the MOST RECENTLY rotated segment — through
    ``path.N``, the oldest surviving content). The active file is not
    included; callers append it last when they want newest-last ordering,
    though every consumer is ts-keyed and order-independent.

    Segments are discovered from the directory listing so a hand-truncated
    family (N raised then lowered) still reads completely.
    """
    path = str(path)
    base = os.path.basename(path)
    parent = os.path.dirname(path) or "."
    found = []
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    for name in names:
        split = split_segment(name)
        if split and split[0] == base:
            found.append((split[1], os.path.join(parent, name)))
    found.sort()
    return [p for _n, p in found]


def _stamp_segment(path: str) -> None:
    """Prepend the rotation marker to a segment. Best-effort: a failed
    stamp leaves the segment content intact (parseable, just unmarked)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write("%s%d rotated_at=%d\n" % (
                _MARKER_PREFIX, split_segment(path)[1], int(time.time())))
            fh.write(body)
    except OSError:
        pass


def rotate_on_append(path, max_bytes_value=None, rotations_value=None) -> bool:
    """Rotate ``path`` when it is over the cap; return True when a rotation
    happened. Best-effort — never raises, never destroys content on
    failure. Call before each append; a no-op when under the cap."""
    path = str(path)
    cap = max_bytes_value if max_bytes_value is not None else max_bytes()
    keep = rotations_value if rotations_value is not None else rotations()
    try:
        if os.path.getsize(path) <= cap:
            return False
        segments = iter_segments(path)
        # Drop the oldest beyond the keep count (only after the shift below
        # creates a new .1, so the pre-shift overage is keep-1).
        overage = len(segments) + 1 - keep
        for victim in segments[:max(0, overage)]:
            try:
                os.remove(victim)
            except OSError:
                pass
        # Shift .1 -> .2 -> ... oldest first so nothing is overwritten by
        # an occupied slot, then the active file becomes .1.
        existing = iter_segments(path)
        for seg in reversed(existing):
            split = split_segment(seg)
            os.replace(seg, "%s.%d" % (split[0], split[1] + 1))
        os.replace(path, path + ".1")
        _stamp_segment(path + ".1")
        # Fresh empty active file so the caller's append lands in a clean
        # file and size probes between rotation and append see ~0 bytes.
        with open(path, "a", encoding="utf-8"):
            pass
        return True
    except OSError:
        return False
