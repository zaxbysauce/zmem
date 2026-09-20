"""Read-only, deterministic replay evaluator for the #155 decision surface.

The evaluator intentionally consumes only the files named on its command line.
It snapshots those files into a private temporary directory, parses the staged
copies, and opens the staged SQLite snapshot through a read-only URI.  In
particular, it never follows rotated decision logs, ops rings, a failure DB, or
the operator's store.  The output is an audit report, not a live efficacy
claim: empty observation inputs are reported as unavailable.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile
from bisect import bisect_right
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "memory" / "scripts"
REPORT_LANES = ("claude", "hermes-provider")
REPORT_MOMENTS = tuple(sorted(("session_start", "user_prompt", "pretool", "precompact")))
COUNT_KEYS = (
    "decisions", "candidates", "delivered", "reference_checked", "miss",
    "empty_pool", "already_delivered",
)
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_ATTR_TOKEN_RE = re.compile(r"(?:^|\s)(?:lane|ver|t_ms)=")
_TIMESTAMP_LINE_RE = re.compile(r"^\[(\d+)\] zmem-hook\b")
# The launcher watchdog predates the canonical decision-row parser.  Keep its
# diagnostic grammar separate from _BG_LINE_RE: it is an explicit exclusion,
# never a decision row, and accepting it in the canonical regex would invent
# report coverage for an event with no ids/all fields.  Values intentionally
# use \S* (rather than a narrower identifier grammar) because the writer emits
# environment/hook values verbatim, including '=' and empty values.
_OUTER_TIMEOUT_DIAGNOSTIC_RE = re.compile(
    r"^\[(?P<ts>\d+)\] zmem-hook status=silent reason=omitted "
    r"outer_timeout=1 stage=launcher timeout_ms=(?P<timeout_ms>\d+) "
    r"tier0_emitted=(?P<tier0_emitted>[01]) tier2_rows=0 "
    r"hook=(?P<hook>\S*) ns=(?P<namespace>\S*) "
    r"sid=(?P<sid>\S*) moment=(?P<moment>\S*)$"
)
_DOMAIN = b"zmem-replay-transcripts-v1\0"
# Transcript inputs are explicit evidence, but they are still untrusted
# process-boundary data.  Bound each read and the aggregate before decoding or
# accumulating JSON records; a changing source file is caught by the final
# byte-integrity check after evaluation.
MAX_TRANSCRIPTS = 16
MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024
MAX_TRANSCRIPT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_TRANSCRIPT_LINES = 100_000
# The store snapshot and decision log are explicit untrusted inputs too. Keep
# their one-shot reads bounded before staging or parsing (transcripts have
# separate, smaller limits above).
MAX_STORE_BYTES = 512 * 1024 * 1024
MAX_LOG_BYTES = 64 * 1024 * 1024
# Observational action matching (issue #156). The window and overlap are a
# fixed contract, deliberately not environment-overridable: the report
# records both values so a consumer can identify the matching contract from
# the output alone.
ZMEM_MATCH_WINDOW_S = 1800
ZMEM_MATCH_MIN_OVERLAP = 2
MAX_ACTIONS_BYTES = 4 * 1024 * 1024
_ACTIONS_DELIVERED_FIELDS = ("id", "session_id", "timestamp", "operation")
_ACTIONS_EVIDENCE_FIELDS = ("session_id", "timestamp", "event_kind", "operation")


class ReplayError(ValueError):
    """An input or evaluation error suitable for the stable CLI surface."""


def _parse_outer_timeout_diagnostic(raw: str) -> bool:
    """Recognize the exact legacy launcher diagnostic, without imports.

    This pure recognizer is shared by the pre-bootstrap replay clock and the
    strict staged-log validator.  A line containing the marker but failing the
    full grammar is deliberately *not* recognized; callers reject it instead
    of silently dropping a malformed lookalike.
    """
    match = _OUTER_TIMEOUT_DIAGNOSTIC_RE.fullmatch(raw.strip())
    if not match:
        return False
    # Matching is the only required operation.  Do not convert unbounded
    # decimal fields here: the original bytes remain the audit input and the
    # diagnostic is excluded from all numeric report calculations.
    return True


def _scan_log_surface(text: str) -> tuple[list[tuple[int, str]], int]:
    """Classify hook lines before store-library imports or report parsing.

    The first result contains non-diagnostic hook rows (they still require the
    canonical parser); the second counts exact, intentionally excluded
    launcher diagnostics. Non-hook maintenance lines retain the historical
    tolerance. Other hook lines, including malformed lookalikes, are left for
    canonical validation so valid IDs containing ``outer_timeout=`` remain
    compatible.
    """
    decision_lines: list[tuple[int, str]] = []
    exclusions = 0
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "zmem-hook" not in raw:
            continue
        diagnostic = _parse_outer_timeout_diagnostic(stripped)
        if diagnostic:
            exclusions += 1
            continue
        decision_lines.append((number, stripped))
    return decision_lines, exclusions


def _operator_store_candidates() -> set[Path]:
    """Resolve operator-store aliases before replay isolation is installed."""
    env = os.environ
    candidates: set[Path] = set()

    def add(raw: str | os.PathLike[str] | None) -> None:
        if not raw:
            return
        try:
            p = Path(raw).expanduser()
            # Resolve aliases even when the path is not present.  strict=False
            # keeps a typo from turning the refusal check into an exception.
            candidates.add(p.resolve())
        except (OSError, RuntimeError, TypeError, ValueError):
            return

    # Mirror host.py's precedence: lower-priority configured aliases are not
    # operator paths when a higher-priority override is active.  This matters
    # for isolated callers that retain ZMEM_DATA while explicitly pointing
    # ZMEM_STORE at a disposable ambient path.
    if env.get("ZMEM_STORE"):
        add(env.get("ZMEM_STORE"))
    elif env.get("ZMEM_DATA"):
        add(Path(env["ZMEM_DATA"]).expanduser() / "store.sqlite")
    elif env.get("CLAUDE_PLUGIN_DATA"):
        add(Path(env["CLAUDE_PLUGIN_DATA"]).expanduser() / "store.sqlite")
    elif env.get("ZCODE_PLUGIN_DATA"):
        add(Path(env["ZCODE_PLUGIN_DATA"]).expanduser() / "store.sqlite")
    home = Path(os.path.expanduser("~"))
    add(home / ".zmem" / "store.sqlite")
    add(home / ".zcode" / "memory" / "store.sqlite")
    # Capture the host's real final fallback as well.  In a pre-migration
    # installation this can be the newest ~/.zcode/cli/plugins/data/*zmem*/
    # store, which is deliberately not discoverable from an isolated replay
    # environment.  host.py is stdlib-only and performs no import-time I/O;
    # resolve it now, while the caller's configured override precedence is
    # still intact (ZMEM_STORE/ZMEM_DATA/etc. remain authoritative).
    saved_path = sys.path[:]
    try:
        sys.path.insert(0, str(SCRIPTS))
        import host  # type: ignore[import-not-found]
        add(host.resolve_store_path())
    except Exception:
        # The explicit env/default candidates above remain protective if a
        # legacy host resolver is unavailable or has a filesystem race.
        pass
    finally:
        sys.path[:] = saved_path
    return candidates


def _resolved_regular(path_text: str, label: str) -> Path:
    try:
        path = Path(path_text).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ReplayError(f"replay: invalid --{label} path: {exc}") from exc
    if not path.is_file() or not stat.S_ISREG(path.stat().st_mode):
        raise ReplayError(f"replay: --{label} must be a regular file\n")
    return path


def _refuse_operator_store(store: Path, candidates: set[Path]) -> None:
    if _is_operator_alias(store, candidates):
        raise ReplayError(
            "replay: --store must be a regular file outside the operator store\n"
        )


def _is_operator_alias(path: Path, candidates: set[Path]) -> bool:
    if path in candidates:
        return True
    for candidate in candidates:
        try:
            if path.exists() and candidate.exists() and os.path.samefile(path, candidate):
                return True
        except OSError:
            continue
    return False


def _same_existing_file(left: Path, right: Path) -> bool:
    if left == right:
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def _bootstrap_env(store: str | Path, staging: Path, replay_now: int) -> None:
    """Install every store/model/data path before importing ``storelib``."""
    # The replay contract is independent of the caller's host/plugin knobs.
    # Capture operator candidates before reaching this boundary, then clear
    # every ambient ZMEM_* override so import-time globals and call-time recall
    # caps cannot alter the same staged corpus.
    for key in list(os.environ):
        if key.startswith("ZMEM_") or key in {"CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"}:
            os.environ.pop(key, None)
    store = str(store)
    os.environ["ZMEM_STORE"] = store
    os.environ["ZMEM_DATA"] = str(staging / "data")
    os.environ["ZMEM_HOME"] = str(staging / "home")
    os.environ["ZMEM_MODELS_DIR"] = str(staging / "missing-models")
    os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    os.environ["ZMEM_EMBED_PROFILE"] = "fake"
    os.environ["ZMEM_TEST_NOW"] = _iso_epoch(replay_now)
    # Explicitly pin the import-time MMR default; the other recall caps use
    # their documented defaults after the ambient environment is cleared.
    os.environ["ZMEM_MMR_LAMBDA"] = "0.7"
    os.environ["PYTHONUTF8"] = "1"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bounded(path: Path, label: str, maximum: int) -> bytes:
    """Read one input once while enforcing a byte bound without ``stat`` trust."""
    chunks: list[bytes] = []
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(min(1024 * 1024, maximum - total + 1))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > maximum:
                    raise ReplayError(
                        f"replay: {label} exceeds the {maximum}-byte limit\n"
                    )
    except ReplayError:
        raise
    except OSError as exc:
        raise ReplayError(f"replay: cannot read {label}: {exc}\n") from exc
    return b"".join(chunks)


def _digest(store: bytes, log: bytes, transcripts: list[bytes], actions: bytes | None = None) -> str:
    if not transcripts and actions is None:
        return _sha(store + log)
    h = hashlib.sha256()
    h.update(store)
    h.update(log)
    h.update(_DOMAIN)
    for data in transcripts:
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    if actions is not None:
        # Domain-separate the actions contribution so a transcript whose
        # bytes equal an actions file can never produce a colliding digest.
        h.update(b"zmem-replay-actions-v1\0")
        h.update(len(actions).to_bytes(8, "big"))
        h.update(actions)
    return h.hexdigest()


def _parse_timestamp(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def _parse_failure_timestamp(failure: object) -> int:
    if not isinstance(failure, dict):
        return 0
    try:
        return int(failure.get("ts_s") or 0)
    except (TypeError, ValueError):
        return 0


def _iso_epoch(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ReplayError("replay: duplicate JSON object key in transcript\n")
        out[key] = value
    return out


def _reject_json_constant(value: str) -> object:
    raise ReplayError(f"replay: non-finite JSON constant {value}\n")


def _validate_transcript(path: Path, data: bytes) -> list[dict]:
    """Parse JSONL once and reject malformed observational input."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReplayError(f"replay: malformed transcript {path.name}: UTF-8\n") from exc
    records: list[dict] = []
    for number, line in enumerate(text.splitlines(), 1):
        if number > MAX_TRANSCRIPT_LINES:
            raise ReplayError(
                f"replay: transcript {path.name} exceeds the "
                f"{MAX_TRANSCRIPT_LINES}-line limit\n"
            )
        if not line.strip():
            continue
        try:
            value = json.loads(
                line,
                object_pairs_hook=_strict_object_pairs,
                parse_constant=_reject_json_constant,
            )
        except (ValueError, ReplayError) as exc:
            raise ReplayError(
                f"replay: malformed transcript {path.name} line {number}\n"
            ) from exc
        if not isinstance(value, dict):
            raise ReplayError(
                f"replay: malformed transcript {path.name} line {number}\n"
            )
        records.append(value)
    return records


def _validate_log(staged: Path):
    """Parse staged decisions and return ``(rows, excluded_diagnostics)``.

    Exact launcher timeout diagnostics are retained as explicit exclusions;
    every other ``zmem-hook`` line remains subject to the canonical parser and
    fail-closed validation.
    """
    sys.path.insert(0, str(SCRIPTS))
    try:
        from storelib.miss_rate import _BG_LINE_RE, parse_bg_log
    finally:
        # Keep the evaluator's import boundary explicit; imported modules stay
        # loaded, but repeated calls do not accumulate path entries.
        if sys.path and sys.path[0] == str(SCRIPTS):
            sys.path.pop(0)
    try:
        text = staged.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReplayError(f"replay: cannot read decision log: {exc}\n") from exc
    hook_lines, exclusions = _scan_log_surface(text)
    for number, raw in hook_lines:
        match = _BG_LINE_RE.match(raw.strip())
        if not match:
            raise ReplayError(f"replay: malformed decision row line {number}\n")
        groups = match.groups()
        lane, version, t_ms = groups[11], groups[12], groups[13]
        if _ATTR_TOKEN_RE.search(raw):
            if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
                raise ReplayError(f"replay: malformed decision row line {number}\n")
            if not isinstance(t_ms, str) or not t_ms.isdigit():
                raise ReplayError(f"replay: malformed decision row line {number}\n")
            if lane is not None and lane not in {
                "claude", "codex", "zcode", "hermes-provider", "hermes-compat",
            }:
                raise ReplayError(f"replay: malformed decision row line {number}\n")
        try:
            ids = ast.literal_eval(groups[4])
            all_ids = ast.literal_eval(groups[5])
        except (ValueError, SyntaxError) as exc:
            raise ReplayError(f"replay: malformed decision row line {number}\n") from exc
        if (not isinstance(ids, list) or not isinstance(all_ids, list)
                or any(not isinstance(item, str) for item in ids + all_ids)):
            raise ReplayError(f"replay: malformed decision row line {number}\n")
    # parse_bg_log uses the same staged path and cannot discover ambient
    # rotations because the private directory contains no sibling segments.
    parsed = parse_bg_log(str(staged))
    if len(parsed) != len(hook_lines):
        raise ReplayError("replay: malformed decision log row\n")
    return parsed, exclusions


def _latest_log_timestamp(staged: Path) -> int:
    """Read decision timestamps before importing store libraries.

    Recognized launcher diagnostics are excluded from the replay clock.  The
    surface scan is intentionally shared with ``_validate_log`` so malformed
    timeout lookalikes cannot be treated as harmless maintenance output.
    """
    try:
        text = staged.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ReplayError(f"replay: cannot read decision log: {exc}\n") from exc
    decision_lines, _exclusions = _scan_log_surface(text)
    timestamps = []
    for _number, raw in decision_lines:
        match = _TIMESTAMP_LINE_RE.match(raw)
        if match:
            try:
                timestamps.append(int(match.group(1)))
            except ValueError:
                continue
    if not timestamps:
        raise ReplayError("replay: decision log contains no valid rows\n")
    return max(timestamps)


def _nearest_rank(samples: list[int], p: float) -> int | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = max(0, math.ceil(p * len(ordered)) - 1)
    return ordered[index]


def _empty_counts() -> dict[str, int]:
    return {key: 0 for key in COUNT_KEYS}


def _measurement_windows(lines: list[dict], norm_sid, *,
                        before_s: int | None = None,
                        after_s: int | None = None) -> dict[str, list[tuple[int, int]]]:
    """Build the inclusive same-session inverse join-window union.

    ``run_miss_report`` attributes a failure at ``f`` to a decision ``d``
    when ``f-before_s <= d <= f+after_s``.  The exact inverse is therefore
    ``d-after_s <= f <= d+before_s``.  Keep this helper shared by the failure
    prefilter and the global observation marker so those two predicates cannot
    drift.  Lines without a real normalized session or usable timestamp are
    intentionally not evidence for a named session.
    """
    if before_s is None or after_s is None:
        # Resolve the shared symbols only after the caller's bootstrap has
        # pinned ambient replay settings. A module-level import would execute
        # storelib/__init__.py too early and freeze ZMEM_* knobs.
        sys.path.insert(0, str(SCRIPTS))
        try:
            from storelib.miss_rate import (
                MEASUREMENT_WINDOW_AFTER_S,
                MEASUREMENT_WINDOW_BEFORE_S,
            )
        finally:
            if sys.path and sys.path[0] == str(SCRIPTS):
                sys.path.pop(0)
        if before_s is None:
            before_s = MEASUREMENT_WINDOW_BEFORE_S
        if after_s is None:
            after_s = MEASUREMENT_WINDOW_AFTER_S
    windows: dict[str, list[tuple[int, int]]] = {}
    for line in lines:
        if not isinstance(line, dict):
            continue
        sid = norm_sid(line.get("sid"))
        if not sid or sid == "unknown":
            continue
        try:
            ts = int(line.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        windows.setdefault(sid, []).append((ts - after_s, ts + before_s))
    # Merge overlapping/adjacent intervals once per session. Membership then
    # uses binary search instead of scanning every decision window for every
    # failure, preserving union semantics while bounding replay cost.
    for sid, intervals in windows.items():
        merged: list[list[int]] = []
        for lo, hi in sorted(intervals):
            if merged and lo <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        windows[sid] = [tuple(interval) for interval in merged]
    return windows


def _failure_in_measurement_union(
    failure: dict,
    windows: dict[str, list[tuple[int, int]]],
    norm_sid,
) -> bool:
    """Return whether one failure is inside any named-session window."""
    if not isinstance(failure, dict):
        return False
    sid = norm_sid(failure.get("session_id"))
    if not sid or sid == "unknown":
        return False
    try:
        ts = int(failure.get("ts_s") or 0)
    except (TypeError, ValueError):
        return False
    if ts <= 0:
        return False
    intervals = windows.get(sid, ())
    if not intervals:
        return False
    index = bisect_right(intervals, (ts, float("inf"))) - 1
    return index >= 0 and ts <= intervals[index][1]


def _valid_version(lines: list[dict]) -> str | None:
    versions = {
        line.get("ver") for line in lines
        if isinstance(line.get("ver"), str) and line.get("ver")
    }
    if len(versions) > 1:
        raise ReplayError("replay: mixed nonempty decision-log versions\n")
    return next(iter(versions), None)


def _read_only_connection(store: Path) -> sqlite3.Connection:
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(store.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT count(*) FROM memory").fetchone()
        conn.execute("SELECT count(*) FROM memory_fts").fetchone()
        # A present meta table must carry an integer schema version.  Do not
        # hard-code v14: replay remains usable for older compatible snapshots.
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        if row is not None and (not str(row[0]).isdigit() or int(row[0]) <= 0):
            raise ReplayError("replay: schema mismatch\n")
        return conn
    except ReplayError:
        if conn is not None:
            conn.close()
        raise
    except Exception as exc:
        if conn is not None:
            conn.close()
        raise ReplayError(f"replay: schema mismatch: {type(exc).__name__}\n") from exc


def _bucket_observations(
    bucket: list[dict],
    failures: list[dict],
    conn: sqlite3.Connection,
    staged_transcripts: list[Path],
    isolated_data_dir: Path,
    *,
    prompt_events_override=None,
    recall_cache=None,
) -> tuple[int, int, int, int]:
    """Return (reference_checked, used, missed, surfaced) for one bucket.

    Miss/surfaced counts are delegated once to ``run_miss_report`` over the
    complete bucket. The explicit eligibility filter is the replay contract's
    deliberate distinction from the historical false-injection report, which
    counts an injected line as a false reference even when no later observation
    exists.
    """
    sys.path.insert(0, str(SCRIPTS))
    try:
        from storelib.false_inject import _norm_sid, _read_prompt_events
        from storelib.miss_rate import (
            MEASUREMENT_WINDOW_AFTER_S,
            MEASUREMENT_WINDOW_BEFORE_S,
            run_miss_report,
        )
    finally:
        if sys.path and sys.path[0] == str(SCRIPTS):
            sys.path.pop(0)
    # The shared helpers partition named sessions by the same sanitizer used
    # by the writers.  Normalize punctuation-bearing SIDs into a private copy
    # before the join; missing/empty/``unknown`` SIDs are deliberately not
    # attributed to a named bucket (they remain legacy/weak-match inputs in
    # the historical helper, but replay must not turn that into cross-session
    # evidence).  Keep the original parsed row for the closed report counts.
    real_lines = []
    for line in bucket:
        sid = _norm_sid(line.get("sid"))
        if not sid or sid == "unknown":
            continue
        normalized = line if line.get("sid") == sid else {**line, "sid": sid}
        real_lines.append(normalized)
    real_sids = {str(line.get("sid")) for line in real_lines}
    relevant_failures = []
    for failure in failures:
        sid = _norm_sid(failure.get("session_id"))
        if sid and sid != "unknown" and sid in real_sids:
            relevant_failures.append(
                failure if failure.get("session_id") == sid
                else {**failure, "session_id": sid}
            )
    reference_events = list(
        _read_prompt_events([str(p) for p in staged_transcripts])
        if prompt_events_override is None else prompt_events_override
    )
    for failure in relevant_failures:
        text = " ".join(str(failure.get(key) or "") for key in ("operation", "error", "tool")).strip()
        if text:
            try:
                timestamp = int(failure.get("ts_s") or 0)
            except (TypeError, ValueError):
                timestamp = 0
            sid = _norm_sid(failure.get("session_id"))
            if timestamp and sid:
                reference_events.append((timestamp, text, sid))
    # The join substrate is deliberately narrower than the reference side:
    # out-of-window failures are not evidence for this measurement.  Build a
    # conservative union over every decision line in this lane×moment bucket
    # (including silent, empty-pool, and already-delivered rows).  These are
    # exactly the lines ``run_miss_report`` receives below; using another
    # bucket's window would admit a failure that this join cannot attribute
    # and would therefore misclassify as missed.  The unfiltered ``failures``
    # length remains the limit basis so prefiltering cannot change truncation
    # semantics.
    windows = _measurement_windows(real_lines, _norm_sid)
    filtered_failures = [
        failure for failure in relevant_failures
        if _failure_in_measurement_union(failure, windows, _norm_sid)
    ]
    eligible = []
    for line in real_lines:
        sid = str(line.get("sid"))
        ts = int(line.get("ts") or 0)
        if line.get("ids") and any(event[2] == sid and event[0] > ts for event in reference_events):
            eligible.append(line)
    checked = len(eligible)
    try:
        from storelib.false_inject import build_false_injection_report
    finally:
        if sys.path and sys.path[0] == str(SCRIPTS):
            sys.path.pop(0)
    used = 0
    if eligible:
        false_report = build_false_injection_report(
            eligible,
            conn=conn,
            data_dir=str(isolated_data_dir),
            failure_rows=relevant_failures,
            transcripts=[str(p) for p in staged_transcripts],
            prompt_events_override=prompt_events_override,
        )
        if (not isinstance(false_report, dict)
                or false_report.get("degraded")
                or false_report.get("error")):
            raise ReplayError("replay: reference measurement unavailable\n")
        overall = false_report.get("overall")
        if not isinstance(overall, dict):
            raise ReplayError("replay: reference measurement unavailable\n")
        try:
            used = int(overall["used"])
        except (KeyError, TypeError, ValueError):
            raise ReplayError("replay: reference measurement unavailable\n")
    report = run_miss_report(
        str(conn.execute("PRAGMA database_list").fetchone()[2]),
        db_path=None,
        transcripts=[str(p) for p in staged_transcripts],
        bg_log_path=None,
        data_dir=str(isolated_data_dir),
        window_before_s=MEASUREMENT_WINDOW_BEFORE_S,
        window_after_s=MEASUREMENT_WINDOW_AFTER_S,
        limit=max(200, len(failures) + 1),
        # Keep all real-session decisions, including all=[], in the join.  A
        # failure may be recalled while the decision's candidate list is empty;
        # the shared join must then classify it as a miss rather than dropping
        # the audit before the matching/window logic runs.
        decision_lines=real_lines,
        # A bucket is a session/moment audit.  Restrict the failure substrate
        # to this decision's real session before invoking the shared join;
        # otherwise unrelated transcript sessions would be counted as misses
        # for every selected row.
        failure_rows_override=filtered_failures,
        prompt_events_override=prompt_events_override,
        recall_cache=recall_cache,
    )
    if (not isinstance(report, dict)
            or report.get("error") or report.get("db_error") or report.get("recall_errors")
            or report.get("failures_truncated")):
        raise ReplayError("replay: observation measurement unavailable\n")
    counts = report.get("counts")
    if not isinstance(counts, dict):
        raise ReplayError("replay: observation measurement unavailable\n")
    try:
        missed = int(counts["missed"])
        surfaced = int(counts.get("surfaced_sid", 0)) + int(counts.get("surfaced_legacy", 0))
    except (KeyError, TypeError, ValueError):
        raise ReplayError("replay: observation measurement unavailable\n")
    return (
        checked,
        used,
        missed,
        surfaced,
    )


def _build_report(
    lines: list[dict],
    store: Path,
    store_sha256: str,
    days: int,
    staged_transcripts: list[Path],
    version: str | None,
    transcript_records: list[list[dict]] | None = None,
) -> tuple[dict, list[str]]:
    if not lines:
        raise ReplayError("replay: decision log contains no valid rows\n")
    latest = max(int(line["ts"]) for line in lines)
    cutoff = latest - days * 86400
    selected = [line for line in lines if cutoff <= int(line["ts"]) <= latest]
    # Only rows projected into the fixed eight-bucket report can contribute
    # measurement coverage.  Keep this exact set as the basis for the global
    # observation marker; each miss join uses its own lane×moment bucket.
    report_lines = [
        line for line in selected
        if line.get("lane") in REPORT_LANES
        and line.get("moment") in REPORT_MOMENTS
    ]
    # Observation inputs obey the same fixed replay window as decisions.  Make
    # private, exact JSONL projections so the shared helpers cannot read an
    # out-of-window prompt/failure or any original path/rotation sibling.
    filtered_transcript_dir = store.parent / "observations"
    filtered_transcript_dir.mkdir()
    filtered_transcripts: list[Path] = []
    for index, source in enumerate(staged_transcripts):
        destination = filtered_transcript_dir / f"transcript-{index}.jsonl"
        kept: list[dict] = []
        records = (transcript_records[index] if transcript_records is not None
                   else _validate_transcript(source, source.read_bytes()))
        for obj in records:
            ts = _parse_timestamp(obj.get("timestamp"))
            if ts is not None and cutoff <= ts <= latest:
                kept.append(obj)
        destination.write_text(
            "".join(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n" for obj in kept),
            encoding="utf-8", newline="\n",
        )
        filtered_transcripts.append(destination)
    diagnostics: list[str] = []
    lane_excluded: dict[str, int] = {}
    moment_excluded: dict[str, int] = {}
    for line in selected:
        lane = line.get("lane")
        moment = line.get("moment")
        if lane not in REPORT_LANES:
            lane_excluded[str(lane or "<missing>")] = lane_excluded.get(str(lane or "<missing>"), 0) + 1
        if moment not in REPORT_MOMENTS:
            moment_excluded[str(moment or "<missing>")] = moment_excluded.get(str(moment or "<missing>"), 0) + 1
    if lane_excluded:
        diagnostics.append(
            "replay: excluded lanes (valid in-window rows): "
            + ", ".join(f"{key}={lane_excluded[key]}" for key in sorted(lane_excluded))
            + "\n"
        )
    if moment_excluded:
        diagnostics.append(
            "replay: excluded moments (valid in-window rows): "
            + ", ".join(f"{key}={moment_excluded[key]}" for key in sorted(moment_excluded))
            + "\n"
        )

    sys.path.insert(0, str(SCRIPTS))
    try:
        from storelib.miss_rate import failures_from_transcript_rich
    finally:
        if sys.path and sys.path[0] == str(SCRIPTS):
            sys.path.pop(0)
    failures: list[dict] = []
    for path in filtered_transcripts:
        failures.extend(failures_from_transcript_rich(str(path)))
    sys.path.insert(0, str(SCRIPTS))
    try:
        from storelib.false_inject import _norm_sid, _read_prompt_events
    finally:
        if sys.path and sys.path[0] == str(SCRIPTS):
            sys.path.pop(0)
    prompt_events = _read_prompt_events([str(path) for path in filtered_transcripts])
    measurement_windows = _measurement_windows(report_lines, _norm_sid)
    usable_observation = any(
        isinstance(ts, int) and ts > 0
        and isinstance(text, str) and bool(text.strip())
        and _failure_in_measurement_union(
            {"session_id": sid, "ts_s": ts}, measurement_windows, _norm_sid
        )
        for ts, text, sid in prompt_events
    )
    usable_observation = usable_observation or any(
        isinstance(failure, dict)
        and _parse_failure_timestamp(failure) > 0
        and bool(" ".join(str(failure.get(key) or "") for key in (
            "operation", "error", "tool")).strip())
        and _failure_in_measurement_union(failure, measurement_windows, _norm_sid)
        for failure in failures
    )
    isolated_data_dir = store.parent / "isolated-data"
    isolated_data_dir.mkdir()
    conn = _read_only_connection(store)
    try:
        rows: list[dict] = []
        miss_num_total = miss_den_total = ref_ok_total = ref_checked_total = 0
        # Recall is pure for a pinned read-only store and query.  Keep one
        # caller-owned cache for the eight bucket joins; shared-helper callers
        # that omit it retain the historical per-report cache.
        recall_cache: dict = {}
        for lane in REPORT_LANES:
            for moment in REPORT_MOMENTS:
                bucket = [line for line in selected if line.get("lane") == lane and line.get("moment") == moment]
                counts = _empty_counts()
                timing = [line["t_ms"] for line in bucket if isinstance(line.get("t_ms"), int)]
                for line in bucket:
                    counts["decisions"] += 1
                    counts["candidates"] += len(line.get("all") or ())
                    counts["delivered"] += len(line.get("ids") or ())
                    if line.get("reason") == "empty-pool":
                        counts["empty_pool"] += 1
                    if line.get("reason") == "already-delivered" and line.get("all"):
                        counts["already_delivered"] += 1
                ref_checked, ref_ok, miss, surfaced = _bucket_observations(
                    bucket, failures, conn, filtered_transcripts,
                    isolated_data_dir,
                    prompt_events_override=prompt_events,
                    recall_cache=recall_cache,
                )
                counts["reference_checked"] = ref_checked
                counts["miss"] = miss
                miss_den = miss + surfaced
                miss_num_total += miss
                miss_den_total += miss_den
                ref_ok_total += ref_ok
                ref_checked_total += ref_checked
                rows.append({
                    "lane": lane,
                    "moment": moment,
                    "counts": counts,
                    "reference_precision": ref_ok / ref_checked if ref_checked else 0,
                    "false_injection_rate": (ref_checked - ref_ok) / ref_checked if ref_checked else 0,
                    "miss_rate": miss / miss_den if miss_den else 0,
                    "empty_pool_rate": counts["empty_pool"] / counts["decisions"] if counts["decisions"] else 0,
                    "already_delivered_rate": counts["already_delivered"] / sum(
                        1 for line in bucket if line.get("all")
                    ) if any(line.get("all") for line in bucket) else 0,
                    "t_ms": {"p50": _nearest_rank(timing, 0.50), "p95": _nearest_rank(timing, 0.95)},
                })
    finally:
        conn.close()
    report = {
        "schema_version": 1,
        "input_digest": "",
        "store_sha256": store_sha256,
        "days": days,
        "rows": rows,
        "aggregate": {
            "reference_precision": ref_ok_total / ref_checked_total if ref_checked_total else 0,
            "miss_rate": miss_num_total / miss_den_total if miss_den_total else 0,
        },
        "input_metadata": {"version": version, "parsed_rows": len(lines)},
        "usable_observation": usable_observation,
        "generated_at": _iso_epoch(latest),
    }
    if not (miss_den_total or ref_checked_total):
        diagnostics.append(
            "replay: no usable miss/reference observations; observations unavailable; numeric zero fields "
            "are empty-denominator compatibility values, not measured success or failure\n"
        )
    return report, diagnostics


def _excluded_diagnostic_messages(exclusions: int) -> list[str]:
    """Render deterministic stderr diagnostics for non-decision rows."""
    if not exclusions:
        return []
    return [
        "replay: excluded diagnostics (not decisions): "
        + f"outer_timeout=1 count={exclusions}\n"
    ]


def _parse_thresholds(values: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for value in values:
        if "=" not in value:
            raise ReplayError(f"replay: invalid --fail-under value: {value}\n")
        name, raw = value.split("=", 1)
        if name not in {"precision_delta", "miss_delta"}:
            raise ReplayError(f"replay: invalid --fail-under value: {value}\n")
        try:
            number = float(raw)
        except ValueError as exc:
            raise ReplayError(f"replay: invalid --fail-under value: {value}\n") from exc
        if not math.isfinite(number):
            raise ReplayError(f"replay: invalid --fail-under value: {value}\n")
        out[name] = number
    return out


def _json_bytes(report: dict) -> bytes:
    return (json.dumps(report, ensure_ascii=False, indent=2, separators=(",", ": ")) + "\n").encode("utf-8")


def _json_bytes_sorted(report: dict) -> bytes:
    return (json.dumps(report, ensure_ascii=False, indent=2, separators=(",", ": "), sort_keys=True) + "\n").encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    if path.parent and not path.parent.is_dir():
        raise ReplayError("replay: --json-out parent directory does not exist\n")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise ReplayError(f"replay: cannot write report: {exc}\n") from exc


def _check_baseline(report: dict, baseline_path: Path, thresholds: dict[str, float]) -> bool:
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReplayError(f"replay: invalid baseline: {exc}\n") from exc
    if not isinstance(baseline, dict):
        raise ReplayError("replay: invalid baseline\n")
    if baseline.get("schema_version") != report.get("schema_version"):
        raise ReplayError("replay: baseline schema mismatch\n")
    baseline_metadata = baseline.get("input_metadata")
    if not isinstance(baseline_metadata, dict):
        raise ReplayError("replay: baseline input_metadata must be an object\n")
    report_metadata = report.get("input_metadata")
    if not isinstance(report_metadata, dict):
        raise ReplayError("replay: report input_metadata must be an object\n")
    if baseline_metadata.get("version") != report_metadata.get("version"):
        raise ReplayError("replay: baseline version mismatch\n")
    try:
        baseline_precision = float(baseline["aggregate"]["reference_precision"])
        baseline_miss = float(baseline["aggregate"]["miss_rate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ReplayError("replay: invalid baseline metrics\n") from exc
    if not math.isfinite(baseline_precision) or not math.isfinite(baseline_miss):
        raise ReplayError("replay: invalid baseline metrics\n")
    precision_delta = float(report["aggregate"]["reference_precision"]) - baseline_precision
    miss_delta = float(report["aggregate"]["miss_rate"]) - baseline_miss
    if "precision_delta" in thresholds and precision_delta < thresholds["precision_delta"]:
        return True
    if "miss_delta" in thresholds and miss_delta > thresholds["miss_delta"]:
        return True
    return False


def _parse_action_instant(raw: object, *, kind: str, index: int) -> datetime:
    """Strictly parse one action-row timestamp into a UTC instant.

    Unlike the lenient log parser, action rows reject naive timestamps and
    non-UTC offsets: the matcher compares instants, so a silent local-time
    assumption would misplace the window boundary.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ReplayError(f"replay: actions-input {kind} row {index}: invalid timestamp\n")
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ReplayError(f"replay: actions-input {kind} row {index}: invalid timestamp\n") from exc
    if moment.tzinfo is None or moment.utcoffset() != timezone.utc.utcoffset(None):
        raise ReplayError(f"replay: actions-input {kind} row {index}: timestamp must be UTC\n")
    return moment


def _validated_action_rows(rows: object, *, kind: str) -> list[dict]:
    if not isinstance(rows, list):
        raise ReplayError(f"replay: actions-input {kind}_rows must be a list\n")
    fields = _ACTIONS_DELIVERED_FIELDS if kind == "delivered" else _ACTIONS_EVIDENCE_FIELDS
    validated: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ReplayError(f"replay: actions-input {kind} row {index}: must be an object\n")
        for name in fields:
            if name not in row:
                raise ReplayError(f"replay: actions-input {kind} row {index}: missing field '{name}'\n")
            if not isinstance(row[name], str) or not row[name].strip():
                raise ReplayError(f"replay: actions-input {kind} row {index}: field '{name}' must be a non-empty string\n")
        validated.append({
            "id": row.get("id"),
            "session_id": row["session_id"],
            "event_kind": row.get("event_kind"),
            "operation": row["operation"],
            "instant": _parse_action_instant(row["timestamp"], kind=kind, index=index),
            "order": index,
        })
    return validated


def _load_action_rows(blob: bytes) -> tuple[list[dict], list[dict]]:
    """Validate captured action-observation bytes into delivered/evidence rows.

    The bytes arrive from the initial bounded snapshot in ``main`` so the
    matching result is derived from exactly the bytes the report digest
    covers; a file replaced mid-run is caught by the final re-verification.
    """
    # ``_read_bounded`` (the caller) raises ReplayError for oversize or
    # unreadable inputs; only decode/parse failures are re-wrapped here so
    # those stable diagnostics are not converted into a JSON-parse message.
    try:
        payload = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"replay: actions-input is not valid JSON: {exc}\n") from exc
    if not isinstance(payload, dict):
        raise ReplayError("replay: actions-input must be a JSON object\n")
    for key in ("delivered_rows", "evidence_rows"):
        if key not in payload:
            raise ReplayError(f"replay: actions-input missing '{key}'\n")
    return payload["delivered_rows"], payload["evidence_rows"]


def _derive_ops_tokens():
    saved = sys.path[:]
    try:
        sys.path.insert(0, str(SCRIPTS))
        from storelib.ops_tokens import derive_ops_tokens  # type: ignore[import-not-found]
        return derive_ops_tokens
    except Exception as exc:
        raise ReplayError(f"replay: cannot import ops tokenizer: {type(exc).__name__}\n") from exc
    finally:
        sys.path[:] = saved


def match_observational_actions(
    delivered_rows: list[dict],
    evidence_rows: list[dict],
    *,
    window_s: int = ZMEM_MATCH_WINDOW_S,
    min_overlap: int = ZMEM_MATCH_MIN_OVERLAP,
) -> list[dict]:
    """Classify each delivered row by the first later same-session evidence event.

    Report-only by contract (issue #156): this function never opens a store,
    writes a counter, or mutates a delivery ledger. A matching event is the
    earliest same-session evidence strictly after the delivered timestamp and
    within ``window_s`` whose ``derive_ops_tokens`` normalization shares at
    least ``min_overlap`` tokens with the delivered operation; original
    evidence order breaks equal-timestamp ties. ``success`` maps to
    ``applied``, ``failure`` to ``violated``, and everything else (including
    no match) to ``ignored``.
    """
    delivered = _validated_action_rows(delivered_rows, kind="delivered")
    evidence = _validated_action_rows(evidence_rows, kind="evidence")
    derive = _derive_ops_tokens()
    by_session: dict[str, list[dict]] = {}
    for event in evidence:
        by_session.setdefault(event["session_id"], []).append(event)
    results: list[dict] = []
    for row in delivered:
        trigger = set(derive(row["operation"]))
        selected: tuple[datetime, int, int, dict] | None = None
        for event in by_session.get(row["session_id"], []):
            elapsed = (event["instant"] - row["instant"]).total_seconds()
            if elapsed <= 0 or elapsed > window_s:
                continue
            overlap = len(trigger & set(derive(event["operation"])))
            if overlap < min_overlap:
                continue
            if selected is None or (event["instant"], event["order"]) < (selected[0], selected[1]):
                selected = (event["instant"], event["order"], overlap, event)
        if selected is None:
            results.append({
                "delivered_id": row["id"],
                "session_id": row["session_id"],
                "action": "ignored",
                "event_kind": None,
                "elapsed_s": None,
                "overlap_count": 0,
                "_sort_instant": None,
                "_order": row["order"],
            })
            continue
        _, _, overlap, event = selected
        kind = event["event_kind"]
        action = "applied" if kind == "success" else "violated" if kind == "failure" else "ignored"
        results.append({
            "delivered_id": row["id"],
            "session_id": row["session_id"],
            "action": action,
            "event_kind": kind,
            "elapsed_s": float((event["instant"] - row["instant"]).total_seconds()),
            "overlap_count": overlap,
            "_sort_instant": event["instant"],
            "_order": row["order"],
        })
    results.sort(key=lambda item: (
        item["session_id"],
        item["delivered_id"],
        item["_sort_instant"].timestamp() if item["_sort_instant"] is not None else float("inf"),
        item["_order"],
    ))
    for item in results:
        del item["_sort_instant"]
        del item["_order"]
    return results


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="eval_replay.py")
    parser.add_argument("--store", dest="store", type=str, required=True, help="read-only store snapshot")
    parser.add_argument("--log", dest="log", type=str, required=True, help="decision log to replay")
    parser.add_argument("--days", dest="days", type=int, required=True, help="look back N days")
    parser.add_argument("--compare-baseline", dest="compare_baseline", type=str, default=None, help="baseline JSON to compare")
    parser.add_argument("--fail-under", dest="fail_under", type=str, nargs="+", default=[], help="ratchets such as precision_delta=-0.01 miss_delta=0.01")
    parser.add_argument("--transcript", dest="transcripts", type=str, action="append", default=[], help="explicit transcript JSONL observation (repeatable)")
    parser.add_argument("--actions", dest="actions", action="store_true", default=False, help="report first same-session evidence action for each delivered row")
    parser.add_argument("--actions-input", dest="actions_input", type=str, default=None, help="recorded action observation rows (JSON) consumed by --actions")
    parser.add_argument("--json-out", dest="json_out", type=str, default=None, help="write the report JSON")
    return parser


def main() -> int:
    args = _parser().parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass
    try:
        if args.days <= 0:
            raise ReplayError("replay: --days must be a positive integer\n")
        thresholds = _parse_thresholds(args.fail_under)
        candidates = _operator_store_candidates()
        try:
            store_candidate = Path(args.store).expanduser().resolve()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ReplayError(f"replay: invalid --store path: {exc}\n") from exc
        # Refuse the configured/default operator alias before the regular-file
        # probe.  The refusal is intentionally stable even when the operator
        # store has not been created yet.
        if _is_operator_alias(store_candidate, candidates):
            raise ReplayError(
                "replay: --store must be a regular file outside the operator store\n"
            )
        store = _resolved_regular(args.store, "store")
        log = _resolved_regular(args.log, "log")
        _refuse_operator_store(store, candidates)
        if args.actions and not args.actions_input:
            raise ReplayError("replay: --actions requires --actions-input\n")
        if args.actions_input and not args.actions:
            raise ReplayError("replay: --actions-input requires --actions\n")
        transcript_paths = [_resolved_regular(value, "transcript") for value in args.transcripts]
        actions_path = _resolved_regular(args.actions_input, "actions-input") if args.actions_input else None
        all_inputs = [store, log, *transcript_paths, *([actions_path] if actions_path else [])]
        if any(_same_existing_file(all_inputs[i], all_inputs[j])
               for i in range(len(all_inputs)) for j in range(i)):
            raise ReplayError("replay: input files must be distinct\n")
        out = Path(args.json_out).expanduser().resolve() if args.json_out else None
        if any(_is_operator_alias(path, candidates) for path in [log, *transcript_paths, *([actions_path] if actions_path else [])]):
            raise ReplayError("replay: input file is inside the operator store\n")
        if out is not None and (_is_operator_alias(out, candidates)
                                or any(_same_existing_file(out, path) for path in all_inputs)):
            raise ReplayError("replay: --json-out must not overwrite an input\n")
        if args.compare_baseline:
            baseline = Path(args.compare_baseline).expanduser().resolve()
            if (_is_operator_alias(baseline, candidates)
                    or any(_same_existing_file(baseline, path) for path in all_inputs)
                    or (out is not None and _same_existing_file(baseline, out))):
                raise ReplayError("replay: baseline must be distinct from inputs/output\n")
            if not baseline.is_file():
                raise ReplayError("replay: invalid baseline\n")
        else:
            baseline = None
        wal = Path(str(store) + "-wal")
        if wal.is_file() and wal.stat().st_size:
            raise ReplayError("replay: --store is a live WAL-backed database; supply a standalone snapshot\n")
        if len(transcript_paths) > MAX_TRANSCRIPTS:
            raise ReplayError(
                f"replay: at most {MAX_TRANSCRIPTS} transcript inputs are allowed\n"
            )
        source_bytes = {
            store: _read_bounded(store, "store", MAX_STORE_BYTES),
            log: _read_bounded(log, "decision log", MAX_LOG_BYTES),
        }
        transcript_total = 0
        for path in transcript_paths:
            data = _read_bounded(path, f"transcript {path.name}", MAX_TRANSCRIPT_BYTES)
            transcript_total += len(data)
            if transcript_total > MAX_TRANSCRIPT_TOTAL_BYTES:
                raise ReplayError(
                    "replay: transcript inputs exceed the "
                    f"{MAX_TRANSCRIPT_TOTAL_BYTES}-byte total limit\n"
                )
            source_bytes[path] = data
        # Snapshot the actions input with the same one-shot bounded read so
        # the matching result is derived from exactly the bytes the report
        # digest covers.
        actions_bytes = (
            _read_bounded(actions_path, "actions input", MAX_ACTIONS_BYTES)
            if actions_path is not None else None
        )
        with tempfile.TemporaryDirectory(prefix="zmem-replay-") as raw:
            staging = Path(raw)
            staged_store = staging / "store.sqlite"
            staged_log = staging / "decisions.log"
            # Stage exactly the bytes covered by the initial digest.  A second
            # source read here would create a TOCTOU gap if a writer replaced a
            # file between hashing and staging.
            staged_store.write_bytes(source_bytes[store])
            staged_log.write_bytes(source_bytes[log])
            staged_transcripts: list[Path] = []
            transcript_records: list[list[dict]] = []
            for index, path in enumerate(transcript_paths):
                staged = staging / f"transcript-{index}.jsonl"
                staged.write_bytes(source_bytes[path])
                staged_transcripts.append(staged)
                transcript_records.append(_validate_transcript(path, source_bytes[path]))
            replay_now = _latest_log_timestamp(staged_log)
            _bootstrap_env(staged_store, staging, replay_now)
            lines, exclusions = _validate_log(staged_log)
            version = _valid_version(lines)
            report, diagnostics = _build_report(
                lines, staged_store, _sha(source_bytes[store]), args.days,
                staged_transcripts, version, transcript_records,
            )
            diagnostics = _excluded_diagnostic_messages(exclusions) + diagnostics
            report["input_digest"] = _digest(
                source_bytes[store], source_bytes[log],
                [source_bytes[path] for path in transcript_paths],
                actions_bytes,
            )
            if args.actions:
                # Validate and match strictly BEFORE any output bytes are
                # written: a malformed actions input must exit 2 without
                # replacing --json-out (frozen check C6).
                delivered_raw, evidence_raw = _load_action_rows(actions_bytes)
                report["actions"] = {
                    "window_s": ZMEM_MATCH_WINDOW_S,
                    "min_overlap": ZMEM_MATCH_MIN_OVERLAP,
                    "results": match_observational_actions(
                        delivered_raw, evidence_raw,
                        window_s=ZMEM_MATCH_WINDOW_S,
                        min_overlap=ZMEM_MATCH_MIN_OVERLAP,
                    ),
                }
            if _read_bounded(store, "store", MAX_STORE_BYTES) != source_bytes[store]:
                raise ReplayError("replay: store changed during read-only evaluation\n")
            for path, before in source_bytes.items():
                label = "store" if path == store else "decision log" if path == log else f"transcript {path.name}"
                if _read_bounded(path, label, MAX_STORE_BYTES if path == store else MAX_LOG_BYTES if path == log else MAX_TRANSCRIPT_BYTES) != before:
                    raise ReplayError(f"replay: input changed during evaluation: {path.name}\n")
            if actions_bytes is not None and _read_bounded(
                actions_path, "actions input", MAX_ACTIONS_BYTES
            ) != actions_bytes:
                raise ReplayError("replay: input changed during evaluation: actions input\n")
            report_bytes = _json_bytes_sorted(report) if args.actions else _json_bytes(report)
            breached = _check_baseline(report, baseline, thresholds) if baseline else False
            if out is not None:
                _write_atomic(out, report_bytes)
            if diagnostics:
                sys.stderr.write("".join(diagnostics))
            if breached:
                return 1
            return 0
    except ReplayError as exc:
        message = str(exc)
        if not message.endswith("\n"):
            message += "\n"
        sys.stderr.write(message)
        return 2
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        sys.stderr.write(f"replay: evaluation failed: {type(exc).__name__}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
