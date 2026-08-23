"""Assert the storelib split preserves the full store module export surface.

Every name tests / hermes / the CLI could resolve on the pre-split store module
must still resolve on the post-split shim (issue #57, 2.6). The expected list
below is frozen from the pre-split store.py (ddce432).

Run: python tests/test_storelib_exports.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

# Point ZMEM_STORE at a throwaway path BEFORE importing store (host precedence:
# ZMEM_STORE outranks ZMEM_DATA / home defaults) so the real ~/.zmem is never
# touched at import time.
_IMPORT_TMP = tempfile.mkdtemp(prefix="zmem-export-")
os.environ["ZMEM_STORE"] = os.path.join(_IMPORT_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")

sys.path.insert(0, str(SCRIPTS_DIR))

import store  # noqa: E402


EXPECTED_EXPORTS = [
    "ALLOWED_SIGNALS", "ALLOWED_TYPES", "BACKUP_DEFAULT_RETENTION", "BACKUP_LOCK_STALE_SECONDS", "CAPTURE_MODES", "CONFIDENCE_FLOOR",
    "CONSOLIDATE_DEFAULT_THRESHOLD", "CONSOLIDATE_GROWTH_THRESHOLD", "CONSOLIDATE_LEXICAL_THRESHOLD", "CONSOLIDATE_LOCK_STALE_SECONDS", "CONSOLIDATE_MAX_ROWS_PER_NAMESPACE", "CONSOLIDATE_MIN_INTERVAL_DAYS",
    "CORE_MD_PATH", "CapturePolicyRefusal", "ContentTooLarge", "DEDUP_SIMILARITY_THRESHOLD", "EXPORT_PACK_DEFAULT_GLOBAL_LIMIT", "EXPORT_PACK_DEFAULT_MAX_BYTES",
    "EXPORT_PACK_DEFAULT_MIN_CONFIDENCE", "EXPORT_PACK_DEFAULT_PROJECT_LIMIT", "GLOBAL_NAMESPACE", "INGEST_MAX_CONTENT_CHARS", "INGEST_MAX_FUTURE_SKEW_SECONDS", "MAINTENANCE_LOCK_STALE_SECONDS",
    "MAINTENANCE_POLL_SECONDS", "MAINTENANCE_WAIT_SECONDS", "MAX_CONTENT_CHARS", "MAX_LINE_CHARS", "PRERESTORE_PREFIX", "PROMOTE_CONFIDENCE_FLOOR",
    "PROMOTE_SIGNALS", "PROMOTE_USE_FLOOR", "PROMOTION_REVIEW_DIRNAME", "PROMPT_INJECTION_PATTERNS", "RECENCY_HALF_LIFE_DAYS", "SCHEMA_LOCK_POLL_SECONDS",
    "SCHEMA_LOCK_STALE_SECONDS", "SCHEMA_LOCK_WAIT_SECONDS", "SCHEMA_VERSION_KEY", "SECRET_PATTERNS", "SENTINEL_PREFIXES", "SENTINEL_SWEEP_DAYS_DEFAULT",
    "SIDECAR_SUFFIXES", "SIGNAL_CONFIDENCE", "SNAPSHOT_GLOB", "SNAPSHOT_PREFIX", "SNAPSHOT_SUFFIX", "STORE_PATH",
    "SUPPORTED_SCHEMA_VERSION", "SnapshotError", "WRITER_LEASE_STALE_SECONDS", "W_BM25", "W_CONFIDENCE", "W_POPULARITY",
    "W_RECENCY", "_CONSOLIDATE_NEGATOR_RE", "_GLOBAL_NEAR_MISS_STEMS", "_INGEST_ID_RE", "_LEXICAL_STOPWORDS", "_NS_MIGRATION_CHECKOUTS",
    "_SAMPLE_EXTRACT_LIMIT", "_SIGNAL_RANK", "_absorb_decision", "_absorb_into_keeper", "_acquire_lock", "_acquire_writer_lease",
    "_aggregate_errors", "_apply_capture_policy", "_backup_dir", "_backup_due", "_backup_interval_days", "_bump_telemetry",
    "_check_secrets", "_classify_correction", "_classify_error_type", "_cleanup_stale_writer_leases", "_collapse_line_breaks", "_commit",
    "_degraded_embedding_warned", "_detect_duplicate", "_detect_patterns", "_discard_snapshot", "_embeddings", "_ensure_backup_dir",
    "_env_float", "_expand_namespace_aliases", "_extract_user_messages", "_failures_from_db", "_failures_from_transcript", "_fetch_by_ids",
    "_find_semantic_duplicate", "_first_sentence", "_format_recency", "_global_near_miss_key", "_has_any_embedding", "_has_injection_risk_tag",
    "_has_prompt_injection_risk", "_host", "_ingest_row", "_integrity_check_readonly", "_is_rejection_text", "_lexical_similarity",
    "_lexical_tokens", "_load_ns_migration_checkouts", "_load_vec", "_lock_path", "_merge_on_dedup", "_merge_tag_strings",
    "_merge_tiers", "_mine_corrections_from_transcript", "_new_snapshot_path", "_normalize_capture_mode", "_normalize_content", "_normalize_text",
    "_ns_migration_map", "_pack_query", "_parse_iso_to_epoch", "_polarity_signature", "_prepare_store", "_queue_mined",
    "_read_schema_version", "_recall_one_tier", "_recent_one_tier", "_record_ns_migration", "_redact_error_pattern_samples", "_redact_secret_like_text",
    "_reembed", "_rejection_reason", "_rekey_namespaces", "_release_lock", "_release_named_lock", "_release_writer_lease",
    "_render_pack", "_resolve_core_md_path", "_resolve_promotion_review_dir", "_resolve_skills_dirs", "_resolve_store_path", "_restore_locked",
    "_result_text", "_retry_pending_ns_migration", "_row_counts", "_rrf_fuse", "_sanitize_correction_message", "_sanitize_error_text",
    "_sanitize_exc_text", "_sanitize_pack_content", "_sanitize_tool_name", "_slugify_skill_name", "_snapshot_stamp", "_source_hash",
    "_strict_acquire_lock", "_sweep_candidate_dirs", "_synthesize_trigger_description", "_to_win_path", "_transcript_mtime_iso", "_unique_tokens",
    "_uses_count", "_validate_namespace", "_validate_sync_row", "_vector_knn", "_wait_for_maintenance_clear", "_warn_degraded_embeddings_once",
    "_writer_dir", "_yaml_dquote", "add_memory", "apply_retention", "cmd_backup", "cmd_corrections",
    "cmd_export_jsonl", "cmd_export_pack", "cmd_failures", "cmd_ingest_jsonl", "cmd_mine_history", "cmd_queue_clear",
    "cmd_queue_list", "cmd_restore", "cmd_sweep", "compute_score", "connect", "consolidate",
    "counts_agree", "create_snapshot", "get_memory", "init_db", "list_memory", "list_snapshots",
    "main", "migrate", "nonnegative_int", "now_iso", "promote_memory", "recall_memory",
    "recent_memory", "rekey_namespace", "stats", "supersede_memory", "verify_snapshot",
]


class ExportSurfaceTests(unittest.TestCase):
    def test_every_presplit_export_still_resolves(self):
        missing = sorted(n for n in EXPECTED_EXPORTS if not hasattr(store, n))
        self.assertEqual(
            missing, [],
            "storelib split dropped export(s). Re-export them from the shim:\n"
            + "\n".join(f"  - {n}" for n in missing),
        )

    def test_storelib_package_importable(self):
        import storelib  # noqa: F401
        import storelib.cli  # noqa: F401

    def test_core_callables_callable(self):
        for name in ("add_memory", "recall_memory", "consolidate", "supersede_memory",
                     "connect", "init_db", "migrate", "promote_memory", "stats"):
            self.assertTrue(callable(getattr(store, name)), f"{name} not callable")

    def test_constants_intact(self):
        self.assertAlmostEqual(
            store.W_BM25 + store.W_CONFIDENCE + store.W_RECENCY + store.W_POPULARITY,
            1.0, places=6,
        )
        self.assertGreaterEqual(store.CONFIDENCE_FLOOR, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
