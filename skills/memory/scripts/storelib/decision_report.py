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

from storelib.false_inject import _moment_of
from storelib.miss_rate import parse_bg_log

DECISIONS_LOG_NAME = "zmem-decisions.log"
LEGACY_LOG_NAME = "zmem-bg.log"

TIER_PROJECT = "project"
TIER_GLOBAL = "global"
TIER_UNKNOWN = "unknown"
GLOBAL_NAMESPACE = "user:global"

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
    "the sum of rows[*].delivered. withheld counts DISTINCT CANDIDATES: it is "
    "the number of ids appearing in a line's all list that are not in the same "
    "line's ids list at that moment, so the same id offered twice is one "
    "candidate, not two."
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
    from storelib import schema
    return str(Path(_resolve_store_path()).parent)


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


def parse_decision_log(log_path: str) -> list:
    """Rotation-aware, CRLF-tolerant, never-raising parse of the log family.

    Delegates to the single shared parser rather than forking one: it reads
    ``<log>.1 .. <log>.N`` then the active file, skips torn and non-decision
    lines, and returns ``[]`` for a missing file."""
    try:
        return parse_bg_log(Path(log_path))
    except Exception:
        return []


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
    """Copy ``store.sqlite`` + ``-wal`` into a private temp dir and open the
    copy. ``-shm`` is intentionally not copied. Returns (conn, staging_dir);
    the caller owns closing both."""
    stage = tempfile.mkdtemp(prefix="zmem-report-stage-")
    conn = None
    try:
        for name in (str(store_path), str(store_path) + "-wal"):
            src = Path(name)
            if not src.exists():
                continue
            try:
                shutil.copy2(src, Path(stage, src.name))
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
        shutil.rmtree(stage, ignore_errors=True)
        raise


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
    if not wal.exists():
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
    caveats.append(
        "the store carried WAL residue; it was read through a private copy so "
        "the operator's files were never opened or modified")
    conn, stage = _stage_copy(store_path)
    return conn, stage, caveats


def namespace_tiers(conn, ids) -> dict:
    """Map each id to a tier derived from its stored namespace. Ids absent
    from the store (purged, or committed inside the snapshot window) become
    ``unknown`` rather than failing the report."""
    tiers = {i: TIER_UNKNOWN for i in ids}
    if conn is None or not ids:
        return tiers
    id_list = sorted(ids)
    # Chunked so a very large log cannot build an unbounded IN clause.
    for start in range(0, len(id_list), 400):
        chunk = id_list[start:start + 400]
        marks = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                "SELECT id, namespace FROM memory WHERE id IN (%s)" % marks,
                chunk).fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            ns = row["namespace"] or ""
            if ns == GLOBAL_NAMESPACE:
                tiers[row["id"]] = TIER_GLOBAL
            elif ns.startswith("project:"):
                tiers[row["id"]] = TIER_PROJECT
    return tiers


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
    lines = parse_decision_log(log_path)

    delivered_per_id: dict = {}
    per_session: dict = {}
    max_per_session: dict = {}
    sessions_seen: dict = {}
    sessionless_lines = 0
    tier_moment: dict = {}
    candidates: set = set()

    for line in lines:
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

        for row_id in ids:
            delivered_per_id[row_id] = delivered_per_id.get(row_id, 0) + 1
            sid = line.get("sid")
            if sid:
                sessions_seen.setdefault(row_id, set()).add(sid)
                key = (row_id, sid)
                # Running max per row, so a long log stays linear rather than
                # rescanning every (row, session) pair per reported row.
                per_session[key] = per_session.get(key, 0) + 1
                if per_session[key] > max_per_session.get(row_id, 0):
                    max_per_session[row_id] = per_session[key]
        if ids or withheld:
            buckets = tier_moment.setdefault(moment, {})
            # PER-LINE counting (the approved plan's formula): every id in this
            # line contributes 1 to its own tier bucket. Ids are accumulated as
            # a Counter keyed by id so that a repeat within ONE line is counted
            # once while the SAME id in a LATER line counts again — which is
            # what makes the tier-summed totals reconcile with the sum of
            # rows[*].delivered.
            got = buckets.setdefault("delivered_ids", Counter())
            for row_id in ids:          # occurrence-wise, NOT the deduped set
                got[row_id] += 1
            miss = buckets.setdefault("withheld_ids", Counter())
            for row_id in withheld:
                miss[row_id] += 1
        if not line.get("sid") and ids:
            # Only lines that actually DELIVERED something are worth the
            # caveat: a sidless line with no ids contributes nothing to
            # `delivered`, so claiming it "counts toward delivered" would be
            # literally false.
            sessionless_lines += 1

    conn = None
    stage = None
    caveats: list = []
    try:
        conn, stage, open_caveats = open_store_for_report(Path(store_path))
        caveats.extend(open_caveats)
        tiers = namespace_tiers(conn, candidates)

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
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)

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
            "%d decision line(s) carried no sid=, so they count toward "
            "delivered but cannot contribute a session" % sessionless_lines)
    caveats.append(_COUNTING_BASES_CAVEAT)
    caveats.append(_UNDETERMINED_TIER_CAVEAT)
    caveats.append(_SNAPSHOT_CAVEAT)

    return {
        "decision_log": str(log_path),
        "decision_lines": len(lines),
        "rows": rows,
        "by_tier_moment": by_tier_moment,
        "caveats": caveats,
    }


def render_text(report: dict) -> str:
    """Human table. All of this goes to STDOUT only in non-JSON mode; under
    --json stdout stays strictly json.loads-parseable."""
    out = []
    out.append("decision log: %s" % report["decision_log"])
    out.append("decision lines: %d" % report["decision_lines"])
    out.append("")
    out.append("repeat rate per row id (delivered / sessions / max per session)")
    if not report["rows"]:
        out.append("  (no delivered rows)")
    for row_id, agg in report["rows"].items():
        out.append("  [%s] tier=%s delivered=%d sessions=%d max_per_session=%d"
                   % (row_id, agg["tier"], agg["delivered"], agg["sessions"],
                      agg["max_per_session"]))
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
            out.append("  - %s" % note)
    return "\n".join(out)


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
        print("[zmem] report: %s" % exc, file=sys.stderr)
        return 2

    if args.as_json:
        # One compact, sorted, LF-terminated document. sys.stdout.buffer.write
        # is used because a Windows text-mode newline would become CRLF and
        # break the byte contract (the rescan-secrets shape, cli.py:3281).
        payload = json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
        sys.stdout.buffer.write(payload.encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0

    print(render_text(report))
    return 0