# FLEET — The zmem fleet parity contract

Workstream T PR 1 of 12 (issue #280, epic #292). This document is the
written definition of "full parity on every machine and harness" that every
later Workstream T PR is judged against. The machine-checkable companion is
[`docs/fleet-capability-matrix.json`](fleet-capability-matrix.json) — one
row per (harness, moment) with per-input locality, hub operations, and the
declared degraded behaviour; `tests/test_t01_fleet_matrix.py` fails whenever
a `hooks/hooks.*.json` manifest registers an event the matrix does not
account for, so coverage gaps cannot reopen silently.

## Goal

Give every agent harness on every machine of the fleet (one private
Tailscale tailnet) the same zmem behaviour: the same passive injection at
every lifecycle moment, the same explicit tools, and the same write and
capture semantics.

## Consistency model

**Connected parity at an identified hub revision; degrade, never block,
when the hub is unreachable.**

- **Connected parity at an identified hub revision.** When a client can
  reach the hub, every moment behaves identically to every other client at
  the same hub revision: one authoritative SQLite writer (the hub, on
  vsan-1) owns retrieval models, policy, and the delivery ledger; a small
  broker on every machine owns client-local context extraction, session
  identity, deadlines, and a durable typed-operation queue. Literal
  byte-identical behaviour during a partition is impossible by
  construction — several inputs are client-local (below) — so the contract
  is parity *at a hub revision*, never an unconstrained "always
  identical".
- **Degrade, never block, when the hub is unreachable.** No lifecycle
  moment may hang or fail because the hub is down. Each moment's declared
  degraded behaviour is a cell of the matrix and is normative for T-08
  (per-machine broker), T-09 (durable typed operation queue), and T-11
  (declared offline mode):

| Moment class | Moments | Declared degraded behaviour |
|---|---|---|
| Inject | SessionStart, UserPromptSubmit (recall), PreToolUse, PostToolBatch, SubagentStart, PreCompact, PostCompact, hermes `pre_llm_call` | Inject from a valid, unexpired broker snapshot labelled `[OFFLINE SNAPSHOT r<rev>, <age> old]` (T-11); if no valid unexpired snapshot exists, inject nothing and record `reason: "hub-unreachable"`. |
| Reflect | Stop, SubagentStop, hermes `pre_verify` | Compute failure evidence locally, queue it to the durable op queue (T-09), and render the reflection nudge locally. |
| Capture | PostToolUse, PostToolUseFailure, UserPromptSubmit (capture-correction), hermes `post_tool_call`, every explicit write | Capture locally and enqueue to the durable op queue (T-09); acknowledge locally and replay on reconnect. |
| Session end | SessionEnd | Flush the durable op queue best-effort; never block session teardown. |
| Explicit recall | `store.py recall` / explicit memory tools | Answer from the valid, unexpired broker snapshot with the same `[OFFLINE SNAPSHOT r<rev>, <age> old]` label; if none, return no rows with `reason: "hub-unreachable"`. |

## Codex first-prompt double-fire policy

Codex CLI fires SessionStart and UserPromptSubmit back-to-back on the
first prompt of a session (openai/codex#15266). This is handled
**unconditionally by ledger/reservation dedupe, never by version gating**:
the delivery ledger's cross-moment dedupe (and, from T-05, reservations)
absorbs the double delivery on every supported Codex build. The matrix's
codex SessionStart and UserPromptSubmit rows carry this note. A future
change that gates the dedupe on a Codex version floor violates this
contract.

## Client-local vs hub-authoritative inputs

Several per-moment inputs exist only on the client machine and cannot be
computed on the hub: the project namespace (derived from the client
checkout's git remote via `host.resolve_namespace`, with a client-local
cache fallback), the Tier 0 files (`core.md` / `AGENTS.md`, where the host
decides whether AGENTS.md is included at all), the session transcript, the
agent transcript, and the working directory. The hub is authoritative for
memory rows, embeddings, the delivery ledger, and selection policy. Every
matrix row classifies each of its inputs as `client_local` or
`hub_authoritative`; `tests/test_t01_fleet_matrix.py` enforces the
classification and pins the nine canonical inputs the issue named, so T-08
cannot silently ship a moment whose client-local input is read on the hub.

## Supported harness floors

The matrix pins one `min_version` per harness row. Floors are
point-in-time facts that rot as hosts move; raising one is a deliberate
contract change made through a matrix PR that updates this provenance
block, never a silent edit.

| Harness | Floor | Provenance |
|---|---|---|
| Claude Code | 2.1.283 | vsan-1 install fact at issue #280 filing (2026-10-05); no in-repo numeric pin exists. |
| Codex CLI | 0.160.0 | vsan-1 install fact at issue #280 filing (2026-10-05); no in-repo numeric pin exists. |
| OpenCode | 1.18.30 | vsan-1 install fact at issue #280 filing (2026-10-05); no adapter ships yet (issue #66) — every OpenCode row is `capability_gap` until it lands. |
| Hermes | 0.21.1 | In-repo source pin `cdf4c76` (`scripts/host_canary.py` default; `tests/fixtures/canary/hermes/cdf4c76.json`; asserted by `tests/test_host_canary.py`). Present at the issue's pinned base 80b39602 and unchanged at d116a67. |
| ZCode | 3.15.3 | **Disclosed deviation:** AC4 asks for "the versions their adapters in this repo target", but the repo contains no numeric ZCode host pin anywhere (checked the plugin manifests, the canary, README, and docs/CLAIMS-AUDIT at both 80b39602 and d116a67). The floor recorded here is `ZCode.exe` ProductVersion 3.15.3.8237, probed on the dev box on 2026-10-15 — the host generation the `hooks.zcode.json` process-hook adapter actually runs on — and is a probe, not an adapter-targeted pin. |

## Trust model

The hub trusts authenticated per-machine principals (T-02) and clamps
caller-claimed trust, taint, signal, and confidence at the ingest boundary
(T-03); one authorization-aware selection policy serves local and remote
recall alike (T-04). Hook payloads, transcripts, and tool output are
untrusted data on every path; capture payloads are redacted before they
leave the machine (the standing hook redaction convention).

## Relationship to the single-machine docs

This contract supersedes the "Phase-1 limitation" paragraph in
`README.md` (one canonical physical store path on one machine) and the
"single-machine phase 1" assumption in `docs/CLOUD.md` **for fleet
deployments**. Those documents still govern single-machine installs until
the Workstream T runtime PRs land; nothing outside this repo's docs+
matrix+tests changed in this PR (T-01 is a contract artifact, not
runtime).

## How later PRs use this

The matrix is the single source of truth: T-08 (per-machine broker),
T-09 (durable typed operation queue), and T-11 (declared offline mode) add
or update the rows they implement rather than inventing their own moment
lists; T-04's parity fixtures live under `tests/fixtures/fleet/` so local
and remote envelope equality can be asserted against the same recorded
payloads and seed rows.
