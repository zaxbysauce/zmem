"""Assert the storelib split preserves the full store module export surface.

Every name tests / hermes / the CLI could resolve on the pre-split store module
must still resolve on the post-split shim (issue #57, 2.6). The expected list
below is frozen from the pre-split store.py (ddce432).

Run: python tests/test_storelib_exports.py
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

# Imported in setUpClass (not at module import) so the env/sys.path mutations
# `import store` needs are confined to this test class and do not leak into a
# shared runner's collection.
store = None


EXPECTED_EXPORTS = [
    "ALLOWED_SIGNALS", "ALLOWED_TAINTS", "ALLOWED_TYPES", "BACKUP_DEFAULT_RETENTION", "BACKUP_LOCK_STALE_SECONDS", "CAPTURE_MODES", "CONFIDENCE_FLOOR",
    "CONSOLIDATE_DEFAULT_THRESHOLD", "CONSOLIDATE_GROWTH_THRESHOLD", "CONSOLIDATE_LEXICAL_THRESHOLD", "CONSOLIDATE_LOCK_STALE_SECONDS", "CONSOLIDATE_MAX_ROWS_PER_NAMESPACE", "CONSOLIDATE_MIN_INTERVAL_DAYS",
    "CORE_MD_PATH", "CapturePolicyRefusal", "ContentTooLarge", "DEDUP_SIMILARITY_THRESHOLD", "ENTITY_KINDS", "ENTITY_ROLE_DEFAULT", "ENTITY_STOPWORDS", "EXPORT_PACK_DEFAULT_GLOBAL_LIMIT", "EXPORT_PACK_DEFAULT_MAX_BYTES",
    "EXPORT_PACK_DEFAULT_MIN_CONFIDENCE", "EXPORT_PACK_DEFAULT_PROJECT_LIMIT", "GLOBAL_NAMESPACE", "INGEST_MAX_CONTENT_CHARS", "INGEST_MAX_FUTURE_SKEW_SECONDS", "MAINTENANCE_LOCK_STALE_SECONDS",
    "MAINTENANCE_POLL_SECONDS", "MAINTENANCE_WAIT_SECONDS", "MAX_CONTENT_CHARS", "MAX_LINE_CHARS", "MMR_LAMBDA", "MMR_LAMBDA_DEFAULT", "PRERESTORE_PREFIX", "PROMOTE_CONFIDENCE_FLOOR",
    "PROMOTE_SIGNALS", "PROMOTE_USE_FLOOR", "PROMOTION_REVIEW_DIRNAME", "PROMPT_INJECTION_PATTERNS", "RECENCY_HALF_LIFE_DAYS", "SCHEMA_LOCK_POLL_SECONDS",
    "SCHEMA_LOCK_STALE_SECONDS", "SCHEMA_LOCK_WAIT_SECONDS", "SCHEMA_VERSION_KEY", "SECRET_PATTERNS", "SENTINEL_PREFIXES", "SENTINEL_SWEEP_DAYS_DEFAULT",
    "SIDECAR_SUFFIXES", "SIGNAL_CONFIDENCE", "SNAPSHOT_GLOB", "SNAPSHOT_PREFIX", "SNAPSHOT_SUFFIX", "STORE_PATH",
    "SUPPORTED_SCHEMA_VERSION", "TAINT_RANK", "TAINT_TRUSTED_SIGNALS", "SnapshotError", "WRITER_LEASE_STALE_SECONDS", "W_BM25", "W_CONFIDENCE", "W_POPULARITY",
    "W_RECENCY", "_CONSOLIDATE_NEGATOR_RE", "_GLOBAL_NEAR_MISS_STEMS", "_INGEST_ID_RE", "_LEXICAL_STOPWORDS", "_NS_MIGRATION_CHECKOUTS",
    "_SAMPLE_EXTRACT_LIMIT", "_SIGNAL_RANK", "_absorb_decision", "_absorb_into_keeper", "_acquire_lock", "_acquire_writer_lease",
    "_aggregate_errors", "_apply_capture_policy", "_as_of_temporal_predicate", "_backup_dir", "_backup_due", "_backup_interval_days", "_bump_telemetry",
    "_check_secrets", "_classify_correction", "_classify_error_type", "_cleanup_stale_writer_leases", "_collapse_line_breaks", "_commit",
    "_cosine_blob", "_degraded_embedding_warned", "_detect_duplicate", "_detect_patterns", "_discard_snapshot", "_embeddings", "_ensure_backup_dir",
    "_env_float", "_expand_namespace_aliases", "_extract_user_messages", "_failures_from_db", "_failures_from_transcript", "_fetch_by_ids",
    "_fetch_embeddings_for_ids", "_find_semantic_duplicate", "_first_sentence", "_format_recency", "_global_near_miss_key", "_has_any_embedding", "_has_injection_risk_tag",
    "_has_prompt_injection_risk", "_host", "_ingest_row", "_integrity_check_readonly", "_is_rejection_text", "_jaccard_norm", "_lexical_similarity",
    "_lexical_tokens", "_load_ns_migration_checkouts", "_load_vec", "_lock_path", "_merge_on_dedup", "_merge_tag_strings",
    "_merge_tiers", "_mine_corrections_from_transcript", "_mmr_order", "_new_snapshot_path", "_normalize_capture_mode", "_normalize_content", "_normalize_text",
    "_ns_migration_map", "_pack_query", "_parse_iso_to_epoch", "_polarity_signature", "_prepare_store", "_queue_mined",
    "_read_schema_version", "_recall_one_tier", "_recent_one_tier", "_record_ns_migration", "_redact_error_pattern_samples", "_redact_secret_like_text",
    "_reembed", "_rejection_reason", "_rekey_namespaces", "_release_lock", "_release_named_lock", "_release_writer_lease",
    "_render_pack", "_resolve_core_md_path", "_resolve_promotion_review_dir", "_resolve_skills_dirs", "_resolve_store_path", "_restore_locked",
    "_result_text", "_retry_pending_ns_migration", "_row_counts", "_rrf_fuse", "_sanitize_correction_message", "_sanitize_error_text",
    "_sanitize_exc_text", "_sanitize_pack_content", "_sanitize_tool_name", "_slugify_skill_name", "_snapshot_stamp", "_source_hash",
    "_strict_acquire_lock", "_sweep_candidate_dirs", "_synthesize_trigger_description", "_to_win_path", "_transcript_mtime_iso", "_unique_tokens",
    "_uses_count", "_validate_namespace", "_validate_sync_row", "_vec_knn_in_namespace", "_vector_knn", "_wait_for_maintenance_clear", "_warn_degraded_embeddings_once",
    "_writer_dir", "_yaml_dquote", "_default_taint_for_signal", "add_memory", "apply_retention", "backfill_entities", "cmd_backup", "cmd_corrections",
    "cmd_entity_list", "cmd_entity_merge", "cmd_export_jsonl", "cmd_export_pack", "cmd_failures", "cmd_ingest_jsonl", "cmd_mine_history", "cmd_queue_clear",
    "cmd_queue_list", "cmd_restore", "cmd_sweep", "compute_score", "connect", "consolidate",
    "counts_agree", "create_snapshot", "entities_for_memory", "entities_for_memories", "entity_match_ids", "extract_entities", "get_memory", "init_db", "link_memory_entities", "list_memory", "list_snapshots",
    "main", "migrate", "nonnegative_int", "now_iso", "organize", "promote_memory", "recall_memory",
    "recent_memory", "rekey_namespace", "relink_memory", "stats", "supersede_memory", "update_memory", "validate_taint", "verify_snapshot", "worse_taint",
]


class ExportSurfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Confine process-global state changes (ZMEM_STORE, SCRIPTS_DIR on
        # sys.path, the sys.modules['store']/'storelib' cache pointing at the
        # tmp store) to this test class and restore them after. The cleanup
        # hook is registered BEFORE the import + after the sys.path insert so
        # it always runs (even if the import raises), and the import path
        # evicts any pre-cached store/storelib from sys.modules so the
        # isolation actually takes effect under unittest collection (where
        # other test modules may have already imported them with a different
        # env).
        cls.tmp = tempfile.mkdtemp(prefix="zmem-export-")
        cls.addClassCleanup(shutil.rmtree, cls.tmp, True)

        env = {**os.environ,
               "ZMEM_STORE": os.path.join(cls.tmp, "store.sqlite"),
               "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        cls.addClassCleanup(cls._restore_import_state)
        # Register the sys.path-restore cleanup BEFORE inserting, so a raise
        # inside the import or the env patch still leaves sys.path clean.
        sys.path.insert(0, str(SCRIPTS_DIR))
        with mock.patch.dict(os.environ, env):
            # Evict any pre-cached store/storelib (e.g. imported at collection
            # time by an earlier test module) so the import below re-runs the
            # load-time `_refresh_env_state()` against THIS test's env. Without
            # this, import_module returns the cached module bound to whatever
            # the FIRST importer's ZMEM_STORE was — a silent no-op for
            # isolation.
            for mod_name in ("store", "storelib"):
                sys.modules.pop(mod_name, None)
            global store
            store = importlib.import_module("store")

    @classmethod
    def _restore_import_state(cls):
        # Drain every SCRIPTS_DIR entry from sys.path, not just the first.
        # store.py unconditionally inserts its dirname at every fresh import;
        # between this class and other tests in the same process that also
        # import store, multiple copies can accumulate.
        while True:
            try:
                sys.path.remove(str(SCRIPTS_DIR))
            except ValueError:
                break
        # Drop store/storelib from sys.modules so a later `import store` in
        # the same process re-runs load-time setup against the caller's env
        # instead of seeing a singleton pointing at our deleted tmp dir.
        for mod_name in ("store", "storelib"):
            sys.modules.pop(mod_name, None)

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
