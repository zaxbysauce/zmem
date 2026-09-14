#!/usr/bin/env python3
"""Fail-open standard-library rotation for hook telemetry logs.

Rotation is deliberately evidence-preserving: if discovery or a rename fails,
the caller continues appending and the log grows rather than losing bytes.
The active file is moved into a numbered segment, older segments are shifted,
and the new segment is stamped through a temporary file and atomic replace.
"""

from __future__ import annotations

import os
import re
import sys
import time


BG_LOG_DEFAULT_MAX_BYTES = 262144
DEFAULT_MAX_BYTES = BG_LOG_DEFAULT_MAX_BYTES
DEFAULT_ROTATIONS = 3
_SEGMENT_RE = re.compile(r"^(?P<base>.+)\.(?P<num>[1-9][0-9]*)$")
_MARKER_PREFIX = "# zmem-seq="
_MARKER_RE = re.compile(r"^# zmem-seq=(\d+) rotated_at=(\d+)$")


def max_bytes(default: int = BG_LOG_DEFAULT_MAX_BYTES) -> int:
    raw = os.environ.get("ZMEM_BG_LOG_MAX_BYTES", "")
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def rotations(default: int = DEFAULT_ROTATIONS) -> int:
    raw = os.environ.get("ZMEM_LOG_ROTATIONS", "")
    try:
        value = int(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def split_segment(path):
    match = _SEGMENT_RE.match(str(path))
    if not match:
        return None
    return match.group("base"), int(match.group("num"))


def iter_segments(path):
    """Return existing numbered segments in ascending number order.

    Directory-listing failure is intentionally raised to the writer. Treating
    it as an empty family could clobber an existing ``.1`` segment.
    """
    path = str(path)
    parent = os.path.dirname(path) or "."
    base = os.path.basename(path)
    found = []
    for name in os.listdir(parent):
        split = split_segment(name)
        if split and split[0] == base:
            found.append((split[1], os.path.join(parent, name)))
    found.sort()
    return [segment for _number, segment in found]


def _max_marker_seq(path: str) -> int:
    best = 0
    try:
        segments = iter_segments(path)
    except OSError:
        return 0
    for segment in segments:
        try:
            with open(segment, "r", encoding="utf-8", errors="replace") as handle:
                first = handle.readline(256).strip()
        except OSError:
            continue
        match = _MARKER_RE.match(first)
        if match:
            try:
                best = max(best, int(match.group(1)))
            except ValueError:
                pass
    return best


def _stamp_segment(path: str, seq_base: int) -> None:
    """Atomically prepend the next unique family sequence marker."""
    temporary = path + ".tmp-stamp"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            body = handle.read()
        with open(temporary, "w", encoding="utf-8", newline="") as handle:
            handle.write("%s%d rotated_at=%d\n" %
                         (_MARKER_PREFIX, seq_base + 1, int(time.time())))
            handle.write(body)
        os.replace(temporary, path)
    except OSError:
        try:
            os.remove(temporary)
        except OSError:
            pass


def rotate_on_append(path, max_bytes_value=None, rotations_value=None) -> bool:
    """Rotate an over-cap append-only file without destructive truncation."""
    path = str(path)
    cap = max_bytes_value if max_bytes_value is not None else max_bytes()
    keep = rotations_value if rotations_value is not None else rotations()
    try:
        if os.path.getsize(path) <= cap:
            return False

        # Discover the family before touching it; listing failure means no
        # mutation, preserving the failure-means-growth-never-loss contract.
        segments = iter_segments(path)
        for segment in reversed(segments):
            base, number = split_segment(segment)
            os.replace(segment, "%s.%d" % (base, number + 1))
        os.replace(path, path + ".1")

        # The highest surviving segment is oldest. Evict only after the new
        # family exists, and keep the newest `keep` segments.
        survivors = iter_segments(path)
        for victim in survivors[max(0, keep):]:
            try:
                os.remove(victim)
            except OSError:
                pass

        # The marker sequence is based on the complete family, including the
        # freshly moved .1, then written via temp + atomic replace.
        _stamp_segment(path + ".1", _max_marker_seq(path))
        with open(path, "a", encoding="utf-8"):
            pass
        return True
    except OSError:
        return False


if __name__ == "__main__":
    try:
        if len(sys.argv) > 1:
            rotate_on_append(sys.argv[1])
    except Exception:
        pass
