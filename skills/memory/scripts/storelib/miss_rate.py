"""Read-only miss-rate measurement (issue #94).

Measures the number the proactive-memory epic (#100) gates on: "a failure
occurred in a session, a matching memory existed in the store, and nothing
surfaced at that moment." This module MEASURES, it never tunes — no ranking
changes, no floor changes, no schema changes, and by construction no writes:

  - the zmem store is opened with a ``mode=ro`` URI and the recall runs with
    ``no_telemetry=True`` (CLI ``--no-bump`` alone still bumps
    ``surfaced_count`` — recall.py's bump path writes unless telemetry is
    fully disabled), so the report cannot modify the store file at all.
    (Opening a WAL-mode store read-only may create EMPTY ``-wal``/``-shm``
    bookkeeping files next to it — standard SQLite read-open behavior for
    WAL-mode databases; zero data is ever written.);
  - ``connect()``/``_prepare_store()`` are never called, so a missing or
    older-schema store is reported as an error instead of being created or
    migrated (the report must never upgrade a real store);
  - the ZCode episodic db (failure side) is likewise opened ``mode=ro``;
  - the bg log and ops rings are plain file reads.

Definitions (pinned by the #94 spec, mirrored in SKILL.md):
  - ``missed``       — failure + floor-passing store match + no injection of
                       a matched row in the window ⇒ the miss rate counts it.
  - ``capture_gap``  — failure + NO store match (a write-side problem, not a
                       retrieval one; counted separately by design).
  - ``surfaced_sid`` — an injected bg-log line whose ``sid=`` proves it is
                       this session's decision carries a matched row id.
  - ``surfaced_legacy`` — same evidence but on a pre-#94 line (no ``sid=``):
                       window+id-overlap attribution only, ALWAYS reported
                       separately because it cannot prove session identity.
  - ``no_query``     — nothing derivable for the failure (no recovered
                       operation, no ring events): a measurement limitation,
                       NOT a capture gap.
  - ``miss_rate``            = missed / (missed + surfaced_sid + surfaced_legacy)
  - ``miss_rate_strict_sid`` = missed / (missed + surfaced_sid), null when no
                       sid-carrying lines exist at all (today's logs).

Recall mirroring: the join calls ``recall_memory`` exactly as the hook's
``store.py recall --no-bump`` does, with ONE deliberate pin —
``link_hops=0``. Link expansion (recall.py: link_hops >= 1) runs
independently of ``--no-bump`` and would pull one-hop associative neighbors
into the result set; the miss question is "did the store contain a row the
failure's QUERY matched", so the matched set here is exactly the
FTS/hybrid-ranked rows. Link-expanded rows the hook injected can still be
counted surfaced for themselves via bg-log id overlap, but they can never
manufacture a false miss (they are never in the matched set).

Importable both as a package member (``from storelib import miss_rate``)
and via sys.path on the storelib dir, mirroring the import tolerance of the
sibling modules.
"""

from __future__ import annotations

import ast
import contextlib
import glob as _glob
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1]

# Canonical session-id sanitize rule (mirrors ops_tokens._ring_path and the
# hook body's _pending_ops_path): a hostile session id must not be able to
# forge log structure or a ring filename.
_SID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

# One bg-log decision line, either writer shape:
#   writer A: [ts] zmem-hook status=.. reason=.. [omitted=N] ids=[..] all=[..] [tokens=a/b] [ops=N] [sid=..]
#   writer B: [ts] zmem-hook status=.. ids=[..] all=[..] [tokens=a/b] [sid=..]
# reason/omitted/tokens/ops/sid are all optional in the regex because writer B
# omits reason=/omitted=/ops= and every pre-#94 line omits sid=.
_BG_LINE_RE = re.compile(
    r"^\[(\d+)\] zmem-hook status=(\S+)"
    r"(?: reason=(\S+))?"
    r"(?: omitted=(\d+))?"
    r" ids=(\[[^\]]*\]) all=(\[[^\]]*\])"
    r"(?: tokens=(\S+))?"
    r"(?: ops=(\d+))?"
    r"(?: sid=(\S+))?"
    r"\s*$"
)

_HOME_ENV_VARS = ("ZMEM_STORE", "ZMEM_DATA",
                  "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")

# The join's recall pins (see module docstring): link_hops=0 keeps the
# matched set exactly the query-ranked rows.
_JOIN_RECALL_KWARGS = dict(no_bump=True, no_telemetry=True, link_hops=0)


def sanitize_sid(session_id: str) -> str:
    """Sanitize a session id for logging/joining (canonical ops-lane rule).

    ``[^A-Za-z0-9._-]`` → ``_``, cap 128, ``"unknown"`` when empty — the
    log-label fallback, deliberately distinct from the ring-path fallback
    (``"session"``) because this names a log field, not a file.
    """
    return _SID_SAFE_RE.sub("_", session_id or "")[:128] or "unknown"


def _ro_connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_id_list(raw: str) -> list:
    try:
        val = ast.literal_eval(raw)
        if isinstance(val, list):
            return [v for v in val if isinstance(v, str)]
    except (ValueError, SyntaxError):
        pass
    return re.findall(r"'([^']*)'", raw)


def parse_bg_log(path) -> list:
    """Parse decision lines from a zmem-bg.log.

    Returns ``[{ts, status, reason, omitted, ids, all, ops, sid}]`` where
    ``reason``/``ops`` are None when the line lacks them (writer B omits
    ``reason=``; pre-#94 lines lack ``sid=``) and ``ids``/``all`` are lists
    of memory id strings. Torn or maintenance lines (no ``zmem-hook``
    marker, unparseable shape) are skipped — the log is appended
    concurrently, so a torn final line is normal. Never raises.
    """
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return out
    for line in lines:
        if "zmem-hook" not in line:
            continue  # maintenance output ([zmem] backup: ...) etc.
        m = _BG_LINE_RE.match(line.strip())
        if not m:
            continue
        (ts, status, reason, omitted, ids_raw, all_raw, _tok, ops, sid) = \
            m.groups()
        try:
            ts = int(ts)
        except ValueError:
            continue
        out.append({
            "ts": ts,
            "status": status,
            "reason": reason,
            "omitted": int(omitted) if omitted else 0,
            "ids": _parse_id_list(ids_raw),
            "all": _parse_id_list(all_raw),
            "ops": int(ops) if ops else None,
            "sid": sid,
        })
    return out


def _iso_to_epoch_s(value) -> int | None:
    """Best-effort ISO-8601 → epoch seconds (transcript timestamps)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _operation_from_input(tool_input) -> str:
    """Extract the operation string (command or primary path) from a tool
    call's input dict — the same fields the pretool hook derives its query
    from. Returns "" when nothing usable is present."""
    if not isinstance(tool_input, dict):
        return ""
    for field in ("command", "file_path", "notebook_path", "path"):
        val = tool_input.get(field)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _failures_from_part_inputs(conn: sqlite3.Connection,
                               session_ids: list) -> dict:
    """One session-scoped pass over the ZCode db ``part`` table building
    ``{callID: operation string}`` for tool calls in the given sessions.

    The part table stores each tool call as JSON in ``data`` (``callID``,
    ``tool``, ``state.input`` with the command / file_path the call used —
    ``tool_usage`` itself has no command column). Scoped to the BOUNDED
    failure result's sessions, never the whole table. Fail-open: any error
    degrades to {} (failures then fall back to ring/tool-name derivation).
    """
    if not session_ids:
        return {}
    out = {}
    try:
        placeholders = ",".join("?" * len(session_ids))
        rows = conn.execute(
            f"SELECT data FROM part WHERE session_id IN ({placeholders})",
            session_ids,
        ).fetchall()
    except Exception:
        return out
    for row in rows:
        try:
            obj = json.loads(row[0])
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        call_id = obj.get("callID")
        state = obj.get("state")
        if not isinstance(call_id, str) or not isinstance(state, dict):
            continue
        op = _operation_from_input(state.get("input"))
        if op:
            out[call_id] = op
    return out


def failures_from_db_rich(db_path, since_s=None, until_s=None,
                          limit=500) -> list:
    """Mine failed tool calls from the ZCode episodic db with the fields the
    miss-rate join needs (mine.py's ``store.py failures`` outputs only
    {tool, error}; the join additionally needs session, timestamp, and the
    failed operation).

    Same failure predicate as ``mine._failures_from_db`` (read_only=0 AND
    (status='error' OR exit_code!=0)). Time bounds: ``since_s``/``until_s``
    apply to ``completed_at/1000`` as ``>= since_s`` / ``< until_s``,
    matching ``ts_s = completed_at // 1000``. Newest-first, bounded by
    ``limit``. Read-only; a missing db is a legitimate "nothing to check"
    ([]) and the function never raises.
    """
    try:
        path = Path(db_path).expanduser()
    except (TypeError, ValueError):
        return []
    if not db_path or not path.is_file():
        return []
    try:
        conn = _ro_connect(path)
    except Exception as exc:
        # Swarm-review PRR-002: a PRESENT but unreadable db (garbage bytes,
        # locked file, permission error) must not masquerade as "no
        # failures" — raise so the caller can caveat the broken substrate,
        # mirroring mine.cmd_failures' #36 M7 contract.
        raise RuntimeError(f"episodic db unreadable (read-only): "
                           f"{type(exc).__name__}: {exc}") from exc
    try:
        sql = (
            "SELECT session_id, completed_at, tool_name, tool_call_id,"
            " error_message, error_type FROM tool_usage"
            " WHERE COALESCE(read_only, 0) = 0"
            " AND (status = 'error'"
            "      OR (exit_code IS NOT NULL AND exit_code != 0))"
        )
        params: list = []
        if since_s is not None:
            sql += " AND completed_at >= ?"
            params.append(int(since_s) * 1000)
        if until_s is not None:
            sql += " AND completed_at < ?"
            params.append(int(until_s) * 1000)
        sql += " ORDER BY completed_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = conn.execute(sql, params).fetchall()
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        # PRR-002: substrate error (schema drift on the count query, locked
        # db) — surface it, never a misleading [].
        raise RuntimeError(f"episodic db unreadable (read-only): "
                           f"{type(exc).__name__}: {exc}") from exc
    failures = []
    for r in rows:
        ts_ms = r["completed_at"] or 0
        # Broad-review M5: tolerate rows recorded in epoch SECONDS (mixed
        # units across db generations) — anything below 1e11 predates
        # 1973-in-ms, so it can only be seconds.
        if 0 < ts_ms < 10 ** 11:
            ts_ms *= 1000
        failures.append({
            "session_id": r["session_id"] or "",
            "ts_s": int(ts_ms) // 1000,
            "tool": r["tool_name"] or "?",
            "operation": "",
            "error": r["error_message"] or "",
            "error_type": r["error_type"] or "",
            "call_id": r["tool_call_id"] or "",
        })
    # Recover each call's operation (command / file_path) from the part
    # table, scoped to exactly the sessions of the bounded result above.
    ops = _failures_from_part_inputs(conn, sorted({f["session_id"] for f
                                                   in failures
                                                   if f["session_id"]}))
    try:
        conn.close()
    except Exception:
        pass
    for f in failures:
        if f["call_id"] and f["call_id"] in ops:
            f["operation"] = ops[f["call_id"]]
    return failures


def _load_mine_helpers():
    """Reuse mine.py's transcript classification helpers (rejection split,
    result-text extraction) so the rich parser and ``store.py failures``
    can never drift apart on what counts as a failure (swarm-review
    PRR-004). Falls back to minimal local versions if the sibling cannot
    be imported."""
    try:
        from storelib import mine as _mine
        return (_mine._result_text, _mine._is_rejection_text,
                _mine._rejection_reason)
    except Exception:
        # Faithful local copies of mine.py's helpers (byte-semantics match:
        # substring marker match, space-join, string-element blocks) so the
        # fallback classification can never drift from `store.py failures`.
        def _result_text(content):
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(b.get("text") or "")
                    elif isinstance(b, str):
                        parts.append(b)
                return " ".join(p for p in parts if p)
            return ""

        def _is_rejection_text(text):
            return bool(text) and "the user doesn't want to proceed" in str(
                text).lower()

        def _rejection_reason(text):
            # Faithful copy of mine._rejection_reason (the reason follows
            # the "user said:" marker; localized forms share the shorter
            # marker). Currently destructured but unused — rejections are
            # excluded upstream — kept parity-complete so a future caller
            # cannot silently diverge.
            s = str(text or "")
            lower = s.lower()
            marker = "user said:"
            idx = lower.find(marker)
            if idx < 0:
                return ""
            after = s[idx + len(marker):]
            lines = [ln.strip() for ln in after.splitlines() if ln.strip()]
            return " ".join(lines)
        return (_result_text, _is_rejection_text, _rejection_reason)


def failures_from_transcript_rich(path) -> list:
    """Mine failed tool calls from a Claude Code transcript JSONL with the
    fields the join needs: per-record session/timestamp plus the failed
    tool_use's input (command / file_path). Classification mirrors
    ``mine._failures_from_transcript`` exactly (swarm-review PRR-004): an
    ``is_error`` block OR a sibling ``toolUseResult`` "Error…" string is a
    failure, and user rejections ("The user doesn't want to proceed…") are
    split OUT and excluded — never counted as failures. Record-level
    session id is read as ``session_id`` OR ``sessionId`` (swarm-review
    PRR-005: the dominant real CC record shape is camelCase-only).
    Never raises; [] on any read/parse problem.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            raw = [ln for ln in fh if ln.strip()]
    except OSError:
        return []
    records = []
    for ln in raw:
        try:
            records.append(json.loads(ln))
        except Exception:
            continue
    result_text, is_rejection_text, _rejection_reason = _load_mine_helpers()
    # Pass 1: tool_use_id -> (tool name, operation from the call input).
    calls = {}
    for obj in records:
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if (isinstance(block, dict) and block.get("type") == "tool_use"
                    and isinstance(block.get("id"), str) and block["id"]):
                calls[block["id"]] = (
                    block.get("name") or "?",
                    _operation_from_input(block.get("input")),
                )
    # Pass 2: failure classification, same predicate as mine.py — an
    # is_error block, a record-level toolUseResult "Error…" string, or
    # rejection-shaped effective text; deduped per tool_use_id; rejections
    # split out and EXCLUDED (issue #46 semantics, not failures).
    out = []
    seen = set()
    anon = 0
    for obj in records:
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        tur = obj.get("toolUseResult")
        tur_is_err = isinstance(tur, str) and tur.strip().lower().startswith(
            "error")
        # mine.py's pure-sibling REJECTION branch is intentionally absent:
        # rejections are excluded from this lane entirely, so a tur-only
        # rejection record simply produces no failure row.
        sid_val = obj.get("session_id") or obj.get("sessionId")
        sid = sid_val if isinstance(sid_val, str) else ""
        ts_s = _iso_to_epoch_s(obj.get("timestamp"))
        classified = False
        block_tids = []
        if isinstance(content, list):
            for block in content:
                if not (isinstance(block, dict)
                        and block.get("type") == "tool_result"):
                    continue
                is_err = block.get("is_error") is True
                effective = result_text(block.get("content"))
                if not effective and isinstance(tur, str):
                    effective = tur
                tid = block.get("tool_use_id")
                tid = tid if isinstance(tid, str) and tid else None
                if tid:
                    block_tids.append(tid)
                if not (is_err or tur_is_err
                        or (effective and is_rejection_text(effective))):
                    continue
                key = tid if tid else ("anon:%d" % anon)
                if key in seen:
                    continue
                seen.add(key)
                if not tid:
                    anon += 1
                classified = True
                if is_rejection_text(effective):
                    continue  # user rejection — not a failure (issue #46)
                tool, operation = calls.get(tid, ("?", ""))
                out.append({
                    "session_id": sid,
                    "ts_s": ts_s,
                    "tool": tool,
                    "operation": operation,
                    "error": (effective or "")[:300],
                    "error_type": "",
                    "call_id": tid,
                })
        # Pure sibling-string form: the record's toolUseResult is itself an
        # error but no tool_result block classified it (mine.py's shape).
        if not classified and tur_is_err:
            tid = block_tids[0] if block_tids else None
            key = tid if tid else ("anon:%d" % anon)
            if key in seen:
                continue
            seen.add(key)
            if not tid:
                anon += 1
            tool, operation = calls.get(tid, ("?", ""))
            out.append({
                "session_id": sid,
                "ts_s": ts_s,
                "tool": tool,
                "operation": operation,
                "error": tur[:300],
                "error_type": "",
                "call_id": tid,
            })
    return out[::-1]


def _ring_events_before(data_dir, session_id, ts_s, max_events=8) -> list:
    """Read the session's ops-ring events strictly BEFORE the failure (the
    hook at the failing turn would have seen exactly these). Ring path
    mirrors ops_tokens._ring_path (same sanitize rule; the ring-path
    fallback is "session"). Returns the descriptors, oldest-first, newest
    ``max_events`` kept. [] on any problem."""
    if not data_dir or not session_id:
        return []
    safe = _SID_SAFE_RE.sub("_", session_id)[:128] or "session"
    path = os.path.join(data_dir, "ops", safe + ".log")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # torn concurrent append
        if not isinstance(obj, dict):
            continue
        try:
            ev_ts = int(obj.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if ts_s and ev_ts > ts_s:
            continue  # only events the failing turn could have seen
        desc = obj.get("ops")
        if isinstance(desc, str) and desc:
            events.append(desc)
    return events[-max_events:] if max_events > 0 else []


def _load_ops_tokens():
    sys_path_saved = None
    try:
        import sys as _sys
        sys_path_saved = _sys.path[:]
        lib_dir = str(Path(__file__).resolve().parent)
        if lib_dir not in _sys.path:
            _sys.path.insert(0, lib_dir)
        import ops_tokens
        return ops_tokens
    except Exception:
        return None
    finally:
        if sys_path_saved is not None:
            import sys as _sys
            _sys.path[:] = sys_path_saved


def failure_query(failure: dict, data_dir, _ops_cache={}) -> tuple:
    """Derive the failure's query. Chain: the recovered OPERATION (the
    failed command for Bash, the file path for Edit/Read/Write — from the
    part table or the transcript's tool_use input) → the session's ops-ring
    events before the failure. Returns ``(query, source)`` where source is
    "operation" | "ring" | "none". A bare tool NAME is not a chain step:
    derive_ops_tokens("Bash") derives nothing (runner-head/file-shape
    gating), so name-only failures honestly land in no_query."""
    ops_mod = _ops_cache.get("mod")
    if ops_mod is None:
        ops_mod = _load_ops_tokens()
        _ops_cache["mod"] = ops_mod
    if ops_mod is None:
        return "", "none"
    operation = (failure.get("operation") or "").strip()
    if operation:
        tokens = ops_mod.derive_ops_tokens(operation)
        if tokens:
            return " ".join(tokens), "operation"
    events = _ring_events_before(
        data_dir, failure.get("session_id", ""), failure.get("ts_s") or 0)
    if events:
        tokens = ops_mod.derive_ops_tokens(*events)
        if tokens:
            return " ".join(tokens), "ring"
    return "", "none"


def host_default_store() -> str:
    """The store path the host WOULD resolve with a clean env (the path the
    miss-rate guard must refuse). Mirrors scripts/eval_self_corpus.py's
    _default_store_path: the home-override env vars are popped so an
    ambient ZMEM_STORE cannot make the guard compare against itself."""
    import sys as _sys
    saved = {k: os.environ.pop(k) for k in _HOME_ENV_VARS if k in os.environ}
    sys_path_saved = _sys.path[:]
    try:
        if str(_SCRIPTS_DIR) not in _sys.path:
            _sys.path.insert(0, str(_SCRIPTS_DIR))
        import host
        return str(host.resolve_store_path())
    finally:
        os.environ.update(saved)
        _sys.path[:] = sys_path_saved


def _pct(numerator: int, denominator: int):
    if denominator <= 0:
        return None
    return round(100.0 * numerator / denominator, 1)


def run_miss_report(store_path, db_path=None, transcripts=(),
                    bg_log_path=None, data_dir=None,
                    window_before_s=1800, window_after_s=300,
                    limit=200, verbose=False) -> dict:
    """Join mined failures × store recall × bg-log injections (read-only).

    See the module docstring for the pinned bucket definitions. Returns a
    plain dict; ``{"error": ...}`` (never an exception) when the store is
    missing/unreadable/too old for the join — the report must not create or
    migrate stores. Memory CONTENT stays out of the default output (ids and
    namespaces only); ``verbose=True`` adds a short content preview per top
    missed id.
    """
    try:
        store = Path(store_path).expanduser()
    except (TypeError, ValueError):
        return {"error": "invalid store path"}
    # Swarm-review PRR-001: a negative window inverts the interval (empty
    # candidate set) and silently classifies every query-matched failure as
    # missed — reject loudly instead.
    try:
        window_before_s = int(window_before_s)
        window_after_s = int(window_after_s)
    except (TypeError, ValueError):
        return {"error": "invalid window: before/after must be integers"}
    if window_before_s < 0 or window_after_s < 0:
        return {"error": "invalid window: --miss-window-before/after must "
                         "be >= 0 seconds (got "
                         f"before={window_before_s}, after={window_after_s})"}
    if not store.is_file():
        return {"error": f"store not found (the join never creates one): "
                         f"{store}"}
    # Never connect()/_prepare_store: a mode=ro URI cannot create or migrate.
    # Probe BOTH load-bearing tables: a stale-schema store whose `memory`
    # table exists but whose `memory_fts` does not would otherwise slip past
    # this check and classify every failure as capture-gap (recall's FTS
    # OperationalError degrades to rows=[] inside storelib) — a schema
    # problem must be a loud error, never a misclassification.
    try:
        conn = _ro_connect(store)
        conn.execute("SELECT count(*) FROM memory").fetchone()
        conn.execute("SELECT count(*) FROM memory_fts").fetchone()
    except Exception as exc:
        return {"error": "store unreadable by the join (read-only; never "
                         f"migrated): {type(exc).__name__}: {exc}"}
    try:
        # Import lazily so a broken sibling cannot break the whole module.
        _sys_path_patch = None
        import sys as _sys
        _sys_path_patch = _sys.path[:]
        if str(_SCRIPTS_DIR) not in _sys.path:
            _sys.path.insert(0, str(_SCRIPTS_DIR))
        try:
            from storelib.recall import recall_memory
        finally:
            _sys.path[:] = _sys_path_patch
    except Exception as exc:
        try:
            conn.close()
        except Exception:
            pass
        return {"error": f"recall import failed: {type(exc).__name__}: {exc}"}

    if data_dir is None:
        data_dir = str(store.resolve().parent)
    if bg_log_path is None:
        bg_log_path = os.path.join(data_dir, "zmem-bg.log")
    lines = parse_bg_log(bg_log_path)
    # An injection line is EITHER writer A's explicit reason=injected OR
    # writer B's shape (the session-start writer has never carried reason=;
    # its injections are marked status=injected with no reason field).
    # Baseline-run finding: filtering on reason=injected alone silently
    # dropped every session-start injection and inflated the miss rate.
    injected = [ln for ln in lines
                if ln.get("reason") == "injected"
                or (ln.get("reason") is None
                    and ln.get("status") == "injected")]
    decision_ts = [ln["ts"] for ln in lines]
    period = [min(decision_ts), max(decision_ts)] if decision_ts else None
    sid_lines = sum(1 for ln in lines if ln.get("sid") is not None)

    failures = []
    unmatched_globs = []
    db_error = None
    if db_path:
        try:
            failures.extend(failures_from_db_rich(db_path, limit=limit))
        except Exception as exc:
            # PRR-002: a broken failure substrate is a loud caveat (and a
            # report field), never a silent zero.
            db_error = f"{type(exc).__name__}: {exc}"
    for pattern in transcripts or ():
        matches = _glob.glob(str(pattern), recursive=True)
        if not matches:
            unmatched_globs.append(str(pattern))
        for path in matches:
            failures.extend(failures_from_transcript_rich(path))
    # Fair merge before the limit truncates (broad-review M4): db-first
    # concatenation would starve every transcript failure whenever the db
    # alone fills the limit. Sort ALL failures newest-first (timestamp-less
    # last), THEN dedupe (call_id keeps distinct same-second failures
    # apart — broad-review L7), THEN truncate.
    failures.sort(key=lambda f: f.get("ts_s") or 0, reverse=True)
    deduped = []
    seen = set()
    for f in failures:
        key = (f.get("session_id"), f.get("ts_s"), f.get("tool"),
               f.get("call_id"), (f.get("error") or "")[:60])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(f)
    failures_truncated = len(deduped) > max(1, int(limit))
    failures = deduped[:max(1, int(limit))]

    counts = {"surfaced_sid": 0, "surfaced_legacy": 0, "missed": 0,
              "capture_gap": 0, "no_query": 0}
    query_source = {"operation": 0, "ring": 0, "none": 0}
    missed_all_only = 0
    legacy_attributions = 0
    in_period = 0
    no_timestamp = 0
    recall_errors = 0
    missed_id_counts: dict = {}
    id_meta: dict = {}
    missed_shapes: dict = {}
    recall_cache: dict = {}

    def _recall(query: str):
        """Zero-write recall; returns the row list, or None when the recall
        itself errored (swarm-review PRR-003: a retrieval failure must be
        distinguishable from a genuine capture gap, never counted as one)."""
        if query in recall_cache:
            return recall_cache[query]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                rows = recall_memory(conn, query=query, namespace=None,
                                     limit=5, **_JOIN_RECALL_KWARGS)
            except Exception:
                rows = None
        recall_cache[query] = rows
        return rows

    for f in failures:
        # Broad-review M5: a failure with no usable timestamp has no window
        # — classify it as unmeasurable (excluded + caveated), never as
        # missed (a dead [-1800,+300]-around-1970 window would guarantee a
        # false miss).
        if not f.get("ts_s"):
            no_timestamp += 1
            continue
        query, source = failure_query(f, data_dir)
        query_source[source] += 1
        if period is not None and f.get("ts_s") \
                and period[0] <= f["ts_s"] <= period[1]:
            in_period += 1
        if not query:
            counts["no_query"] += 1
            continue
        rows = _recall(query)
        if rows is None:
            # PRR-003: a failed recall is a measurement error, not a
            # capture gap — exclude from every rate with a caveat.
            recall_errors += 1
            continue
        if not rows:
            counts["capture_gap"] += 1
            continue
        matched = {r.get("id") for r in rows if r.get("id")}
        for r in rows:
            rid = r.get("id")
            if rid:
                id_meta[rid] = (r.get("namespace") or "",
                                (r.get("content") or "")[:120])
        ts_s = f.get("ts_s") or 0
        lo = ts_s - int(window_before_s)
        hi = ts_s + int(window_after_s)
        # Broad-review M6: only a REAL session id on both sides proves
        # attribution. sid-less lines (pre-#94) AND sid=unknown lines
        # (a host that supplied no session id — e.g. non-launcher
        # session-start invocations) get the weaker legacy treatment,
        # never sid-proof and never attribution-dead.
        f_sid = sanitize_sid(f.get("session_id", ""))
        f_sid_real = bool((f.get("session_id") or "").strip())
        cand_sid = []
        cand_legacy = []
        for ln in injected:
            if not (lo <= ln["ts"] <= hi):
                continue
            ln_sid = ln.get("sid")
            if ln_sid is None or ln_sid == "unknown":
                cand_legacy.append(ln)
            elif ln_sid == f_sid and f_sid_real:
                cand_sid.append(ln)
        if any(matched & set(ln["ids"]) for ln in cand_sid):
            counts["surfaced_sid"] += 1
        elif any(matched & set(ln["ids"]) for ln in cand_legacy):
            counts["surfaced_legacy"] += 1
            legacy_attributions += 1
        else:
            counts["missed"] += 1
            tool = f.get("tool") or "?"
            missed_shapes[tool] = missed_shapes.get(tool, 0) + 1
            for rid in matched:
                missed_id_counts[rid] = missed_id_counts.get(rid, 0) + 1
            if any(matched & set(ln["all"]) for ln in cand_sid + cand_legacy):
                missed_all_only += 1

    try:
        conn.close()
    except Exception:
        pass

    surfaced_total = counts["surfaced_sid"] + counts["surfaced_legacy"]
    strict_denom = counts["missed"] + counts["surfaced_sid"]
    miss_rate = (round(counts["missed"] / (counts["missed"] + surfaced_total), 3)
                 if (counts["missed"] + surfaced_total) > 0 else None)
    # The strict rate is meaningless until sid-carrying lines exist at all
    # (missed/(missed+0) would read as a false 1.0 on a legacy-only log).
    strict = None
    if sid_lines > 0 or counts["surfaced_sid"] > 0:
        strict = (round(counts["missed"] / strict_denom, 3)
                  if strict_denom > 0 else None)

    top_missed = sorted(missed_id_counts.items(), key=lambda kv: -kv[1])[:10]
    top_missed_ids = []
    for rid, n in top_missed:
        ns, content = id_meta.get(rid, ("", ""))
        entry = {"id": rid, "namespace": ns, "missed_count": n}
        if verbose:
            entry["content_preview"] = content
        top_missed_ids.append(entry)

    caveats = []
    for pattern in unmatched_globs:
        caveats.append(f"transcript glob matched no files: {pattern}")
    if db_error:
        caveats.append(
            f"the episodic failure db could not be read ({db_error}) — the "
            "failure side of this run is incomplete; fix the db or point "
            "--miss-db at a healthy copy and re-run")
    if recall_errors:
        caveats.append(
            f"{recall_errors} failure(s) could not be recalled (store read "
            "error) — excluded from every rate, never counted as "
            "capture-gap")
    if (failures and counts["missed"] == 0 and counts["surfaced_sid"] == 0
            and counts["surfaced_legacy"] == 0):
        caveats.append(
            "no miss-rate denominator: every examined failure landed in "
            "capture-gap or no-query — the rate fields are null by "
            "construction for this run.")
    if failures_truncated:
        caveats.append(
            f"failure limit ({limit}) reached — older failures beyond the "
            "limit were not examined; raise --miss-limit for fuller "
            "coverage")
    if no_timestamp:
        caveats.append(
            f"{no_timestamp} failure(s) had no usable timestamp — excluded "
            "from every rate (no window can be evaluated)")
    if query_source["none"]:
        caveats.append(
            f"{query_source['none']} failure(s) derived no query (no "
            "recovered operation and no ops ring) — unmeasurable, excluded "
            "from every rate. On boxes where the ops-ring lane is not yet "
            "deployed this bucket can dominate until rings accumulate.")
    if legacy_attributions:
        caveats.append(
            f"{legacy_attributions} surfaced attribution(s) rest on pre-#94 "
            "sid-less bg-log lines (time-window + id overlap only); "
            "miss_rate_strict_sid excludes them.")
    if counts["missed"] and period and in_period < counts["missed"]:
        caveats.append(
            "bg-log coverage is partial (the log truncates at its size cap);"
            " missed counts may include failures whose injections predate"
            " the surviving log window.")
    if not lines:
        caveats.append("no bg-log decision lines found — every matched "
                       "failure is classified missed by construction.")

    return {
        "store": str(store.resolve()),
        "data_dir": data_dir,
        "bg_log_path": bg_log_path,
        "bg_log_decision_lines": len(lines),
        "bg_log_period": period,
        "failures_examined": len(failures),
        "failures_truncated": failures_truncated,
        "no_timestamp": no_timestamp,
        "recall_errors": recall_errors,
        "db_error": db_error,
        "failures_in_bg_log_period": in_period,
        "counts": counts,
        "miss_rate": miss_rate,
        "miss_rate_strict_sid": strict,
        "missed_all_only": missed_all_only,
        "query_source": query_source,
        "no_query_pct": _pct(counts["no_query"], len(failures)),
        "sid_coverage_pct": _pct(sid_lines, len(lines)),
        "legacy_attributions": legacy_attributions,
        "window": {"before_s": int(window_before_s),
                   "after_s": int(window_after_s)},
        "top_missed_ids": top_missed_ids,
        "missed_shapes": sorted(missed_shapes.items(),
                                key=lambda kv: -kv[1])[:10],
        "caveats": caveats,
    }
