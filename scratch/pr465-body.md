Closes #463

## Summary

Optional, default-off external knowledge workflow: explicit capture of local Markdown and a configured knowledge-base HTTP endpoint (create/read only), readback integrity verification, deterministic immutable versions, a private crash-recovery journal, and exact-ref pointer advancement via compare-and-swap inside the zmem store transaction — never in the KB API. Reserved `knowledge:` source refs stay isolated from ordinary writers, dedup folds, imports, and consolidation. Capability-gated knowledge delivery keeps the core queue untouched; an old or rolled-back hub fails feature-only.

## Scope

- In: `knowledge` config object in the existing store config (strict boolean); reserved `knowledge:` refs and typed `knowledge_link` op; `skills/memory/scripts/knowledge/` (config, documents, journal, workflow, delivery, locking) plus the `skills/memory/scripts/storelib/knowledge.py` handler; guards on ordinary writer/import/ingest/consolidate/supersede paths; secret-scan-then-reject admission, LF + single terminal newline, full SHA-256; `tests/test_knowledge_*.py` (8 suites incl. real-wire hub integration); operator docs `docs/KNOWLEDGE-COMPANION.md`; release 0.154.0 surfaces (8 host manifests + dated CHANGELOG section + re-emitted `release-manifest.json`).
- Out: conditional discovery wiring (PR2, stacked on this branch); native OpenCode adapter (approved exclusion; PR2 ships a manual shim); passive remote fetch; new schema/table/metadata; fleet deployment.

## Sequence

- PR 1 of 2 — two minimal stacked PRs, not one cumulative diff.
- Base: `main` at 5bee7e6909cf4e2af98eb6903141f0fc5fbf436d (0.153.0 tip).
- Branch `feat/knowledge-companion`, two commits: implementation 5a1fe3b664b620b7b652b01c2410d580e3fe3e8f, then initial evidence commit 817739f46a5188112818c4e6cc7fbf3f00bd3a57 recording the committed-runtime native canary under `docs/host-gate-evidence/deepseek/`.
- Diff vs `main`: 48 files, +14007/−75. Pushed; origin tip verified to match HEAD exactly.
- PR2 (#466, conditional discovery) stacks on this branch as a 30-file delta.

## Tests

The results below are local validation. Remote CI is now running on this PR; check the live checks panel for its current verdict. Pre-commit suites ran against the implementation working tree derived from the cited base; counts are per-suite — groups overlap, so no sum or total is claimed.

- Seven feature suites (`tests/test_knowledge_{config,documents,journal,links,guards,workflow,delivery}.py`): 227 passed, 1 skip (Windows file-symlink privilege; the directory-junction guard path is tested), 8 subtests — run before the final crash-recovery fix.
- Post-fix crash-delta suite (workflow/delivery/journal): 107 passed, 8 subtests — this rerun supersedes the affected pre-fix results, it is not added to them.
- Isolated real-wire MCP integration (loopback hub subprocess, real HTTP clients, temp stores/tokens only): 21/21 after the typed-refusal fix; the two previously RED secure-contract probes are pinned as regressions.
- Floors: legacy rescan + docs/links/guards 109 passed (+1 skip); core auth/remote 41 passed; dedup/consolidate 97 passed (+1 skip); release/version/docs 100 passed; final release evidence 8 passed.
- Native canary at the committed runtime commit 5a1fe3b664b6: 7/7 lanes, headless win32 carrier, recorded in-repo at `docs/host-gate-evidence/deepseek/canary-report.json`.
- `release-manifest.json`: 0.154.0, 163 files, digest e1c8ca77caf0a7ca7cdcc55aec0342d865205046549ac4807566f292897bb8d7.

Detailed logs are session-local and not publicly available; reproduce from this branch (the base SHA and manifest digest identify the exact state).

## Independent reviews

- Corrected plan: two plan critics APPROVE.
- Two implementation reviewer shards (pointer: documents/write/knowledge/rekey/schema; workflow: mcp-client/transport/workflow/delivery/cli): NEEDS_REVISION in rounds 1–2, every finding fixed FIX-NOW, then APPROVE on the frozen snapshots; the post-critic correction was separately re-reviewed APPROVE.
- Final critic: NEEDS_REVISION (verified crash-recovery blocker between authoritative pointer commit and delivery-intent completion), then APPROVE after the fix, rerunning the original crash probe plus the negative concurrent-writer case.
- Publication check (commit/push evidence only): reviewer APPROVE + final critic APPROVE on delta/ancestry/tree/hygiene. No runtime edits after the approvals — HEAD only updates the evidence record.
- All approvals are on bounded evidence (frozen file snapshots and local runs) and are the final reviews of record.

## Remaining limitations

- Windows file-symlink test is an environment skip; no platform-coverage claim until Linux/privileged CI runs.
- Native OpenCode adapter absent (approved exclusion); PR2 adds a manual instruction shim only.
- Native canary is headless win32 only; desktop carrier untested (preview status).
- Delivery quarantine tombstones the live row but retains append-only raw history until explicit purge — no erasure claim.
- Readback verification proves content integrity, not semantic truth.
- No live-fleet deployment, installed-config migration, or cross-host live canary; that phase is separately owner-gated (the deployed hub predates the knowledge capability).

## Issue linkage

Workstream U: #463 -> #464. Blocks the discovery PR; merge order is PR1 then PR2.

## Current publication state

- PR head: `4b4a97a1` (release **0.157.0**, retargeted past the 0.155.0 PR-#468 and 0.156.0 PR-#470 releases that consumed the version mid-flight; full repo retarget ritual run, including the CI-006 package-lock dependency-version repair and the dsh host-gate evidence re-run at this head).
- Current review diff: 60 files changed, 18716 insertions(+), 354 deletions(-).
- Round-3 closure: both new swarm reviews fully triaged; every verified finding fixed with tests, disproved, or explicitly routed. Ordered gates: Stage B reviewer, fresh falsification test engineer, separate closeout reviewer, final critic (each with correction rounds). Round-3 ledger posted as the latest PR comment.
- Canary evidence: actual run at the committed retarget head, 8/8 lanes (policy lane in helper-absent default-off form, disclosed in-report), committed-runtime provenance superseding all working-tree runs.
- Owner decisions resolved (2026-11-15 owner ruling): the counsel/critic recommendations on FB-008 scoped recovery, FB-031 knowledge-aware namespace maintenance, and the #432 reopen question are ADOPTED; each becomes a collated follow-up filing at closeout (non-blocking for this merge). No owner decision remains open on this PR.- No merge, deployment, restart, or live configuration change; step 2 remains held.

