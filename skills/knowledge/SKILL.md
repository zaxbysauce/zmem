---
name: knowledge
description: >
  Publish a durable, curated note into the OPTIONAL knowledge companion
  (a local Markdown vault or a configured private fleet knowledge-base
  endpoint) and record a verifiable pointer in the zmem store. Use when
  the user explicitly asks to save/publish/pin a piece of knowledge
  ("save this to the knowledge base", "publish this note", "put this in
  the vault"), or when a closeout the ACTUAL USER requested in this
  session runs with the companion enabled (that standing opt-in
  authorizes evaluating this session's knowledge candidates — no
  per-note confirmation and no magic phrase required). Not for autonomous
  background capture; an agent-initiated or hook-reminder closeout is
  NOT user authorization (draft only, capture nothing).
---

# Knowledge Note Publication (explicit workflow, portable fallback)

The knowledge companion publishes a note YOU author, once, verbatim — after
it passes a screening step — into an operator-configured target, then stores
a pointer so future sessions can find, re-read, and integrity-check it. It
never sweeps transcripts, hooks, or the memory store. Targets are the
operator-configured LOCAL vault or PRIVATE fleet endpoint only — existing
permissions apply; this workflow is never a channel for publishing to
external public services.

It is OFF unless the operator enabled it in the store config; a disabled
companion is reported and stops there — never an error, never a reason to
improvise a substitute write.

## Authorization (who may run a capture, when)

- **Standalone / mid-task:** the user explicitly asked to save or publish
  this knowledge now.
- **User-requested closeout:** the ACTUAL USER asked for this closeout in
  THIS session (their own request, any phrasing — not an agent-initiated
  after-task closeout, not a hook-reminder nudge), the companion is
  enabled (`knowledge status` says so), and `ZMEM_CAPTURE` is not `0` —
  that standing opt-in authorizes EVALUATING this session's knowledge
  candidates and capturing the worthwhile ones; do not re-ask per note
  and do not demand a magic phrase.
- **Agent- or reminder-initiated closeout (NOT user authorization):**
  evaluate and DRAFT the worthwhile note texts in your reply only; run no
  capture, no pointer write, no flush. Publication waits for the user's
  explicit request.
- **Never autonomous:** no background sweeps, no inferred closeouts, no
  capture the user did not authorize through one of the paths above.

## Step 0 — Locate the store CLI and check the companion status

Resolve the interpreter and `store.py` exactly like the closeout skill: use
the SessionStart-injected `# Memory skill: invoke ...` command when present
(`$P` = interpreter token, `$S` = store.py path), else fall back to `py -3`
and the plugin-rooted scripts path. This skill is portable — it runs
anywhere the store CLI is reachable and nowhere else. Run every command
below with the SAME `$P`/`$S`.

Check the companion (bounded, read-only):

```bash
"$P" "$S" knowledge status --json
```

- `enabled=false` (or exit 2 with the disabled hint): say the companion is
  disabled and stop. Do not write anything, anywhere, as a substitute.
- Enabled: note the configured TARGET IDS. Target ids are slugs (for
  example `notes` or `fleet`); the target's kind and everything else about
  it stays in the config — commands below take only the id.

If this session cannot reach `store.py` at all (remote/no-store session),
do NOT fabricate publication or a pointer. Say so, deliver the authored
note draft in your reply, and state that a store-reachable session must
run the capture — that honest handoff replaces a write you cannot perform.

## Step 1 — The capture bar (only worthwhile long-lived knowledge)

Evaluate candidates against ALL of these; the bar decides, not a quota —
most sessions, even most closeouts, capture nothing:

1. The capture is authorized (see Authorization above).
2. It is long-lived: a future session, possibly on another box, facing a
   different task would still be helped by it months from now.
3. You can name its evidence: the sources, files, decisions, or
   measurements the note cites — and that evidence is IN the note body,
   not just claimed. A note without its sources is not published.
4. It is a CURATED document you author now — never a transcript excerpt,
   session log, chat dump, or a memory-store row copied out.

**Never** include secrets, credentials, tokens, API keys, PII, or anything
secret-shaped in the note body or `--summary`. The screening step rejects
secret-like content before anything is written; dropping the note entirely
is the correct outcome when the knowledge is only meaningful with a secret
embedded. Do not paste conversation transcripts — link or cite them by
reference (repo path, issue, commit) instead.

## Step 2 — Derive the namespace (never hand-write it)

Pointer rows are namespace-scoped exactly like memory rows, and READS
resolve through the same namespace — a capture without its namespace
writes a pointer that scoped reads will miss. Derive the key with the
resolved interpreter, the same way the closeout skill does:

```bash
"$P" -c "import sys;sys.path.insert(0,r'$(dirname "$S")');import host;print(host.resolve_namespace('.'))"
```

Use the derived `project:...` key when the note is only true inside this
repo; use `user:global` when the note holds anywhere. A guessed namespace
writes somewhere nothing ever queries.

## Step 3 — Author and capture the note

`--key` is the logical locator: 1..8 `/`-separated lowercase slug segments,
each 1..31 chars, total ≤ 128. `--content -` reads the body from stdin
(UTF-8); write the FULL note this way, never through a shell-quoted
argument. `--summary` is optional bounded authored text (≤ 512 chars).
Substitute the real target id and the derived namespace in EVERY command:

```bash
"$P" "$S" knowledge capture --target <target-id> \
  --key <key> --content - \
  --summary "One-line summary" \
  --namespace "<derived namespace or user:global>" --json < note.md
```

Screening, the immutable version write, and the pointer op are one
pipeline; a crash mid-flight is recovered by re-running the SAME capture
(idempotent for byte-identical content) or by `knowledge flush` — never by
improvising a second, differently-keyed note.

## Step 4 — Verify, then report honestly

Readback verification is part of publication, not an optional extra, and
it needs the SAME target and namespace (an explicit `--namespace` is how a
project-scoped pointer is found):

```bash
"$P" "$S" knowledge read --target <target-id> --key <key> \
  --namespace "<namespace>" --json
```

Report against the ACTUAL outcome, by exit code and `pointer.status`:

- **Verified note, pointer landed** (exit 0, status `created`/`advanced`/
  `noop`/`applied`): report the note key, target, namespace, and that
  readback matched. Only now is the note "captured".
- **`pending` or `queued` pointer** (exit 3): the immutable note version is
  published but the pointer has NOT landed. That is NOT success — say
  "published, pointer pending" and stop. A pointer that is pending
  WITHOUT being enqueued is resolved by re-running the SAME capture
  (idempotent), not by a flush; an enqueued one is drained by
  `"$P" "$S" knowledge flush --json` in remote profile (local profile
  leftovers need remote mode) — and flush drains the WHOLE knowledge
  queue, so check `knowledge status --json` for unrelated pending entries
  before flushing. Do not describe the note as captured.
- Exit 1 (conflict / stale CAS / check mismatch) and exit 2 (refusal —
  invalid config, screening, unknown target): report the refusal as-is and
  write nothing further.

Integrity re-checks later use the same shape:

```bash
"$P" "$S" knowledge check --target <target-id> --key <key> \
  --namespace "<namespace>" [--locator <locator>] --json
```

Keep closeout-time checking bounded: this session's own keys only, never a
sweep of the target.

## Never

- Never capture outside the authorized paths (explicit save/publish, or a
  closeout the actual user requested this session with the companion
  enabled and capture on).
- Never publish transcripts, session logs, credentials, or PII.
- Never treat `pending`/`queued` as success.
- Never hand-write a namespace or invent a target id that `knowledge
  status` did not list.
- Never re-run `knowledge capture` with edited content to "fix" a pending
  pointer — same content is idempotent, different content is a NEW version.
- Never claim Obsidian or any specific editor is required or used — a
  `markdown` target is a plain directory of files.
- Never treat this target as a channel to external public publishing —
  local vault and private fleet endpoint only.

## Portability and non-equivalence

This skill is the EXPLICIT PORTABLE FALLBACK for hosts without native
knowledge wiring (docs/KNOWLEDGE-ROUTING.md). It is NOT equivalent to the
native adapters: there is no per-session routing policy injection here —
the workflow runs only when explicitly invoked or at a closeout the
actual user requested this session. Hosts with native wiring (Hermes
provider system prompt, dsh once-per-session pre-step append) also
surface a short routing policy; on any other host this skill being
present is the whole surface.
