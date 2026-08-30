# Claims Audit — what zmem actually claims (issue #82)

Every public capability claim in `README.md` maps to a code path, a committed
eval artifact, or is marked **aspirational**. Scores from `eval_runner.py`
never gate CI (`--fail-under` is off; `.github/workflows/ci.yml`). No Argos /
LongMemEval / third-party benchmark numbers appear anywhere in this repo's
claims; zmem publishes only what its own harness measures on its own fixture.

| Claim (README / docs surface) | Evidence (path + symbol) | Status |
|---|---|---|
| Hybrid FTS5 + vector + entity recall, RRF-fused, composite re-ranked | `skills/memory/scripts/storelib/recall.py` `recall_memory` / `_recall_one_tier` / `_rrf_fuse`; pinned by `tests/test_mmr.py`, `tests/test_hybrid_default.py` | shipped |
| Keyword recall with a confidence floor, high-precision-first | `storelib/schema.py` `CONFIDENCE_FLOOR`, `storelib/recall.py` `_recall_one_tier` (floor in lane SQL); `tests/test_injection_recall.py` | shipped |
| `--as-of` point-in-time recall; `update`/`supersede`/`invalidate` are append-only (tombstone + lineage) | `storelib/recall.py` `_as_of_temporal_predicate` usage; `storelib/write.py` `update_memory`/`supersede_memory`; `tests/test_as_of_recall.py`, `tests/test_update_invalidate.py` | shipped |
| Offline eval harness, 42-item gold, deterministic (fake embedder + pinned clock), scores do NOT gate CI | `scripts/eval_runner.py` (`--store` required, `--fail-under` default None), `storelib/eval_gold.py` `BUCKETS`/`evaluate_items`, `eval/gold.jsonl`, `.github/workflows/ci.yml` (no `--fail-under`) | shipped |
| Retrieval debugger: `recall --explain [--target ID\|fragment] [--json]`, zero-write, closed reason set | `storelib/recall.py` `EXPLAIN_REASONS`/`explain_recall`; `tests/test_explain_recall.py` | shipped (issue #82) |
| Change-intent lineage unfold on EXPLICIT recall only (`[PREVIOUSLY]`, budgeted, never bumped; hooks/`--no-bump`/`search` never unfold) | `storelib/recall.py` `_CHANGE_INTENT_RES`/`_unfold_enabled`/`unfold_change_history`; `tests/test_chain_unfold.py` | shipped (issue #82) |
| Eval honesty buckets: retraction, polarity, change-intent (+ optional `explicit` gold flag) | `eval/gold.jsonl` items `retract-*`, `polarity-*`, `ci-*`; `tests/fixtures/eval_store.py` rowids 51–64; `tests/test_eval_runner.py` | shipped (issue #82) |
| Self-corpus probe (`scripts/eval_self_corpus.py`), `--store` required, home path refused, byte-passive | `scripts/eval_self_corpus.py`; `tests/test_eval_self_corpus.py` | shipped (issue #82) |
| Injection-risk scanning, emit-time re-classify, `--no-bump` omission | `storelib/schema.py` `PROMPT_INJECTION_PATTERNS`, `storelib/recall.py` `_classify_injection`; `tests/test_injection_recall.py` | shipped |
| "Capture-correction … never writes the store" | `hooks/lib/*` queue sidecar (`<store-data-dir>/queue/<ns>.json`); `tests/test_launcher.js` | shipped — **and scoped**: this claim is about the capture-correction hook only |
| "Hooks never `add` rows" | Hooks pass `--no-bump` and never invoke `add`; `tests/test_surface_consistency.py`, `tests/test_feedback_promote.py` source scans | shipped — **this is the accurate general claim**. The stronger sentence "hooks never write the store" was overstated and is QUALIFIED in `skills/memory/SKILL.md` (the detached SessionStart `session-cadence` maintenance task legitimately runs organize/backup/sweep, which write; the recall/capture hooks themselves write nothing to the store) |
| Cross-encoder rerank is CLI-explicit-only, never on hooks | `storelib/cross_encoder.py` `cli_allowed`; `tests/test_cross_encoder.py` | shipped |
| Fake embed profile is CI-only (never real writes) | `skills/memory/scripts/embed_profiles.py` `fake_embed`; doctor warning; `ZMEM_EMBED_PROFILE=fake` in CI only | shipped |
| Schema forward/backward compatibility window (older clients keep working on newer stores) | `skills/memory/scripts/schema_meta.py` `FORWARD_COMPAT_SCHEMA_VERSION`; `tests/test_schema_compat.py` | shipped |
| LongMemEval / LoCoMo numbers, third-party benchmark scores | — | **not a claim**: adapters (`scripts/eval_adapters.py`) convert public corpora into zmem gold format only; no score is published and CI downloads nothing |
| Vector recall quality on real corpora at scale | — | **aspirational** (self-corpus harness above measures it per-store; no published number) |
| Cross-machine store sync / conflict-free multi-writer | `storelib/sync.py` export/import; `storelib/schema.py` writer leases | partial — leases serialize writers on one box; sync is offline file exchange, not live replication |
