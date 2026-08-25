from __future__ import annotations

# PRR-032 note: the from-import lines below re-import several names from
# DIFFERENT submodules (ALLOWED_SIGNALS, STORE_PATH, stdlib modules, ...).
# Python binds the LAST occurrence, so when adding a new submodule line,
# verify any name it re-exports that an earlier line also imports still
# resolves to the intended object — a new submodule defining its own copy
# would silently shadow the earlier import. The current submodules
# re-export from storelib.schema, so the values are identical objects.

from storelib.schema import ALLOWED_SIGNALS, ALLOWED_TYPES, ALLOWED_TAINTS, CAPTURE_MODES, CONFIDENCE_FLOOR, CORE_MD_PATH, GLOBAL_NAMESPACE, MAINTENANCE_LOCK_STALE_SECONDS, MAINTENANCE_POLL_SECONDS, MAINTENANCE_WAIT_SECONDS, MAX_CONTENT_CHARS, PROMPT_INJECTION_PATTERNS, Path, SCHEMA_LOCK_POLL_SECONDS, SCHEMA_LOCK_STALE_SECONDS, SCHEMA_LOCK_WAIT_SECONDS, SCHEMA_VERSION_KEY, SIGNAL_CONFIDENCE, STORE_PATH, SUPPORTED_SCHEMA_VERSION, TAINT_RANK, TAINT_TRUSTED_SIGNALS, WRITER_LEASE_STALE_SECONDS, ZMEM_VEC_NS_OVERFETCH_DEFAULT, ZMEM_VEC_NS_OVERFETCH_ENV, _acquire_writer_lease, _as_of_temporal_predicate, _cleanup_stale_writer_leases, _commit, _env_float, _format_recency, _host, _load_ns_migration_checkouts, _load_vec, _lock_path, _normalize_content, _parse_iso_to_epoch, _prepare_store, _read_schema_version, _record_ns_migration, _rekey_namespaces, _release_named_lock, _release_writer_lease, _resolve_core_md_path, _resolve_skills_dirs, _resolve_store_path, _retry_pending_ns_migration, _strict_acquire_lock, _vec_knn_in_namespace, _wait_for_maintenance_clear, _writer_dir, annotations, argparse, calendar, connect, contextlib, datetime, glob, hashlib, init_db, json, math, migrate, now_iso, os, re, shutil, sqlite3, struct, subprocess, sys, time, timezone, uuid, validate_taint, worse_taint
from storelib.write import ALLOWED_TAINTS, ALLOWED_SIGNALS, ALLOWED_TYPES, CAPTURE_MODES, CapturePolicyRefusal, ContentTooLarge, DEDUP_SIMILARITY_THRESHOLD, GLOBAL_NAMESPACE, MAX_CONTENT_CHARS, PROMPT_INJECTION_PATTERNS, Path, SCHEMA_VERSION_KEY, SECRET_PATTERNS, SIGNAL_CONFIDENCE, SUPPORTED_SCHEMA_VERSION, TAINT_RANK, TAINT_TRUSTED_SIGNALS, _GLOBAL_NEAR_MISS_STEMS, _SAMPLE_EXTRACT_LIMIT, _SIGNAL_RANK, _aggregate_errors, _apply_capture_policy, _check_secrets, _classify_error_type, _commit, _default_taint_for_signal, _detect_duplicate, _detect_patterns, _env_float, _extract_user_messages, _find_semantic_duplicate, _global_near_miss_key, _has_injection_risk_tag, _has_prompt_injection_risk, _host, _merge_on_dedup, _merge_tag_strings, _normalize_capture_mode, _normalize_content, _redact_secret_like_text, _source_hash, _to_win_path, _validate_namespace, _warn_degraded_embeddings_once, add_memory, annotations, argparse, calendar, contextlib, datetime, glob, hashlib, json, math, now_iso, os, re, rekey_namespace, shutil, sqlite3, struct, subprocess, supersede_memory, sys, time, timezone, update_memory, uuid, validate_taint, worse_taint
from storelib.recall import CONFIDENCE_FLOOR, GLOBAL_NAMESPACE, Path, RECENCY_HALF_LIFE_DAYS, STORE_PATH, W_BM25, W_CONFIDENCE, W_POPULARITY, W_RECENCY, ZMEM_FENCE_CLOSE, ZMEM_FENCE_OPEN, _bump_telemetry, _classify_injection, _commit, _expand_namespace_aliases, _fetch_by_ids, _format_fenced_recall, _format_recency, _has_any_embedding, _has_injection_risk_tag, _merge_tiers, _ns_migration_map, _parse_iso_to_epoch, _recall_one_tier, _recent_one_tier, _reembed, _rrf_fuse, _source_hash, _uses_count, _vec_knn_in_namespace, _vector_knn, annotations, argparse, calendar, compute_score, contextlib, datetime, get_memory, glob, hashlib, json, list_memory, math, now_iso, os, re, recall_memory, recent_memory, shutil, sqlite3, stats, struct, subprocess, sys, time, timezone, uuid
from storelib.consolidate import CONSOLIDATE_DEFAULT_THRESHOLD, CONSOLIDATE_GROWTH_THRESHOLD, CONSOLIDATE_LEXICAL_THRESHOLD, CONSOLIDATE_MIN_INTERVAL_DAYS, INGEST_MAX_CONTENT_CHARS, Path, _CONSOLIDATE_NEGATOR_RE, _LEXICAL_STOPWORDS, _absorb_decision, _absorb_into_keeper, _env_float, _lexical_similarity, _lexical_tokens, _merge_on_dedup, _normalize_content, _normalize_text, _polarity_signature, _unique_tokens, annotations, argparse, calendar, consolidate, contextlib, datetime, glob, hashlib, json, math, now_iso, os, re, shutil, sqlite3, struct, subprocess, supersede_memory, sys, time, timezone, uuid
from storelib.backup import BACKUP_DEFAULT_RETENTION, BACKUP_LOCK_STALE_SECONDS, CONSOLIDATE_LOCK_STALE_SECONDS, MAINTENANCE_LOCK_STALE_SECONDS, PRERESTORE_PREFIX, Path, SCHEMA_LOCK_POLL_SECONDS, SCHEMA_LOCK_STALE_SECONDS, SCHEMA_LOCK_WAIT_SECONDS, SENTINEL_PREFIXES, SENTINEL_SWEEP_DAYS_DEFAULT, SIDECAR_SUFFIXES, SNAPSHOT_GLOB, SNAPSHOT_PREFIX, SNAPSHOT_SUFFIX, STORE_PATH, SnapshotError, _acquire_lock, _backup_dir, _backup_due, _backup_interval_days, _cleanup_stale_writer_leases, _commit, _discard_snapshot, _ensure_backup_dir, _env_float, _host, _integrity_check_readonly, _lock_path, _new_snapshot_path, _parse_iso_to_epoch, _read_schema_version, _release_lock, _release_named_lock, _restore_locked, _row_counts, _snapshot_stamp, _strict_acquire_lock, _sweep_candidate_dirs, annotations, apply_retention, argparse, calendar, cmd_backup, cmd_restore, cmd_sweep, contextlib, counts_agree, create_snapshot, datetime, glob, hashlib, json, list_snapshots, math, now_iso, os, re, shutil, sqlite3, struct, subprocess, sys, time, timezone, uuid, verify_snapshot
from storelib.sync import ALLOWED_SIGNALS, ALLOWED_TYPES, CapturePolicyRefusal, EXPORT_PACK_DEFAULT_GLOBAL_LIMIT, EXPORT_PACK_DEFAULT_MAX_BYTES, EXPORT_PACK_DEFAULT_MIN_CONFIDENCE, EXPORT_PACK_DEFAULT_PROJECT_LIMIT, GLOBAL_NAMESPACE, INGEST_MAX_CONTENT_CHARS, INGEST_MAX_FUTURE_SKEW_SECONDS, MAX_CONTENT_CHARS, MAX_LINE_CHARS, Path, SIGNAL_CONFIDENCE, STORE_PATH, _GLOBAL_NEAR_MISS_STEMS, _INGEST_ID_RE, _apply_capture_policy, _commit, _detect_duplicate, _global_near_miss_key, _ingest_row, _merge_on_dedup, _normalize_content, _pack_query, _parse_iso_to_epoch, _render_pack, _sanitize_error_text, _sanitize_pack_content, _validate_sync_row, _warn_degraded_embeddings_once, annotations, argparse, calendar, cmd_export_jsonl, cmd_export_pack, cmd_ingest_jsonl, contextlib, datetime, glob, hashlib, json, math, now_iso, os, re, shutil, sqlite3, struct, subprocess, supersede_memory, sys, time, timezone, uuid
from storelib.promote import PROMOTE_CONFIDENCE_FLOOR, PROMOTE_SIGNALS, PROMOTE_USE_FLOOR, PROMOTION_REVIEW_DIRNAME, Path, STORE_PATH, _first_sentence, _resolve_promotion_review_dir, _resolve_skills_dirs, _slugify_skill_name, _synthesize_trigger_description, _yaml_dquote, annotations, argparse, calendar, contextlib, datetime, glob, hashlib, json, math, os, promote_memory, re, shutil, sqlite3, struct, subprocess, sys, time, timezone, uuid
from storelib.mine import Path, _SAMPLE_EXTRACT_LIMIT, _aggregate_errors, _classify_correction, _classify_error_type, _collapse_line_breaks, _detect_patterns, _extract_user_messages, _failures_from_db, _failures_from_transcript, _host, _is_rejection_text, _mine_corrections_from_transcript, _normalize_capture_mode, _queue_mined, _redact_error_pattern_samples, _redact_secret_like_text, _rejection_reason, _result_text, _sanitize_correction_message, _sanitize_error_text, _sanitize_exc_text, _sanitize_pack_content, _sanitize_tool_name, _transcript_mtime_iso, annotations, argparse, calendar, cmd_corrections, cmd_failures, cmd_mine_history, cmd_queue_clear, cmd_queue_list, contextlib, datetime, glob, hashlib, json, math, os, re, shutil, sqlite3, struct, subprocess, sys, time, timezone, uuid
from storelib.cli import ALLOWED_SIGNALS, ALLOWED_TYPES, BACKUP_DEFAULT_RETENTION, CAPTURE_MODES, CONSOLIDATE_DEFAULT_THRESHOLD, CONSOLIDATE_LOCK_STALE_SECONDS, CapturePolicyRefusal, ContentTooLarge, EXPORT_PACK_DEFAULT_GLOBAL_LIMIT, EXPORT_PACK_DEFAULT_MAX_BYTES, EXPORT_PACK_DEFAULT_MIN_CONFIDENCE, EXPORT_PACK_DEFAULT_PROJECT_LIMIT, GLOBAL_NAMESPACE, Path, SENTINEL_SWEEP_DAYS_DEFAULT, SNAPSHOT_GLOB, STORE_PATH, _acquire_lock, _acquire_writer_lease, _prepare_store, _reembed, _release_lock, _release_writer_lease, _wait_for_maintenance_clear, add_memory, annotations, argparse, calendar, cmd_backup, cmd_corrections, cmd_export_jsonl, cmd_export_pack, cmd_failures, cmd_ingest_jsonl, cmd_mine_history, cmd_queue_clear, cmd_queue_list, cmd_restore, cmd_sweep, connect, consolidate, contextlib, datetime, get_memory, glob, hashlib, json, list_memory, main, math, nonnegative_int, os, promote_memory, re, recall_memory, recent_memory, rekey_namespace, shutil, sqlite3, stats, struct, subprocess, supersede_memory, sys, time, timezone, uuid

## ---- live forwarded mutable globals (issue #57) ----
# Consumers (tests, doctor, hooks) READ these on `store`/`storelib` while the
# owning submodule mutates the same flag/object internally. A value import
# would snapshot them; __getattr__ forwards LIVE so `store.X` returns what the
# submodule currently holds. Writes are NOT forwarded: a module-level
# `__setattr__` is not honoured by CPython (attribute assignment always goes to
# the module __dict__), so code that reassigns/mocks one of these must target
# the owning submodule directly (e.g. storelib.consolidate.X).
import sys as _sys
import storelib.schema as _schema
import storelib.write as _write
import storelib.recall as _recall
# `storelib.consolidate` is ambiguous: the re-exported `consolidate()` function
# (callable CLI surface) clobbers the submodule attribute set by the import
# machinery, so `import storelib.consolidate as _consolidate` would bind the
# FUNCTION, not the module. Reach the module via sys.modules so the live
# forwarded reads below return the namespace the code actually reads.
_consolidate = _sys.modules['storelib.consolidate']

## ---- per-load env refresh (issue #57) ----
# Pre-split, every `store.py` load re-read ZMEM_STORE / ZMEM_DATA / etc at its
# own import and bound STORE_PATH / CORE_MD_PATH (and never again). These are
# frozen module globals, NOT lazily re-resolved. storelib is a shared process
# singleton, so without the refresh below those paths would stick at the FIRST
# load's value. The store.py shim calls _refresh_env_state() right after
# importing storelib so callers that reload store.py with a different env
# (notably the model-absent test harness, which loads one store module per
# throwaway ZMEM_STORE) observe the same per-load contract as before.
import storelib.sync as _sync
import storelib.backup as _backup
import storelib.promote as _promote
import storelib.cli as _cli

def _refresh_env_state() -> None:
    """Re-derive env-derived process state at each store.py load.

    Refreshes STORE_PATH/CORE_MD_PATH (and their by-value snapshots in the five
    submodules that import them: recall, sync, backup, promote, cli) plus any
    ZMEM_* float tunable currently present in the env, in each owning submodule.
    Keeps the refreshed-submodule list here in sync with any future module that
    snapshots a path/tunable exported from storelib.schema.
    """
    schema_path = _schema._resolve_store_path()
    core_md = _schema._resolve_core_md_path()
    _schema.STORE_PATH = schema_path
    _schema.CORE_MD_PATH = core_md
    # Submodules that imported STORE_PATH/CORE_MD_PATH by value still snapshot
    # them; refresh those snapshots so every consumer sees the same path.
    _recall.STORE_PATH = schema_path
    _sync.STORE_PATH = schema_path
    _backup.STORE_PATH = schema_path
    _promote.STORE_PATH = schema_path
    _cli.STORE_PATH = schema_path
    globals()["STORE_PATH"] = schema_path
    globals()["CORE_MD_PATH"] = core_md

    # The env-tunable knobs are also frozen module globals (parsed from env at
    # import). Re-apply a PRESENT override on each load (a reload with a
    # different ZMEM_... must honour it, as pre-split), while leaving the
    # module's own default untouched when the var is absent. Re-deriving in the
    # OWNING submodule is what matters: that is the namespace the runtime reads.
    _refresh_env_floats(_schema, (
        ("SCHEMA_LOCK_STALE_SECONDS", "ZMEM_SCHEMA_LOCK_STALE_SECONDS"),
        ("SCHEMA_LOCK_WAIT_SECONDS", "ZMEM_SCHEMA_LOCK_WAIT_SECONDS"),
        ("SCHEMA_LOCK_POLL_SECONDS", "ZMEM_SCHEMA_LOCK_POLL_SECONDS"),
        ("MAINTENANCE_LOCK_STALE_SECONDS", "ZMEM_MAINTENANCE_LOCK_STALE_SECONDS"),
        ("MAINTENANCE_WAIT_SECONDS", "ZMEM_MAINTENANCE_WAIT_SECONDS"),
        ("MAINTENANCE_POLL_SECONDS", "ZMEM_MAINTENANCE_POLL_SECONDS"),
        ("WRITER_LEASE_STALE_SECONDS", "ZMEM_WRITER_LEASE_STALE_SECONDS"),
    ))
    _refresh_env_floats(_consolidate, (
        ("CONSOLIDATE_DEFAULT_THRESHOLD", "ZMEM_CONSOLIDATE_THRESHOLD"),
        ("CONSOLIDATE_MIN_INTERVAL_DAYS", "ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS"),
        ("CONSOLIDATE_GROWTH_THRESHOLD", "ZMEM_CONSOLIDATE_GROWTH_THRESHOLD"),
        ("CONSOLIDATE_LEXICAL_THRESHOLD", "ZMEM_CONSOLIDATE_LEXICAL_THRESHOLD"),
    ))
    _refresh_env_floats(_backup, (
        ("SENTINEL_SWEEP_DAYS_DEFAULT", "ZMEM_SENTINEL_SWEEP_DAYS"),
        ("BACKUP_LOCK_STALE_SECONDS", "ZMEM_BACKUP_LOCK_STALE"),
        ("CONSOLIDATE_LOCK_STALE_SECONDS", "ZMEM_CONSOLIDATE_LOCK_STALE"),
    ))
    _refresh_env_floats(_write, (
        ("DEDUP_SIMILARITY_THRESHOLD", "ZMEM_DEDUP_THRESHOLD"),
    ))

def _refresh_env_floats(mod, pairs) -> None:
    """Re-apply env overrides for float knobs on `mod` iff the var is present.

    `mod._env_float` falls back to the current value when the var is absent, so
    the module's import-time default is preserved exactly; only an explicitly
    present override changes anything (matching the pre-split per-load read).
    """
    get_env = getattr(mod, "_env_float")
    for name, envvar in pairs:
        if envvar in os.environ:
            setattr(mod, name, get_env(envvar, getattr(mod, name)))

def __getattr__(name):
    if name == '_embeddings': return _schema._embeddings
    if name == '_NS_MIGRATION_CHECKOUTS': return _schema._NS_MIGRATION_CHECKOUTS
    if name == 'CONSOLIDATE_MAX_ROWS_PER_NAMESPACE': return _consolidate.CONSOLIDATE_MAX_ROWS_PER_NAMESPACE
    if name == '_degraded_embedding_warned': return _write._degraded_embedding_warned
    raise AttributeError(name)
