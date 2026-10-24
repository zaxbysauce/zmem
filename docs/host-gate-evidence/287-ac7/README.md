# AC7 host-gate evidence — issue #287 (T-08 broker)

> Credential cleanup: the three UserPromptSubmit launcher recall captures
> (`claude-launcher-recall.json`, `codex-launcher-recall.json`, and
> `zcode-launcher-recall.json`) were removed because they contained live
> credentials. The historical description below does not substitute for those
> removed captures; their contents are no longer published evidence.


Captured 2026-10-24 against the vsan hub (`http://192.168.1.148:8765/mcp`,
zmem 0.120.0, schema 19, minilm) from the hub-host workstation. Per-harness
model-visible injection at the three AC7 moments (SessionStart,
UserPromptSubmit, PreToolUse) is proven two ways:

1. **Launcher-contract captures** (all three harnesses): each
   `<harness>-launcher-<moment>.json` drives the REAL production launcher
   (`hooks/zmem-launch.js` from that harness's installed zmem 0.121.0
   plugin — the exact code the harness invokes per its hook wiring) with
   `ZMEM_REMOTE_URL` pointing at the hub, and records the launcher's
   `additionalContext` output — the model-visible injection. Every capture
   shows `model_visible_fence: true` (the `ZMEM_UNTRUSTED_FENCE` block);
   the UserPromptSubmit (`recall`) captures additionally show
   `hub_canary_visible: true` — the canary row seeded into the hub's
   store over MCP immediately before the capture, proving the content
   traveled hub → broker → harness. The SessionStart captures carry the
   hub-owned Tier-0 `core.md` (5130 chars), the T-08 hub-authoritative
   Tier-0 behavior.

2. **Real harness transcript** (codex): `codex-exec-transcript.jsonl` is
   the session log of `codex exec` (codex-cli 0.160.0) run with
   `ZMEM_REMOTE_URL` set, redacted exactly one way: absolute home
   directory segments (`...\Users\<user>` placeholders, per the
   tracked-file hygiene ratchet in `tests/test_doc_drift.py`) — every
   byte of the injection payload, hook events and model output is
   untouched. The harness fired the zmem
   `UserPromptSubmit` hook (visible in the log as
   `hook: UserPromptSubmit Completed`), and the MODEL'S OWN ANSWER in the
   transcript confirms the fenced `Relevant memories` block was visible
   to it. `claude-run2.json` documents the claude leg's harness wiring
   proof (the CLI invoked the launcher's session-end hook; the model leg
   of that run failed on an expired OAuth session, which is why the
   claude evidence is the launcher-contract capture — the hook contract
   output is by definition the model-visible injection for that
   harness's runner).

Harness plugin versions at capture: claude 0.121.0 (auto-updated,
gitCommitSha 05c1bfa8), codex 0.121.0 (auto-updated), zcode 0.121.0
(registered; cache materialized from the same release tree). All three
harnesses ran against the same single hub with the same token
(`~/.zmem/.mcp_token`, bearer auth).

The capability-matrix rows for these moments were already `registered` at
generation (the T-01 contract registered the designed capability); this
evidence discharges the HOST_ONLY obligation recorded in
`docs/FLEET.md`'s T-08 section.

## Safe evidence publication

Do not publish raw recall memory in host-gate evidence. The regression test
requires the three exposed captures to remain absent, and permits future
launcher recall captures only when `stdout_raw` contains the fixed redaction
notice defined by the test. Use synthetic credentials and curated evidence
when capturing production hook behavior.
