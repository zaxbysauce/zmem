# Knowledge routing policy

The knowledge companion (`docs/KNOWLEDGE-COMPANION.md`, PR1) is an
explicit, opt-in note-publication workflow. PR2 adds one thing on top: a
**canonical concise routing policy** — a short instruction block telling
an agent the knowledge lane exists, which target ids are configured, and
that capture is explicit-only — surfaced once per session on the hosts
that have native wiring. This document is the routing truth table.

The feature modifies no server/hub canonical recall envelope, adds no new
hook events, and performs no network I/O, credential read, vault-root
stat, or model call while rendering or appending the policy.

## The policy helper

`skills/memory/scripts/knowledge/policy.py` renders the policy; no host
re-implements the wording or the config resolution. Delivered API:

- `render_policy(config_path=None, environ=None, allow_capture=True) -> str`
- CLI: `--config PATH` (optional), `--no-capture`, `--text` or `--json`.
  Exit 0 for rendered-or-disabled; exit 2 for usage errors — unknown
  flag, conflicting modes, or a MISSING `--config` value (end of
  arguments, or the next flag where a path was expected); a missing
  value never falls back to default config resolution.
- `resolve_config_path(environ=None, *, home=None)` — `home` is
  keyword-only (a test-isolation seam); resolves the canonical
  store-side `config.json`, mirroring the host store resolution chain
  (explicit flags, then the box/home store chain, file-shaped variable
  refusals included).

Contract:

- **Display-only, target-ids-only.** The rendered text names the enabled
  state and the target ids — never target values, endpoints, credential
  locations, or note content. No network, no root stat while rendering.
- **Fail-closed display.** A config file that exists but is undecodable
  (strict UTF-8, BOM-tolerant), carries a duplicate JSON key, an unknown
  `knowledge`/target key, or any invalid target kind/spec/field shape
  suppresses the ENTIRE render — the same config the real validator
  (`knowledge/config.py`, the authority a command invocation hits)
  refuses to load never renders a partial roster here. Refusals are
  silent-to-display and content-free (no ids, no values, no file
  content). The "(+N not shown)" roster note counts only VALID ids
  beyond the display cap — invalid keys are not counted, they refuse the
  whole render. Value judgments (absolute roots, URL grammar, numeric
  ranges) stay with the validator; the display lane checks structure
  only.
- **Short** (under 200 words) and identical on every host.
- **Gates.** `ZMEM_INJECT=0` is enforced by each adapter before any
  spawn: no output, no subprocess. `ZMEM_CAPTURE=0` does not silence the
  policy — the adapter passes `--no-capture` and the helper renders
  read-routing without any capture invitation.
- **Default off is empty.** No `knowledge` object or `enabled: false`
  renders empty text; adapters append nothing.

Adapters spawn the helper as a bounded subprocess WITHOUT `--config` —
the resolver variables (`ZMEM_STORE` / `ZMEM_DATA` / plugin-data vars and
the home variables) drive `resolve_config_path`, so the policy reflects
the same canonical config the store uses. The dsh launcher passes an
EXPLICIT MINIMAL env (the resolver variables plus interpreter/system
basics only): notably `ZMEM_STORE` is preserved even in remote mode,
because the policy child resolves the CONFIG, not the store — the
store-lane "delete `ZMEM_STORE` under `ZMEM_REMOTE_URL`" normalization
never reaches it, and no credential or other sentinel env var rides
along. Hermes inherits its provider process environment verbatim. No
credential value ever appears in an adapter's argv. A missing helper
(checkouts without it), a non-zero exit, a timeout, or empty output all
fail open to "append nothing". Adapters never import storelib or the
knowledge package.

## Native surfaces

| Host | Surface | Gate | Cadence |
|---|---|---|---|
| Hermes (provider mode) | `system_prompt_block`: Tier-0 core.md, then the policy | `ZMEM_INJECT=0` → empty block, no spawn | once per provider instance per capture mode (in-memory cache) |
| dsh / Cordis (DeepSeek plugin) | `agent/pre-step` append after the inject gate, source kind `zmem-policy`, before the fenced recall text | `ZMEM_INJECT=0` → no append, no spawn; `ZMEM_CAPTURE=0` → still appends (read-routing) | once per session per adapter process |
| Hook hosts (ZCode / Claude Code / Codex) | SessionStart hook final-context append (ships with PR2's hook seam) | `ZMEM_INJECT=0` → no output; `ZMEM_CAPTURE=0` → read-routing | once per session start |

Notes that are contract:

- Hermes appends client-side in the provider's own block; the recall
  envelope is untouched, and a Hindsight sidecar deployed beside zmem is
  unaffected (compat mode never calls `system_prompt_block`).
- The dsh policy message deliberately does NOT reuse the `zmem` source
  kind: the admission matcher commits recall reservations on `zmem`-kind
  admissions, so a shared kind could false-ack a recall that never
  landed. The once-per-session latch lives in the plugin's native state
  (`policyResolved` / `policyDelivered`; top-level preview
  `policyDeliveredText` / `policyReason`) and is per-process: the plugin
  does not rehydrate the persisted state file at boot, so a fresh adapter
  process re-primes the session exactly like the recall delivery epoch
  and may deliver the policy once more. At most one helper spawn happens
  per session per process even when the companion is off. The append is
  independent of recall — a missing recall envelope still delivers the
  policy.
- **Default-off byte-identity:** with the companion off, every surface's
  output is byte-identical to the pre-knowledge output — no extra bytes,
  no extra message; each surface performs at most one bounded probe per
  its cadence, and no surface persists its latch across a restart.
  Disclosed cost of that probe: enabled state is knowable only by
  spawning the helper (adapters never read config.json themselves), so
  the policy note spawns the helper once per payload/session even when
  the companion is off — about 0.2 s cold on the measured hosts, and
  hard-bounded (2 s for the SessionStart hook lane, 5 s for the dsh
  launcher and Hermes) so a wedged helper can never stall the surface.

## Compat mode and hosts without native wiring

Hermes compatibility mode (Hindsight kept as `memory.provider`) gains no
new per-turn policy path. For compat mode and any host not in the table,
the fallback is the **explicit portable skill** (`skills/knowledge/
SKILL.md`, surfaced at closeout by `skills/closeout/SKILL.md` only when a
bounded `knowledge status` check reports the companion enabled — silent
otherwise). That fallback is documented **non-equivalence**: manual and
explicit-only, with no per-session policy injection — a session that
never invokes it never sees the knowledge lane.

## OpenCode — manual instruction shim; the gap remains

OpenCode has no zmem adapter (issue #66; every OpenCode row of the fleet
capability matrix is `capability_gap`). PR2 does not change that. The
supported path is a manual operator action:

1. `python skills/memory/scripts/knowledge/policy.py --text` (add
   `--config <path>` when the store is not resolvable from the
   environment).
2. Paste the rendered text into OpenCode's own instructions array config.

There is no app-config install tool, no automatic sync, and no native
OpenCode adapter claim. The pasted policy is static until the operator
re-runs the helper; the fleet matrix gap remains until a native adapter
ships.

## Non-goals

- No server/hub canonical envelope modification — appends are host-side.
- No new hook events — existing seams only.
- No passive remote fetch, vault sync, mandatory editor (Obsidian or
  otherwise), or autonomous capture; PR1's explicit-only law holds.
- Release 0.155 ships in this PR: the CHANGELOG and release manifest
  restamp, and the deepseek canary/support host-gate evidence
  (`docs/host-gate-evidence/deepseek/`) is regenerated against the
  qualified working tree. The canary now includes a dedicated `policy`
  lane exercising BOTH helper legs — the default-off render (empty,
  byte-bounded) and an explicit `knowledge.enabled=true` markdown-target
  render (target id present, word-bounded) via the real
  `skills/memory/scripts/knowledge/policy.py` (in this stacked branch;
  the PR1-only checkout discloses a helper-absent default-off pass).
  Remaining precision on that evidence: the dsh `agent/pre-step`
  conversation leg itself still runs with knowledge disabled, so the
  enabled-config render is proven by the canary's helper leg and the
  repo's test suites, not by an end-to-end enabled pre-step delivery.
