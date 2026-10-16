# FLEET — The zmem fleet parity contract

Workstream T PR 1 of 12 (issue #280, epic #292). This document is the
written definition of "full parity on every machine and harness" that every
later Workstream T PR is judged against. The machine-checkable companion is
[`docs/fleet-capability-matrix.json`](fleet-capability-matrix.json) —
one row per (harness, moment, hook_event) — the single deliberate two-row
moment is codex capture-failure, whose second row records the
PostToolUseFailure event codex does not have — with per-input locality, hub
operations, and the declared degraded behaviour — produced by
`docs/generate-fleet-matrix.py` (run it with `--check` to verify; the
wiring is derived from `hooks/hooks.*.json` and the Hermes
`register_hook` surface on every run). `tests/test_t01_fleet_matrix.py`
fails whenever a `hooks/hooks.*.json` manifest registers an event the
matrix does not account for, and enforces the grid, the closed input
vocabulary, the floors, and the launcher bindings by identity rather than
by presence, so coverage gaps cannot reopen silently.

## Goal

Give every agent harness on every machine of the fleet (one private
Tailscale tailnet) the same zmem behaviour: the same passive injection at
every lifecycle moment, the same explicit tools, and the same write and
capture semantics. Parity is asserted among machines **at or above their
harness's pinned floor** (see Supported harness floors): a machine running
below its harness's floor is out of parity scope, not a parity defect.

## Consistency model

**Connected parity at an identified hub revision; degrade, never block,
when the hub is unreachable.**

- **Connected parity at an identified hub revision.** When a client can
  reach the hub, every moment behaves identically to every other client at
  the same hub revision: one authoritative SQLite writer (the hub — a
  single host on the tailnet) owns retrieval models, policy, and the
  delivery ledger; a small
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

Two notes on this table. **PostCompact is registered but inert today:**
`hooks/zmem-postcompact.sh` is a deliberate no-op per issue #158 (it prints
`{}` and exits, and the verb is absent from `TRANSLATED_HOOKS`), so the
Inject row for PostCompact describes the moment T-08 will implement, not
behaviour any client performs now — the matrix row carries the same note.
And **"valid, unexpired" is defined by T-11, not here:** issue #290 AC2/AC5
already settles the parameters (72 h expiry with `reason:
"snapshot-expired"`, 64 MB cap, owner-only permissions, hourly refresh,
`user:global` plus authorized project namespaces touched in the last 30
days, `mode=ro`/`query_only=1`, atomic replace), and T-12's `doctor
--fleet` reports snapshot age and full-disk-encryption-off (issue #291
AC2). The snapshot is **principal-scoped**: it is filtered by the T-04
selection/authorization policy before it leaves the hub, and a reconnect
supersedes it rather than merging with it.

The `[OFFLINE SNAPSHOT r<rev>, <age> old]` label is a **broker-emitted
fence header line, never row content**: the rendered fence must not let a
captured row forge it (the marker-grammar discipline of
`storelib/inject.py`), and `<age>` is computed on the client from a
possibly skewed clock, so it is advisory.

The `reason: "hub-unreachable"` token (and #290 AC2's `snapshot-expired`)
are **new contract tokens, not members of today's closed vocabulary**:
`INJECT_SILENT_REASONS` (`skills/memory/scripts/schema_meta.py`) currently holds
exactly `empty-pool`, `omitted`, `below-bar`, `budget-drop`,
`below-relevance`, `already-delivered`, `expired`, and the classifier
rewrites an unrecognised reason to `empty-pool`. T-11 must therefore add
both tokens to that tuple (and to the consumers that tally it —
`storelib/eval_gold.py`'s silent-reason counters and the launcher's
`reason=` status line) in the same change that first emits them;
until then a degraded-mode reason recorded today would be silently
rewritten, which is why the obligation is stated here rather than
assumed.

## Codex first-prompt double-fire policy

Codex CLI fires SessionStart and UserPromptSubmit back-to-back on the
first prompt of a session (openai/codex#15266). This is handled
**unconditionally by ledger/reservation dedupe, never by version gating**:
the delivery ledger's cross-moment dedupe (and, from T-05, reservations)
absorbs the double delivery on every supported Codex build. The matrix's
codex `session-start` and codex `recall` rows carry this note — the two
delivery rows; codex `capture-correction` also rides UserPromptSubmit but
is a capture moment, so the delivery-ledger dedupe does not apply to it. A
future change that gates the dedupe on a Codex version floor violates this
contract. Until T-05's reservations land, the dedupe rests on the delivery
ledger alone; the pre-existing cross-process gap in that ledger
(`storelib/delivery_ledger.py` `record()` takes no lock) is tracked as a
follow-up and is T-05's to close.

## Client-local vs hub-authoritative inputs

Several per-moment inputs exist only on the client machine and cannot be
computed on the hub: the project namespace, the Tier 0 files (`core.md` /
`AGENTS.md`, where the host decides whether AGENTS.md is included at all),
the session transcript, the agent transcript, and the working directory.
The hub is authoritative for memory rows, embeddings, the delivery ledger,
and selection policy. Every matrix row classifies each of its inputs as
`client_local` or `hub_authoritative` from a closed vocabulary;
`tests/test_t01_fleet_matrix.py` enforces the classification, requires
every issue-named input to be present, and rejects names outside the
vocabulary — so T-08 cannot silently ship a moment whose client-local
input is read on the hub, and cannot drop an input by renaming it.

The namespace deserves its own sentence because it is the identity-bearing
input. `host.resolve_namespace` has four outcomes, all client-derived: the
git remote (`project:<host/org/repo>`), the checkout's absolute path when
no origin exists (`project:<abspath>`), a client-local cache entry, and —
when git errors and no cache exists — the literal `user:global`, the one
namespace every session on the box reads. The matrix's `git remote` input
is the derivation input for the remote case; the other three outcomes are
derivation results, not additional inputs.

One terminology note, because the word is already loaded in this repo: the
existing `fleet:<value>` namespace scope label (from `ZMEM_FLEET`, via
`host.resolve_scopes`) names ONE machine's scope — `fleet:dgx-spark` is a
single box, not the collection — whereas this contract's "the fleet" means
every machine on the tailnet. T-02's per-machine principals must not assume
the existing label already scopes to the fleet.

A namespace is therefore a
client-asserted row key, never an authenticated principal: binding
authorization to the authenticated machine principal instead is T-02's
"server-derived attribution" (issue #281), and until it lands the hub must
treat the caller-claimed namespace as untrusted.

## Supported harness floors

The matrix pins one `min_version` per harness row. Floors are
point-in-time facts that rot as hosts move; raising one is a deliberate
contract change made through a matrix PR that updates this provenance
block **and** the `PINNED_MIN_VERSIONS` dict in
`tests/test_t01_fleet_matrix.py` (the guardrail asserts every row against
it, so a matrix-only edit fails CI), never a silent edit.

| Harness | Floor | Provenance |
|---|---|---|
| Claude Code | 2.1.283 | Fleet-hub install fact at issue #280 filing (2026-10-05); no in-repo numeric pin exists. |
| Codex CLI | 0.160.0 | Fleet-hub install fact at issue #280 filing (2026-10-05); no in-repo numeric pin exists. |
| OpenCode | 1.18.30 | Fleet-hub install fact at issue #280 filing (2026-10-05); no adapter ships yet (issue #66) — every OpenCode row is `capability_gap` until it lands. |
| Hermes | 0.21.1 | In-repo source pin `cdf4c76` (`scripts/host_canary.py` default; `tests/fixtures/canary/hermes/cdf4c76.json`; asserted by `tests/test_host_canary.py`). Present at the issue's pinned base 80b39602 and unchanged at d116a67. |
| ZCode | 3.15.3 | **Disclosed deviation:** AC4 asks for "the versions their adapters in this repo target", but the repo contains no numeric ZCode host pin anywhere (checked the plugin manifests, the canary, README, and docs/CLAIMS-AUDIT at both 80b39602 and d116a67). The floor recorded here is `ZCode.exe` ProductVersion 3.15.3.8237, probed on the dev box on 2026-10-15 — the host generation the `hooks.zcode.json` process-hook adapter actually runs on — and is a probe, not an adapter-targeted pin. |

## Trust model

The hub trusts authenticated per-machine principals (T-02) and clamps
caller-claimed trust, taint, signal, and confidence at the ingest boundary
(T-03); one authorization-aware selection policy serves local and remote
recall alike (T-04). None of those controls ships yet — this contract
states the target, and T-02/T-03/T-04 own the implementation.

Hook payloads, transcripts, and tool output are untrusted data on every
path. Capture payloads are redacted before they leave the machine:
`skills/memory/scripts/redaction.py` is the standing convention, and every
surface that actually crosses a network boundary forces capture mode
`auto` (`hermes-plugin/server/mcp_server.py` sets it for network writes;
`storelib/write.py` states the rule), so only count-only messages leave.

Credential lifecycle is T-02's to specify, but the substrate already exists
and the reader should be pointed at it: the only network transport today
resolves a single shared operator token (`hermes-plugin/transport.py`,
`hermes-plugin/server/auth.py` — a scoped token file with a `namespaces`
allow-list and a fail-closed `namespace_not_allowed` denial, plus the
doctor's unscoped-token advisory). Per-machine principals replace that
shared secret; rotation and revocation are part of T-02, not of this
contract.

Two limits are stated here because T-08/T-09 will inherit them: redaction
covers secret-shaped values, not PII or absolute paths (those strippers
live on the training-capture path), and remote content is
**untrusted until reviewed — only promote or ingest reviewed content into
the shared store** (the README/CUTOVER rule; T-03 owns the ingest-side
enforcement, issue #288 AC4/AC6).

## Relationship to the single-machine docs

This contract supersedes, **for fleet deployments**: the "Phase-1
limitation" paragraph in `README.md` (one canonical physical store path on
one machine), the repeat of that rule in the README Operations notes, the
"single-machine phase 1" assumption in `docs/CLOUD.md`, the equivalent
single-store statements in `CUTOVER.md` and
`skills/memory/SKILL.md`, and the "All memory stays on your machine. No
telemetry, no cloud calls." sentence in the README Security notes — which
is the paragraph a fleet operator is most likely to act on. Those
documents still govern single-machine installs until the Workstream T
runtime PRs land; nothing outside this repo's docs+matrix+tests changed in
this PR (T-01 is a contract artifact, not runtime). The supersession is
recorded here, in the PR, and as forward pointers added to README and
docs/CLOUD.md; the superseded rules themselves stay in force until the
Workstream T runtime PRs land.

## How later PRs use this

The matrix is the single source of truth: T-08 (per-machine broker),
T-09 (durable typed operation queue), and T-11 (declared offline mode) add
or update the rows they implement rather than inventing their own moment
lists — regenerate with `docs/generate-fleet-matrix.py` (or edit rows and
run it with `--check`), never by hand-editing the JSON alone. T-04's
parity fixtures live under `tests/fixtures/fleet/` so local and remote
envelope equality can be asserted against the same recorded payloads and
seed rows; their contract, provenance policy, and replay preconditions are
in that directory's README. The write half of the contract has no
authorization input yet (the `policy` input appears on selection rows
only); T-03 (issue #282) and T-09 (issue #288) add it to the rows they
implement, and the durable-queue replay's idempotency key is T-09's
`applied_ops` table (issue #288) — until then, treat duplicate-delivery
trust inflation as the known gap the follow-up issue tracks.
