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
from storelib.entity import ENTITY_KINDS, cmd_entity_list, cmd_entity_merge
from storelib.mine import cmd_corrections, cmd_failures, cmd_mine_history, cmd_queue_clear, cmd_queue_list
from storelib.promote import promote_memory
from storelib.recall import _reembed, get_memory, list_memory, recall_memory, recent_memory, stats
from storelib.schema import ALLOWED_SIGNALS, ALLOWED_TYPES, ALLOWED_TAINTS, CAPTURE_MODES, GLOBAL_NAMESPACE, STORE_PATH, _acquire_writer_lease, _prepare_store, _release_writer_lease, _wait_for_maintenance_clear, connect
from storelib.sync import EXPORT_PACK_DEFAULT_GLOBAL_LIMIT, EXPORT_PACK_DEFAULT_MAX_BYTES, EXPORT_PACK_DEFAULT_MIN_CONFIDENCE, EXPORT_PACK_DEFAULT_PROJECT_LIMIT, cmd_export_jsonl, cmd_export_pack, cmd_ingest_jsonl
from storelib.write import CapturePolicyRefusal, ContentTooLarge, add_memory, rekey_namespace, supersede_memory, update_memory

def nonnegative_int(value: str) -> int:
    """argparse type= for flags fed straight into a SQL LIMIT: SQLite treats a
    negative LIMIT as UNBOUNDED, so a negative --project-limit/--global-limit
    would silently defeat the cap instead of erroring. Reject it up front."""
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative integer, got {value!r}")
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
    ap = argparse.ArgumentParser(prog="store.py", description="ZMem semantic store")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the store if absent (idempotent)")

    p_add = sub.add_parser("add", help="add a memory")
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

    p_recall = sub.add_parser("recall", help="recall relevant memories")
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

    p_recent = sub.add_parser("recent", help="most recent live memories (no FTS, admin pull)")
    p_recent.add_argument("--namespace", default=None)
    p_recent.add_argument("--limit", type=nonnegative_int, default=5)
    p_recent.add_argument("--min-confidence", type=float, default=0.5)
    p_recent.add_argument("--json", action="store_true")
    p_recent.add_argument("--no-bump", action="store_true",
                          help="suppress the retrieval_count/last_retrieved write; record "
                               "surfaced_count/last_surfaced instead (passive recent, used "
                               "by hook-driven subagent recall — issue #21)")
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

    p_search = sub.add_parser("search", help="keyword search (no confidence floor)")
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

    p_sup = sub.add_parser("supersede", help="tombstone a memory")
    p_sup.add_argument("--id", required=True)
    p_sup.add_argument("--reason", default="")

    p_inv = sub.add_parser(
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

    p_upd = sub.add_parser(
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

    p_get = sub.add_parser(
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

    p_list = sub.add_parser("list", help="list memories")
    p_list.add_argument("--namespace", default=None)
    p_list.add_argument("--limit", type=nonnegative_int, default=50)
    p_list.add_argument("--include-superseded", action="store_true")

    sub.add_parser("stats", help="store statistics")

    # Print only the resolved store path (the 6-level ZMEM_STORE > ZMEM_DATA >
    # CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem > legacy chain). Lets
    # scripts/tooling query the path without parsing `stats` output (#39 E5).
    sub.add_parser("path", help="print the resolved store path")

    # Run the three session-start cadence ops (consolidate, backup --if-due,
    # sweep) in ONE process instead of three detached python invocations
    # (#39 E9). Each op keeps its own cadence gate / single-flight lock / exit
    # semantics — this entrypoint only sequences them.
    p_session_cadence = sub.add_parser(
        "session-cadence",
        help="run consolidate + backup --if-due + sweep in one process "
             "(session-start cadence batch)")
    p_session_cadence.add_argument("--backup-retention", type=int,
                                   default=BACKUP_DEFAULT_RETENTION,
                                   help=f"backup retention in days (default {BACKUP_DEFAULT_RETENTION})")

    sub.add_parser("rebuild-fts", help="rebuild the FTS5 index from scratch")

    sub.add_parser("reembed", help="backfill embeddings for live memories missing them")

    p_consolidate = sub.add_parser("consolidate", help="merge near-duplicate memories")
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

    p_promote = sub.add_parser("promote", help="promote high-confidence lessons to SKILL.md files")
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

    p_rekey = sub.add_parser(
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

    p_backup = sub.add_parser(
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

    p_restore = sub.add_parser(
        "restore", help="restore the store from a snapshot (verifies first, "
                        "backs up the current store first)")
    p_restore.add_argument("--from", dest="from_path", required=True,
                           help="path to the snapshot .sqlite to restore from")
    p_restore.add_argument("--force", action="store_true",
                           help="required to overwrite an existing destination store")
    p_restore.add_argument("--out-dir", default=None,
                           help="where to put the pre-restore backup (default: same as "
                                "`backup`)")

    p_export_pack = sub.add_parser(
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

    p_export_jsonl = sub.add_parser(
        "export-jsonl",
        help="export Tier 3 sync JSONL (one memory row per line, no embeddings)")
    p_export_jsonl.add_argument("--out", default=None,
                                help="write to this file (UTF-8, LF); default: stdout")
    p_export_jsonl.add_argument("--namespace", default=None,
                                help="limit to a specific namespace (default: all namespaces)")
    p_export_jsonl.add_argument("--include-superseded", action="store_true",
                                help="also export tombstoned rows (default: live rows only)")

    p_ingest_jsonl = sub.add_parser(
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

    p_fail = sub.add_parser(
        "failures",
        help="detect failed tool calls for a session (transcript JSONL or db.sqlite)")
    p_fail.add_argument("--session", default="",
                        help="session id (used with the db.sqlite substrate)")
    p_fail.add_argument("--transcript", default="",
                        help="Claude Code transcript JSONL path (wins when present)")
    p_fail.add_argument("--db", default=os.path.expanduser("~/.zcode/cli/db/db.sqlite"),
                        help="ZCode episodic db.sqlite path (default ~/.zcode/cli/db/db.sqlite)")

    p_corr = sub.add_parser(
        "corrections",
        help="mine user corrections from a Claude Code transcript JSONL (read-only)")
    p_corr.add_argument("--transcript", default="",
                       help="Claude Code transcript JSONL path")
    p_corr.add_argument("--json", action="store_true",
                       help="emit JSON (default output is already JSON; kept for parity)")

    p_queue_list = sub.add_parser(
        "queue-list",
        help="list live-capture correction candidates for a namespace "
             "(read-only sidecar, store-independent)")
    p_queue_list.add_argument("--namespace", required=True,
                              help="namespace to list (e.g. the derived project key)")
    p_queue_list.add_argument("--json", action="store_true",
                              help="emit {\"count\": N, \"items\": [...]} "
                                   "(default: human list)")

    p_queue_clear = sub.add_parser(
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

    p_mine = sub.add_parser(
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
    p_mine.add_argument("--json", action="store_true",
                        help="emit the full merged candidate report as JSON")

    p_sweep = sub.add_parser(
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
    p_entity_list = sub.add_parser(
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
    p_entity_merge = sub.add_parser(
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
    # mining. Host input surface is Claude-Code-transcript-only by design.
    if args.cmd == "mine-history":
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

    writer_lease = None
    if (
        args.cmd in {"add", "supersede", "invalidate", "update", "rebuild-fts", "reembed", "ingest-jsonl"}
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
        or (args.cmd == "recall" and not args.no_bump)
        or (args.cmd == "recent" and not args.no_bump)
        or (args.cmd == "search" and not args.no_bump)
        or (args.cmd == "rekey-namespace" and not args.dry_run and args.confirm)
        # v10 (issue #60): entity-merge writes ONLY under --confirm; the
        # dry-run default is read-only and must not take the writer lease.
        or (args.cmd == "entity-merge" and args.confirm)
    ):
        writer_lease = _acquire_writer_lease(args.cmd)

    try:
        if args.cmd == "init":
            print(f"[zmem] store ready at {STORE_PATH}")
        elif args.cmd == "add":
            try:
                add_memory(
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
                update_memory(
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
            recall_memory(conn, query=args.query, namespace=args.namespace,
                          limit=args.limit, as_json=args.json, hybrid=hybrid_arg,
                          no_bump=args.no_bump, include_global=args.include_global,
                          global_limit=args.global_limit, as_of=args.as_of,
                          no_mmr=args.no_mmr)
        elif args.cmd == "recent":
            recent_memory(conn, namespace=args.namespace, limit=args.limit,
                          min_confidence=args.min_confidence, as_json=args.json,
                          no_bump=args.no_bump, include_global=args.include_global,
                          global_limit=args.global_limit, as_of=args.as_of)
        elif args.cmd == "search":
            # I1 critic-fix: ``search`` is keyword-only by contract — pass
            # hybrid=False explicitly so the new default sentinel does
            # not silently flip search to vector-hybrid. Search output
            # stays byte-identical to pre-change.
            recall_memory(conn, query=args.text, namespace=args.namespace, limit=args.limit,
                          as_json=False, min_confidence=0.0,
                          include_global=args.include_global,
                          global_limit=args.global_limit, no_bump=args.no_bump,
                          hybrid=False, as_of=args.as_of)
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
            _reembed(conn)
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
        elif args.cmd == "backup":
            rc = cmd_backup(conn, retention=args.retention, out_dir=args.out_dir,
                            if_due=args.if_due)
            sys.exit(rc)
        elif args.cmd == "session-cadence":
            # Batch the three session-start cadence ops into one process (#39 E9).
            # Each op keeps its EXACT standalone semantics: consolidate takes its
            # single-flight lock + cadence gate (force=False respects it), backup
            # runs with --if-due (cheap no-op when not due), and sweep is the same
            # store-independent file reaper. A failure in any one op is reported
            # but does not abort the others (cadence ops are independent).
            # sweep already ran BEFORE connect() (store-independence, PRR-004)
            # — fold its result into the summary here.
            steps: list[str] = []
            failures = 0
            # 1) consolidate (cadence-gated via force=False, single-flighted)
            c_token = _acquire_lock("consolidate", CONSOLIDATE_LOCK_STALE_SECONDS)
            if c_token is None:
                steps.append("consolidate: already running - skipped")
            else:
                try:
                    consolidate(conn, force=False)
                    steps.append("consolidate: ok")
                except Exception as exc:  # never let one cadence op abort the batch
                    steps.append(f"consolidate: error - {type(exc).__name__}: {exc}")
                    failures += 1
                finally:
                    _release_lock("consolidate", c_token)
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
    finally:
        _release_writer_lease(writer_lease)
        conn.close()
