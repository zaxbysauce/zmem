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
are **contract tokens of the closed vocabulary since T-11 (0.122.0)**:
`INJECT_SILENT_REASONS` (`skills/memory/scripts/schema_meta.py`) now holds
exactly `empty-pool`, `omitted`, `below-bar`, `budget-drop`,
`below-relevance`, `already-delivered`, `expired`, `hub-unreachable`,
`snapshot-expired`, with byte-identical fallback literals in
`storelib/inject.py`, `storelib/eval_gold.py`, and the MCP server twin —
the exact-equality pin in `tests/test_inject_silent_reason.py` (both the
schema_meta arm and the partial-deployment fallback arm) is the guardrail.
The classifier still never emits either remote-mode token (both are
produced by the broker/transport layer only). The launcher's outer-timeout
`reason=omitted` decision-log lines are a single pre-existing vocabulary
token by design and stay as-is (they name a watchdog event, never a
silent-reason tally).

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
| DeepSeek (dsh) | 0.2.1-rc.2 | Native Cordis adapter (`deepseek-plugin/`, issues #374/#375). The peerDependencies in `deepseek-plugin/package.json` pin the exact published `@deepseek-ai/*` set the adapter was probed against; **preview-scoped** — first-class support claims require the passing native canary at `docs/host-gate-evidence/deepseek/` (currently `win32`/`headless` only). |

## DeepSeek adapter (T-13/T-14, preview)

`deepseek-plugin/` is the native DeepSeek Harness adapter: passive recall
enters the **first accepted `agent/pre-step`** through the T-05
`prefetch --reserve` CLI seam with the reservation acknowledged only after
observed session admission; the explicit `zmem_*` tools ride the same
backend (typed `ops-enqueue`/`ops-flush --local` receipts — pending in
remote mode, committed locally); capture delegates to the shared python
surface (`hooks/lib/zmem-capture-entry.py`) with always-on redaction and
content+session dedup, and reflection runs the shared reflect hook
(`hooks/zmem-reflect.sh`) inside a once-per-session bound whose nudge is
surfaced as state for the host (never a self-triggered continuation);
 `mine-history --source deepseek` parses the versioned session
export with refusal contracts; `doctor --fleet` reports the
`deepseek-plugin` lane. The harness's own plugin loader is not part of
the published packages: installation is validated at the profile-row
(`cordis.yml`) level, and activation honestly requires a host restart.

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

Host-gate evidence (AC7, HOST_ONLY): CAPTURED 2026-10-24 — the operator
ran the T-01 harnesses against the vsan hub (http://192.168.1.148:8765/mcp,
zmem 0.120.0, schema 19). Per-harness model-visible injection at
SessionStart, UserPromptSubmit and PreToolUse for claude, codex and zcode
is recorded in `docs/host-gate-evidence/287-ac7/`: launcher-contract
captures for all three harnesses at all three moments (every capture shows
the untrusted-fence block; the UserPromptSubmit captures additionally show
the hub-seeded canary, and the SessionStart captures carry the hub-owned
Tier-0 core.md), plus a verbatim `codex exec` transcript in which the
model itself confirms the injected memories block. The frozen checks
C1-C6 prove the wired equivalence; the matrix rows for these moments are
`registered`.

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
that is allow-listed (the sibling tools' #109 rule). A scoped
single-token caller (no principal map) reaches the same engine-side
enforcement through apply-ops' `--namespace-scope` flag (issue #420): a
newline-separated allow-list the hub forwards from the token config;
it can only narrow the legacy identity, refuses an explicit empty
value, and is mutually exclusive with `--principal`.

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

## T-10 delivery: the CLI and explicit tools route through the backend (issue #289)

When `ZMEM_REMOTE_URL` is set, `store.py` gains a remote-mode gate (one
choke point after argparse, before any store access) that sorts every
subcommand into four bands — no split-brain local store on a remote box:

- **ROUTE-WRITE** (`add`, `update`, `supersede`, `invalidate`): enqueue a
  T-09 typed op (`fleet/opqueue`, principal `local`) and print one JSON
  receipt line `{ok, op_id, op, status: pending, queue, result: queued}`;
  never open a local store. The routed write carries the flags the T-09
  applier honors (`--namespace/--type/--content/--tags/--signal/
  --source-ref/--capture-mode` for add; `--id/--content` + the same
  metadata set for update; `--id/--reason` for supersede/invalidate) and,
  since 0.129.0 (issue #326), `--expiry-class/--inject-until` on add and
  update — a state add requires `--expiry-class`, and an update naming a
  non-state type while carrying expiry flags is refused. Everything else
  — `--confidence`, `--taint`, `--evidence`, `--no-dedup`,
  occurrence flags, `--expected-namespace`, `--type event` (occurrence
  semantics do not travel), and a state add without `--expiry-class` — is
  REFUSED with exit 2 naming the flag rather than silently dropped.
  `--content -` reads stdin before enqueue; capture-mode resolves the
  `ZMEM_CAPTURE_MODE` intent exactly like the local dispatch.
- **ROUTE-READ** (`recall`, `search`, `recent`, `list`, `get`): call the
  hub's MCP tools (`recall`/`search`/`recent` exist; the new
  `list_memories`/`get_memory` tools serve list/get, backed by the new
  additive `store.py list --json`). Reads print the JSON envelope — with
  or without `--json` — plus a `[PENDING]` overlay rendered BEFORE the
  best-effort queue flush, so the overlay names the ops that were pending
  when the command started (a flush inside the call may commit them
  first). Per-command envelope parity: recall/search carry `arms`; recent
  does not (its local envelope has none). `--text` is search's query
  flag. Any flag beyond each command's allow list (query/namespace/limit
  for recall/search/recent, plus --include-superseded for list and --id
  for get) is refused with exit 2.
  Failures print `{"ok": false, "reason": <r>}` and exit 1:
  `hub-unreachable` for connect-level failures ONLY,
  `hub-response-invalid` for a reachable-but-degraded hub response, and
  (the `get` route only) `hub-refused` for hub-ANSWERED refusals
  (not found / namespace-guard denial) — the transport's reason is
  preserved through every branch.
- **ALLOW-LOCAL** (verified store-independent or read-only-no-create):
  `failures`, `signals`, `corrections`, `queue-list`, `queue-clear`,
  `queue-add`, `source-exists`, `source-ref-exists`, `ops-append`,
  `retain`, `retain-adapt`, `ledger-clear`, `delivery-clear`,
  `hermes-reflect`, `hermes-context --action commit-cursor`. These keep
  every remote-mode hook consumer alive (the capture-failure dedup probe
  and nudge, the convention ring, session-end retention, the delivery
  ledger, the compat bridges). Standing divergence: `source-exists`/
  `source-ref-exists` answer from the LOCAL table when one exists
  (base-identical; the hub-side equivalent is the apply-time dedup).
- **REFUSE** (everything else): exit 2 with `this command runs on the
  hub; ZMEM_REMOTE_URL is set` before touching anything — the admin six
  (`consolidate`, `organize`, `reembed`, `purge`, `export-dataset`,
  `init`) plus every other memory-table surface (`evidence`, `backup`,
  `operation-feedback` — its caller ignores the rc, a disclosed
  telemetry gap — `apply-ops`, `prefetch`, `hermes-convention`,
  `hermes-context --action ack-failure`, exports, entity/episode tools,
  …). The broker-fallback family (launcher cannot route to the broker →
  local hook bodies run under remote mode) fails OPEN on the refusal:
  empty envelope, no store creation — strictly better than base, which
  created a local store on those paths.

Configuration error: `ZMEM_STORE` set together with `ZMEM_REMOTE_URL`
exits 2 naming `ZMEM_STORE`, `ZMEM_DATA` and `ZMEM_REMOTE_URL`. `ZMEM_DATA`
ALONE is legal in remote mode — it names the local data dir (op queue,
sidecars, broker cache); the launcher itself exports it to every hook
child, so it cannot be part of the hard error. (The issue text reads the
constraint over both variables; this narrowing is the reconciliation that
keeps hooks alive and is disclosed here and in the PR.)

Hermes provider: in MCP mode (`ZMEM_MCP_URL`/`ZMEM_REMOTE_URL`) every
explicit tool routes through the transport — `zmem_add`/`zmem_supersede`/
`zmem_update`/`zmem_invalidate` to the hub's write tools, `zmem_search`
to the hub `search` tool (shared row projection with the local leg), the
session-start pull to the hub `session_start` tool (the caller's `limit`
is forwarded; the hub's include-global policy gate is the remote semantic
for the local `--include-global --global-limit 2`), and the session-end
note to the hub `add` tool. A transport failure returns a structured
agent-visible error, never a silent success. Local-maintenance legs skip
in MCP mode with a log line (session-end `organize`/`backup`; the native
and delegation evidence writes — evidence rows live in the hub's store).
The compat hooks (`zmem-hermes-reflect.py`, `zmem-hermes-convention.py`)
carry per-call remote guards: only the memory-table calls skip
(ack-failure, hermes-convention, the evidence writes, the MCP-url-less
prefetch); the sidecar calls pass through.

Hub constraint (self-enforced): the hub's `_run_store` strips
`ZMEM_REMOTE_URL` from its own `store.py` children — the hub IS the store
owner, so its tool subprocesses always run local even when the hub process
itself carries the variable (self-referential or stale operator env would
otherwise refuse every hub-side tool call; the pre-fix wedge was
reproduced and the strip pinned by
`test_hub_run_store_strips_remote_url_from_children`).

Envelope passthrough: `_parse_results` now passes the retrieval-telemetry
`arms` member (and `excluded`) through and int-normalizes the token
counts, so a remote read prints the same key set as its local twin.

## T-11 delivery: declared offline mode — the revisioned, expiring, revocable broker snapshot (issue #290)

`skills/memory/scripts/fleet/snapshot.py` owns the offline artifacts under
`<data>/broker/`: `snapshot.sqlite` + `snapshot.meta.json` (one rollover
pair `snapshot.prev.sqlite` kept; a meta-write failure after a successful
sqlite replace restores the previous pair). The PUBLIC build seam is
`build_snapshot` — the only `…snapshot…`-named public callable in fleet
(the T-10 probe shape); it reads a source store read-only, applies the AC5
dual scope law IN THE BUILDER (`user:global` unconditionally plus
authorized PROJECT namespaces with read-or-wrote activity in the last 30
days — authorization AND recency), writes the scoped pair atomically with
owner-only permissions, and refuses a payload over 64 MB (the old pair
survives). `_load` opens the snapshot `mode=ro` + `PRAGMA query_only=1`
with `row_factory=sqlite3.Row` (raw tuples TypeError in the selector's
row consumers); `_state(meta)` settles none | expired | valid against the
72 h default window.

Three lanes serve from it, and every one runs the SAME selector
(`select_and_budget_for_injection`) in the NEW zero-write mode
(`reserve=False` + `no_telemetry=True` — the surfaced-count bump AND both
delivery-ledger writes are skipped; offline serves are advisory, so a row
served offline may be re-delivered live after reconnect by design):

- **Broker/Inject lane** (`fleet/broker.py::_handle_read`): on the
  unreachable branch, a valid snapshot is served as the standard #158
  envelope with the `[OFFLINE SNAPSHOT r<rev>, <age> old]` label as a
  broker-emitted fence header (never row content) plus the pending T-09
  overlay; an EXPIRED snapshot serves nothing and logs the distinct
  `reason: "snapshot-expired"`; state none keeps the exact T-08 fail-open.
  Every unreachable moment writes the persisted
  `<data>/broker/hub-unreachable.stamp` — the only cross-moment memory in
  a per-moment-subprocess fleet. On a SUCCESSFUL hub read the broker
  best-effort refreshes under `min(2000 ms, remaining // 4)` AFTER the
  live result is chosen: refresh fires on none / stale (hourly cadence,
  `ZMEM_SNAPSHOT_REFRESH_S` override) / the persisted
  unreachable→reachable transition, and the stamp clears ONLY when a
  refresh completed (a skipped or failed refresh re-arms it for the next
  moment).
- **Hermes provider lane** (`hermes-plugin/__init__.py::pre_llm_call`): an
  McpHttp-prefetch `hub-unreachable` failure serves through
  `fleet.snapshot._serve_offline_read` (the module is loaded beside the
  checkout's scripts dir under a unique name with a scoped, restored
  `sys.path` entry — the provider never imports storelib in-process
  otherwise, and no zero-write CLI surface exists for a subprocess
  serve). The served envelope is the STANDARD #158 key set — the label
  rides inside `rendered`, no `offline_snapshot` key, and it passes the
  strict `_envelope_is_valid` unknown-key rejection; it is NOT a failure
  envelope, so the transport-failure cache never suppresses subsequent
  recall. This lane writes no stamp and runs no refresh (a hermes-only
  deployment has no refresh writer — its matrix "if none …
  hub-unreachable" degrade is the documented behavior; a broker-bearing
  machine refreshes); state expired degrades to `{}`.
- **CLI lane** (`storelib/remote_gate.py::_offline_read`): on
  hub-unreachable, a VALID snapshot answers `recall`/`search`/`recent`
  with the live envelope key set PLUS exactly one additive
  `offline_snapshot` provenance key (the label string) and a stderr note
  — stdout stays JSON-parseable; `list` prints its 3-key object plus the
  provenance key; `get` prints the row JSON plus the provenance key. An
  expired or missing snapshot keeps the CURRENT error contract — the CLI
  reports `hub-unreachable` (a deliberate asymmetry, not a bug: AC2's
  `snapshot-expired` reason belongs to the passive Inject path; this
  row's "if none" is the explicit-read contract).

The hub side is one additive prefetch-shaped MCP tool, `snapshot`
(`hermes-plugin/server/mcp_server.py`): protocol + deadline gates, a
principal-bound scope (the caller's token allow-list — unscoped operator:
all namespaces — intersected with the same 30-day activity law), one
consistent SQLite read emitting live rows (selector-relevant columns,
embedding blobs base64 for the hub's ACTIVE profile; a broker-side
profile mismatch degrades the snapshot to lexical retrieval, never fails
the serve — the payload ships embedding blobs in `memory.embedding` only,
with no vector chunks, so the vec lane finds nothing and every serve is
lexical), the revocation list (currently-superseded ids in scope —
superseded-only by the scope law: purged ids are unnamespaced post-purge,
so emitting or copying them would leak other principals' row identifiers;
a purged row's absence from the live rows IS its revocation — replacement
semantics make AC3 hold: a row purged after r1 is absent from r2), an
advisory `snapshot_revision` (live + revoked count; the broker's
receive-time `issued_at` disambiguates), and a > 64 MB structured
refusal. The transport wrapper is `McpHttp.snapshot` (best-effort like
`hello`). A hub-ANSWERED refusal (any payload without an explicit
`ok: true`) never destroys the previous pair and never clears the stamp —
the refresh retries on a later moment. Deliberate deviation from the
issue's advisory file list: there is NO separate `store.py snapshot` CLI
subcommand — the hub-side build lives in the MCP tool and
`fleet.snapshot.build_snapshot`. One disclosed degrade shape: an offline
SessionStart serves through the query-shaped selector (no query exists
for the moment), so it degrades to the fail-open envelope plus the
pending overlay rather than a recency-shaped serve.

The capability-matrix rows for the Inject and explicit-recall degrade
cells move from obligation to shipped with this delivery; the T-12
`doctor --fleet` family (issue #291) ships with the fleet-operations
release, including the snapshot-age check.

## Installing the fleet (issue #291)

Surfaces shipped with the fleet-operations release (under
`scripts/fleet/` and `skills/memory/scripts/`):

- **Hub**: `install-hub.ps1 -HttpsPort <n>` / `install-hub.sh REPO_ROOT
  DATA_DIR TOKENS_FILE [PORT [HTTPS_PORT]]` register the hub bound to
  127.0.0.1 only, refuse a broad-ACL (SID-matched) / non-0600 principal
  map, and configure `tailscale serve --bg --https=<https-port>
  http://127.0.0.1:<port>` (issue #423: the serve HTTPS port defaults to
  443 and is a parameter; an installer targeting the default :443 when a
  live non-zmem handler already occupies it REFUSES before re-pointing
  serve — name another port with `-HttpsPort`/`HTTPS_PORT` instead of
  replacing an operator service). The Linux
  unit runs as the invoking (sudo) user with NoNewPrivileges and verifies
  the service came up before reporting success.
- **Per-machine principals**: `scripts/fleet/issue-principal.py --map
  FILE --principal ID --host H --harness X [--namespaces a,b]
  [--capabilities c1,c2] [--preset off-tailnet]` appends to the hub's map
  under a strict fail-closed lock and prints the token exactly once. An
  entry WITHOUT `--namespaces` is UNSCOPED hub-side (every namespace
  allowed; capabilities never gate add/capture) — the issuer refuses it
  unless `--allow-unscoped` is passed. `--preset off-tailnet` means
  capabilities `read,passive` AND requires `--namespaces`.
- **Broker**: `install-broker.ps1` (token piped via the PowerShell
  pipeline) / `install-broker.sh REPO_ROOT HUB_URL [NAMESPACES]` (token
  piped via stdin) write the singular per-broker credential the CLIENT
  transport reads, refuse conflicting effective config (ZMEM_HERMES_MODE
  other than unset/`mcp`, a differing `ZMEM_MCP_URL`, any plural map, any
  `ZMEM_STORE`) in BOTH the root and invoking-user scopes under sudo, and
  validate with a fresh child asserting the transport resolves mcp BEFORE
  persisting the configuration.
- **Health**: `doctor --fleet` reports seven checks (hub reachable, hello
  compatible, principal, op-queue backlog with oldest age, snapshot age,
  stray local core.md, full-disk encryption while a complete snapshot
  pair exists). `ZMEM_FLEET_FDE_PROBE` (`on`/`off`) overrides the
  encryption probe for operators and tests.

## Tailscale identity facts (recorded per issue #291)

Two Tailscale facts bound the fleet design (from the issue's design
critique): Serve's `Tailscale-User-*` identity headers are ABSENT for
tagged devices, and a backend `WhoIs` lookup through Serve identifies the
Serve PROXY, not the original node. Node identity therefore cannot be
inferred naively from either — the bearer principal (per-machine, via the
principal map) remains the ONLY authorization authority, and Tailscale
identity is defence in depth at best.

## Peer ingest trust mode (issue #282)

Every machine importing rows it did not author runs
`ingest-jsonl --trust untrusted-peer --allow-namespace <ns>
--capture-mode auto` (add `--attribute-host <this box>` to stamp local
attribution; `--allow-namespace` is repeatable, one namespace per flag).
Trust claims inside the rows are clamped, namespaces are
gated, and evidence records from peers are refused; see docs/CLOUD.md.

## Migrating from a peer JSONL exchange (issue #291)

The interim topology this replaces: a laptop runs
`~/.zmem/zmem_exchange.py` hourly (scheduled task `ZMemExchange`),
exporting timestamped JSONL to a synced `exchanges/outbox/` and ingesting
`exchanges/inbox/`. The hub topology is authoritative; this runbook ends
the peer exchange WITHOUT losing a row.

1. **Quiesce the exchange.** Disable the scheduled task / cron entry on
   every machine that runs one, then let the final export finish so the
   outbox is a complete tail.
2. **Final authoritative ingest.** On the hub, ingest each machine's
   outbox ONCE with the T-03 peer-trust mode (the file goes via `--in`,
   and `--allow-namespace` is repeatable, one namespace per flag):
   `store.py ingest-jsonl --in <outbox.jsonl> --trust untrusted-peer
   --allow-namespace <ns> --capture-mode auto --attribute-host <box>`.
   Trust claims are clamped and namespaces gated (the #282 contract).
3. **Re-embed on the hub** in its local context: run `store.py reembed`
   on the hub so the ingested rows are searchable on the hub's embedding
   profile (peer-imported embeddings are not assumed compatible).
4. **Switch each former peer to broker mode.** On each machine (the
   laptop first):
   - UNSET `ZMEM_STORE` in every scope — `ZMEM_STORE` together with
     `ZMEM_REMOTE_URL` is a configuration error (the #289 rule).
   - Make `ZMEM_DATA` a writable DIRECTORY (a value naming an existing
     file is now refused with exit 2; the laptop's historical
     `ZMEM_DATA=...store.sqlite` misconfiguration is exactly this).
   - Set `ZMEM_REMOTE_URL` to the hub's full `/mcp` endpoint and install
     the per-broker credential with `scripts/fleet/install-broker.{ps1,sh}`
     (the singular token file; the hub's principal map never leaves the
     hub).
5. **Keep the old store read-only as a backup** — MOVE it OUT of the
   active data dir (e.g. `store.sqlite.pre-fleet.bak`) so the broker's
   queue/snapshot sidecars never share a directory with it.
6. **Verify**: `doctor --fleet` green on every broker machine. A stray
   local `core.md` in remote mode is flagged (the hub owns Tier 0).

## Hub backup with Litestream (issue #291)

Litestream gives the hub DISASTER RECOVERY — a point-in-time replica it
can restore after disk loss. It is NOT availability: a running hub that
is unreachable stays unreachable until the operator restores, and
replication does NOT restore the filesystem sidecars (op queue, broker
snapshots, decision logs) — only `store.sqlite`.

Reference replica (operator attachment): Litestream SFTP replica to
`r640ai` over the tailnet at `/srv/zmem-litestream/store`, snapshot
interval 24h, retention 30 days.

The restore drill (run once on the hub box and attach the output to the
PR):

```bash
litestream restore -o /tmp/zmem-restore-drill/store.sqlite sftp://r640ai/srv/zmem-litestream/store
python - <<'EOF'
import sqlite3
conn = sqlite3.connect("/tmp/zmem-restore-drill/store.sqlite")
print(conn.execute("PRAGMA integrity_check").fetchone()[0])
EOF
python skills/memory/scripts/doctor.py --store /tmp/zmem-restore-drill/store.sqlite
```

Green (`ok`, `integrity_check` = `ok`) closes the drill. The restored
store is a SCRATCH copy — the live hub keeps serving from its own disk;
see `docs/CLOUD.md` for the off-tailnet runner tiers.
