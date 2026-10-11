"""Read-only decision-log composition report (issue #249, Workstream P PR 1).

``store.py report --decision-log <path> [--json]`` aggregates the passive
injection lane's own decision log into the two tables the workstream's
downstream fixes (#250/#251/#252) need as a before/after baseline:

  * ``rows[<id>]`` — ``delivered`` (total deliveries), ``sessions`` (distinct
    ``sid=`` count) and ``max_per_session`` (busiest single session), the
    per-session repeat rate.
  * ``by_tier_moment[<tier>][<moment>]`` — ``delivered`` (the ``ids=[...]``
    entries) against ``withheld`` (the ``all=[...]`` candidates that were not
    delivered), split by the tier derived from each row's stored namespace.

READ-ONLY CONTRACT. This module never opens the operator's store for writing,
never migrates or checkpoints it, and never appends to the log. Two properties
are load-bearing and are proven by tests rather than assumed:

  * A plain ``?mode=ro`` open CREATES ``-wal``/``-shm`` sidecars next to the
    store, and against a store that already carries residue it even MUTATES the
    ``-shm`` bytes in place (WAL read-marks). Both break a byte-identity claim,
    so neither is used here.
  * ``?mode=ro&immutable=1`` creates nothing, but refuses nothing — so it is
    used directly ONLY when no ``-wal`` residue exists (the common case: a
    cleanly-closed store leaves none). When residue IS present, a private COPY
    of ``store.sqlite`` + ``store.sqlite-wal`` is staged outside the operator's
    data dir and read instead. ``-shm`` is deliberately NOT copied: it is
    derived state SQLite rebuilds from the ``-wal``, and copying it raised
    ``PermissionError`` under a live writer on Windows.

SNAPSHOT CAVEAT, disclosed in the report's own ``caveats`` array. A multi-file
copy is not atomic against a concurrent checkpointer, so tier labels come from a
point-in-time snapshot. Read order is therefore LOG-FIRST (the log is parsed
before the store is read): a decision line referencing id X is written only after
X is committed, so every id the report must classify is already committed when
the snapshot is taken. What remains possible is that an id's row is missing from
the snapshot, which degrades that id's tier label to ``unknown``. It NEVER
changes ``rows[...].delivered/sessions/max_per_session``, which are purely
log-derived, and it never changes the tier-SUMMED delivered/withheld totals; it
can move a row's contribution between per-tier buckets. Consumers comparing
before/after must read ``caveats`` and must not treat ``unknown``-bucket
movement as evidence of tier migration.

Tier vocabulary. The live five-tier scoped model spells its global tier
``user_global`` (``storelib.recall.SCOPED_TIER_ORDER``), and its cross-project
tiers are CALLER-RELATIVE — with no current namespace, every live ``project:*``
row is foreign (``recall.py`` ``cross_project_admissions``). A read-only offline
report has no caller in scope, so it emits the three labels the contract pins
(``project`` / ``global`` / ``unknown``) derived from the stored namespace, and
records the divergence in ``caveats`` rather than pretending to reproduce a
caller-relative classification it cannot compute.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

from storelib.false_inject import _is_injected_line, _moment_of
from storelib.miss_rate import parse_bg_log

DECISIONS_LOG_NAME = "zmem-decisions.log"
LEGACY_LOG_NAME = "zmem-bg.log"

TIER_PROJECT = "project"
TIER_GLOBAL = "global"
TIER_UNKNOWN = "unknown"
GLOBAL_NAMESPACE = "user:global"

# The writers substitute this literal when the host supplies no session id
# (plugin __init__.py, zmem-recall-body.py, mcp_server.py). It is NOT a
# session: pooling every such delivery under one bucket would report an
# invented session and attribute unrelated deliveries to it. The sibling
# readers already treat it as sessionless (false_inject._is_injected_line
# consumers, miss_rate sid_coverage), so this report follows the repo's own
# convention rather than inventing a second one.
SESSIONLESS_SID = "unknown"

_UNDETERMINED_TIER_CAVEAT = (
    "Tier labels come from the row's stored namespace, using the three labels "
    "this report's contract defines: project (a project: namespace), global "
    "(user:global), unknown (any other namespace, or a row no longer in the "
    "store). The live five-tier scoped recall model spells its global tier "
    "'user_global' and additionally has domain, fleet_host and cross_project "
    "tiers; its cross-project tiers are caller-relative and cannot be "
    "reproduced by an offline report with no current project, so they are not "
    "invented here."
)

_SNAPSHOT_CAVEAT = (
    "Tier labels are read from a point-in-time snapshot of the store. An id "
    "whose row is absent from that snapshot is reported under the 'unknown' "
    "tier. rows[*].delivered/sessions/max_per_session and the tier-summed "
    "delivered/withheld totals are unaffected, because they are derived from "
    "the decision log alone; only the per-tier split can shift."
)

_COUNTING_BASES_CAVEAT = (
    "delivered counts DELIVERY OCCURRENCES: by_tier_moment[*][*].delivered is "
    "the number of times an id of that tier appears in a decision line's ids "
    "list at that moment, which is why the tier-summed delivered total equals "
    "the sum of rows[*].delivered. withheld counts DISTINCT CANDIDATES WITHIN "
    "ONE LINE: it is the number of ids appearing in a line's all list that are "
    "not in that same line's ids list, so the same id offered twice on one "
    "line is one candidate, not two. The same id withheld on two different "
    "lines is two candidate opportunities and is counted twice."
)

_RETENTION_CAVEAT = (
    "This report covers the retained decision-log family only. Rotation caps "
    "how much history survives (see ZMEM_BG_LOG_MAX_BYTES and ZMEM_LOG_ROTATIONS), "
    "so a long gap between two runs is not evidence that nothing was injected "
    "-- it may simply have rotated away. Compare ts_first/ts_last across runs "
    "before reading a before/after difference as a behaviour change."
)


class ReportError(RuntimeError):
    """A user-facing refusal: the report cannot be produced as asked."""


def _resolve_store_path() -> str:
    """The store this report reads. Resolved through the same singleton the
    rest of storelib uses, so the report cannot drift onto a different store
    than the hooks write to."""
    from storelib import schema
    return str(schema.STORE_PATH)


def _resolve_data_dir() -> str:
    """Where a bare decision log lives when --decision-log is omitted."""
    return str(Path(_resolve_store_path()).parent)


def _counts_as_session(sid) -> bool:
    """Whether a line's ``sid=`` denotes a real, distinguishable session.

    False for an absent sid AND for the writers' ``unknown`` placeholder: the
    latter is what every host without a session id emits, so counting it would
    invent one session out of unrelated deliveries from different hosts.
    """
    return bool(sid) and sid != SESSIONLESS_SID


def _escape_rendered(value) -> str:
    """Neutralize control characters before untrusted text reaches a terminal.

    ``moment`` is sanitized at parse time because it becomes a report KEY, but a
    row ``id`` is only ever printed, and the text surface prints it raw. A
    hostile or hand-edited id carrying ANSI/OSC escapes would otherwise drive
    the operator's terminal. This mirrors the escaper storelib.cli applies to
    rendered evidence, and is deliberately a no-op on the JSON surface: JSON
    escapes control characters itself, and the ids there are machine keys that
    a consumer joins on, so they must stay byte-exact.
    """
    text = str(value)
    out = []
    for char in text:
        codepoint = ord(char)
        if char == "\t":
            out.append(r"\t")
        elif char == "\r":
            out.append(r"\r")
        elif char == "\n":
            out.append(r"\n")
        elif codepoint < 0x20 or 0x7F <= codepoint <= 0x9F:
            out.append("\\x%02x" % codepoint)
        else:
            out.append(char)
    return "".join(out)


# ---------------------------------------------------------------- log side


def resolve_log_path(explicit: str | None, data_dir: str) -> str:
    """Explicit path wins, else the decision log, else the legacy co-located
    log. Mirrors the precedence in ``miss_rate.run_miss_report`` so both
    surfaces resolve the same file."""
    if explicit:
        return str(explicit)
    decisions = os.path.join(data_dir, DECISIONS_LOG_NAME)
    legacy = os.path.join(data_dir, LEGACY_LOG_NAME)
    if os.path.exists(decisions):
        return decisions
    if os.path.exists(legacy):
        return legacy
    return decisions


def parse_decision_log(log_path: str) -> tuple:
    """Rotation-aware, CRLF-tolerant parse of the log family.

    Delegates to the single shared parser rather than forking one: it reads
    ``<log>.1 .. <log>.N`` then the active file and skips torn and non-decision
    lines.

    Returns ``(lines, problems)``. A missing log and a parse failure both yield
    zero lines, but they are DIFFERENT outcomes and the caller must be able to
    tell them apart -- an empty report from a typo'd path is otherwise
    indistinguishable from a genuinely decision-free window, which is exactly
    the kind of silent wrong answer this tool exists to avoid. Each is reported
    as a caveat rather than a refusal so that a partially readable log still
    produces its real numbers.
    """
    problems: list = []
    path = Path(log_path)
    if not path.exists():
        problems.append(
            "no decision log at %s; every row is reported as undelivered. Check "
            "the --decision-log path -- an absolute or relative typo resolves "
            "quietly to an empty report" % log_path)
        return [], problems
    try:
        lines = parse_bg_log(path)
    except Exception as exc:  # never expected: parse_bg_log is documented
        # never-raising, but the contract is only as good as its guards, and a
        # swallowed total parse failure must never look like an empty window.
        problems.append(
            "the decision log at %s could not be parsed (%s: %s); the report "
            "below covers no lines" % (log_path, type(exc).__name__, exc))
        return [], problems
    if not lines:
        problems.append(
            "the decision log at %s was read but contained no decision lines. "
            "An empty log and an unreadable one look the same in the numbers "
            "below" % log_path)
    return lines, problems


# --------------------------------------------------------------- store side


def _verify_readable(conn, what: str) -> None:
    """Prove the connection can actually read the store, before any caller
    relies on it. ``sqlite3.connect`` is LAZY: a corrupt, zero-byte, or
    table-less store connects fine and only fails on first use. Without this
    the failure would surface as silently-empty results (every id 'unknown'),
    which an operator cannot distinguish from 'the rows were purged'."""
    try:
        conn.execute("SELECT id, namespace FROM memory LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        raise ReportError("cannot read %s: %s" % (what, exc)) from exc


def _stage_copy(store_path: Path) -> tuple:
    """Copy the store family into a private temp dir and open the copy.

    ``-shm`` is intentionally not copied: it is derived state SQLite rebuilds
    from the ``-wal``. ``-journal`` IS copied when present, so that SQLite
    replays the rollback on the staged copy instead of this report reading
    uncommitted pages as committed ones.

    ``copyfile`` rather than ``copy2``: copy2 preserves the mode bits, so a
    read-only store produced a read-only staged copy that rmtree could not
    remove on Windows -- leaking a full copy of the operator's store on a
    perfectly successful run.

    Returns (conn, staging_dir); the caller owns closing both.
    """
    conn = None
    stage = None
    try:
        # Inside the try AND converted to ReportError: a full disk or an
        # unwritable TMPDIR must surface as the contracted refusal, exactly
        # like the copy2 failure below, not as a raw OSError traceback at
        # exit 1. One function, one failure shape.
        try:
            stage = tempfile.mkdtemp(prefix="zmem-report-stage-")
        except OSError as exc:
            raise ReportError(
                "cannot create a staging directory: %s" % exc) from exc
        # Owner-only where the platform supports it: this directory transiently
        # holds a copy of the operator's entire store.
        try:
            os.chmod(stage, 0o700)
        except OSError:
            pass
        for name in (str(store_path), str(store_path) + "-wal",
                     str(store_path) + "-journal"):
            src = Path(name)
            if not src.exists():
                continue
            try:
                shutil.copyfile(src, Path(stage, src.name))
            except OSError as exc:
                # A TOCTOU delete, a full disk, or an antivirus/OneDrive lock
                # all land here. Every one must surface as the contracted
                # refusal with the staging dir cleaned up -- never a traceback
                # at exit 1, and never a leaked copy of the operator's store.
                raise ReportError(
                    "cannot stage the store snapshot: %s" % exc) from exc
        staged = Path(stage, store_path.name)
        try:
            conn = sqlite3.connect(staged.as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            row = conn.execute("PRAGMA integrity_check").fetchone()
        except sqlite3.Error as exc:
            raise ReportError(
                "cannot read the store snapshot: %s" % exc) from exc
        if not row or row[0] != "ok":
            raise ReportError("store snapshot failed integrity_check")
        _verify_readable(conn, "the store snapshot")
        return conn, stage
    except BaseException:
        # Close BEFORE removing: on Windows an open handle makes rmtree fail
        # silently, leaking a full copy of the operator's store per attempt.
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _discard_stage(stage)
        raise


def _discard_stage(stage) -> str | None:
    """Remove a staging directory, reporting whether it actually went away.

    ``rmtree(ignore_errors=True)`` cannot distinguish "removed" from "silently
    refused", so a leftover full copy of the operator's store was invisible.
    Returns a warning when anything survived, None on a clean removal.
    """
    if stage is None:
        return None
    shutil.rmtree(stage, ignore_errors=True)
    if os.path.isdir(stage):
        return ("a temporary copy of the store staged at %s could not be "
                "removed and still holds the operator's memory rows; delete "
                "that directory by hand" % stage)
    return None


def open_store_for_report(store_path: Path):
    """Two-tier read-only store access.

    Returns (conn, staging_dir_or_None, caveats). Never writes to, migrates,
    checkpoints, or mutates the operator's store.
    """
    caveats: list = []
    if not store_path.is_file():
        return None, None, ["no store at %s; every id is reported under the "
                            "'unknown' tier" % store_path]
    wal = Path(str(store_path) + "-wal")
    journal = Path(str(store_path) + "-journal")
    has_wal = wal.exists()
    has_journal = journal.exists()
    if not (has_wal or has_journal):
        # Common case: a cleanly-closed store leaves no residue, so the
        # operator's file can be read directly and atomically without copying
        # anything or creating a sidecar.
        conn = None
        try:
            conn = sqlite3.connect(
                store_path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
            conn.row_factory = sqlite3.Row
            _verify_readable(conn, "the store")
        except sqlite3.Error as exc:
            # Covers the EAGER failures too — a permission/lock-denied store
            # raises here rather than on first use, and must surface as the
            # contracted refusal rather than a traceback at exit 1.
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            raise ReportError("cannot read the store: %s" % exc) from exc
        except BaseException:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            raise
        return conn, None, caveats
    # Residue present: the shared store must not be opened at all (a mode=ro
    # read mutates the -shm bytes in place), so read a private copy.
    if has_wal:
        caveats.append(
            "the store carried WAL residue; it was read through a private copy "
            "so the operator's files were never opened or modified")
    if has_journal:
        # A hot rollback journal means a transaction was in flight when this
        # ran. immutable=1 would ignore the journal entirely and report the
        # UNCOMMITTED pages as real rows, so this deliberately does NOT take the
        # direct path: staging copies the journal too and SQLite replays the
        # rollback against the copy.
        caveats.append(
            "the store carried an in-flight rollback journal, so an unfinished "
            "transaction was live; tier labels come from a private copy on "
            "which that transaction was rolled back, and so may predate the "
            "transaction rather than reflect it")
    conn, stage = _stage_copy(store_path)
    return conn, stage, caveats


def namespace_tiers(conn, ids) -> tuple:
    """Map each id to a tier derived from its stored namespace.

    Returns ``(tiers, failed_chunks, first_error)``. Ids absent from the store
    (purged, or committed inside the snapshot window) become ``unknown``, which
    is an honest answer. A chunk whose lookup FAILED is a different thing
    entirely -- a corrupt or partially-unreadable store would otherwise report
    every real row as ``unknown`` with no signal that anything went wrong, so
    the failure is counted and handed back for the caller to disclose.
    """
    tiers = {i: TIER_UNKNOWN for i in ids}
    if conn is None or not ids:
        return tiers, 0, None
    id_list = sorted(ids)
    failed = 0
    first_error = None
    # Chunked so a very large log cannot build an unbounded IN clause.
    for start in range(0, len(id_list), 400):
        chunk = id_list[start:start + 400]
        marks = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                "SELECT id, namespace FROM memory WHERE id IN (%s)" % marks,
                chunk).fetchall()
        except sqlite3.Error as exc:
            # Verified against a store whose first page reads fine but whose
            # later pages are corrupt: LIMIT 1 succeeded, this query raised,
            # and the report still exited 0 with every row 'unknown'.
            failed += 1
            if first_error is None:
                first_error = str(exc)
            continue
        for row in rows:
            ns = row["namespace"] or ""
            if ns == GLOBAL_NAMESPACE:
                tiers[row["id"]] = TIER_GLOBAL
            elif ns.startswith("project:"):
                tiers[row["id"]] = TIER_PROJECT
    return tiers, failed, first_error


# -------------------------------------------------------------- aggregation


def build_decision_report(log_path: str, store_path: str) -> dict:
    """Aggregate the decision log into the repeat and composition tables.

    The log is parsed FIRST and the store snapshot taken SECOND, so that every
    id the report must classify is already committed when the store is read.

    TWO TABLES, TWO COUNTING BASES, chosen per quantity rather than by
    accident:

    * ``rows[<id>].delivered`` counts every OCCURRENCE of the id in a parsed
      line's ``ids`` list. That is what makes the repeat rate meaningful: three
      deliveries in one session is three.
    * ``by_tier_moment[<tier>][<moment>].delivered`` counts OCCURRENCES the same
      way, so the tier-summed delivered total always equals the sum of
      ``rows[*].delivered`` -- the composition table is a partition of the
      repeat table by tier and moment.
    * ``by_tier_moment[<tier>][<moment>].withheld`` counts DISTINCT CANDIDATES:
      ``all`` is a candidate list, so a line offering the same id twice offers
      one candidate. Within a line ``withheld = set(all) - set(ids)``,
      order-independent, per the approved plan.

    The two bases differ deliberately. A repeated delivery is a real event worth
    counting; a repeated candidate is one thing that was not chosen.
    """
    lines, caveats = parse_decision_log(log_path)

    delivered_per_id: dict = {}
    per_session: dict = {}
    max_per_session: dict = {}
    sessions_seen: dict = {}
    sessionless_lines = 0
    non_injected_lines = 0
    tier_moment: dict = {}
    candidates: set = set()
    stamps: list = []

    for line in lines:
        stamp = line.get("ts")
        if isinstance(stamp, int):
            stamps.append(stamp)
        moment = _moment_of(line)
        # Occurrence-wise: rows[].delivered counts EVERY appearance of an id,
        # so the tier table must too or the reconciliation invariant breaks on
        # a line that repeats an id. `all` is a CANDIDATE list, so withheld
        # uses set difference -- the same id offered twice is one candidate,
        # not two.
        ids = [i for i in (line.get("ids") or []) if i]
        pool = [i for i in (line.get("all") or []) if i]
        delivered_set = set(ids)
        withheld = sorted(set(pool) - delivered_set)
        candidates.update(ids)
        candidates.update(withheld)

        # DELIVERED is gated on the shared injected-line predicate: a legacy or
        # mixed-version envelope can carry ids on a status=silent line, and
        # counting those would report deliveries that never happened.
        # WITHHELD is not gated -- a candidate pool was offered regardless of
        # whether the line went on to deliver, which is exactly what the
        # delivered-vs-withheld composition measures.
        injected = _is_injected_line(line)
        if not injected:
            non_injected_lines += 1

        if injected:
            for row_id in ids:
                delivered_per_id[row_id] = delivered_per_id.get(row_id, 0) + 1
                sid = line.get("sid")
                if _counts_as_session(sid):
                    sessions_seen.setdefault(row_id, set()).add(sid)
                    key = (row_id, sid)
                    # Running max per row, so a long log stays linear rather
                    # than rescanning every (row, session) pair per row.
                    per_session[key] = per_session.get(key, 0) + 1
                    if per_session[key] > max_per_session.get(row_id, 0):
                        max_per_session[row_id] = per_session[key]
        if injected and ids or withheld:
            buckets = tier_moment.setdefault(moment, {})
            # PER-LINE counting (the approved plan's formula): every id in this
            # line contributes 1 to its own tier bucket. Ids are accumulated as
            # a Counter keyed by id so that a repeat within ONE line is counted
            # once while the SAME id in a LATER line counts again — which is
            # what makes the tier-summed totals reconcile with the sum of
            # rows[*].delivered.
            got = buckets.setdefault("delivered_ids", Counter())
            for row_id in ids if injected else ():
                got[row_id] += 1      # occurrence-wise, NOT the deduped set
            miss = buckets.setdefault("withheld_ids", Counter())
            for row_id in withheld:
                miss[row_id] += 1
        if injected and ids and not _counts_as_session(line.get("sid")):
            # Only lines that actually DELIVERED something are worth the
            # caveat: a sessionless line with no ids contributes nothing to
            # `delivered`, so claiming it "counts toward delivered" would be
            # literally false.
            sessionless_lines += 1

    conn = None
    stage = None
    try:
        conn, stage, open_caveats = open_store_for_report(Path(store_path))
        caveats.extend(open_caveats)
        tiers, failed_chunks, first_error = namespace_tiers(conn, candidates)

        by_tier_moment: dict = {}
        for moment, buckets in sorted(tier_moment.items()):
            for counts, field in ((buckets["delivered_ids"], "delivered"),
                                  (buckets["withheld_ids"], "withheld")):
                for row_id, occurrences in counts.items():
                    tier = tiers.get(row_id, TIER_UNKNOWN)
                    cell = by_tier_moment.setdefault(tier, {}).setdefault(
                        moment, {"delivered": 0, "withheld": 0})
                    cell[field] += occurrences
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        # Surfaced, not swallowed: a leftover staging dir is a full copy of the
        # operator's store sitting in temp, and the operator has to know.
        leftover = _discard_stage(stage)
        if leftover:
            caveats.append(leftover)

    rows = {}
    for row_id in sorted(delivered_per_id):
        rows[row_id] = {
            "delivered": delivered_per_id[row_id],
            "sessions": len(sessions_seen.get(row_id, ())),
            "max_per_session": max_per_session.get(row_id, 0),
            "tier": tiers.get(row_id, TIER_UNKNOWN),
        }

    if sessionless_lines:
        caveats.append(
            "%d delivering line(s) carried no usable sid= (absent, or the "
            "writers' %r placeholder for a host with no session id), so they "
            "count toward delivered but cannot contribute a session"
            % (sessionless_lines, SESSIONLESS_SID))
    if non_injected_lines:
        caveats.append(
            "%d parsed line(s) were not delivery lines (status/reason pairs "
            "the shared injected-line predicate rejects); ids on them are "
            "excluded from delivered rather than counted as deliveries"
            % non_injected_lines)
    if failed_chunks:
        caveats.append(
            "%d id-lookup chunk(s) failed against the store (%s), so the ids "
            "in them are reported under the 'unknown' tier. This is a store "
            "read failure, not an absent row -- do not read those 'unknown' "
            "labels as 'not in the store'" % (failed_chunks, first_error))
    caveats.append(_COUNTING_BASES_CAVEAT)
    caveats.append(_UNDETERMINED_TIER_CAVEAT)
    caveats.append(_SNAPSHOT_CAVEAT)
    caveats.append(_RETENTION_CAVEAT)
    if stamps:
        caveats.append(
            "This run covered decision timestamps %d..%d inclusive; a "
            "before/after comparison is only like-for-like if both runs cover "
            "an overlapping window, because the log family is capped by "
            "rotation." % (min(stamps), max(stamps)))

    return {
        "decision_log": str(log_path),
        "decision_lines": len(lines),
        "ts_first": min(stamps) if stamps else None,
        "ts_last": max(stamps) if stamps else None,
        "rows": rows,
        "by_tier_moment": by_tier_moment,
        "caveats": caveats,
    }


def render_text(report: dict) -> str:
    """Human table. All of this goes to STDOUT only in non-JSON mode; under
    --json stdout stays strictly json.loads-parseable."""
    out = []
    out.append("decision log: %s" % _escape_rendered(report["decision_log"]))
    out.append("decision lines: %d" % report["decision_lines"])
    if report.get("ts_first") is not None:
        out.append("timestamps: %d..%d"
                   % (report["ts_first"], report["ts_last"]))
    out.append("")
    out.append("repeat rate per row id (delivered / sessions / max per session)")
    if not report["rows"]:
        out.append("  (no delivered rows)")
    for row_id, agg in report["rows"].items():
        # Ids come from log text and are only ever printed here, so they are
        # escaped at the rendering boundary. JSON mode leaves them byte-exact.
        out.append("  [%s] tier=%s delivered=%d sessions=%d max_per_session=%d"
                   % (_escape_rendered(row_id), agg["tier"], agg["delivered"],
                      agg["sessions"], agg["max_per_session"]))
    out.append("")
    out.append("delivered vs withheld by tier and moment")
    if not report["by_tier_moment"]:
        out.append("  (no decisions)")
    for tier in sorted(report["by_tier_moment"]):
        for moment in sorted(report["by_tier_moment"][tier]):
            cell = report["by_tier_moment"][tier][moment]
            out.append("  tier=%-8s moment=%-14s delivered=%d withheld=%d"
                       % (tier, moment, cell["delivered"], cell["withheld"]))
    if report["caveats"]:
        out.append("")
        out.append("caveats")
        for note in report["caveats"]:
            out.append("  - %s" % _escape_rendered(note))
    return "\n".join(out)


def _emit_json(payload: dict) -> None:
    """Write exactly one compact, sorted, LF-terminated JSON document.

    ``sys.stdout.buffer.write`` rather than ``print``: a Windows text-mode
    newline would become CRLF and break the byte contract (the same shape
    ``rescan-secrets`` uses in cli.py).
    """
    text = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def main(argv: list | None = None) -> int:
    """`store.py report --decision-log <path> [--json]`."""
    ap = argparse.ArgumentParser(prog="store.py report")
    ap.add_argument("--decision-log", dest="decision_log", default=None,
                    help="explicit decision-log path (default: "
                         "<data dir>/zmem-decisions.log, falling back to the "
                         "legacy zmem-bg.log)")
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="emit one machine-readable JSON document on stdout")
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    data_dir = _resolve_data_dir()
    try:
        log_path = resolve_log_path(args.decision_log, data_dir)
        store_path = _resolve_store_path()
        report = build_decision_report(log_path, store_path)
    except ReportError as exc:
        return _refuse(str(exc), args.as_json)
    except Exception as exc:  # noqa: BLE001 - see _refuse for why this is broad
        # An unexpected failure must still honour the --json promise: a caller
        # piping stdout into jq gets valid JSON either way, and the traceback
        # still reaches stderr so the bug stays debuggable.
        import traceback
        traceback.print_exc()
        return _refuse("unexpected %s: %s" % (type(exc).__name__, exc),
                       args.as_json)

    if args.as_json:
        _emit_json(report)
        return 0

    print(render_text(report))
    return 0


def _refuse(message: str, as_json: bool) -> int:
    """Refuse with exit 2, keeping stdout valid JSON when --json was asked for.

    The documented --json contract is that stdout is exactly one compact JSON
    document, so it can be piped into jq unchanged. Emitting nothing there made
    ``json.loads(r.stdout)`` raise on every refusal, which is exactly when a
    caller most needs to read the error programmatically. The human line still
    goes to stderr, so text-mode callers see the same message as before.
    """
    if as_json:
        _emit_json({"error": message, "ok": False})
    print("[zmem] report: %s" % message, file=sys.stderr)
    return 2