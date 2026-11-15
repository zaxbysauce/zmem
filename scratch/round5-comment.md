## Feedback round 5 — retarget to 0.157.0 + CI repair; all native CI green

The retarget past the 0.155.0 (#468) and 0.156.0 (#470) releases that consumed the version mid-flight is complete, and the swarm-r5 REQUEST_CHANGES findings were already fixed pre-retarget (F-001 admission ordering in `workflow.py`, F-002 CLI busy-arm, F-003 lock-busy mapping, F-004 pointer-refusal wrap, CI-005 package anchor — all with suites green locally and in CI).

| Item | Outcome | Evidence |
|---|---|---|
| Retarget 0.155.0 → 0.157.0 | DONE | Full ritual: main merged, CHANGELOG union, CLAIMS-AUDIT four-column rewrite adopted, 8 manifests + package files bumped, five-lanes regen, manifest re-emit; both release gates green |
| CI-006 (windows+ubuntu npm ci red) | FIXED | The retarget version bump had rewritten every `node_modules/*` resolved version in both package-lock.json files to 0.157.0; locks restored byte-identically from 45856ac5 with ONLY the two own-version fields bumped, `npm ci --dry-run` validated both plugins (423a6aaa) |
| Evidence version parity | FIXED | dsh canary re-run at 423a6aaa (7/7 lanes) with the repaired locks in tree; support report bumped to 0.157.0 with the canary-to-evidence delta disclosure; manifest re-emitted (4b4a97a1) |
| CI at head | GREEN | Run 43676251350 at 4b4a97a1: test ubuntu 35m31s PASS, test windows 1h28m55s PASS, embeddings ×2, isolation-canary, real-model, source-hermes all PASS. Entire Gates remains the third-party non-required check per repo ruleset (requires only the native test jobs) |
| Owner decisions | RESOLVED | Owner ruling 2026-11-15 adopted the counsel recommendations on FB-008 recovery contract, FB-031 namespace-maintenance contract, and the #432 reopen question; each becomes a collated follow-up filing at closeout (non-blocking) |

Merging on the user's standing merge authorization. Step 2 (deploy/rollout) remains HELD.
