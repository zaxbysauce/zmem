# Proposed Workstream T PR 13 of 14

**Issue title:** [Workstream T] PR 13 of 14: Native DeepSeek Harness adapter with broker-backed recall and explicit tools

**Parent:** [#292](https://github.com/zaxbysauce/zmem/issues/292). Implementation contract appended to Workstream T. One issue equals one implementation PR. No dependency is added to an existing T-01 through T-12 slot.

## Outcome

DeepSeek Harness runs a native Cordis bundle whose automatic memory and explicit operations use the same selected ZMem backend and fleet policy as the existing harnesses. It preserves current local mode when remote mode is unset and uses the T-08/T-10 broker boundary when remote mode is configured. Installable preview support is demonstrated by a native canary; complete learning parity and unrestricted first-class claims belong to T-14.

## Dependencies

- **Depends on:** T-10 [#289](https://github.com/zaxbysauce/zmem/issues/289), which transitively supplies the broker, typed write queue, principals, shared selection policy, delivery reservations, deadlines, and negotiated protocol.
- **Depends on:** T-11 [#290](https://github.com/zaxbysauce/zmem/issues/290) for the declared offline snapshot path.
- Rebase onto the merged dependencies before freezing implementation acceptance checks. Leave in-flight PRs #365/#372 and their review evidence intact.
- Coordinate with [#362](https://github.com/zaxbysauce/zmem/issues/362) on concurrent check-and-commit semantics and [#359](https://github.com/zaxbysauce/zmem/issues/359) on release integrity. Establish whether their fixes are already consumed by T-05/release work; do not duplicate those fixes in the adapter.

## Scope

Add `deepseek-plugin/` as a native Cordis plugin/bundle with explicit supported runtime compatibility. The preparation reference is `dsh@0.2.1-rc.2`, not a promise that every later preview is compatible. Carry the bundle's package version and runtime files through the repository's release inventory, content digest, and distribution checks.

Register canonical harness/lane `deepseek` across the selector, evidence, MCP, decision/report, canary, and fallback vocabularies. Keep historical lane ordering stable and update exact-set expectations deliberately. Extend the generated production fleet capability matrix and native-registration coverage; no fabricated shell-hook manifest is introduced.

Resolve native session and subagent lineage, the effective working directory, and project namespace locally. Use the T-05 identity tuple, including server-derived principal and harness identity; a native session id alone is insufficient across machines. Attach canonical untrusted `rendered` context to the current accepted `agent/pre-step` decision. Keep host-owned Tier-0 instructions distinct from recalled rows. No direct SQLite access, custom row renderer, independent network protocol, or retrieval-engine change is in scope.

Explicit memory operations use one native tool surface routed through the same backend as passive recall. Do not expose an accidental second MCP/native tool set. Preserve ordinary host approvals and distinguish queued/pending receipts from committed writes. Passive failures remain bounded and fail open; explicit operations report actionable errors and must not report an unsuccessful save as success.

Installation design remains unverified against a running 0.2.1-rc.2 installation. Validate supported profile/bundle contracts, preserve user patch rows, report restart-required activation honestly, and retain the shared store on uninstall. Verify the desktop carrier's managed-profile installation path independently of the CLI profile. Do not mutate the Claude/ZCode plugin registry codec.

## Acceptance criteria

1. **AC1 — Actual first-request delivery.** With a synthetic isolated store and real supported harness runtime, startup plus query recall reach the first admitted model-bound request as fenced data. Rejected/cancelled input is not acknowledged as delivered. Repeat delivery and retry exercise T-05's idempotent event/reservation behavior.
2. **AC2 — Backend parity.** Local and connected-remote passive/explicit operations resolve the expected authoritative store and principal policy. The native `add` tool cannot silently create a second local store in remote mode; pending versus committed receipts are tested.
3. **AC3 — Isolation and lifecycle.** Concurrent agents, identical native ids under different principals, subagent lineage, fork, persisted resume, compaction, and unload have explicit identity/ledger tests. Ordinary turn end or live-agent disposal never impersonates persisted-session deletion. Pending broker work drains within the owned deadline.
4. **AC4 — Timing and policy.** Prove pre-tool and batch guidance timing, authorization denial, kill-switch behavior, bounded input/output, deadlines, missing Python/backend, malformed replies, and cancellation. Declare unsupported moments as capability gaps; do not claim that next-request guidance can change the already-selected tool.
5. **AC5 — Offline behavior.** Reuse T-11 snapshots, revision/expiry labels, authorized namespace filtering, and no-valid-snapshot behavior. A revoked or expired snapshot cannot be presented as connected current memory.
6. **AC6 — Installation and release.** Test supported profile installation, upgrade, activation/restart reporting, uninstall retention, package compatibility refusal, version alignment, package contents, digest coverage, and the plugin directory's inclusion in drift checks.
7. **AC7 — Native canary and support claims.** Extend host canary schemas and the generated fleet matrix with a DeepSeek registration source. Canary evidence shows isolated-store access and actual admitted context, with supported carrier/platform/version combinations explicit. README remains preview-scoped until T-14 passes.

Planned discriminating checks belong in `tests/test_t13_deepseek_contract.py` and the native adapter's own runtime suite. They are **not present or frozen yet**. The preparation probe proves five host API properties only, not these seven feature criteria. Freeze genuine pre-fix failures on the merged dependency base when implementation begins.

## Exclusions

No HY Memory engine adoption, subject-aware dedup rewrite, additional schema migration, new fleet transport, automatic trust promotion, or new global retrieval lane. T-14 owns capture/retention/source provenance and complete operational parity.
