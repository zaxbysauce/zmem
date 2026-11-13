# Knowledge companion (optional, off by default)

The knowledge companion is an **explicit, operator-driven** workflow for
publishing durable notes into an external knowledge target — a local
Markdown vault or a configured fleet knowledge-base HTTP endpoint — and
recording a *pointer* to each published note inside the zmem store so it can
be found, re-read, and integrity-checked later.

It is OFF BY DEFAULT. With no `knowledge` object in the store's
`config.json`, `capture`, `read`, and `check` refuse with the operator hint
(exit 2), `status` reports `enabled=false`, and nothing is captured,
published, or sent — no journal entry or queue entry is ever written.
Nothing happens unless a `store.py knowledge ...` command is invoked
explicitly — by you, or by an agent acting on your explicit instruction;
there is no automatic, conditional, or injected trigger anywhere in PR1.

Scope of this guide (PR1, approved plan 2026-11-13):

- What ships here: the explicit capture/read/check/status/flush workflow,
  local Markdown and KB HTTP targets, immutable content-addressed versions,
  readback verification, crash-recovery journal, reserved store pointers,
  and the separate capability-gated fleet delivery lane.
- What does NOT ship here: any passive remote fetch, vault sync, or an
  OpenCode native adapter. The PR2 routing policy ships separately
  ([KNOWLEDGE-ROUTING.md](KNOWLEDGE-ROUTING.md)): it is display-only —
  it names the lane and its target ids and never injects knowledge
  content into sessions; capture stays explicit-command-only.
- All commands and example outputs below are **illustrative** — they
  document the contract, they are not transcripts of live runs. Substitute
  your own hosts, paths, and token locations; never commit production
  hostnames or credential values.

## What it is — and is not

- **Curated documents, not raw transcripts.** The companion publishes a
  note you explicitly author and pass to `knowledge capture`. It never
  sweeps session transcripts, hooks, or the memory store into a target.
- **Provider-neutral, no Obsidian dependency.** The two target kinds are a
  plain filesystem directory of Markdown files and a small custom JSON API
  (`kb-http`, defined below). The `kb-http` kind is **not** the official
  Obsidian API (or any vendor API) — it is the minimal create/read contract
  this client speaks, which a self-hosted fleet KB implements. Obsidian or
  any other editor opening the same files is your business; nothing here
  depends on it.
- **Memory fails independently.** The knowledge lane has its own queue,
  sidecars, and op kind; it never rides the core `add`/`update` operation
  queue. A knowledge failure (old hub, unreachable hub, refused publication)
  is a feature-only failure — core memory keeps working. The separation is
  enforced CLIENT-side by this helper: knowledge pointer ops go through a
  dedicated transport method that refuses a mixed core/knowledge batch and
  carries only `knowledge_link` envelopes, and the core queue's route-write
  band never maps a knowledge op. The hub engine, in turn, admits each op
  individually by type, capability, and namespace — the queue split is the
  helper's contract, not a server-side law about batch composition.
- **Backend read/create only, immutable versions.** Targets only ever
  create and read immutable, content-addressed versions. The client never
  PUTs, PATCHes, renames, or deletes: an existing byte-identical version is
  success (`existed_match`), existing *different* content under the same
  locator is a hard conflict, and a version, once written, is never
  rewritten.

## Opt-in configuration (default off)

Opt in by adding one strict `knowledge` object to the store-side config file
— the same `<store_dir>/config.json` that already gates `auto_retain`
(issue #177). `<store_dir>` is the directory holding `store.sqlite`,
resolved through the usual `ZMEM_STORE` / `ZMEM_DATA` / default chain. In
remote mode (no local store) the sidecars land in the data dir, exactly like
the other fleet sidecars.

A concrete two-provider configuration (generic placeholder host and paths —
use your own):

```json
{
  "knowledge": {
    "enabled": true,
    "targets": {
      "notes": {
        "kind": "markdown",
        "root": "D:/notes-vault"
      },
      "fleet": {
        "kind": "kb-http",
        "endpoint": "https://kb.example.invalid/api/notes",
        "credential_env": "ZMEM_KB_FLEET_TOKEN",
        "timeout_s": 10,
        "max_response_bytes": 1048576
      }
    }
  }
}
```

Rules (enforced at load; a present-but-invalid `knowledge` object is a hard
config error, exit 2 — never silently half-enabled):

- `enabled` must be a JSON boolean (`true`/`false`) — anything else is an
  error, not silently-off.
- Unknown keys anywhere in the `knowledge` object are errors: a typo'd field
  fails loudly at load.
- Target ids are slugs: 1..24 chars, lowercase `[a-z0-9-]`, no leading or
  trailing hyphen, unique (JSON object keys).
- Exactly two kinds exist today.

### `markdown` target

| Field  | Required | Meaning |
|--------|----------|---------|
| `kind` | yes | the literal `markdown` |
| `root` | yes | ONE absolute path to the vault root (no `..` components) |

The root must **already exist and be a real directory** — the companion
never creates the vault root, only note files (and the locator's
subdirectories) under it. Symlinks/junctions (reparse points) on the note
path, any ancestor, or the root itself are refused.

### `kb-http` target

| Field | Required | Meaning |
|-------|----------|---------|
| `kind` | yes | the literal `kb-http` |
| `endpoint` | yes | ONE exact URL: `http`/`https`, non-empty host, path starting `/` and not ending `/`, **no** query, fragment, or userinfo |
| `allow_http` | no | plain `http://` endpoints require explicit `"allow_http": true` (a disclosed LAN-only carve-out; https is the default) |
| `credential_env` | exactly one of `credential_env` / `token_file` | name of the environment variable holding the bearer token |
| `token_file` | (see above) | absolute path to a file holding the bearer token (≤ 4096 bytes) |
| `timeout_s` | no | request timeout, 0.1..120 s (default 10) |
| `max_response_bytes` | no | response body cap, 256..16777216 (default 1048576) |

Credentials are explicit by design: exactly one named location per target,
and the token **value** is read only at request time — it is never written
to config, logs, sidecars, or error messages. Redirects are never followed
(the configured endpoint string is the only URL the client will ever touch)
and proxy environment variables are ignored for these requests.

### The `kb-http` wire contract (custom, minimal)

The client appends only the percent-encoded note locator to the configured
endpoint path and sends `Authorization: Bearer <token>`:

- `POST {endpoint}/{locator}` with JSON body
  `{"mode": "create", "content": "<canonical note text>"}` — 200/201 means
  accepted (the client then verifies by reading back); 409 means the locator
  exists (the client recovers via GET-and-compare); 401/403 is an
  authorization refusal; anything else is a transport error.
- `GET {endpoint}/{locator}` must answer 200 with a JSON object containing a
  string `content` field (the canonical note text), 404 when no such version
  exists, or 401/403.
- The server must implement create-only semantics: it never overwrites an
  existing locator. The client relies on that and never sends an update.

## CLI: capture / read / check / status / flush

Run through the store CLI like every other subcommand
(`<interpreter> <store.py> knowledge ...`); `py -3` on Windows. The command
dispatches early and owns its own store handling:

- **local profile** (no `ZMEM_REMOTE_URL`): `capture` opens the *existing*
  store under the same writer lease and schema gates as every other write;
  `read`/`check`/`status` open the STORE CONNECTION read-only (their
  journal/delivery sidecars may still take advisory locks and create their
  dirs — sidecar bookkeeping, not a store write).
- **remote profile** (`ZMEM_REMOTE_URL` set): the knowledge command runs as
  a helper sidecar and **never opens or creates a local store** — pointer
  ops go to the capability-gated delivery queue (below).

There is no separate knowledge mode flag: the existing local/remote profile
authority (`ZMEM_REMOTE_URL`, the same gate as every other subcommand)
decides the mode.

```
<interpreter> <store.py> knowledge capture --target notes \
  --key recipes/sourdough --content - [--summary "…"] \
  [--namespace user:global] [--json]

<interpreter> <store.py> knowledge read --target notes \
  --key recipes/sourdough [--locator <key>/<digest>.md] \
  [--namespace user:global] [--json]

<interpreter> <store.py> knowledge check --target notes \
  --key recipes/sourdough [--locator <locator>] \
  [--namespace user:global] [--json]

<interpreter> <store.py> knowledge status [--target notes] \
  [--key recipes/sourdough] [--namespace user:global] [--json]

<interpreter> <store.py> knowledge flush [--json]
```

Argument notes:

- `--content` is required; the literal `-` reads the note body from stdin
  as UTF-8. Bodies are screened (below) before anything is written.
- `--key` is the logical key: 1..8 `/`-separated segments, each a 1..31
  char lowercase slug `[a-z0-9-]` or a dashed lowercase UUID (the one
  documented exception — a UUID's longest contiguous hex run is 12, so it
  is scanner-safe); total length ≤ 128 chars. Slug segments are kept
  below 32 chars precisely so no key segment can collide with the
  32+-hex/alnum secret shapes the advisory scanner rejects; the scan
  itself may still reject note bodies or summaries that merely MENTION
  such a shape (advisory, below).
- `--locator` on `read`/`check` addresses one explicit immutable version;
  in remote mode (where pointer state lives on the hub) it is how you read
  a version that was captured on another machine.
- `--summary` defaults to the first non-empty line of the canonical
  content, bounded to 512 chars. A summary you supply is authored text
  that rides the pointer row and the delivery sidecar verbatim (nothing
  downstream re-scans it), so it is screened with the SAME secret-like
  pattern source as the content in the admission step — any detection
  refuses the publication (exit 2) BEFORE the journal intent, the create,
  or any enqueue — and length-validated in that same step (≤ 512 chars,
  the pointer payload cap): an oversized summary is a clean refusal
  before anything is written, not a late payload error. The
  advisory-scan caveat below applies to it too: a summary that merely
  mentions a secret-shaped string can be rejected.
- `--namespace` scopes the pointer row exactly like every memory row
  (default `user:global`).
- Under `--json`, stdout is exactly one JSON line (human progress goes to
  stderr). Without `--json`, `read` prints the note content, `check`
  prints an `OK`/`MISMATCH` verdict, and `status` prints a human summary.

### Exit codes

- `0` — completed: the operation's own outcome is ok (for `capture`, the
  pointer landed).
- `1` — a reported failure/conflict: pointer `cas_stale`/`conflict`, a
  `rejected` delivery, or a `check` digest mismatch.
- `2` — a refusal: usage errors, an invalid `knowledge` config object, a
  disabled companion, a screening refusal (secret-like content), an
  unknown target, or an unavailable store.
- `3` — pending: a `capture` whose immutable version IS published but
  whose pointer is `pending` or merely `queued`, or a `flush` that left
  entries pending (including local-mode leftovers that need remote mode).
  Distinct from both `0` and `1` on purpose.

`queued` and `pending` are **states**, not success. When the distinction
matters, read `pointer.status` (`created` / `advanced` / `noop` /
`applied` / `queued` / `pending` / `rejected` / `cas_stale` / `conflict`)
rather than relying on the exit code alone. (An earlier build reported
`pending`/`queued` captures as exit `0` with top-level `"ok": true`; that
known discrepancy is fixed — exit `3` is the contract, frozen by tests.)

## The capture pipeline and crash recovery

Capture order is frozen; each step is durable before the next touches
anything:

1. **Admission/screening** — the secret scan runs FIRST. Any detection
   (count > 0, including already-redacted markers) REJECTS the publication
   before a journal entry, a target file, or a pointer row exists. The
   companion never silently publishes altered content. A supplied
   `--summary` is screened with the same single pattern source in this
   same step, before anything is recorded. Survivors are normalized
   (CRLF/CR → LF, exactly one terminal newline) and hashed.
2. **Journal intent** — a metadata-only intent is recorded durably BEFORE
   the create.
3. **Predecessor observation** — the current pointer is observed BEFORE the
   create; its row id becomes the CAS stake (`expected_row_id`) and the next
   version number is derived from it.
4. **Immutable create** — exclusive create at the target, verified by
   readback (`doc_verified`).
5. **Pointer link** — the atomic compare-and-swap on the authoritative
   store (local SQLite, or the hub's ops engine in remote mode — the SAME
   handler either way).
6. **Idempotence** — retrying with the SAME screened content is a no-op
   everywhere: the journal re-records nothing, the create answers
   `existed_match`, and the link answers `noop`.

The journal lives at `<store_dir>/knowledge/journal.json` (remote mode:
the data dir). It records identity, locator, digest, and a state — **never
the note body** — bounded to 1000 entries / 4 MiB, written atomically
(tmp + fsync + replace) under a cross-process OS byte lock.

Journal states:

| State | Meaning |
|-------|---------|
| `doc_pending` | intent recorded, target create not yet attempted |
| `doc_verified` | immutable version exists at the target, read back and digested |
| `pointer_queued` | pointer op handed to the store / delivery queue |
| `applied` | pointer landed (terminal) |
| `quarantined` | operator escape hatch from any non-terminal state (terminal) |

Recovery rules (from the code): after a crash at any point, re-running
`capture` with the same screened input resumes idempotently from the
recorded state — the pointer outcome is a CHECKED reconciliation against
the live row (same version+digest is a `noop`; a pointer another writer
already advanced goes through the CAS/version law, never a blind noop).
While an identity's latest intent is still ACTIVE
(non-terminal), a capture with a DIFFERENT digest is refused before the
create (drive it to a terminal state or quarantine it first); once the
latest intent is terminal, a new digest records a fresh `doc_pending`
entry — the next version of the same note.

## The pointer contract (namespace, payload, CAS)

Each logical note has one authoritative pointer per namespace: an ordinary
`fact` row (no new table, column, type, or schema version) addressed by the
reserved source ref `knowledge:<target>:<key>`, tagged `knowledge`, taint
`untrusted_tool` (local and remote knowledge are tool-sourced — no trust
bonus for being local). The row's `content` is a closed, versioned JSON
payload:

```json
{"date": "<ISO-Z>", "fullhash": ["8hex", "x8"], "logical_id": "knowledge:notes:recipes/sourdough",
 "locator": "recipes/sourdough/<16hex>-<16hex>-<16hex>-<16hex>.md", "summary": "…",
 "verified": true, "version": 1}
```

- **Full digest, chunked for the scanner.** The digest is the FULL SHA-256
  over the canonical bytes. It is stored as eight 8-hex chunks (`fullhash`)
  and appears in the locator as four 16-hex groups — so no ≥32-hex run
  exists anywhere the generic secret scanner could trip on (or "redact").
  The locator is deterministic: `<logical-key>/<segmented-digest>.md`,
  derived from (key, digest) only; a caller-supplied locator must match it
  byte-for-byte.
- **CAS, not read-then-write.** Advancing a pointer is a single atomic
  SQLite compare-and-swap on the authoritative store (`expected_row_id`),
  never a KB API call. The predecessor read is mode-authoritative:
  locally, this process reads the store it holds; in remote mode the
  AUTHORITATIVE predecessor comes from the hub's capability-gated
  `knowledge_pointer` read route, so a second host stakes the pointer the
  hub actually holds (namespace identity is checked hub-side, before any
  pointer-existence lookup). `expected_row_id: null` means initial create and is
  refused if a verified pointer already exists; a wrong or stale stake is
  rejected with nothing written (`cas_stale`). More than one live verified
  row sharing a ref is treated as store corruption and refused — a winner
  is never silently picked.
- **Version law.** The same version with the same digest is an idempotent
  `noop`; the same version with a different digest is a `conflict` (an
  immutable version is bound to one digest); a version below the live one
  is a stale advance; a version above advances (tombstone + fresh row, one
  transaction). Statuses: `created` / `advanced` / `noop`.
- **Unforgeable reservation.** `knowledge:` joins the reserved source-ref
  set. Only the knowledge handler can mint a `verified: true` pointer row —
  the exemption is an identity-compared private object in the handler, not
  a flag any CLI argument, ops payload, or JSONL row can carry. Ordinary
  `add`/`update` claiming a `knowledge:` ref are refused; `supersede`/
  `invalidate` of a pointer row are refused (a pointer tombstone is a
  pointer state transition and happens only inside the handler's CAS).
  Scope of that guarantee: it is an integrity boundary over the SERIALIZED
  writer surfaces (CLI arguments, ops payloads, import/JSONL rows), not a
  security sandbox. It carries no assurance against in-process malicious
  Python — code already running inside the store's process could call the
  handler or write the database directly; nothing here defends against
  that threat model.
- **Namespace scoping.** Pointer rows are namespace-scoped like every
  memory row; lookups never leak across namespaces.

## Local and remote profiles

The mode follows the existing local/remote profile authority
(`ZMEM_REMOTE_URL`), with one law: **remote mode NEVER opens or creates a
local store.**

- **Local profile:** the pointer CAS runs in this process against the local
  SQLite store (under the CLI writer lease). `status` additionally shows
  the authoritative pointer rows.
- **Remote profile (`ZMEM_REMOTE_URL` set):** the pointer op is enqueued on
  the separate knowledge delivery queue (below) and applied by the hub's
  ops engine through the SAME handler — identical grammar, taint, and
  immutability semantics. The hub transport is the existing fleet resolver:
  URL from `ZMEM_REMOTE_URL`, bearer token from the transport's own chain
  (`ZMEM_MCP_TOKEN` / `ZMEM_MCP_TOKEN_FILE`), deadline from
  `ZMEM_HERMES_DEADLINE_S` (default 6 s) — no new auth surface.

### Capability negotiation and the old-hub law

Nothing is enqueued until the configured hub answers `hello` advertising
the `knowledge_link_v1` capability (the hub advertises it only because its
ops engine implements the `knowledge_link` op — additive, never a protocol
bump). Consequences, all explicit:

- **Old hub** (reachable, no capability): pointer status `pending`
  (`hub-unsupported`), nothing enqueued, no core-queue fallback.
- **Unreachable hub:** pointer status `pending` (`hub-unreachable`),
  nothing enqueued.
- **Rollback:** `flush` re-checks the capability before every apply; a hub
  that lost it pauses every entry `queued` — nothing sent, nothing dropped.
- In every case the immutable version was still published to its target;
  re-run `capture` (same content → idempotent) or `knowledge flush` once
  the hub advertises the capability. Core memory is unaffected throughout.

### The delivery queue and flush

The queue is a separate, bounded, metadata-only sidecar at
`<store_dir>/knowledge/delivery.json` (remote mode: the data dir) — queue
entries carry identity/locator/digest-chunks/summary, **never the note
body**; bounded to 256 queued entries / 512 receipts / 1024 intents /
2 MiB, atomic and locked like the journal. It is not the core op queue —
the helper keeps the two lanes separate and its knowledge transport
refuses a mixed batch (see "Memory fails independently" above).

- `capture` in remote mode: capability check, enqueue (`state: queued` with
  a fresh `kl-<uuid>` op id), then one best-effort immediate delivery; the
  result reports `applied`, `rejected`, or still-`queued`.
- `knowledge flush` (remote profile): drains the queue to the hub in
  batches of 64, re-checking the capability first. Applied entries
  (`committed` / `duplicate` receipts) leave the queue; rejected entries
  leave the queue with their receipt KEPT — the hub recorded the rejection
  against that op id, so a resend would replay it forever; the operator
  resolves the cause and records a new intent. Paused entries stay queued
  with an attempt counter and a content-free last error.
- `knowledge flush` (local profile): local capture applies pointers
  directly, so there is normally nothing to flush; leftover queued entries
  (remote-mode leftovers) are reported and exit `3` (pending, not failure)
  — flushing them requires the remote profile.

**Receipts are not success.** A `queued` entry is distinct from an `applied`
one in every JSON surface, and the journal is reconciled only for op ids
the hub actually applied (`pointer_queued` → `applied`).

## Integrity model

- Every create is verified by reading the version back and comparing the
  digest (`IntegrityError` on mismatch — the publication is not claimed).
- `knowledge check` re-reads the recorded version and compares the full
  SHA-256: exit `0` / `"matches": true` when the target still holds the
  exact canonical bytes, exit `1` on drift. Readback ≠ semantic truth: a
  matching digest proves the bytes, not that the note is correct.
- `knowledge read` resolves the version in this order: an explicit
  `--locator` (`provenance` `locator-arg`), then the AUTHORITATIVE pointer
  — the local store's in local profile (`pointer`), or the hub's
  capability-gated `knowledge_pointer` read in remote profile
  (`hub-pointer`, so a second host with empty local sidecars reads the
  version the hub actually holds). Only when the authoritative pointer is
  unavailable does resolution fall back to the delivery intent sidecar
  (`intent`) and then the journal entry (`journal`) — pending/unapplied
  local publications — and the fallback is labeled: `provenance` carries
  an `-stale` suffix (`intent-stale` / `journal-stale`). A hub read that
  is DENIED (authorization or namespace guard) never falls back — it is an
  honest error, because a local fallback would bypass the hub's access
  decision; an UNAVAILABLE hub (old hub, unreachable, or a transport
  without the read route) is what falls back, labeled stale.
  `provenance` in the JSON output names which one was used.

## Content rules and limits

- **Screening is advisory, not a guarantee.** The scan reuses the store's
  secret-like pattern detector (regex + entropy heuristic). It rejects on
  any detection — but a determined or novel secret format can evade any
  heuristic. Do not treat a passed scan as proof of no secrets.
- Normalization: CRLF and lone CR become LF; the canonical form ends with
  exactly one terminal newline; UTF-8; note content ≤ 4 MiB.
- No module logs, and no error message ever embeds note content or a token
  value — errors name target ids, keys, statuses, and paths only.

## Security posture (truths, not promises)

- The secret scan is an advisory pre-publication gate (see above), not a
  guarantee. If it detects something, publication is refused rather than
  silently altered — that is the whole of the claim.
- The journal, delivery sidecar, receipts, queue entries, and pointer
  payloads carry metadata only — never note bodies, never token values.
- The companion publishes curated documents you author; it has no
  transcript-harvesting path and never publishes raw session logs.
- KB credentials live in a named env var or token file, read only at
  request time, sent only as a bearer header to the exact configured
  endpoint (no redirects, no proxies, response bodies capped).
- Imported pointer rows are preserved but demoted (below); trust is never
  minted by transport, only by the handler's verified readback/relink.
- Pointer rows are `untrusted_tool`-tainted on arrival at recall surfaces,
  local and remote alike.

## Imports, exports, and consolidation

- **Imports demote pointers.** `export-jsonl` → `ingest-jsonl` and
  `import-store.py` preserve a `knowledge:` source ref but force the
  payload's `verified` to `false` before it lands — an imported pointer is
  an inert copy, never authoritative. Re-verification requires an explicit
  relink/readback: re-running `capture` with the same screened content (or
  the handler's link path) tombstones the unverified copies in the same
  transaction and mints the verified row. Unparseable payloads pass through
  byte-identical; they can never be authoritative.
- **No remote tombstones of pointers.** A sync file asking to tombstone a
  pointer row is refused (`tombstone_refused`) — pointer state advances
  only through the handler's CAS.
- **Consolidation protects refs.** Pointer rows are excluded from every
  near-duplicate selection surface in both directions: write-time exact and
  semantic dedup, consolidate seeds/keepers/neighbors, organize working
  sets, and write-time link neighbors. A normal fact never folds into a
  pointer; a pointer never absorbs or gets absorbed; its tags never
  propagate onto neighbors.

## Concurrency and known limitations

Stated from the code — no stronger claim is made:

- **Local capture serializes on the CLI writer lease** (the same single
  -writer discipline as every other write), so concurrent `knowledge
  capture` processes on one box queue behind each other.
- **Same identity, different digests, simultaneously:** the journal allows
  one ACTIVE intent per identity — a concurrent different-digest capture
  while one is in flight fails honestly BEFORE the create; the same digest
  is idempotent.
- **Concurrent pointer advances:** CAS makes exactly one winner; the loser
  gets `cas_stale` with nothing written and must re-read the pointer and
  retry. The immutable version itself may already exist at the target
  (create is exclusive and idempotent), which is fine.
- **Journal locking** is a cross-process OS byte lock (kernel-released when
  the holder exits), acquired non-blocking with a bounded retry (default
  5 s); a busy journal delays writers rather than corrupting.
- **The delivery sidecar is locked with the same OS byte-lock discipline**
  (msvcrt/fcntl on a persistent `.lock` file): a live lock is never
  deleted and never taken over by age — the kernel releases it when its
  holder exits. (An earlier design broke locks held >30 s by age; that
  takeover no longer exists — the `stale_lock_s` constructor argument is
  retained for signature compatibility and is ignored.) A holder stalled
  mid-write delays other writers until it exits; the atomic
  tmp + fsync + replace keeps the file consistent throughout.
- **Markdown reparse checks narrow, not eliminate, the TOCTOU window**
  between the path check and the exclusive create (the code re-verifies
  the chain immediately before creating; that is the whole of the claim).
- **No multi-host vault coordination.** Two machines publishing into one
  shared Markdown root rely solely on exclusive create; there is no
  cross-machine locking. Pointer state is per-store (and, in fleet remote
  mode, on the hub).
- **KB create races** are resolved by the endpoint's own create-exclusivity
  plus GET-and-compare recovery (409 or a lost POST → compare digests:
  identical = `existed_match`, different = conflict, absent = explicit
  "outcome unknown" transport error).
- Bounded queues refuse loudly: a full delivery queue (256) or a
  shape-unrecognized sidecar is an operator-review error, never a silent
  drop or reset. A corrupt (invalid-JSON or shape-unrecognized) journal
  or delivery sidecar surfaces as an honest operator-facing refusal —
  exit `2`, and under `--json` exactly one `{"ok": false, "error": …}`
  object naming the sidecar — never a traceback.

## The hub side (remote pointer ops)

Remote pointer ops are typed `knowledge_link` operations applied by the
hub's ops engine (the bundled MCP server advertises `knowledge_link_v1` in
`hello` only because it implements them). The op args are a closed
whitelist — `namespace`, `target`, `key`, `version`, `locator`, `fullhash`
(eight 8-hex chunks, reassembled and validated engine-side), `summary`,
`date`, `expected_row_id` — no note content, no writer flags, and no
server-filesystem path access: path-shaped values (absolute/drive-rooted,
backslash, or `..` traversal forms) in the routing fields `target`,
`key`, `locator`, and `expected_row_id` are refused outright — only the
prose `summary` may legitimately cite a path, and no file is ever opened
from it. The `locator` that survives is a relative slug the hub records
as metadata but never resolves against its filesystem. The op bypasses the add/capture
identical-content replay lane (its "content" is the server-minted pointer
payload).

Authorization (the existing hub principal capability model — no new auth
surface):

| Op | Required capability |
|----|---------------------|
| first create — `expected_row_id: null`, no existing verified pointer | `write` (or `mutate`/`admin`) |
| any op supplying a non-null `expected_row_id` stake — an advance, or a would-be idempotent noop (same version AND digest) | `mutate` (or `admin`) |

A null stake against an existing verified pointer is refused by the
handler CAS, so there is no write-only idempotent-noop workflow — an
idempotent retry re-sends its recorded stake and needs `mutate`.

Namespace scope is checked BEFORE any pointer-existence lookup — a
capability verdict can never become a cross-namespace oracle. Revoked and
explicitly-empty-capability principals are denied; a principal with
`capabilities: null` is legacy-allowed (existing fleet semantics). Receipts
are content-free: `committed`/`duplicate` with `link_status` (`created`/
`advanced`/`noop`), or rejected with `pointer_cas_stale`,
`pointer_conflict`, `capability_denied`, `namespace_not_allowed`, or
`bad_op`.

## Not in this feature

- No passive injection: knowledge documents are never injected into
  sessions. The PR2 routing policy
  ([KNOWLEDGE-ROUTING.md](KNOWLEDGE-ROUTING.md)) is display-only — it
  names the lane and its target ids, never note content, and capture
  stays explicit-command-only.
- No passive remote fetch, no vault sync, no vendor API (Obsidian or
  otherwise), no OpenCode native adapter or autonomous writes.
- No new schema surface: pointer rows are ordinary fact rows; no new
  table, metadata column, type, or schema version.
