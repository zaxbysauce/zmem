"""False-injection counter — the other direction of the gate (issue #129).

The miss-rate join (#94) measures the miss direction: failures that recall
FAILED to surface. This module measures the false-injection direction: of
the rows the hooks actually INJECTED, how many were never referenced by any
later operation, prompt, or captured failure in the same session. Both
rates print together in ``doctor --miss-rate`` so neither Phase 2 narrowing
(which risks re-creating misses) nor Phase 4 widening (which risks more
false injections) can happen against half the evidence.

Conservative and auditable, no LLM judgment:
- Denominator: every injected decision LINE (``status=injected`` with a
  non-empty ``ids=`` list), counted ONCE — the same memory id re-injected
  at two moments is two denominator lines, never two entries per moment and
  never one entry double-counted across moments.
- A line counts as USED when any same-session reference event strictly
  AFTER the line's timestamp (a) contains one of the line's memory ids
  literally, or (b) shares >= ``min_token_overlap`` distinct
  ``derive_ops_tokens`` tokens with the injected row's store content.
- Reference events: captured failures (the same mined rows the miss-rate
  join consumes), the per-session ops ring, transcript user prompts mined
  from the same JSONL transcripts the join consumes, and transcript-
  derived failure rows (already merged into the join's failure list
  upstream). Sid discipline mirrors the PRR-004 rule the disabled bucket
  follows: a reference from session B never credits session A's lines;
  sid-less legacy lines (``sid=unknown`` or missing) weak-match any
  session, mirroring the surfaced_sid/surfaced_legacy split.
- Sid normalization (review PRR-017, documented): the shared
  ``[^A-Za-z0-9._-]`` + 128-char rule is many-to-one, so distinct crafted
  session ids can collide into one bucket and cross-pollinate ring reads.
  Inherent to the writer-side sanitize rule (paths and log labels share
  it); accepted for this telemetry surface.

Legacy decision lines (no ``moment=`` field, pre-#129) are NOT excluded:
they report under the ``legacy`` bucket so old windows stay measurable,
with a caveat stating the legacy share.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]

# Same sanitize rule as miss_rate.sanitize_sid / the writers.
_SID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

LEGACY_MOMENT = "legacy"

# Injected-line predicate: writer A's explicit reason=injected OR writer
# B's legacy shape (status=injected, no reason field) — the SAME filter the
# miss-rate join uses (see miss_rate.run_miss_report; filtering on
# reason=injected alone silently dropped session-start injections once).
_INJECTED_REASONS = ("injected",)


def _is_injected_line(ln: dict) -> bool:
    reason = ln.get("reason")
    if reason is not None:
        # Review PRR-010: the explicit-reason branch must not count a
        # contradictory writer bug (status=silent + reason=injected);
        # legacy writer-B lines carry no reason and keep the status path.
        return reason in _INJECTED_REASONS and ln.get("status") in (
            None, "injected")
    return ln.get("status") == "injected"


def _norm_sid(sid) -> str:
    if not sid:
        return ""
    return _SID_SAFE_RE.sub("_", str(sid))[:128]


def _moment_of(ln: dict) -> str:
    moment = ln.get("moment")
    if isinstance(moment, str) and moment.strip():
        # Same charset rule the writers apply (review PRR-014): a forged or
        # hand-edited line must not smuggle metacharacters into per_moment
        # report keys or the doctor summary text.
        return _SID_SAFE_RE.sub("_", moment.strip())[:32]
    return LEGACY_MOMENT


def _read_ring_events(data_dir, sid: str) -> list:
    """[(ts, text)] from the per-session ops ring, oldest first.

    Reads the JSONL ring directly (read_ops_ring drops the ts the strictly-
    after comparison needs). Fail-open: missing/torn/corrupt -> [].
    """
    if not data_dir or not sid:
        return []
    safe = _norm_sid(sid)
    if not safe:
        return []
    path = os.path.join(str(data_dir), "ops", safe + ".log")
    events = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                desc = obj.get("ops") or ""
                if not isinstance(desc, str) or not desc:
                    continue
                try:
                    ts = int(obj.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0
                events.append((ts, desc))
    except OSError:
        return []
    return events


def _row_tokens(conn, ids, cache: dict) -> dict:
    """{id: [tokens]} for the injected rows, read-only. A row deleted
    between injection and report yields no entry (id-arm matching only)."""
    needed = [i for i in ids if i and i not in cache]
    if needed and conn is not None:
        try:
            marks = ",".join("?" * len(needed))
            rows = conn.execute(
                "SELECT id, content FROM memory WHERE id IN (%s)" % marks,
                needed).fetchall()
            for r in rows:
                rid, content = r[0], r[1]
                cache[rid] = _derive(content or "")
        except Exception:
            pass  # fail-open: content arm degrades, id arm still works
    return cache


def _derive(text: str) -> list:
    try:
        from storelib.ops_tokens import derive_ops_tokens
    except Exception:
        try:
            from ops_tokens import derive_ops_tokens  # type: ignore
        except Exception:
            return []
    try:
        return derive_ops_tokens(text or "")
    except Exception:
        return []


def _iso_to_epoch_s(value) -> int:
    """ISO-8601 transcript timestamp -> epoch seconds (0 on anything
    unusable). Local to this module so the counter never imports the
    heavier miss_rate stack."""
    if not isinstance(value, str) or not value:
        return 0
    try:
        from datetime import datetime, timezone
        raw = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return 0


def _read_prompt_events(transcripts) -> list:
    """[(ts, text, sid)] user-prompt reference events mined from transcript
    JSONL files (review PRR-007: prompts are the second reference surface
    of the contract — the parameter used to be accepted but inert, so a
    failure whose only same-session evidence was the operator's own prompt
    quoting the row read as a false injection). Record shape mirrors
    ``miss_rate.failures_from_transcript_rich``: ``message.content`` is a
    list of typed blocks (only ``text`` blocks are prompts — tool_result
    blocks belong to the captured-failure lane) or a bare string;
    ``timestamp`` is ISO-8601; the session id rides ``session_id`` OR
    ``sessionId`` and feeds the same sid partition as every other
    reference. Never raises; [] per unreadable file."""
    import glob as _glob
    events = []
    for pattern in transcripts or ():
        try:
            matches = _glob.glob(str(pattern), recursive=True)
        except Exception:
            continue
        for path in matches:
            try:
                with open(path, "r", encoding="utf-8",
                          errors="replace") as fh:
                    raw = fh.readlines()
            except OSError:
                continue
            for line in raw:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                msg = obj.get("message")
                content = msg.get("content") if isinstance(msg, dict) else None
                texts = []
                if isinstance(content, str):
                    texts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if (isinstance(block, dict)
                                and block.get("type") == "text"
                                and isinstance(block.get("text"), str)):
                            texts.append(block["text"])
                if not texts:
                    continue
                sid_raw = obj.get("session_id") or obj.get("sessionId")
                sid = _norm_sid(sid_raw if isinstance(sid_raw, str) else "")
                ts = _iso_to_epoch_s(obj.get("timestamp"))
                for text in texts:
                    text = text.strip()
                    if text:
                        events.append((ts, text, sid))
    return events


def build_false_injection_report(decision_lines, conn=None, data_dir=None,
                                 failure_rows=(), transcripts=(),
                                 min_token_overlap: int = 2) -> dict:
    """Counter over parsed decision lines (see module docstring).

    ``decision_lines``: the list ``parse_bg_log`` returns (with ``moment``).
    ``conn``: read-only sqlite connection to the store (row content arm).
    ``failure_rows``: the join's mined failures (dicts with session_id,
    ts_s, tool, operation, error). ``transcripts``: resolved transcript
    JSONL paths whose user prompts are mined as reference events (review
    PRR-007). Never raises.
    """
    try:
        threshold = max(1, int(min_token_overlap))
    except (TypeError, ValueError):
        threshold = 2
    caveats = []

    # Distinct reference events: (ts, text, sid).
    references = []
    for f in failure_rows or ():
        if not isinstance(f, dict):
            continue
        text = " ".join(str(f.get(k) or "") for k in
                        ("operation", "error", "tool")).strip()
        if not text:
            continue
        # Review PRR-015: ts fields from non-standard callers are guarded,
        # keeping the documented "Never raises" contract true.
        try:
            ts_val = int(f.get("ts_s") or 0)
        except (TypeError, ValueError):
            ts_val = 0
        references.append((ts_val, text, _norm_sid(f.get("session_id"))))
    references.extend(_read_prompt_events(transcripts))
    ring_sids = {_norm_sid(ln.get("sid")) for ln in decision_lines
                 if isinstance(ln, dict) and ln.get("sid")}
    for sid in sorted(ring_sids):
        if not sid or sid == "unknown":
            continue
        events = _read_ring_events(data_dir, sid)
        if not events:
            continue
        references.extend((ts, text, sid) for ts, text in events)
        if len(events) >= 64:
            caveats.append(
                "ops ring for sid %s contributed %d events (ring is "
                "capped; older references are not visible)" % (sid, len(events)))

    # Group references by sid for the PRR-004-style partition.
    by_sid: dict = {}
    for ts, text, sid in references:
        by_sid.setdefault(sid, []).append((ts, text))

    # Row-content token cache across lines.
    token_cache: dict = {}
    derive_cache: dict = {}

    def _derive_cached(text: str) -> list:
        # Review PRR-011: each reference text is tokenized once, not once
        # per candidate injected id.
        toks = derive_cache.get(text)
        if toks is None:
            toks = _derive(text)
            derive_cache[text] = toks
        return toks

    def _bucket() -> dict:
        return {"injected": 0, "used": 0, "false": 0, "false_rate": None}

    overall = _bucket()
    per_moment: dict = {}
    legacy_lines = 0

    for ln in decision_lines:
        if not isinstance(ln, dict) or not _is_injected_line(ln):
            continue
        ids = [i for i in (ln.get("ids") or []) if i]
        if not ids:
            continue  # an injected line with no rows is not a denominator
        moment = _moment_of(ln)
        if moment == LEGACY_MOMENT:
            legacy_lines += 1
        bucket = per_moment.setdefault(moment, _bucket())
        overall["injected"] += 1
        bucket["injected"] += 1

        sid = _norm_sid(ln.get("sid"))
        if sid and sid != "unknown":
            refs = by_sid.get(sid, [])
        else:
            # Sid-less legacy line: weak-match any session (a reference
            # from ANY session still proves the row reached an operator).
            refs = [(ts, text) for evs in by_sid.values()
                    for (ts, text) in evs]
        # Review PRR-015: hostile/non-standard ts values never raise.
        try:
            ts_line = int(ln.get("ts") or 0)
        except (TypeError, ValueError):
            ts_line = 0
        later = [(ts, text) for ts, text in refs if ts and ts > ts_line]
        used = False
        if later:
            tokens_by_id = _row_tokens(conn, ids, token_cache)
            id_lits = [str(i).lower() for i in ids]
            for _ts, text in later:
                low = text.lower()
                if any(i in low for i in id_lits):
                    used = True
                    break
            if not used:
                for i in ids:
                    row_toks = set(tokens_by_id.get(i) or ())
                    if not row_toks:
                        continue
                    for _ts, text in later:
                        shared = row_toks.intersection(_derive_cached(text))
                        if len(shared) >= threshold:
                            used = True
                            break
                    if used:
                        break
        if used:
            overall["used"] += 1
            bucket["used"] += 1
        else:
            overall["false"] += 1
            bucket["false"] += 1

    if legacy_lines:
        caveats.append(
            "%d injected line(s) carry no moment= field (pre-#129 logs); "
            "they report under the legacy bucket" % legacy_lines)
    if not references:
        caveats.append(
            "no reference events found (no captured failures, no ops-ring "
            "events) — every injected line reads as false; the rate is "
            "meaningless without same-session activity evidence")

    def _rates(b: dict) -> dict:
        if b["injected"]:
            b["false_rate"] = round(b["false"] / b["injected"], 4)
        return b

    return {
        "overall": _rates(overall),
        "per_moment": {m: _rates(b) for m, b in sorted(per_moment.items())},
        "min_token_overlap": threshold,
        "caveats": caveats,
    }
