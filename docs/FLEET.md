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
| Session end | SessionEnd | No broker moment (session-end routing stays local by design); queued ops flush at the next read moment. Never blocks session teardown. |
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
contract. The dedupe rests on the delivery ledger. T-05 closed the
cross-process double-fire window on the reserve→ack path (the admit-once
`record_undelivered` API serializes the check+publish under the
delivery-state lock) and the reservations below carry the remote-caller
case. Issue #362's general case is closed as of 0.118.0 on the RECORD
side: the non-reserve local path — every hook and the default MCP prefetch
— records through the same admit-once `record_undelivered` API
(`refresh_delivered=True`, so escalation and bridged-subagent re-deliveries
keep refreshing their entries), and two concurrent fires can no longer both
admit — except the disclosed lock-pathology corner (both callers exhausting
the retry behind a crashed holder fall back lock-free, the module
docstring's fail-open posture). The remaining emission-side window (render
before record) stays the documented residual, bounded by next-turn
suppression.

## Reservations and delivery commitment (T-05, issue #284)

Selection and delivery commitment are separate steps for remote callers:
`prefetch --reserve` (MCP `prefetch(reserve=true)`) returns the selection
plus a reservation id WITHOUT recording delivery (an identical retry
inside the reservation TTL returns the same ids — a lost response is no
longer indistinguishable from a delivered one), and
`prefetch-ack --reservation <id>` (MCP `prefetch_ack`) commits exactly
the reserved ids once (a duplicate ack is a no-op within the
reservation TTL; an expired reservation commits nothing). The delivery
ledger is keyed by `principal|harness|session` when the caller carries
identity (the MCP server forwards it for map-mode principals), so two
machines or harnesses sharing a native session id never share a ledger;
local hooks keep the one-step session-keyed behavior byte-identically.
The cross-process record race (#362) is closed on every 0.118.0 path by
the admit-once `record_undelivered` ledger API (the delivery-state lock
serializes the check+publish; the non-reserve local path routes through it
too, with `refresh_delivered=True` preserving the escalation/bridged upsert
law). Two disclosed boundaries: during a mixed-version window — a live
pre-0.118.0 process beside a 0.118.0 one on the same data dir — the old
process still records lock-free, so admit-once holds only between new
processes until it exits; and in the lock-pathology corner (both callers
exhausting the retry behind a crashed holder) both fall back lock-free, per
`record_undelivered`'s docstring. The emission-side window remains the
documented residual bounded by next-turn suppression.

**Disclosed residual — the ack-vs-ingestion gap:** the broker can
acknowledge only what its adapter EMITTED, never what the model actually
INGESTED. An ack asserts successful emission into the harness's context
channel, not comprehension or use. This gap is inherent to the
architecture (the harness offers no delivery receipt for context it
rendered) and is why the reservation TTL is short (default 120 s,
`ZMEM_RESERVATION_TTL_S`): an unacknowledged selection expires and may be
re-selected, at-least-once emission, rather than pretending ingestion.

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
recall alike (T-04). T-02 (per-machine principals) and T-03 (the
ingest-side trust boundary: `--trust untrusted-peer`, the namespace
allow-list, caller-derived attribution) shipped in 0.103.0 and 0.104.0;
T-04's namespace-allow-list selection shipped in 0.105.0; capability
enforcement and the broker lanes remain (#288, T-05+).

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
only); T-03 (issue #282) shipped that row input in 0.104.0 — the ingest
mode gates every record kind against `--allow-namespace` — while T-09
(issue #288) still owns the queue-side rows, and the durable-queue replay's idempotency key is T-09's
`applied_ops` table (issue #288) — until then, treat duplicate-delivery
trust inflation as the known gap the follow-up issue tracks.


## T-08 delivery: remote passive memory through the per-machine broker (issue #287)

Remote mode is selected by `ZMEM_REMOTE_URL` (plus the same token
resolution the hub already uses: `ZMEM_MCP_TOKEN` / `ZMEM_MCP_TOKEN_FILE` /
per-machine principal map `ZMEM_MCP_TOKENS_FILE`). With the variable set,
the launcher routes the broker-covered moments through
`skills/memory/scripts/fleet/broker_cli.py` — a library entry, never a
daemon — which resolves identity CLIENT-SIDE (git-derived namespace, session
id, Tier-0), calls the hub under the propagated deadline (`deadline_ms`,
T-07) with a T-05 reservation, emits the hub envelope's `rendered` member
(fence-neutralized exactly like the local bash wrappers), and acknowledges
delivery. With the variable unset, every hook takes the unchanged local
path.

Routing (launcher verb → moment): `recall` → UserPromptSubmit,
`pretool-recall` → PreToolUse, `posttoolbatch-recall` → PostToolBatch,
`subagent-recall` → SubagentStart, `precompact` → PreCompact,
`session-start` → SessionStart, `reflect` → Stop, `subagent-reflect` →
SubagentStop. Stop/SubagentStop send bounded, redacted failure EVIDENCE
(the subagent's own transcript for SubagentStop — the parent transcript is
never opened) to the hub's `stop_evidence` tool as data: count plus at most
5 details, each whitespace-collapsed and capped at 200 chars, with secrets
AND email AND filesystem paths scrubbed (`redact_training_text`). The hub
rejects any path-shaped string in the payload before any store write — the
hub never opens a client-supplied path.

Disclosed divergences from the local path (all deliberate):

- **Capture moments stay local.** `capture-correction`,
  `capture-failure`, and `convention-capture` keep their local behavior in
  remote mode (their queue writes land in the local correction queue);
  routing those writes through the durable typed operation queue is T-09
  (issue #288).
- **`session-end` stays local.** The local session-end never recalls
  (delivery-clear + empty envelope) and the Codex fast path keeps
  precedence; hub-side ledger hygiene is owned by T-05 acks plus lazy
  reaping, which already shipped.
- **Query rewrite does not travel.** The local UserPromptSubmit path's
  best-effort `query-rewrite` (fail-open, 1 s budget) is not replicated —
  the remote leg passes the prompt verbatim and the hub selector owns
  relevance. The T-04 parity fixture asserts envelope equality holds for
  the recorded prompts.
- **Hub knobs do not travel.** Per-call limits, the recall-vs-recent
  choice, `--delegation-prompt`, and per-call cross-project flags have no
  hub-tool arguments; in remote mode the hub selector's own limits and
  policy govern (the #159 selector contract: the store-side selector owns
  the relevance gate, the 1,500-token budget, and the delivery ledger).
- **Tier 0 is hub-owned.** In remote mode the broker fetches the hub's
  canonical user-level `core.md` (`tier0_get`) at SessionStart, caches it
  at `<data dir>/broker/core.md` with its revision, and injects the cached
  copy while the hub is unreachable; a local `~/.zmem/core.md` is ignored
  in remote mode. The hub serves `tier0_get` under the standing read scope
  rule (a scoped token may read user-level content only when `user:global`
  is in its allow-list), the broker never clobbers a good cache with an
  empty body, and a hub `no-core-md` answer invalidates a stale cache.
- **Kill switches travel with the moment.** The launcher re-applies the
  local consent gates BEFORE routing remote: `ZMEM_INJECT=0` keeps the
  recall verbs on the local path (which no-ops), and
  `ZMEM_REFLECT=0`/`ZMEM_CAPTURE=0` keep the reflection verbs local —
  remote mode adds reach, never overrides an operator's capture or
  injection opt-out.
- **Stop evidence fires only on failures.** A Stop/SubagentStop with zero
  extracted failures sends nothing (the local gate never emits a
  "0 failed tool call(s)" line); the hub summary carries the TRUE failure
  count with at most five redacted details, redaction applied BEFORE the
  cap. SubagentStop never injects `additionalContext` (the #204 re-loop
  hazard) — its evidence is delivered to the hub silently.

Failure observability: an unreachable hub or deadline hit fails open within
the launcher budget — every hub leg of a moment shares that moment's
REMAINING budget, so a hung hub trips the broker's own fail-open path
before the launcher watchdog — and reports the DISTINCT reason
`hub-unreachable` in one JSON line appended to
`<data_dir>/broker-log.jsonl` and in `zmem doctor`'s remote surface check
(which reports the record's age), never the honest empty-store
`empty-pool` (issue #287 claim 5). A structured hub refusal (path-shaped
field, namespace guard, store-write failure) is logged as `hub-refused`;
a LOCAL misconfiguration (missing `mcp` package, malformed token file)
keeps the historical `empty-pool` — only a connect-level failure claims
an outage.

Host-gate evidence obligation (AC7, HOST_ONLY): per-harness model-visible
injection at SessionStart, UserPromptSubmit and PreToolUse for claude,
codex and zcode against the vsan-1 hub is captured as transcript evidence
per harness by the operator; the frozen checks C1-C6 prove the wired
equivalence, the matrix rows stay `capability_gap → registered` as each
harness's evidence lands.

## T-09 delivery: durable typed operation queue (issue #288)

Remote-mode writes travel as TYPED OPERATIONS, not exported rows. The
correction-capture hook enqueues `{op_id, op: add|update|supersede|
invalidate|capture, args, depends_on?, client_seq}` entries into an
append-only JSONL queue under `<data dir>/queue/ops-<principal>.jsonl` —
redacted BEFORE disk (EVERY string arg through `redact_training_text`),
owner-only, ASCII-escaped on write and read on `\n` boundaries only (a
U+2028 in content cannot tear the journal), with a monotonic
per-principal `client_seq` and a projected 10 MB / 7-day bound (overflow
REFUSES loudly). The broker's read moments flush pending ops to the hub
in 256-op chunks (the hub batch limit) under the moment's REMAINING
deadline. The hub's `apply_ops` (MCP tool and
`store.py apply-ops --in <jsonl-or-->`) applies each batch under ONE
transaction, idempotently by `(op_id, principal)` (the schema-19
`applied_ops` primary key is the at-most-once record — ids are scoped
per principal, so one caller cannot pre-claim or disclose another's
canonical id), returning per-op receipts
`{op_id, op, status: committed|rejected|duplicate|blocked, canonical_id,
deduped}` — a dedup FOLD is `committed` + `deduped: true`; `duplicate` is
only a replay. A dependent op names `id_or_dep` = an op_id, resolved
through the canonical-id map so a supersede follows the id the hub
actually assigned (within the same batch; a dependency committed by an
EARLIER batch is `blocked`, and the queue entry is retired with that
receipt rather than retried).

Capabilities (#281's deferred enforcement): `update`/`supersede`/
`invalidate` require `mutate` (or `admin`) for non-legacy principals —
revoked or unknown principals deny everything, an explicitly empty
capability set denies, legacy (`capabilities` absent) keeps today's
behavior; `add` is never capability-gated. The effective principal is
the authenticated request context: the MCP tool's `principal` parameter
must name THAT principal (a mismatch is refused — no identity switch) and
is only honored unbound for no-auth-context in-process callers; the CLI
`--principal` is the local operator's assertion on their own machine.
The per-op `principal` field is advisory metadata, never an identity
switch. Namespace scope is enforced engine-side for EVERY op: a scoped
principal's declared namespace AND each mutation target row's own
namespace must be in the allow-list — an op that omits the namespace
defaults to `user:global` and is refused for a scoped principal unless
that is allow-listed (the sibling tools' #109 rule).

Read-your-writes: pending ops overlay the broker's read-moment envelopes
as bounded, fence-neutralized `[PENDING]` lines — in BOTH hub states,
including while the hub is unreachable (the offline-recall scenario is
exactly when an operator needs to see their un-landed write) — and a
fire-and-forget flush sends the batch in chunks under the remaining
deadline. The local correction-queue feed reroutes to this queue in
remote mode (capture intent = `auto`), carrying the candidate's
namespace, type and confidence, and FALLS BACK to the local correction
queue when the enqueue itself fails (never a silent drop).

Capture mode is chosen by INTENT, identically on both routes (issue AC6):
explicit agent/user tool writes (`add`/`update` — CLI defaults included)
run `auto`; the operator's `ZMEM_CAPTURE_MODE` override still wins
(normalized); operator restore (`ingest-jsonl --allow-tombstones` without
`--trust`) stays `manual`. Schema 19 is purely additive. The compat gate
refuses any store whose version exceeds the client's FORWARD ceiling: a
0.116-era client (FORWARD 18) REFUSES a v19 store with the standard
`ZMEM_ALLOW_NEWER_SCHEMA=1` message — update all clients on shared-store
machines BEFORE the first v19 client opens the store (a FORWARD-15 client
refuses it the same way).

## Peer ingest trust mode (issue #282)

Every machine importing rows it did not author runs
`ingest-jsonl --trust untrusted-peer --allow-namespace <ns...>
--capture-mode auto` (add `--attribute-host <this box>` to stamp local
attribution). Trust claims inside the rows are clamped, namespaces are
gated, and evidence records from peers are refused; see docs/CLOUD.md.
