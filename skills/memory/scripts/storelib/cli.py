from __future__ import annotations

import argparse
import calendar
import contextlib
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
import uuid
import glob
from datetime import datetime, timezone
from pathlib import Path
from storelib.backup import BACKUP_DEFAULT_RETENTION, CONSOLIDATE_LOCK_STALE_SECONDS, SENTINEL_SWEEP_DAYS_DEFAULT, SNAPSHOT_GLOB, _acquire_lock, _release_lock, cmd_backup, cmd_restore, cmd_sweep
from storelib.consolidate import CONSOLIDATE_DEFAULT_THRESHOLD, consolidate
from storelib.organize import organize
from storelib.entity import ENTITY_KINDS, cmd_entity_list, cmd_entity_merge
from storelib.links import LINK_RELATIONS, cmd_contradict, cmd_links
from storelib.mine import cmd_corrections, cmd_failures, cmd_mine_history, cmd_mine_history_adapters, cmd_queue_clear, cmd_queue_list, cmd_promote_store
from storelib.promote import promote_memory
# _reembed: NOT called here (dispatch uses reembed_embeddings) but kept as
# this module's re-export surface for `storelib/__init__.py` and legacy
# importers — removing it broke that chain.
from storelib.recall import _reembed, explain_recall, get_memory, list_memory, recall_memory, recent_memory, stats
from storelib.cross_encoder import cli_allowed as _ce_cli_allowed
from storelib.recall import reembed_embeddings
from storelib.schema import ALLOWED_SIGNALS, ALLOWED_TYPES, ALLOWED_TAINTS, CAPTURE_MODES, GLOBAL_NAMESPACE, STORE_PATH, _acquire_writer_lease, assert_embedding_compatible, _prepare_store, _release_writer_lease, _wait_for_maintenance_clear, connect
from storelib.sync import EXPORT_PACK_DEFAULT_GLOBAL_LIMIT, EXPORT_PACK_DEFAULT_MAX_BYTES, EXPORT_PACK_DEFAULT_MIN_CONFIDENCE, EXPORT_PACK_DEFAULT_PROJECT_LIMIT, cmd_export_jsonl, cmd_export_pack, cmd_ingest_jsonl
from storelib.write import CapturePolicyRefusal, ContentTooLarge, FeedbackTargetError, _GLOBAL_NEAR_MISS_STEMS, _global_near_miss_key, add_memory, feedback_memory, rekey_namespace, supersede_memory, update_memory
from storelib.tune import tune_weights

def _auto_near_miss_rekey(conn: sqlite3.Connection, force_off: bool = False) -> None:
    """Issue #71 C: run the existing near-miss remediation automatically on
    store open. Rows stranded under a global-near-miss namespace (``global``,
    ``userglobal``, ``users:global``, …) are unreachable from every automatic
    hook; the issue requires the ALREADY-EXISTING guarded remediation
    (``rekey_namespace(near_miss_global=True)`` — PRR-001 canonical exclusion,
    PRR-002/013 destination validation, entity relink, BEGIN IMMEDIATE) to run
    without an operator flag.

    Deliberately narrow: only global-near-miss stems are rekeyed. Project
    namespace splits (``project:foo`` vs ``project:github.com/o/foo``) are a
    per-row operator decision (#97) and are never touched here.

    Behavior contract:
    - Healthy store cost: one indexed SELECT DISTINCT, zero writes, zero output.
    - When stranded rows exist, ``rekey_namespace``'s own prints are rerouted
      to stderr so ``--json`` stdout stays parseable, plus one summary line.
    - Fail-open: any error prints a skip line and never fails the command.
    - Kill switches: ``--no-auto-rekey`` (any subcommand) or
      ``ZMEM_AUTO_REKEY=0``.

    Concurrency: the SELECT-then-UPDATE window between two concurrent opens is
    benign — both compute the same source set, the UPDATE is idempotent, and
    ``BEGIN IMMEDIATE`` + busy_timeout + the ``_commit`` retry inside
    ``rekey_namespace`` serialize the write. No writer lease is taken: this
    runs on read commands too, and read commands must not serialize behind a
    lease (issue #21 hot-path contract).
    """
    if force_off:
        return
    if os.environ.get("ZMEM_AUTO_REKEY", "1").strip() == "0":
        return
    try:
        rows = conn.execute(
            "SELECT DISTINCT namespace FROM memory WHERE superseded_at IS NULL"
        ).fetchall()
        stranded = [
            r["namespace"] for r in rows
            if r["namespace"] != GLOBAL_NAMESPACE
            and _global_near_miss_key(r["namespace"]) in _GLOBAL_NEAR_MISS_STEMS
        ]
        if not stranded:
            return
        with contextlib.redirect_stdout(sys.stderr):
            rekeyed = rekey_namespace(conn, near_miss_global=True)
        print(
            f"[zmem] auto-rekeyed {rekeyed} stranded row(s) from "
            f"{', '.join(repr(s) for s in stranded)} -> {GLOBAL_NAMESPACE!r} "
            "(issue #71 C; ZMEM_AUTO_REKEY=0 or --no-auto-rekey disables)",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"[zmem] near-miss auto-remediation skipped: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)


def nonnegative_int(value: str) -> int:
    """argparse type= for flags fed straight into a SQL LIMIT: SQLite treats a
    negative LIMIT as UNBOUNDED, so a negative --project-limit/--global-limit
    would silently defeat the cap instead of erroring. Reject it up front."""
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative integer, got {value!r}")
    return n


def positive_int(value: str) -> int:
    """argparse type= for top-k cuts: k must be >= 1 (k=0 would make
    tune-weights evaluate an empty result set and report all-miss metrics as
    a successful run)."""
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer, got {value!r}")
    return n

def _iso8601(value: str) -> str:
    """argparse type= for --as-of (issue #58, 3.6): accept an ISO-8601
    timestamp, validate strictly (garbage → argparse error), and return the
    canonical Z-suffixed UTC form the store's lexicographic predicate
    compares against. PRR-022 fix: normalization now covers ANY zone offset
    (e.g. +05:30), not just +00:00 — the store compares strings, and
    'Z' vs '+' ASCII ordering silently mis-filters otherwise. The same
    normalization is applied at recall_memory/recent_memory entry for
    programmatic callers; this type= additionally enforces argparse-level
    validation.
    """
    from datetime import datetime
    if not value:
        raise argparse.ArgumentTypeError("--as-of requires a non-empty ISO-8601 timestamp")
    normalized = value.strip()
    try:
        dt = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 for --as-of: {exc}")
    if dt.tzinfo is not None:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + "Z"

def main():
    # Production-stream encoding hardening (issue #62 editorial round, Claude
    # Code F-005): every ``print`` in store.py, including consolidate's
    # contested-cluster previews and organize's NLI diagnostics, can carry
    # non-ANSI memory content. Under a legacy/redirected stdout codec that
    # would raise UnicodeEncodeError mid-run — and because consolidate commits
    # its cadence-clock write BEFORE those crash points, one poisoned cluster
    # would silently suppress the whole maintenance job for the gate window.
    # Reconfigure ONCE at the single process entry so every site is covered;
    # errors="replace" guarantees a run can never die on a cosmetic print.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # stream not reconfigurable (e.g. detached) — leave as-is

    ap = argparse.ArgumentParser(prog="store.py", description="ZMem semantic store")
    # Issue #71 C: the automatic near-miss rekey (see _auto_near_miss_rekey)
    # runs on every store-opening command, so its opt-out must exist on every
    # subcommand — shared via a parent parser rather than 40 copies.
    _auto_rekey_parent = argparse.ArgumentParser(add_help=False)
    _auto_rekey_parent.add_argument(
        "--no-auto-rekey", action="store_true",
        help="disable the automatic near-miss namespace rekey for this "
             "invocation (same as ZMEM_AUTO_REKEY=0)")

    def _add_parser(name: str, **kwargs):
        kwargs.setdefault("parents", [_auto_rekey_parent])
        return sub.add_parser(name, **kwargs)

    sub = ap.add_subparsers(dest="cmd", required=True)

    _add_parser("init", help="create the store if absent (idempotent)")

    p_add = _add_parser("add", help="add a memory")
    p_add.add_argument("--namespace", required=True)
    p_add.add_argument("--type", required=True, choices=list(ALLOWED_TYPES))
    p_add.add_argument("--content", required=True,
                       help="the memory text; the literal '-' reads content from "
                            "stdin (use for payloads near the content cap — "
                            "Windows argv caps far below MAX_CONTENT_CHARS)")
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--source-ref", default="")
    p_add.add_argument("--confidence", type=float, default=None)
    p_add.add_argument("--signal", default="none", choices=list(ALLOWED_SIGNALS))
    p_add.add_argument("--taint", default=None, choices=list(ALLOWED_TAINTS),
                       help="provenance/trust origin (issue #59, 4.7): "
                            "trusted_internal (human/closeout/grounded), "
                            "untrusted_tool (agent/MCP/Hermes/mine-history), or "
                            "untrusted_web (web fetch). Default derives from "
                            "--signal: grounded signals are trusted_internal, "
                            "`none` is untrusted_tool. Unknown values are refused.")
    p_add.add_argument("--capture-mode", default=None, choices=list(CAPTURE_MODES),
                       help="manual/reviewed keep the original text with warnings; "
                            "auto redacts likely secrets by default before writing")
    p_add.add_argument("--json", action="store_true",
                       help="print a structured write result (id, result, warnings) "
                            "as JSON on stdout instead of the human lines "
                            "(issue #65, 10.8 — consumed by the MCP/Hermes add "
                            "surfaces; stderr advisory lines are unchanged)")

    p_recall = _add_parser("recall", help="recall relevant memories")
    p_recall.add_argument("--query", required=True)
    p_recall.add_argument("--namespace", default=None)
    p_recall.add_argument("--limit", type=nonnegative_int, default=5)
    p_recall.add_argument("--json", action="store_true")
    p_recall.add_argument("--hybrid", action="store_true",
                          help="explicit hybrid BM25+vector recall (alias; hybrid is the "
                               "default when embeddings are available — use --no-hybrid to "
                               "force lexical, issue #58 3.3)")
    p_recall.add_argument("--no-hybrid", action="store_true",
                          help="force lexical-only recall even when embeddings are available "
                               "(issue #58 3.3)")
    p_recall.add_argument("--no-mmr", action="store_true",
                          help="disable MMR diversity re-ranking for this recall "
                               "(issue #60 5.5). By default recall re-orders the "
                               "fused candidate set with Maximal Marginal Relevance "
                               "so near-paraphrase duplicates do not crowd out "
                               "distinct facts; --no-mmr returns pure composite-"
                               "score order. Independent of --no-hybrid (the two "
                               "lanes can be mixed freely). Lambda default 0.7, "
                               "env ZMEM_MMR_LAMBDA.")
    p_recall.add_argument("--no-bump", action="store_true",
                          help="suppress the retrieval_count/last_retrieved write; record "
                               "surfaced_count/last_surfaced instead (passive recall, used "
                               "by hook-driven recall so subagent fan-out does not create N "
                               "concurrent retrieval_count writers — issue #21)")
    p_recall.add_argument("--for-injection", action="store_true",
                          help="passive INJECTION lane (issue #114, P2-3): apply the "
                               "selective inject gate and token budget INSIDE this call, "
                               "return only the rendered rows, and record exactly one "
                               "surfaced_count event per rendered QUERY-MATCHED row "
                               "(link/unfold neighbors render but are never counted). "
                               "Implies --no-bump; the --json envelope gains reason and "
                               "candidate_ids (pre-gate ids) for the hook decision log.")
    p_recall.add_argument("--as-of", type=_iso8601, default=None,
                          help="temporal predicate (issue #59, 4.4): only return "
                               "rows VALID at as_of — valid_from <= as_of AND "
                               "(valid_until empty OR valid_until > as_of). "
                               "May return historically-superseded rows that were "
                               "valid at that instant. Absent = as of now (live "
                               "rows only).")
    p_recall.add_argument("--include-global", action="store_true",
                          help="also surface user:global rows (project-first merge; "
                               "a global row never crowds out a project row). The "
                               "automatic hooks pass this so cross-project lessons "
                               "reach project-scoped sessions (issue #18).")
    p_recall.add_argument("--global-limit", type=nonnegative_int, default=3,
                          help="max user:global rows when --include-global is set "
                               f"(default 3). No effect without --include-global.")
    p_recall.add_argument("--link-hops", type=int, choices=[0, 1], default=1,
                          help="v11 (issue #61, 6.3): walk related/supports links "
                               "ONE hop from each recalled memory and append up to "
                               "--link-budget neighbor rows (contradicts neighbors "
                               "only if they survive the confidence floor, tagged "
                               "[CONTESTED LINK]). Default 1; 0 disables expansion.")
    p_recall.add_argument("--link-budget", type=nonnegative_int, default=2,
                          help="max extra rows appended by 1-hop link expansion "
                               "(default 2; 0 disables expansion — equivalent to "
                               "--link-hops 0).")
    p_recall.add_argument("--explain", action="store_true",
                          help="issue #82: read-only retrieval debugger. Runs the real "
                               "pipeline (zero writes) and prints one blameline per "
                               "verdict explaining why a row did or did not surface. "
                               "Combine with --target to debug a specific row, and "
                               "--json for the machine-readable envelope. Never bumps, "
                               "never unfolds.")
    p_recall.add_argument("--target", default=None,
                          help="with --explain: the row to explain — a memory id "
                               "(full or unambiguous prefix) or a content fragment "
                               "(case-insensitive substring, then token overlap). "
                               "Multiple matches get one verdict per id.")
    p_recall.add_argument("--no-unfold", action="store_true",
                          help="issue #82: disable the change-intent lineage unfold "
                               "(explicit recall only: change-intent queries like "
                               "'what changed about X' otherwise append budgeted "
                               "[PREVIOUSLY] update_of predecessors). Passive "
                               "surfaces never unfold regardless (--no-bump).")

    p_recent = _add_parser("recent", help="most recent live memories (no FTS, admin pull)")
    p_recent.add_argument("--namespace", default=None)
    p_recent.add_argument("--limit", type=nonnegative_int, default=5)
    p_recent.add_argument("--min-confidence", type=float, default=0.5)
    p_recent.add_argument("--json", action="store_true")
    p_recent.add_argument("--no-bump", action="store_true",
                          help="suppress the retrieval_count/last_retrieved write; record "
                               "surfaced_count/last_surfaced instead (passive recent, used "
                               "by hook-driven subagent recall — issue #21)")
    p_recent.add_argument("--for-injection", action="store_true",
                          help="passive INJECTION lane (issue #114, P2-3): apply the "
                               "selective inject gate and token budget INSIDE this call, "
                               "return only the rendered rows, and record exactly one "
                               "surfaced_count event per rendered row. Implies --no-bump; "
                               "the --json envelope gains reason and candidate_ids "
                               "(pre-gate ids) for the hook decision log.")
    p_recent.add_argument("--include-global", action="store_true",
                          help="also surface user:global rows (project-first merge). "
                               "The automatic hooks pass this so cross-project "
                               "lessons reach project-scoped sessions (issue #18).")
    p_recent.add_argument("--global-limit", type=nonnegative_int, default=3,
                          help="max user:global rows when --include-global is set "
                               f"(default 3). No effect without --include-global.")
    p_recent.add_argument("--as-of", type=_iso8601, default=None,
                          help="temporal predicate (issue #59, 4.4): only return "
                               "rows VALID at as_of (valid_from <= as_of AND "
                               "(valid_until empty OR valid_until > as_of)).")

    p_search = _add_parser("search", help="keyword search (no confidence floor)")
    p_search.add_argument("--text", required=True)
    p_search.add_argument("--namespace", default=None)
    p_search.add_argument("--limit", type=nonnegative_int, default=10)
    p_search.add_argument("--include-global", action="store_true",
                          help="also surface user:global rows (project-first merge). "
                               "Use this instead of going unscoped when you want the "
                               "global tier unioned in but still want a per-tier "
                               "budget (issue #18).")
    p_search.add_argument("--global-limit", type=nonnegative_int, default=3,
                          help="max user:global rows when --include-global is set "
                               f"(default 3). No effect without --include-global.")
    p_search.add_argument("--no-bump", action="store_true",
                          help="suppress the retrieval_count/last_retrieved write; record "
                               "surfaced_count/last_surfaced instead (passive search). Search "
                               "defaults to bumping retrieval like recall; pass this for an "
                               "audit query that still counts the surface — issue #21")
    p_search.add_argument("--as-of", type=_iso8601, default=None,
                          help="temporal predicate (issue #59, 4.4): only return "
                               "rows VALID at as_of (valid_from <= as_of AND "
                               "(valid_until empty OR valid_until > as_of)).")
    p_search.add_argument("--json", action="store_true",
                          help="print the result envelope {results, count, omitted, "
                               "injection_risk, tokens_used, tokens_budget} (issue "
                               "#65, 10.8) — plain output is unchanged")

    p_sup = _add_parser("supersede", help="tombstone a memory")
    p_sup.add_argument("--id", required=True)
    p_sup.add_argument("--reason", default="")

    p_inv = _add_parser(
        "invalidate",
        help="tombstone a memory because the fact is no longer true "
             "(supersede with a REQUIRED reason — issue #59, 4.3)",
        description="`invalidate` is the preferred way to record \"this fact is "
                    "no longer true\": it tombstones the row (superseded_at=now, "
                    "valid_until=now) and REQUIRES --reason so the correction is "
                    "auditable. `supersede` remains for general tombstone use "
                    "(e.g. consolidated/pruned rows) where a reason is optional.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_inv.add_argument("--id", required=True, help="id of the memory to invalidate")
    p_inv.add_argument("--reason", required=True,
                       help="why the fact is no longer true (REQUIRED)")

    p_upd = _add_parser(
        "update",
        help="append-only knowledge update: replace a memory, keeping history "
             "(issue #59, 4.2)",
        description="Creates a NEW live row carrying the new content, tombstones "
                    "the target row (superseded_at=now, valid_until=now, "
                    "supersede_reason='updated'), and links the new row back to it "
                    "via update_of. Namespace/type/tags/source_ref/confidence/"
                    "signal are copied from the target unless overridden; the old "
                    "row's content is NEVER mutated. Unknown or already-superseded "
                    "ids are refused (exit 2, nothing written). Dedup runs against "
                    "OTHER live rows (the replaced row is excluded). --as-of before "
                    "the update returns the OLD content; after returns the NEW.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_upd.add_argument("--id", required=True, help="id of the live memory to update")
    p_upd.add_argument("--content", required=True,
                       help="the new content; the literal '-' reads content from "
                            "stdin (use for payloads near the content cap — "
                            "Windows argv caps far below MAX_CONTENT_CHARS)")
    p_upd.add_argument("--namespace", default=None)
    p_upd.add_argument("--type", default=None, choices=list(ALLOWED_TYPES))
    p_upd.add_argument("--tags", default=None)
    p_upd.add_argument("--source-ref", default=None)
    p_upd.add_argument("--confidence", type=float, default=None)
    p_upd.add_argument("--signal", default=None, choices=list(ALLOWED_SIGNALS))
    p_upd.add_argument("--taint", default=None, choices=list(ALLOWED_TAINTS),
                       help="provenance/trust origin override (default: inherit the "
                            "target's lineage, worst-of with the caller's origin)")
    p_upd.add_argument("--capture-mode", default=None, choices=list(CAPTURE_MODES),
                       help="same capture policy as `add` (manual/reviewed keep text "
                            "with warnings; auto redacts secrets)")
    p_upd.add_argument("--json", action="store_true",
                       help="print a structured write result (id, result, created_new, "
                            "warnings) as JSON on stdout instead of the human lines "
                            "(issue #65, 10.8 — consumed by the MCP/Hermes update "
                            "surfaces; stderr advisory lines are unchanged)")

    p_get = _add_parser(
        "get",
        help="show a memory by id",
        description="Show one memory row as JSON (binary columns render as a "
                    "'<N-byte blob>' marker). Exit contract: 0 + JSON on "
                    "stdout when found; 1 with the stable stderr line "
                    "`[zmem] no memory with id <id>` when no row has that id "
                    "— the same not-found code as `supersede`, never a "
                    "traceback.")
    p_get.add_argument("--id", required=True,
                       help="id of the memory to show")

    p_list = _add_parser("list", help="list memories")
    p_list.add_argument("--namespace", default=None)
    p_list.add_argument("--limit", type=nonnegative_int, default=50)
    p_list.add_argument("--include-superseded", action="store_true")

    _add_parser("stats", help="store statistics")

    # Print only the resolved store path (the 6-level ZMEM_STORE > ZMEM_DATA >
    # CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem > legacy chain). Lets
    # scripts/tooling query the path without parsing `stats` output (#39 E5).
    _add_parser("path", help="print the resolved store path")

    # Run the three session-start cadence ops (consolidate, backup --if-due,
    # sweep) in ONE process instead of three detached python invocations
    # (#39 E9). Each op keeps its own cadence gate / single-flight lock / exit
    # semantics — this entrypoint only sequences them.
    p_session_cadence = _add_parser(
        "session-cadence",
        help="run consolidate + backup --if-due + sweep in one process "
             "(session-start cadence batch)")
    p_session_cadence.add_argument("--backup-retention", type=int,
                                   default=BACKUP_DEFAULT_RETENTION,
                                   help=f"backup retention in days (default {BACKUP_DEFAULT_RETENTION})")

    _add_parser("rebuild-fts", help="rebuild the FTS5 index from scratch")

    # Issue #63, 8.3: reembed grew flags. Flagless remains the legacy backfill
    # with byte-identical behavior; --all is the operator-grade rebuild.
    p_reembed = _add_parser(
        "reembed",
        help="backfill missing embeddings (flagless), or rebuild every live "
             "embedding under a profile (--all)",
    )
    p_reembed.add_argument("--all", action="store_true",
                           help="rebuild EVERY live memory's embedding (not "
                                "just missing ones); recreates memory_vec at "
                                "the profile's dimension when it changes")
    try:
        import embed_profiles as _ep_mod
        _PROFILE_CHOICES = sorted(_ep_mod.PROFILES)
    except ImportError:  # pragma: no cover — repo always ships it
        from embed_profiles import PROFILES as _P  # type: ignore

        _PROFILE_CHOICES = sorted(_P)
    p_reembed.add_argument("--profile", choices=_PROFILE_CHOICES, default=None,
                           help="with --all: embedding profile to convert "
                                "the store to (default: active "
                                "ZMEM_EMBED_PROFILE or minilm)")
    p_reembed.add_argument("--batch", type=nonnegative_int, default=64,
                           help="progress-report granularity in rows "
                                "(stderr pacing only; does not affect "
                                "transaction atomicity; values < 1 reset "
                                "to the default 64)")
    p_reembed.add_argument("--dry-run", action="store_true",
                           help="report what --all would change; writes nothing")
    p_reembed.add_argument("--confirm", action="store_true",
                           help="required by --all --profile fake when the "
                                "store holds committed non-fake vectors "
                                "(conversion overwrites them with placeholders)")


    p_consolidate = _add_parser("consolidate", help="merge near-duplicate memories")
    p_consolidate.add_argument("--threshold", type=float,
                               default=CONSOLIDATE_DEFAULT_THRESHOLD)
    p_consolidate.add_argument("--prune", action="store_true",
                               help="also supersede low-value never-retrieved memories")
    p_consolidate.add_argument("--dry-run", action="store_true",
                               help="show what would be consolidated without changing anything")
    p_consolidate.add_argument("--namespace", default=None,
                               help="limit consolidation to a specific namespace")
    p_consolidate.add_argument("--force", action="store_true",
                               help="bypass the cadence gate and run consolidation now")
    p_consolidate.add_argument("--merge-contested", action="store_true",
                               help="also merge contested (mixed negation-polarity) clusters; "
                                    "use only for confirmed heuristic false positives — by "
                                    "default they are reported, never merged")
    p_consolidate.add_argument("--json", action="store_true",
                               help="print a machine-readable run report (contested clusters "
                                    "included) as the ONLY stdout content; human output goes "
                                    "to stderr")

    # Sleep-time organize (issue #62). NOT flagless: it deliberately exposes
    # --prune/--dry-run/--force/--json (each wired to a real behavior below —
    # there is no unwired flag; see the test's FLAGLESS_SUBCMDS allowlist).
    p_organize = _add_parser(
        "organize",
        help="sleep-time organization: bounded-episode consolidation, entity/link "
             "backfill, topic clustering, hierarchical extractive summaries, "
             "compression (issue #62)")
    p_organize.add_argument("--prune", action="store_true",
                            help="also supersede low-value never-retrieved memories "
                                 "(unrecalled prune extension, issue #62 7.6)")
    p_organize.add_argument("--dry-run", action="store_true",
                            help="show what would be organized without changing anything")
    p_organize.add_argument("--force", action="store_true",
                            help="bypass the shared cadence gate and run organize now")
    p_organize.add_argument("--json", action="store_true",
                            help="print a machine-readable run report as the ONLY stdout "
                                 "content; human output goes to stderr")

    p_promote = _add_parser("promote", help="promote high-confidence lessons to SKILL.md files")
    # Issue #71 E: merge a leftover second store into this (canonical) one.
    p_promote_store = _add_parser(
        "promote-store",
        help="merge every row of another zmem store into this one "
             "(idempotent; doctor's second-stores check recommends this)")
    p_promote_store.add_argument("--from", dest="from_path", required=True,
                                 help="path to the source store.sqlite "
                                      "(opened read-only; newer source "
                                      "schemas are refused)")
    p_promote_store.add_argument("--dry-run", action="store_true",
                                 help="report would-promote tallies and "
                                      "defaulted fields without writing")
    p_promote.add_argument("--dry-run", action="store_true",
                           help="show promotion candidates without creating skills")
    p_promote.add_argument("--id", default=None,
                           help="promote a specific memory by UUID")
    p_promote.add_argument("--namespace", default=None,
                           help="limit candidates to a specific namespace")
    p_promote.add_argument("--description", default=None,
                           help="override the synthesized trigger description verbatim "
                                "(used with --id --confirm)")
    p_promote.add_argument("--confirm", action="store_true",
                           help="REQUIRED to write. Promotion creates a SKILL.md in every dir "
                                "in the review-candidate area; add --install-approved for the "
                                "explicit live install into ZMEM_SKILLS_DIRS (default: both "
                                "~/.claude/skills and ~/.zcode/skills). --id alone refuses "
                                "with exit 2. --dry-run needs no confirmation (and lists ALL "
                                "candidates — it ignores --id).")
    p_promote.add_argument("--install-approved", action="store_true",
                           help="explicitly install the generated SKILL.md into the live "
                                "skills dirs after writing the review candidate")

    # v12 (issue #64, 9.4): explicit usage-feedback CLI. The ONLY writer of
    # applied_count / violated_count anywhere in the codebase — hooks,
    # --no-bump recall, PreCompact, and Hermes prefetch never touch them.
    p_feedback = _add_parser(
        "feedback",
        help="record explicit usage feedback on one memory (Voyager counters; "
             "feeds the promote ladder)")
    p_feedback.add_argument("--id", required=True,
                            help="UUID of the memory the feedback is about")
    p_feedback_group = p_feedback.add_mutually_exclusive_group(required=True)
    p_feedback_group.add_argument("--applied", action="store_true",
                                  help="the memory helped: increments applied_count. "
                                       "applied_count >= 3 with violated_count == 0 makes a "
                                       "lesson promote-eligible (see `promote --dry-run`).")
    p_feedback_group.add_argument("--violated", action="store_true",
                                  help="the memory misled: increments violated_count. The "
                                       "2nd violation applies a ONE-TIME -0.15 trust_score "
                                       "drop (signal is never changed); any violation makes "
                                       "the row promote-ineligible.")

    # v12 (issue #64, 9.6): offline weight tuning. Dry-run ONLY — suggested
    # W_* weights are computed in memory from the gold set; nothing is ever
    # written. Applying weights is a documented manual edit of the W_* module
    # constants in storelib/recall.py (SKILL.md §tune-weights).
    p_tune = _add_parser(
        "tune-weights",
        help="suggest recall scoring weights from a gold set (dry-run only; "
             "writes nothing)")
    p_tune.add_argument("--dry-run", action="store_true",
                        help="REQUIRED (the command is analysis-only): evaluate the "
                             "current weights and a deterministic hill-climb over "
                             "candidate weights against the gold set")
    p_tune.add_argument("--gold", required=True,
                        help="path to the gold JSONL (build one with "
                             "scripts/eval_adapters.py, or use eval/gold.jsonl against "
                             "a fixture-built store — never the operator home store)")
    p_tune.add_argument("--k", type=positive_int, default=5,
                        help="top-k cut for hit@k (default 5; applied to gold "
                             "items that do not set their own 'k')")

    p_rekey = _add_parser(
        "rekey-namespace",
        help="admin: rewrite the namespace of live rows (remediate stranded "
             "global-near-miss rows so they surface again)")
    p_rekey.add_argument("--from", dest="from_namespace", default=None,
                         help="source namespace to rekey from (required unless "
                              "--near-miss-global is set). Case-sensitive exact match.")
    p_rekey.add_argument("--to", dest="to_namespace", default=GLOBAL_NAMESPACE,
                         help=f"destination namespace (default {GLOBAL_NAMESPACE}). "
                              "Must not itself be a global near-miss.")
    p_rekey.add_argument("--near-miss-global", action="store_true",
                         help="rekey EVERY live row whose namespace is a global "
                              "near-miss (global, userglobal, users:global, ...) to "
                              "--to. Ignores --from. This is the remediation for "
                              "legacy rows stranded before the write-time guard.")
    p_rekey.add_argument("--dry-run", action="store_true",
                         help="report the candidate count and namespaces without writing")
    p_rekey.add_argument("--confirm", action="store_true",
                         help="REQUIRED to actually write. rekey-namespace without "
                              "--confirm (and without --dry-run) refuses with exit 2.")

    p_backup = _add_parser(
        "backup", help="take a verified, retention-rotated snapshot of the store")
    p_backup.add_argument("--retention", type=int, default=BACKUP_DEFAULT_RETENTION,
                          help=f"keep the newest N '{SNAPSHOT_GLOB}' snapshots and delete "
                               f"only the oldest beyond that (default "
                               f"{BACKUP_DEFAULT_RETENTION}; 0 disables pruning). "
                               f"Nothing else in the backup dir is ever touched.")
    p_backup.add_argument("--out-dir", default=None,
                          help="backup directory override (default: $ZMEM_BACKUP_DIR, "
                               "else <store dir>/backups)")
    p_backup.add_argument("--if-due", action="store_true",
                          help="no-op unless $ZMEM_BACKUP_INTERVAL_DAYS (default 1) has "
                               "elapsed since the last successful backup; used by the "
                               "SessionStart hook so the automatic trigger is cheap. "
                               "Without this flag the backup always runs.")

    p_restore = _add_parser(
        "restore", help="restore the store from a snapshot (verifies first, "
                        "backs up the current store first)")
    p_restore.add_argument("--from", dest="from_path", required=True,
                           help="path to the snapshot .sqlite to restore from")
    p_restore.add_argument("--force", action="store_true",
                           help="required to overwrite an existing destination store")
    p_restore.add_argument("--out-dir", default=None,
                           help="where to put the pre-restore backup (default: same as "
                                "`backup`)")

    p_export_pack = _add_parser(
        "export-pack",
        help="render a Tier 1 markdown memory pack for a namespace (project + user:global)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exit codes:\n"
            "  0  pack written/printed successfully\n"
            "  2  pack would be empty (no live rows at/above --min-confidence in\n"
            "     --namespace AND user:global -- usually a wrong or not-yet-populated\n"
            "     --namespace). Check the exit code before committing --out's file\n"
            "     rather than assuming a nonempty write.\n"
            "\n"
            "See docs/CLOUD.md for the full Tier 1 contract."
        ))
    p_export_pack.add_argument("--namespace", required=True,
                               help="project namespace to pack (e.g. project:foo)")
    p_export_pack.add_argument("--out", default=None,
                               help="write the pack to this file (UTF-8, LF); default: stdout")
    p_export_pack.add_argument("--project-limit", type=nonnegative_int,
                               default=EXPORT_PACK_DEFAULT_PROJECT_LIMIT,
                               help=f"max rows from --namespace (default {EXPORT_PACK_DEFAULT_PROJECT_LIMIT})")
    p_export_pack.add_argument("--global-limit", type=nonnegative_int,
                               default=EXPORT_PACK_DEFAULT_GLOBAL_LIMIT,
                               help=f"max rows from {GLOBAL_NAMESPACE} (default {EXPORT_PACK_DEFAULT_GLOBAL_LIMIT})")
    p_export_pack.add_argument("--min-confidence", type=float,
                               default=EXPORT_PACK_DEFAULT_MIN_CONFIDENCE,
                               help=f"confidence floor for both sections (default {EXPORT_PACK_DEFAULT_MIN_CONFIDENCE})")
    p_export_pack.add_argument("--max-bytes", type=int,
                               default=EXPORT_PACK_DEFAULT_MAX_BYTES,
                               help="budget (UTF-8 bytes) for the bullet lines; a bullet that "
                                    "would exceed it is omitted whole, never truncated, and "
                                    "later smaller bullets are still emitted. The budget "
                                    "applies to the whole rendered pack — structural framing "
                                    "(header, titles, section headings, '(none)') counts "
                                    "toward the cap — so only framing appended after the "
                                    "walk (an empty later section's heading/'(none)' and the "
                                    "trailing omitted-count note, rendered whenever rows "
                                    f"were omitted) can push the output past it (default {EXPORT_PACK_DEFAULT_MAX_BYTES})")

    p_export_jsonl = _add_parser(
        "export-jsonl",
        help="export Tier 3 sync JSONL (one memory row per line, no embeddings)")
    p_export_jsonl.add_argument("--out", default=None,
                                help="write to this file (UTF-8, LF); default: stdout")
    p_export_jsonl.add_argument("--namespace", default=None,
                                help="limit to a specific namespace (default: all namespaces)")
    p_export_jsonl.add_argument("--include-superseded", action="store_true",
                                help="also export tombstoned rows (default: live rows only)")

    p_ingest_jsonl = _add_parser(
        "ingest-jsonl",
        help="import Tier 3 sync JSONL written by export-jsonl")
    p_ingest_jsonl.add_argument("--in", dest="in_path", required=True,
                                help="JSONL file to ingest")
    p_ingest_jsonl.add_argument("--source-ref", default=None,
                                help="override source_ref on every row inserted this run "
                                     "(default: keep each row's own incoming source_ref)")
    p_ingest_jsonl.add_argument("--allow-tombstones", action="store_true",
                                help="let an incoming superseded row TOMBSTONE a live local "
                                     "row with the same id. Off by default: use it only when "
                                     "the file is an export of a store you trust as "
                                     "authoritative for those ids (e.g. rebuilding a local "
                                     "store from your own export). Ingesting a cloud/remote "
                                     "outbox must NOT use it -- without the flag such rows "
                                     "are counted as tombstones_refused and the local rows "
                                     "are left alone. A brand-new id that arrives already "
                                     "tombstoned is still inserted as history either way.")
    p_ingest_jsonl.add_argument("--capture-mode", default=None,
                                choices=list(CAPTURE_MODES),
                                help="apply the same capture policy as `add` to every "
                                     "ingested row. ALWAYS tags prompt-injection-risk "
                                     "(defends against poisoned sync files surfacing into "
                                     "model context); 'auto' additionally redacts "
                                     "secret-like text in content/tags and refuses rows "
                                     "whose source_ref looks like a secret. Default resolves "
                                     "like `add` (ZMEM_CAPTURE_MODE env or 'manual'): verbatim "
                                     "content with injection-risk tagging. Use 'auto' when "
                                     "ingesting an untrusted/remote sync file.")

    p_fail = _add_parser(
        "failures",
        help="detect failed tool calls for a session (transcript JSONL or db.sqlite)")
    p_fail.add_argument("--session", default="",
                        help="session id (used with the db.sqlite substrate)")
    p_fail.add_argument("--transcript", default="",
                        help="Claude Code transcript JSONL path (wins when present)")
    p_fail.add_argument("--db", default=os.path.expanduser("~/.zcode/cli/db/db.sqlite"),
                        help="ZCode episodic db.sqlite path (default ~/.zcode/cli/db/db.sqlite)")

    p_corr = _add_parser(
        "corrections",
        help="mine user corrections from a Claude Code transcript JSONL (read-only)")
    p_corr.add_argument("--transcript", default="",
                       help="Claude Code transcript JSONL path")
    p_corr.add_argument("--json", action="store_true",
                       help="emit JSON (default output is already JSON; kept for parity)")

    p_queue_list = _add_parser(
        "queue-list",
        help="list live-capture correction candidates for a namespace "
             "(read-only sidecar, store-independent)")
    p_queue_list.add_argument("--namespace", required=True,
                              help="namespace to list (e.g. the derived project key)")
    p_queue_list.add_argument("--json", action="store_true",
                              help="emit {\"count\": N, \"items\": [...]} "
                                   "(default: human list)")

    p_queue_clear = _add_parser(
        "queue-clear",
        help="clear processed/deferred live-capture correction candidates "
             "(sidecar, store-independent)")
    p_queue_clear.add_argument("--namespace", required=True)
    # --id / --all / --drop-stale are mutually exclusive: passing --all with
    # --id or --drop-stale was silently dropping --all (a surprising no-op).
    # required=True also makes a FLAG-LESS `queue-clear --namespace X` a hard
    # argparse error (rc 2) instead of silently wiping the whole namespace queue.
    _qc_grp = p_queue_clear.add_mutually_exclusive_group(required=True)
    _qc_grp.add_argument("--id", action="append", default=[],
                         help="remove specific item id(s) (repeatable)")
    _qc_grp.add_argument("--all", action="store_true",
                         help="clear the entire namespace queue")
    _qc_grp.add_argument("--drop-stale", action="store_true",
                         help="remove stale items with confidence < 0.6")

    p_mine = _add_parser(
        "mine-history",
        help="mine corrections/rejections/error-patterns from HISTORICAL Claude Code "
             "transcripts (read-only; CC-transcript host surface only)")
    p_mine.add_argument("--transcript-dir", default="",
                        help="Claude Code transcript root (default ~/.claude/projects)")
    p_mine.add_argument("--all-projects", action="store_true",
                        help="walk every project folder (default: current project only)")
    p_mine.add_argument("--days", type=nonnegative_int, default=None,
                        help="only transcripts modified within this many days "
                             "(default: no time filter)")
    p_mine.add_argument("--min-count", type=nonnegative_int, default=2,
                        help="error-aggregation threshold (default 2)")
    p_mine.add_argument("--limit", type=nonnegative_int, default=None,
                        help="cap the number of correction candidates in output "
                             "(default: no cap; 0 emits none, negatives rejected)")
    p_mine.add_argument("--queue", action="store_true",
                        help="append candidates to the PR-2 review queue "
                             "(source=history-mine; resolves the store namespace "
                             "from the current project's git origin, so it may "
                             "spawn one short `git` subprocess)")
    p_mine.add_argument("--source", choices=["claude", "codex", "hermes"],
                        default="claude",
                        help="input surface (issue #71 I): claude = Claude Code "
                             "transcripts (default, full report); codex = a "
                             "curated Codex MEMORY.md (ZMEM_CODEX_MEMORY or "
                             "~/.codex/MEMORY.md; raw_memories.md is refused); "
                             "hermes = Hermes session JSONL under "
                             "ZMEM_HERMES_SESSIONS or ~/.hermes/sessions. "
                             "codex/hermes emit review-queue candidates only.")
    p_mine.add_argument("--json", action="store_true",
                        help="emit the full merged candidate report as JSON")

    p_sweep = _add_parser(
        "sweep",
        help="remove stale per-session cooldown sentinel files (issue #23)")
    p_sweep.add_argument("--marker-dir", default=None,
                         help="override the directory to sweep (default: the union of "
                              "every dir the capture/convention hooks can write markers "
                              "into)")
    p_sweep.add_argument("--max-age-days", type=float, default=None,
                         help=f"drop markers older than this many days (default "
                              f"{SENTINEL_SWEEP_DAYS_DEFAULT:.0f}; env "
                              f"ZMEM_SENTINEL_SWEEP_DAYS)")
    p_sweep.add_argument("--dry-run", action="store_true",
                         help="count what would be pruned without deleting anything")

    # v10 (issue #60, 5.4): entity inspection surface — so humans and doctor
    # can see what the deterministic extractor minted without raw SQL.
    p_entity_list = _add_parser(
        "entity-list",
        help="list entities (kind, canonical name, aliases, link count)")
    p_entity_list.add_argument("--kind", default=None, choices=list(ENTITY_KINDS),
                               help="filter to one entity kind "
                                    "(person/project/tool/preference/other)")
    p_entity_list.add_argument("--json", action="store_true",
                               help="emit [{id, kind, name, aliases, links}] "
                                    "(default: human list)")

    # v10 (issue #60, 5.6): manual entity reconciliation. DRY RUN by default —
    # without --confirm nothing is written (the plan is printed instead).
    p_entity_merge = _add_parser(
        "entity-merge",
        help="merge two entities: move aliases + memory links to the target, "
             "delete the source (dry-run unless --confirm)")
    p_entity_merge.add_argument("--from", dest="from_id", required=True,
                                help="id of the entity to dissolve (its aliases "
                                     "and links move to --to)")
    p_entity_merge.add_argument("--to", dest="to_id", required=True,
                                help="id of the entity that survives")
    p_entity_merge.add_argument("--confirm", action="store_true",
                                help="REQUIRED to write. Without it the merge is "
                                     "a dry run that prints the plan. Refuses "
                                     "kind mismatches (an entity's kind never "
                                     "changes silently); person-to-person "
                                     "merges are allowed but only ever manual.")

    # v11 (issue #61, 6.5): associative-link inspection + curation. List mode
    # mirrors the `get` not-found contract; --add is the CLI insertion path
    # for the typed relations (updates/extends/derives) and curated supports.
    p_links = _add_parser(
        "links",
        help="inspect a memory's associative links (or insert one with --add)",
        description="List mode (default): `links --id UUID [--json]` prints "
                    "every memory_link edge touching the memory, both "
                    "directions. Missing id exits 1 with the same stderr line "
                    "as `get`. Add mode: `links --add --id A --id B --relation "
                    "R [--score S]` inserts a curated edge — symmetric "
                    "relations (related/supports/contradicts) are stored both "
                    "directions (supports carries the +0.05 trust event); "
                    "typed relations (updates/extends/derives) keep their one "
                    "authored direction. Refuses self-links and cross-"
                    "namespace pairs.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_links.add_argument("--id", required=True, action="append", dest="ids",
                         metavar="UUID",
                         help="memory id; once for list mode, twice (--id A "
                              "--id B) with --add for src and dst")
    p_links.add_argument("--json", action="store_true",
                         help="emit [{src, dst, direction, other, relation, "
                              "score, created_at}] (default: human list)")
    p_links.add_argument("--add", action="store_true",
                         help="insert a link instead of listing (requires "
                              "exactly two --id values and --relation)")
    p_links.add_argument("--relation", default=None, choices=list(LINK_RELATIONS),
                         help="relation to insert (--add mode only)")
    p_links.add_argument("--score", type=float, default=None,
                         help="link score 0..1 (--add mode only; default "
                              "ZMEM_LINK_THRESHOLD)")
    p_links.add_argument("--reason", default="",
                         help="why the link is being recorded; REQUIRED for "
                              "--relation contradicts|supports (they adjust "
                              "trust_score — the `contradict` deliberate-use "
                              "convention; validated and echoed, not "
                              "persisted)")

    p_contradict = _add_parser(
        "contradict",
        help="record that two memories contradict (contradicts pair + trust "
             "-0.10 each)",
        description="`contradict --id A --id B --reason ...` inserts a "
                    "contradicts pair (both directions) and applies the "
                    "-0.10 trust event to BOTH rows — without merging, "
                    "deleting, or changing either row's content, confidence, "
                    "or signal. --reason is REQUIRED (deliberate-use guard, "
                    "the `invalidate` convention); the issue's v11 schema has "
                    "no reason column, so it is validated and echoed but not "
                    "persisted. Re-running the same contradict is an exact "
                    "no-op (idempotent; no second trust delta). Missing ids "
                    "exit 1 (the `get` contract).",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p_contradict.add_argument("--id", required=True, action="append", dest="ids",
                              metavar="UUID",
                              help="the two contradicting memory ids (--id A "
                                   "--id B)")
    p_contradict.add_argument("--reason", required=True,
                              help="why they contradict (REQUIRED)")

    # v13 (issue #65, 10.7): episode container commands. episode-list is
    # read-only; the other three take the writer lease (see the lease block).
    p_ep_open = _add_parser(
        "episode-open", help="open a new episode (session container)")
    p_ep_open.add_argument("--namespace", required=True)
    p_ep_open.add_argument("--json", action="store_true",
                           help="print the created episode row as JSON")

    p_ep_add = _add_parser(
        "episode-add", help="attach a LIVE memory to an open episode")
    p_ep_add.add_argument("--episode", required=True, help="episode id")
    p_ep_add.add_argument("--memory", required=True, help="memory id (must be live)")
    p_ep_add.add_argument("--json", action="store_true",
                          help="print the membership result as JSON")

    p_ep_close = _add_parser(
        "episode-close",
        help="close an open episode (append-only; computes token_count)")
    p_ep_close.add_argument("--episode", required=True, help="episode id")
    p_ep_close.add_argument("--summary", action="store_true",
                            help="attach an extractive summary row built from "
                                 "the episode's LIVE members (written via add "
                                 "with capture-mode auto)")
    p_ep_close.add_argument("--json", action="store_true",
                            help="print the closed episode row as JSON")

    p_ep_list = _add_parser(
        "episode-list", help="list episodes (newest first)")
    p_ep_list.add_argument("--namespace", default=None)
    p_ep_list.add_argument("--json", action="store_true",
                           help="print episodes as JSON")

    args = ap.parse_args()

    # `failures` is store-independent (it reads a transcript JSONL or the ZCode
    # episodic db, never the ZMem store) and must be fail-open: branch BEFORE
    # connect()/assert_local_fs()/migrate() so a bad ZMEM_DATA location, a
    # locked store, or a mid-migration state can never break failure detection.
    if args.cmd == "failures":
        sys.exit(cmd_failures(session=args.session, transcript=args.transcript, db=args.db))

    # `corrections` is store-independent (it mines a transcript JSONL, never the
    # ZMem store) and read-only by design (candidates are reviewed by an
    # agent/human before any `add`). Branch BEFORE connect()/migrate() so a bad
    # ZMEM_DATA location, a locked store, or a mid-migration state can never
    # break correction mining (same policy as `failures`).
    if args.cmd == "corrections":
        sys.exit(cmd_corrections(transcript=args.transcript))

    # `queue-list` / `queue-clear` operate on the store-INDEPENDENT sidecar
    # queue file (correction_queue), never the ZMem store, so they branch
    # BEFORE connect()/migrate() — a bad/locked/missing store can never block
    # closeout queue review (same policy as `failures`/`corrections`/`sweep`).
    if args.cmd == "queue-list":
        sys.exit(cmd_queue_list(namespace=args.namespace, as_json=args.json))
    if args.cmd == "queue-clear":
        sys.exit(cmd_queue_clear(namespace=args.namespace, ids=args.id,
                                 clear_all=args.all, drop_stale=args.drop_stale))

    # `mine-history` is READ-ONLY against transcripts AND the store (the only
    # write surface is the #47 sidecar queue under --queue), so like
    # `failures`/`corrections`/`queue-list`/`sweep` it dispatches BEFORE
    # connect()/migrate() — a bad/locked/missing store can never break cold-start
    # mining. Host input surface: Claude Code transcripts (default) plus the
    # issue #71 I Codex/Hermes adapters (queue candidates only).
    if args.cmd == "mine-history":
        if getattr(args, "source", "claude") != "claude":
            sys.exit(cmd_mine_history_adapters(
                source=args.source,
                transcript_dir=args.transcript_dir or "",
                queue=args.queue,
                as_json=args.json,
                limit=args.limit,
            ))
        sys.exit(cmd_mine_history(
            transcript_dir=args.transcript_dir or None,
            all_projects=args.all_projects,
            days=args.days,
            min_count=args.min_count,
            limit=args.limit,
            queue=args.queue,
            as_json=args.json,
        ))

    # `restore` overwrites the destination store FILE. It must not hold an open
    # sqlite3 connection on that file while doing so (a Windows file handle can
    # block the overwrite), so — following the `failures` precedent above — it
    # is dispatched BEFORE connect()/init_db()/migrate() and does its own
    # minimal, self-contained, open-close-per-step file work.
    if args.cmd == "restore":
        sys.exit(cmd_restore(from_path=args.from_path, force=args.force,
                             out_dir=args.out_dir))

    # `sweep` is pure file maintenance (removes stale cooldown markers), never
    # touches the store itself, so — like `failures`/`restore` above — it is
    # dispatched BEFORE connect()/init_db()/migrate(): a locked or mid-migration
    # store can never block the reaper, and no store.sqlite is required.
    if args.cmd == "sweep":
        sys.exit(cmd_sweep(marker_dir=args.marker_dir,
                           max_age_days=args.max_age_days,
                           dry_run=args.dry_run))

    # `session-cadence` runs sweep BEFORE connect()/_prepare_store() (PRR-004):
    # sweep is store-independent file maintenance, so a locked/mid-restore store
    # must never block the reaper. The sweep result is stashed as (step_str,
    # failed_bool) so the post-connect block folds the summary line in AND counts
    # the failure from the real rc, not by substring-matching the display string
    # (cubic-re #5).
    _cadence_sweep: tuple[str, bool] | None = None
    if args.cmd == "session-cadence":
        try:
            rc_s = cmd_sweep()
            _cadence_sweep = (f"sweep: {'ok' if rc_s == 0 else f'exit {rc_s}'}", rc_s != 0)
        except Exception as exc:
            _cadence_sweep = (f"sweep: error - {type(exc).__name__}: {exc}", True)

    # `path` is a read-only query of the 6-level resolution chain (#39 E5).
    # Dispatch it BEFORE connect()/_prepare_store() so it never creates a
    # store file, blocks on a locked store, or fails on a newer-schema store
    # (PRR-002). STORE_PATH is resolved at module import (line 190), so it is
    # available without opening a connection.
    if args.cmd == "path":
        print(STORE_PATH)
        sys.exit(0)

    # PR-review PRR-P (issue #59 review round): `--content -` reads the content
    # from stdin. Windows argv caps near 32k chars while the content cap is
    # MAX_CONTENT_CHARS (65536), so large-but-valid content cannot always be
    # delivered as an argv element; the Hermes/MCP surfaces pipe oversize
    # payloads through this path instead.
    if getattr(args, "content", None) == "-":
        args.content = sys.stdin.read()

    try:
        _wait_for_maintenance_clear(args.cmd)
        conn = connect()
        _prepare_store(conn)
    except RuntimeError as e:
        print(f"[zmem] {e}", file=sys.stderr)
        sys.exit(2)

    # Issue #71 C: after the store is open and migrated, remediate any rows
    # stranded under a global-near-miss namespace so they become reachable
    # again. Fail-open, silent on healthy stores (see _auto_near_miss_rekey).
    # `rekey-namespace` is EXEMPT for its WHOLE surface (both --near-miss-global
    # and single-namespace --from/--to forms): the operator is explicitly doing
    # remediation work, and the auto pass running first would consume the rows
    # their command targets — turning --dry-run into an empty preview and
    # --confirm into "no matching live rows found".
    if args.cmd != "rekey-namespace":
        _auto_near_miss_rekey(conn, force_off=getattr(args, "no_auto_rekey", False))

    # Issue #63, 8.2: fail-closed embedding-profile gate. Applied ONLY to
    # commands whose success depends on generating or querying vectors — a
    # profile/store dimension mismatch must refuse with the remediation
    # command instead of failing mid-write or crashing recall. Read-only and
    # non-vector surfaces (get/list/recent/stats/export/...) stay untouched:
    # `recent` deliberately guards NOTHING because SessionStart hooks depend
    # on it unconditionally, and it neither embeds nor touches memory_vec.
    # allow_rebuild exempts exactly the escape hatch: reembed --all.
    # review round (issue #63): ingest-jsonl joins the guard set — its row
    # loop embeds fresh vectors via _detect_duplicate and inserts into
    # memory_vec, so an unmatched active profile could previously write
    # wrong-dim blobs with the vec-row insert silently swallowed.
    if args.cmd in {"add", "update", "recall", "search", "consolidate",
                    "organize", "ingest-jsonl"} or args.cmd == "reembed" \
            or (args.cmd == "episode-close" and args.summary):
        # PR-review F1 (PR #81 round 2): episode-close --summary embeds via
        # add_memory — include it so a profile/dimension mismatch refuses
        # upfront instead of silently writing wrong-dim vectors (the KNN
        # and vec0 insert failures are both swallowed downstream).
        try:
            assert_embedding_compatible(
                conn,
                allow_rebuild=(args.cmd == "reembed" and (args.all or args.dry_run)),
            )
        except RuntimeError as e:
            print(f"[zmem] {e}", file=sys.stderr)
            sys.exit(2)

    writer_lease = None
    if (
        args.cmd in {"add", "supersede", "invalidate", "update", "rebuild-fts", "ingest-jsonl"}
        # v12 (issue #64, 9.4): feedback is a write surface — it takes the
        # lease so it serializes against restore/backup like every writer.
        or args.cmd == "feedback"
        # Issue #63, 8.3: reembed takes the writer lease ONLY when it can
        # write. --dry-run is read-only by contract and must never block on —
        # or be blocked as — a writer.
        or (args.cmd == "reembed" and not args.dry_run)
        # recall/recent/search DO write on the `--no-bump` path now that it records a
        # surface (issue #21), but they must NOT take a writer lease when passive:
        # the SubagentStart/UserPromptSubmit/prefetch hook fires at high fan-out, and
        # _acquire_writer_lease() would put a host-lock `_wait_for_maintenance_clear`
        # (up to MAINTENANCE_WAIT_SECONDS) on every hot-path recall — the hot-path
        # latency/write-contention PLAN.md §5 and the issue's "bounded" guidance exist
        # to avoid. The dispatch ALREADY runs `_wait_for_maintenance_clear` before
        # connect for every command (incl. no_bump recall), so a passive writer cannot
        # START during an active restore; the residual mid-flight window is fail-open
        # (lost surface telemetry on POSIX, clean restore refusal on Windows), never
        # corruption. See issue #21 implementation-review closure.
        # Issue #82 (PR-review PRR-002): --explain is a zero-write read-only
        # debugger — it must never hold the writer lease (which would make a
        # concurrent restore/backup refuse against a diagnostic read).
        or (args.cmd == "recall" and not args.no_bump
            and not getattr(args, "explain", False))
        or (args.cmd == "recent" and not args.no_bump)
        or (args.cmd == "search" and not args.no_bump)
        or (args.cmd == "rekey-namespace" and not args.dry_run and args.confirm)
        # v10 (issue #60): entity-merge writes ONLY under --confirm; the
        # dry-run default is read-only and must not take the writer lease.
        or (args.cmd == "entity-merge" and args.confirm)
        # v13 (issue #65, 10.7): episode-open/add/close write; episode-list
        # is read-only and never takes the lease.
        or args.cmd in {"episode-open", "episode-add", "episode-close"}
        # Issue #71 E (PR-review PRR-003): promote-store writes via
        # _ingest_row, so a real run serializes against restore/backup like
        # every other writer; --dry-run is read-only and never takes it.
        or (args.cmd == "promote-store" and not args.dry_run)
    ):
        writer_lease = _acquire_writer_lease(args.cmd)

    try:
        if args.cmd == "init":
            print(f"[zmem] store ready at {STORE_PATH}")
        elif args.cmd == "add":
            try:
                # Under --json the human progress lines ([zmem] added …,
                # dedup/links notices) go to STDERR so stdout is pure JSON —
                # the MCP/Hermes surfaces parse it directly (issue #65, 10.8).
                if args.json:
                    sys.stdout, _human_out = sys.stderr, sys.stdout
                else:
                    _human_out = None
                try:
                    res = add_memory(
                        conn,
                        namespace=args.namespace,
                        type_=args.type,
                        content=args.content,
                        tags=args.tags,
                        source_ref=args.source_ref,
                        confidence=args.confidence,
                        signal=args.signal,
                        taint=args.taint,
                        capture_mode=args.capture_mode,
                    )
                finally:
                    if _human_out is not None:
                        sys.stdout = _human_out
                if args.json:
                    # Structured write result (issue #65, 10.8): consumed by the
                    # MCP/Hermes add surfaces so remote write warnings (e.g. a
                    # redaction) are structured data, not stderr text.
                    print(json.dumps({
                        "id": str(res),
                        "result": "deduped" if res.deduped else "stored",
                        "warnings": res.warnings,
                    }))
            except CapturePolicyRefusal as exc:
                print(f"[zmem] {exc}", file=sys.stderr)
                sys.exit(2)
            except ContentTooLarge as exc:
                # Content-size cap — caught SPECIFICALLY (not as a bare
                # ValueError, which would also swallow UnicodeEncodeError and
                # change stdio-encoding failure behavior) (#36 M17).
                print(f"[zmem] {exc}", file=sys.stderr)
                sys.exit(1)
        elif args.cmd == "invalidate":
            # `invalidate` IS `supersede` with a required reason (--reason is
            # required=True on the parser, so argparse refuses missing at rc 2
            # before we get here) — the preferred "this fact is no longer true"
            # command (issue #59, 4.3). Both tombstone with valid_until=now.
            # PR-review PRR-I: argparse checks PRESENCE, not content — an
            # empty/whitespace reason is refused here so the audit trail can
            # never be blank (MCP/Hermes already strip-refuse at the boundary).
            if not args.reason.strip():
                print("[zmem] invalidate --reason must be non-empty — the reason "
                      "is the audit trail", file=sys.stderr)
                sys.exit(2)
            try:
                ok = supersede_memory(conn, args.id, args.reason)
            except ValueError as exc:
                # PR-review PRR-B: already-tombstoned rows are refused (never
                # re-tombstoned) — a stable exit-2 refusal, not a traceback.
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            sys.exit(0 if ok else 1)
        elif args.cmd == "update":
            try:
                # Same stdout discipline as add --json (issue #65, 10.8).
                if args.json:
                    sys.stdout, _human_out = sys.stderr, sys.stdout
                else:
                    _human_out = None
                try:
                    res, created_new = update_memory(
                        conn,
                        mid=args.id,
                        content=args.content,
                        namespace=args.namespace,
                        type_=args.type,
                        tags=args.tags,
                        source_ref=args.source_ref,
                        confidence=args.confidence,
                        signal=args.signal,
                        taint=args.taint,
                        capture_mode=args.capture_mode,
                    )
                finally:
                    if _human_out is not None:
                        sys.stdout = _human_out
                if args.json:
                    print(json.dumps({
                        "id": str(res),
                        "result": "updated",
                        "created_new": created_new,
                        "warnings": res.warnings,
                    }))
            except CapturePolicyRefusal as exc:
                print(f"[zmem] {exc}", file=sys.stderr)
                sys.exit(2)
            except ContentTooLarge as exc:
                print(f"[zmem] {exc}", file=sys.stderr)
                sys.exit(1)
            except ValueError as exc:
                # update refusals (unknown / already-superseded id) — refused,
                # nothing written (exit 2, matching the rekey/promote refusal
                # convention rather than the 1 used for a not-found `get`).
                print(f"[zmem] {exc}", file=sys.stderr)
                sys.exit(2)
        elif args.cmd == "recall":
            # Issue #58, 3.3: --hybrid and --no-hybrid both parse, but
            # the default is hybrid-when-available (sentinel None). PRR-013
            # fix: --no-hybrid takes PRECEDENCE when both are passed — an
            # explicit force-lexical must never be silently overridden.
            if args.no_hybrid:
                hybrid_arg: bool | None = False
            elif args.hybrid:
                hybrid_arg = True
            else:
                hybrid_arg = None
            # Issue #63, 8.6: cross-encoder rerank is an explicit-recall-only,
            # opt-in feature. The single decision point lives in
            # cross_encoder.cli_allowed; --no-bump excludes every passive hook
            # caller structurally, --no-hybrid keeps search's byte-stable
            # contract out of scope even when aliased through this argv.
            rerank_flag = _ce_cli_allowed(no_bump=args.no_bump,
                                          no_hybrid=args.no_hybrid)
            # Issue #82: --explain dispatches to the read-only retrieval
            # debugger (zero writes, never unfolds, fail-open). It is a flag,
            # not a subcommand, so KNOWN_SUBCMDS stays byte-identical.
            if getattr(args, "explain", False):
                explain_recall(conn, query=args.query, target=args.target,
                               namespace=args.namespace, limit=args.limit,
                               as_json=args.json, hybrid=hybrid_arg,
                               no_bump=args.no_bump,
                               include_global=args.include_global,
                               global_limit=args.global_limit, as_of=args.as_of,
                               no_mmr=args.no_mmr,
                               link_hops=args.link_hops,
                               link_budget=args.link_budget,
                               cross_rerank=rerank_flag)
            else:
                recall_memory(conn, query=args.query, namespace=args.namespace,
                              limit=args.limit, as_json=args.json, hybrid=hybrid_arg,
                              no_bump=args.no_bump, include_global=args.include_global,
                              global_limit=args.global_limit, as_of=args.as_of,
                              no_mmr=args.no_mmr,
                              link_hops=args.link_hops, link_budget=args.link_budget,
                              cross_rerank=rerank_flag,
                              no_unfold=args.no_unfold,
                              for_injection=args.for_injection)
        elif args.cmd == "recent":
            recent_memory(conn, namespace=args.namespace, limit=args.limit,
                          min_confidence=args.min_confidence, as_json=args.json,
                          no_bump=args.no_bump, include_global=args.include_global,
                          global_limit=args.global_limit, as_of=args.as_of,
                          for_injection=args.for_injection)
        elif args.cmd == "search":
            # I1 critic-fix: ``search`` is keyword-only by contract — pass
            # hybrid=False explicitly so the new default sentinel does
            # not silently flip search to vector-hybrid. Search output
            # stays byte-identical to pre-change.
            # v11 (issue #61, 6.3): same reasoning for link expansion — it is
            # a RECALL behavior; search keeps its byte-identical contract.
            # v13 (issue #65, 10.8): --json emits the read envelope.
            recall_memory(conn, query=args.text, namespace=args.namespace, limit=args.limit,
                          as_json=args.json, min_confidence=0.0,
                          include_global=args.include_global,
                          global_limit=args.global_limit, no_bump=args.no_bump,
                          hybrid=False, as_of=args.as_of, link_hops=0)
        elif args.cmd == "supersede":
            try:
                ok = supersede_memory(conn, args.id, args.reason)
            except ValueError as exc:
                # PR-review PRR-B: already-tombstoned rows are refused (never
                # re-tombstoned) — a stable exit-2 refusal, not a traceback.
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            sys.exit(0 if ok else 1)
        elif args.cmd == "get":
            sys.exit(0 if get_memory(conn, args.id) else 1)
        elif args.cmd == "list":
            list_memory(conn, namespace=args.namespace, limit=args.limit, include_superseded=args.include_superseded)
        elif args.cmd == "stats":
            stats(conn)
        elif args.cmd == "rebuild-fts":
            conn.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
            conn.commit()
            print("[zmem] FTS5 index rebuilt")
        elif args.cmd == "reembed":
            if args.profile and not args.all:
                # Silent no-op would be crueler than refusal: --profile alone
                # looks like a conversion but only ever took effect with
                # --all. Say exactly what to type (review round, R2).
                print("[zmem] --profile only takes effect with --all "
                      "(use: reembed --all --profile <name> to convert)",
                      file=sys.stderr)
                sys.exit(2)
            if args.all or args.dry_run:
                sys.exit(reembed_embeddings(
                    conn, rebuild_all=args.all, profile=args.profile,
                    batch=args.batch, dry_run=args.dry_run,
                    confirm=args.confirm))
            # legacy flagless/backfill form: byte-identical stdout contract;
            # --batch passes through purely as stderr progress pacing.
            sys.exit(reembed_embeddings(conn, batch=args.batch))
        elif args.cmd == "consolidate":
            # Single-flight: consolidate() writes, and the SessionStart hook fires a
            # detached one per session. Its meta-key cadence gate is a SOFT gate
            # (read-then-later-write), so without this lock two near-simultaneous
            # runs both pass it and both run the clustering loop. --dry-run models
            # the cadence gate (it announces a would-skip) but still writes nothing,
            # so it still never takes the single-flight lock; only --force bypasses
            # the gate, and --dry-run --force previews what --force would do.
            #
            # --json (issue #49): stdout must remain strictly json.loads-parseable,
            # so under --json every human print from the lock path AND from
            # consolidate() itself is routed to stderr (PRR-004 discipline), and
            # the machine report — or a lock-busy error object — is printed to
            # the RESTORED stdout after the redirect block exits.
            c_token = None
            lock_busy = False
            c_report = None
            redirect = (
                contextlib.redirect_stdout(sys.stderr)
                if args.json else contextlib.nullcontext()
            )
            with redirect:
                if not args.dry_run:
                    c_token = _acquire_lock("consolidate", CONSOLIDATE_LOCK_STALE_SECONDS)
                    if c_token is None:
                        print("[zmem] consolidate: another consolidation is already "
                              "running - skipped")
                        lock_busy = True
                if not lock_busy:
                    try:
                        c_report = consolidate(
                            conn, threshold=args.threshold, prune=args.prune,
                            dry_run=args.dry_run, namespace=args.namespace,
                            force=args.force, merge_contested=args.merge_contested)
                    finally:
                        _release_lock("consolidate", c_token)
            if lock_busy:
                # conn is closed exactly once by main()'s outer finally — the
                # historical explicit close here was a harmless double-close.
                if args.json:
                    print('{"error": "consolidate lock busy"}')
                return
            if args.json:
                print(json.dumps(c_report))
        elif args.cmd == "organize":
            # Single-flight on the SHARED "consolidate" lock + shared cadence
            # gate (issue #62). organize IS the same maintenance act as
            # consolidate (it runs consolidate on a bounded episode), so the two
            # commands share one lock AND one meta-key gate: two entry points,
            # one clock — SessionStart organizing and a manual consolidate can
            # never both fire back-to-back on the same store state. --dry-run
            # writes nothing and still never takes the single-flight lock
            # (mirroring consolidate); only --force bypasses the cadence gate.
            #
            # --json: stdout must remain strictly json.loads-parseable (same
            # discipline as consolidate, PRR-004): every human print from the
            # lock path AND from organize() itself is routed to stderr, and the
            # machine report — or a lock-busy error object — is printed to the
            # RESTORED stdout after the redirect block exits.
            o_token = None
            lock_busy = False
            o_report = None
            o_redirect = (
                contextlib.redirect_stdout(sys.stderr)
                if args.json else contextlib.nullcontext()
            )
            with o_redirect:
                if not args.dry_run:
                    o_token = _acquire_lock("consolidate", CONSOLIDATE_LOCK_STALE_SECONDS)
                    if o_token is None:
                        print("[zmem] organize: another organize/consolidation is "
                              "already running - skipped")
                        lock_busy = True
                if not lock_busy:
                    try:
                        o_report = organize(
                            conn, dry_run=args.dry_run, force=args.force,
                            prune=args.prune)
                    finally:
                        _release_lock("consolidate", o_token)
            if lock_busy:
                # conn is closed exactly once by main()'s outer finally — the
                # explicit close here would be a harmless double-close.
                if args.json:
                    print('{"error": "organize lock busy"}')
                return
            if args.json:
                print(json.dumps(o_report))
        elif args.cmd == "backup":
            rc = cmd_backup(conn, retention=args.retention, out_dir=args.out_dir,
                            if_due=args.if_due)
            sys.exit(rc)
        elif args.cmd == "session-cadence":
            # Batch the three session-start cadence ops into one process (#39 E9).
            # Each op keeps its EXACT standalone semantics: organize takes its
            # single-flight lock + shared cadence gate (force=False respects it —
            # issue #62 7.7 wired SessionStart to organize, NOT consolidate; the
            # consolidate CLI remains for manual/ad-hoc runs), backup runs with
            # --if-due (cheap no-op when not due), and sweep is the same
            # store-independent file reaper. A failure in any one op is reported
            # but does not abort the others (cadence ops are independent).
            # sweep already ran BEFORE connect() (store-independence, PRR-004)
            # — fold its result into the summary here.
            steps: list[str] = []
            failures = 0
            # 1) organize (shares consolidate's lock + meta-key cadence gate via
            # force=False; single-flighted on the shared "consolidate" lock)
            o_token = _acquire_lock("consolidate", CONSOLIDATE_LOCK_STALE_SECONDS)
            if o_token is None:
                steps.append("organize: already running - skipped")
            else:
                try:
                    organize(conn, force=False)
                    steps.append("organize: ok")
                except Exception as exc:  # never let one cadence op abort the batch
                    steps.append(f"organize: error - {type(exc).__name__}: {exc}")
                    failures += 1
                finally:
                    _release_lock("consolidate", o_token)
            # 2) backup --if-due (cheap no-op almost every session)
            try:
                rc_b = cmd_backup(conn, retention=args.backup_retention, if_due=True)
                steps.append(f"backup: {'ok' if rc_b == 0 else f'exit {rc_b}'}")
                if rc_b != 0:
                    failures += 1
            except Exception as exc:
                steps.append(f"backup: error - {type(exc).__name__}: {exc}")
                failures += 1
            # 3) sweep result (already computed pre-connect): fold the summary
            # line in and count the failure from the stashed bool (not by
            # substring-matching the display string — cubic-re #5).
            if _cadence_sweep is not None:
                steps.append(_cadence_sweep[0])
                if _cadence_sweep[1]:
                    failures += 1
            print("[zmem] session-cadence: " + "; ".join(steps))
            # Exit nonzero if any op failed (PRR-003): former separate processes
            # surfaced per-op exit codes; preserve that signal. The hook runs
            # this detached and does not check $?, so the impact is for direct
            # CLI/metrics consumers only.
            if failures:
                sys.exit(1)
        elif args.cmd == "promote-store":
            # Issue #71 E: one-shot merge of a leftover second store. Idempotent
            # (source ids preserved), read-only on the source.
            sys.exit(cmd_promote_store(conn, from_path=args.from_path,
                                       dry_run=args.dry_run))
        elif args.cmd == "rekey-namespace":
            # --confirm (or --dry-run) is a REAL gate: this rewrites the
            # namespace column of live rows. Without either flag it refuses.
            if not args.confirm and not args.dry_run:
                print("[zmem] rekey-namespace: refusing to write without "
                      "--confirm (or --dry-run to preview).", file=sys.stderr)
                sys.exit(2)
            try:
                rekey_namespace(
                    conn, from_namespace=args.from_namespace,
                    to_namespace=args.to_namespace,
                    near_miss_global=args.near_miss_global, dry_run=args.dry_run,
                )
            except ValueError as exc:
                print(f"[zmem] rekey-namespace: {exc}", file=sys.stderr)
                sys.exit(2)
        elif args.cmd == "promote":
            # --confirm is a REAL gate, not decoration. Promotion writes a SKILL.md
            # into the review-candidate area, and live installation now needs the
            # extra explicit --install-approved gate.
            if args.id and not args.dry_run and not args.confirm:
                print("[zmem] refusing to promote without --confirm.", file=sys.stderr)
                print(f"[zmem]   promote --id {args.id} --confirm", file=sys.stderr)
                print("[zmem] (add --description \"...\" to write the trigger line yourself — "
                      "the description is the entire trigger surface)", file=sys.stderr)
            # sys.exit, not return: main()'s return value is discarded by the
            # `if __name__ == "__main__": main()` entrypoint, so a bare `return 2`
            # prints the refusal but still exits 0 — a refusal indistinguishable
            # from success to any caller checking $?. 2 matches cmd_restore's
            # "refused, destination untouched" codes (see its refusal branches);
            # note restore uses 1 for its own missing-flag case, and `failures`
            # surfaces no code at all, so this is deliberately the refused-and-
            # nothing-written convention rather than a blanket house style.
                sys.exit(2)
            rc = promote_memory(conn, memory_id=args.id, dry_run=args.dry_run,
                                namespace=args.namespace, description=args.description,
                                install_approved=args.install_approved)
            if rc:
                sys.exit(rc)
        elif args.cmd == "feedback":
            # v12 (issue #64, 9.4): FeedbackTargetError (unknown/tombstoned id)
            # is an operational refusal -> exit 1, stable stderr message (the
            # `get` convention). argparse's mutually-exclusive required group
            # already refuses both/neither flags and a missing --id with exit 2.
            try:
                result = feedback_memory(conn, memory_id=args.id,
                                         verdict="applied" if args.applied else "violated")
            except FeedbackTargetError as exc:
                print(f"[zmem] {exc}", file=sys.stderr)
                sys.exit(1)
            print(json.dumps(result, ensure_ascii=False))
        elif args.cmd == "tune-weights":
            # v12 (issue #64, 9.6): dry-run only. A missing --dry-run is a
            # usage refusal (exit 2) — it keeps the door visibly closed on an
            # untested apply path; applying weights is a documented manual
            # edit of storelib/recall.py's W_* constants (SKILL.md).
            if not args.dry_run:
                print("[zmem] tune-weights: only --dry-run is implemented; the "
                      "evaluation is read-only. Applying suggested weights is "
                      "a manual edit of the W_* constants in "
                      "skills/memory/scripts/storelib/recall.py (see SKILL.md "
                      "§tune-weights).", file=sys.stderr)
                sys.exit(2)
            sys.exit(tune_weights(conn, gold_path=args.gold, k=args.k))
        elif args.cmd == "export-pack":
            rc = cmd_export_pack(
                conn, namespace=args.namespace, out=args.out,
                project_limit=args.project_limit, global_limit=args.global_limit,
                min_confidence=args.min_confidence, max_bytes=args.max_bytes,
            )
            sys.exit(rc)
        elif args.cmd == "export-jsonl":
            rc = cmd_export_jsonl(
                conn, out=args.out, namespace=args.namespace,
                include_superseded=args.include_superseded,
            )
            sys.exit(rc)
        elif args.cmd == "ingest-jsonl":
            rc = cmd_ingest_jsonl(conn, in_path=args.in_path, source_ref=args.source_ref,
                                  allow_tombstones=args.allow_tombstones,
                                  capture_mode=args.capture_mode)
            sys.exit(rc)
        elif args.cmd == "entity-list":
            sys.exit(cmd_entity_list(conn, kind=args.kind, as_json=args.json))
        elif args.cmd == "entity-merge":
            sys.exit(cmd_entity_merge(conn, from_id=args.from_id, to_id=args.to_id,
                                      confirm=args.confirm))
        elif args.cmd == "links":
            sys.exit(cmd_links(conn, ids=args.ids, as_json=args.json,
                               add=args.add, relation=args.relation,
                               score=args.score, reason=args.reason))
        elif args.cmd == "contradict":
            sys.exit(cmd_contradict(conn, ids=args.ids, reason=args.reason))
        elif args.cmd in {"episode-open", "episode-add", "episode-close",
                          "episode-list"}:
            # v13 (issue #65, 10.7). Refusals are stable exit-2 [zmem] lines
            # (the supersede/invalidate convention), never tracebacks.
            from storelib.episodes import (
                EpisodeError, episode_add, episode_close, episode_list, episode_open,
            )
            try:
                if args.cmd == "episode-list":
                    # episode_list prints its own output (JSON or human rows).
                    episode_list(conn, namespace=args.namespace, as_json=args.json)
                else:
                    # Same stdout discipline as add/update --json: the summary
                    # write inside episode-close prints human lines; keep
                    # stdout pure JSON under --json.
                    if args.json:
                        sys.stdout, _human_out = sys.stderr, sys.stdout
                    else:
                        _human_out = None
                    try:
                        if args.cmd == "episode-open":
                            row = episode_open(conn, namespace=args.namespace)
                        elif args.cmd == "episode-add":
                            res = episode_add(conn, episode_id=args.episode,
                                              memory_id=args.memory)
                        else:
                            row = episode_close(conn, episode_id=args.episode,
                                                with_summary=args.summary)
                    finally:
                        if _human_out is not None:
                            sys.stdout = _human_out
                    if args.cmd == "episode-open":
                        print(json.dumps(row, indent=2) if args.json else
                              f"[zmem] episode opened: {row['id']} "
                              f"(ns={row['namespace']} started={row['started_at']})")
                    elif args.cmd == "episode-add":
                        if args.json:
                            print(json.dumps(res))
                        else:
                            state = ("attached" if res["added"]
                                     else "already attached")
                            print(f"[zmem] episode-add: memory {args.memory} "
                                  f"{state} to episode {args.episode}")
                    else:
                        print(json.dumps(row, indent=2) if args.json else
                              f"[zmem] episode closed: {row['id']} "
                              f"(members={row['member_count']} "
                              f"tokens={row['token_count']}"
                              + (f" summary={row['summary_memory_id']}"
                                 if row["summary_memory_id"] else "") + ")")
            except EpisodeError as exc:
                print(str(exc), file=sys.stderr)
                sys.exit(2)
            except CapturePolicyRefusal as exc:
                # PR-review F2 (PR #81 round 2): episode-open validates the
                # namespace via write._validate_namespace, which raises
                # CapturePolicyRefusal — surface the stable [zmem] refusal
                # line like add/update do, never a traceback.
                print(f"[zmem] {exc}", file=sys.stderr)
                sys.exit(2)
            except ContentTooLarge as exc:
                print(f"[zmem] {exc}", file=sys.stderr)
                sys.exit(1)
    finally:
        _release_writer_lease(writer_lease)
        conn.close()
