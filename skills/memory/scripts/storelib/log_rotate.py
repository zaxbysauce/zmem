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
  deleted). Each rotation stamps the NEW ``.1`` with a ``# zmem-seq=`` and
  ``rotated_at=`` marker so a segment is self-describing even when copied
  out of the rotation family; parsers order segments by filename number,
  and marker lines never match decision-line regexes.
- Retention direction (review PRR-001): eviction deletes from the TAIL of
  the ascending segment list — the OLDEST content. Normal operation keeps
  a contiguous ``.1..N`` family; a hand-pruned or partially-shifted family
  may carry number gaps, which discovery and eviction handle positionally
  (by ascending-number list order), never by content.
- Best-effort by contract: any OSError leaves every pre-call byte intact
  (in the active file or some segment) and the caller appends anyway. A
  failure mid-shift can leave transitional numbering; the next rotation
  renormalizes retention (the shift is collision-free for any family
  shape and the family is re-listed before eviction). Sustained rotation
  failure therefore degrades to unbounded growth, NEVER to evidence loss
  — the fail-open direction the hooks already use ("never let the audit
  log block the hook"), pointed the evidence-preserving way.
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
_MARKER_RE = re.compile(r"^# zmem-seq=(\d+) rotated_at=(\d+)$")


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

    Raises OSError when the directory listing fails — the empty-family and
    the listing-failed cases must stay distinguishable (review PRR-003:
    swallowing the error made the writer rotate blind and clobber a
    pre-existing ``.1``). Readers decide their own failure direction:
    ``parse_bg_log`` degrades to the active file; the writer aborts the
    rotation, touching nothing.
    """
    path = str(path)
    base = os.path.basename(path)
    parent = os.path.dirname(path) or "."
    found = []
    names = os.listdir(parent)
    for name in names:
        split = split_segment(name)
        if split and split[0] == base:
            found.append((split[1], os.path.join(parent, name)))
    found.sort()
    return [p for _n, p in found]


def _max_marker_seq(path: str) -> int:
    """Highest ``zmem-seq`` among the family's markers (0 when none carry
    one). Bounded read: the first line of each segment only. Hand-edited
    markers are honored as-is — the next stamp is max+1 either way."""
    best = 0
    try:
        segments = iter_segments(path)
    except OSError:
        return 0
    for seg in segments:
        try:
            with open(seg, "r", encoding="utf-8", errors="replace") as fh:
                first = fh.readline(256).strip()
        except OSError:
            continue
        m = _MARKER_RE.match(first)
        if m:
            try:
                best = max(best, int(m.group(1)))
            except ValueError:
                continue
    return best


def _stamp_segment(path: str, seq_base: int) -> None:
    """Prepend the rotation marker to the freshly rotated ``.1``. The seq
    is a monotonic rotation-generation counter — one higher than the
    highest marker already in the family (the caller passes that scan in
    as ``seq_base``, computed against the base log so the family is
    discovered correctly) — so every segment self-describes with a unique
    seq plus its true rotation time; gaps in the sequence mean a segment
    was evicted or the family pruned. Crash-safe: the marker+body go to a
    temp file that cannot match the segment-name pattern, then
    ``os.replace`` moves it into place, so a crash mid-stamp leaves the
    segment content intact (review PRR-004). Best-effort: a failed stamp
    leaves the segment unmarked (parseable, just unmarked)."""
    tmp = path + ".tmp-stamp"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            body = fh.read()
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write("%s%d rotated_at=%d\n" % (
                _MARKER_PREFIX, seq_base + 1, int(time.time())))
            fh.write(body)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def rotate_on_append(path, max_bytes_value=None, rotations_value=None) -> bool:
    """Rotate ``path`` when it is over the cap; return True when a rotation
    happened. Best-effort — never raises, never destroys content on
    failure: every byte that existed before the call still exists in the
    active file or some segment afterwards. Call before each append; a
    no-op when under the cap."""
    path = str(path)
    cap = max_bytes_value if max_bytes_value is not None else max_bytes()
    keep = rotations_value if rotations_value is not None else rotations()
    try:
        if os.path.getsize(path) <= cap:
            return False
        # Listing failure aborts before anything is touched (review
        # PRR-003) — iter_segments raises and the outer except returns.
        segments = iter_segments(path)
        # Shift oldest-first by segment NUMBER+1: descending number order
        # makes every target free-or-just-vacated for ANY family shape
        # (contiguous, gapped, hand-pruned), so no pre-deletion is needed
        # (review PRR-002) and a mid-shift failure leaves all content in
        # place, possibly with gapped numbering the next rotation handles.
        for seg in reversed(segments):
            base, num = split_segment(seg)
            os.replace(seg, "%s.%d" % (base, num + 1))
        os.replace(path, path + ".1")
        # Evict only AFTER the family is complete (.1 exists): re-list and
        # drop from the TAIL of the ascending list — the highest numbers
        # are the OLDEST content (review PRR-001: the original code sliced
        # the front, destroying the newest rotated segments).
        survivors = iter_segments(path)
        for victim in survivors[max(0, keep):]:
            try:
                os.remove(victim)
            except OSError:
                pass
        # Stamp the NEW .1 with the family's next generation. The seq scan
        # must run against the BASE log path (passing the .1 path here
        # would discover a fam.log.1.N family — nothing — and seq would
        # never advance; caught by the monotonic-generation test).
        _stamp_segment(path + ".1", _max_marker_seq(path))
        # Fresh empty active file so the caller's append lands in a clean
        # file and size probes between rotation and append see ~0 bytes.
        with open(path, "a", encoding="utf-8"):
            pass
        return True
    except OSError:
        return False
