---
name: closeout
description: >
  Session closeout for the zmem memory store. Reviews queued user corrections,
  saves the few lessons a future session would act on, retires memories this
  session proved wrong, merges duplicates, and records the closeout. Use when
  the user says "closeout", "wrap up", "end of session" or "capture what we
  learned", or after finishing a substantial task whose lessons would otherwise
  be lost. Do not run it to answer a question about closeout, or to close a
  ticket or pull request. If store.py cannot be run, use closeout-remote.
---

# Session closeout

Saving nothing is a good outcome. A wrong or vague memory costs every later
session that retrieves it.

The closeout is recorded only when `closeout-status` prints `"ack_valid":true`
and you have then run `closeout-ack` (Step 7). If you lose your place after a
context compaction, run `closeout-status`; for each missing step, do the
matching step below, then record it.

## Step 0 — Setup

1. **Store command.** The session-start memory header has a line
   `Memory skill: invoke '<python>' '<store.py>' <subcommand>`, written with
   POSIX quoting. In Bash run it as shown. In PowerShell put `& ` in front of
   it. There is no `zmem` program: below, `<store>` means that interpreter and
   `store.py` path together. If the header is missing, ask the user for the
   `store.py` path (on Windows the interpreter is `py -3`).
2. **Session id.** Use `session_id=` from the session-start memory header.
   With an older header, use the id shown in a zmem reminder or gate message,
   or Claude Code's `CLAUDE_CODE_SESSION_ID` environment variable. If you have
   no confirmed id, still do Steps
   0.5 to 5.5 without `--source-ref`, skip Steps 6 and 7, and say in the report
   that the closeout could not be recorded.
3. **Namespace.** Use `namespace=` from the session-start memory header.
   With an older header, derive it from the project directory, with the folder
   that contains `store.py` written out:

   ```bash
   '<python>' -c "import sys; sys.path.insert(0, r'<folder containing store.py>'); import host; print(host.resolve_namespace('.'))"
   ```

   In PowerShell put `& ` in front. Outside a git repository this prints a
   folder-based key; tell the user before saving project lessons under it.
4. **Is this project-bound or box-wide?** Ask this for every lesson. True only
   in this repository (its paths, build, policy) → the project namespace. True
   anywhere (a tool's behaviour, a language trap, a working rule) →
   `user:global`, chosen explicitly.

Every `add` and `update` below takes `--source-ref "session:<session id>" --json`.
Commands are written on one line so they run in any shell.

Hazardous git operations are not closeout work, but if this session is about to
run one, the memory skill's decision-point checkpoints apply first: recall
before `git stash pop`, `git reset --soft`, `git push`, or editing a file named
by a ratchet lesson, and treat a hit as blocking review.

## Step 0.5 — Review the correction queue

```bash
<store> queue-list --namespace "<namespace>" --json
```

Items flagged `secret_warning: true` contain secret-like text. Do not repeat
their messages in your reply.

Decide each item:

- **Save** a correction that would change behaviour in a later session. It
  becomes a candidate lesson for Steps 1 and 2, saved with `--signal user`.
  For a `secret_warning` item, save with `--capture-mode auto` and leave the
  flagged value out of your wording.
- **Reject** questions, one-off task instructions and vague feedback.
- **`remember:` items are never dropped silently.** They have `type`
  `explicit`. Save each one, or tell the user why not.
- The user is the authority on names, versions and flags they corrected.

Then check whether a saved correction contradicts a stored convention:

```bash
<store> convention-drift --namespace "<namespace>" --json
```

It returns ids only; read each with `<store> get --id <id>`. If the correction
wins, retire or correct that convention in Step 3; if the convention stands,
reject the queue item. If the command errors, say "drift scan unavailable" in
the report and continue.

Clear rejected items now. Clear a saved item only after its `add` or `update`
has returned in Step 2 or 3. Leave items you are deliberately deferring.

```bash
<store> queue-clear --namespace "<namespace>" --id <id> --id <id>
<store> queue-clear --namespace "<namespace>" --drop-stale
```

Items with `source: "history-mine"` or `kind: "error_pattern"`: see
[references/queue-items.md](references/queue-items.md).

## Step 1 — Look before you write

For every candidate lesson, with the project namespace and `--include-global`,
even for a `user:global` lesson:

```bash
<store> recall --query "<the lesson in a few words>" --namespace "<namespace>" --include-global --min-confidence 0 --limit 5 --no-bump --json
```

`--min-confidence 0` matters: without it, rows saved with signal `none` are
hidden and you will store a duplicate.

- **Already stored and still true** → save nothing.
- **Stored but this session proved it wrong** → Step 3.
- **Not stored** → Step 2.

## Step 2 — Save lessons (usually 0 to 3)

A lesson earns a row only if all four hold:

1. A later session on a different but similar task would act differently.
2. It cannot be found in the repository, its docs or its git history.
3. It is not a one-off: not a typo, an outage, or a bug already fixed.
4. You verified it or the user stated it, and getting it wrong again would
   cost real time. A plan nobody adopted does not qualify.

Save what worked as well as what failed: a confirmed approach the user
accepted is as useful as a correction. More than five candidates means
re-apply the bar.

Write the conclusion, not how you got there. Plain text, one lesson per row:

> When **<situation you can recognise>**, **<do this>**, because **<reason>**.

Add "Holds while <condition>; check with <command or file>" when the lesson
depends on a version, platform, file or setting that can change.

Bad: "Fixed the consolidate bug." (a story) Bad: "Use pytest for tests."
(already in the repository docs)

```bash
<store> add --namespace "<namespace or user:global>" --type <lesson|convention|fact|preference|decision|constraint> --content "<the lesson>" --tags "a,b" --signal <test|compile|lint|reviewer|user|none> --source-ref "session:<session id>" --json
```

**Signal is evidence, not confidence.** `test`, `compile` or `lint` only if
that tool ran in this session and its result supports the lesson; `reviewer`
only if a separate reviewer said so; `user` only if the user said it;
otherwise `none`. A `none` row is stored but passive recall will not surface
it, so prefer saving nothing over saving a guess.

**Read the result.**

- If result is deduped, nothing new was stored. Read the returned row with
  `<store> get --id <id>`. If it does not contain your lesson, save again with
  `--no-dedup`.
- A warning of type `redacted` means part of your text became
  `[REDACTED_SECRET]`. The store masks any run of 32 or more hex characters
  and any run of 40 or more letters, digits and slashes, so full commit
  hashes, digests and deep paths are masked. Cite commits by 12 characters and
  files by their last two path parts (`storelib/consolidate.py`). If the
  masked value was not a secret, fix the row with `update` and do not print
  the redaction line. Otherwise tell the user:
  `zmem: redacted <N> secret-like value(s) (value not shown)`.
- Notices about an allowlisted `session:` reference, a contradiction link or
  a phrasing advisory are normal; read a contradiction link's other row before
  moving on.
- A stderr warning that the row was stored without an embedding means the
  store is on keyword search only. Carry on; say so once in the report.

Never put secrets, credentials or personal data in a memory.

## Step 3 — Retire or correct what is now wrong

Before writing, phrase one natural question a future user would ask about the
symptom or decision. Keep it for the readback.

The only reversal command is `store.py invalidate --id <uuid> --reason "<why the fact is no longer true>"`;
`--reason` is required. Run it through `<store>`:

```bash
<store> invalidate --id <full-uuid> --reason "<why the fact is no longer true>"
<store> update --id <full-uuid> --content "<corrected lesson>" --signal <signal> --source-ref "session:<session id>" --json
```

Use `update` when the topic stays and the details change: it keeps the old row
as history and returns a new live id. It copies the old row's signal unless
you pass `--signal`, so pass the signal the corrected claim has earned. Read
`result`; `update` can fold into another row unless you pass `--no-dedup`. Use
`invalidate` when the claim is simply false now. A newer statement does not
automatically beat an older one: if both can be true in different conditions
(one platform, one branch), keep both and state the condition in each.

Read the corrected row back in its own namespace with the saved question.
For a project row, use the project namespace:

```bash
<store> recall --query "<natural question>" --namespace "<namespace>" --include-global --min-confidence 0 --no-bump --json
```

For a global row, query `user:global` directly:

```bash
<store> recall --query "<natural question>" --namespace user:global --min-confidence 0 --no-bump --json
```

The project-first merge puts project matches first and appends up to
`--global-limit` global rows (three by default). A healthy global row can fall
outside the first five or be absent from that merged response. Look for the
corrected row's id in `results[].id`, using the new live id after an update.
If it is absent in its own namespace, report the question and result.
`recall --explain --target <id>` can show why a row was omitted.

## Step 4 — Merge duplicates

```bash
<store> consolidate --namespace "<namespace>" --dry-run
```

"Would merge 0" means there is nothing to do. Otherwise the output lists each
cluster with its keeper and absorbed rows. `consolidate` merges every proposed
cluster at once; you cannot pick. Run
`<store> consolidate --namespace "<namespace>"` only if every cluster says the
same thing. Otherwise do not run it, and list the clusters in the report.
Clusters marked contested hold opposite claims: resolve them with Step 3. On a
keyword-only store the dry run can miss real duplicates; do not lower the
threshold or use `--force` or `--prune` to compensate.

## Step 5 — Promotion (rarely)

```bash
<store> promote --dry-run
```

"No promotion candidates found" is the usual result: move on. Promotion turns
a lesson into a skill file every later session loads. Consider it only for a
reusable procedure grounded by `test`, `compile` or `lint` that has been
repeatedly surfaced or retrieved (the dry run already applies this).

`<store> promote --id <id> --description "Use when ..." --confirm` writes a
review candidate only. Installing it needs `--install-approved`. Add that flag
only after the user has read the candidate and said, in this conversation, to
install it.

## Step 5.5 — Knowledge companion (enabled-gated)

```bash
<store> knowledge status --json
```

- `enabled` is false: skip this step silently and do not mention knowledge.
- The command fails: never abort the closeout over it. Put "knowledge closeout
  unavailable, no capture performed" in the report.
- Enabled, and the ACTUAL USER asked for this closeout in THIS session (any
  phrasing): the standing opt-in authorizes evaluating this session's
  knowledge candidates and capturing the worthwhile ones, with no per-note
  confirmation and no magic phrase needed. `ZMEM_CAPTURE=0` removes capture
  (report and verify only). Limit everything to this session's own keys:
  1. Capture through the knowledge skill's workflow (`knowledge capture
     --target <target-id> --key <key> --content - --namespace "<namespace>"`).
     Its bar decides, not a quota; most closeouts capture nothing.
  2. `knowledge flush` drains the WHOLE knowledge queue; there is no own-key
     filter. Flush only if this session's entries are the only queued ones;
     otherwise do NOT flush and report this session's keys as pending. A
     pending pointer that was never queued is resolved by re-running the SAME
     capture (idempotent), not by a flush.
  3. Read back each note captured this session (`knowledge read --target
     <target-id> --key <key> --namespace "<namespace>" --json`), compare the
     returned content with the authored note and verify digest agreement
     before reporting success. In the report, pending is not success.
- Enabled, but this closeout was not user-requested (agent initiated after a
  task, or surfaced by a session hook reminder): knowledge work is DRAFT-ONLY.
  Draft the worthwhile notes in your reply; run no capture and no flush.

## Step 6 — Review this session's rows and record the steps

Make every change first (Steps 2 and 3). Then:

```bash
<store> closeout-status --session-id "<session id>"
```

For each id under `unreviewed_rows`, read the row with `get` and record a
verdict. Use `keep` for a live row that is right as stored. For
`resignal`, run `update --signal <signal> --no-dedup` first. For `invalidate`, run
`invalidate --reason` first. Record the verdict against the original id,
then run `closeout-status` again to review any new row.

```bash
<store> closeout-review --session-id "<session id>" --memory-id <full-uuid> --verdict <keep|resignal|invalidate>
```

Record the receipts last, one for each step you actually carried out:

```bash
<store> closeout-step --session-id "<session id>" --step review-queue
<store> closeout-step --session-id "<session id>" --step recall-before-write
<store> closeout-step --session-id "<session id>" --step consolidate
<store> closeout-step --session-id "<session id>" --step promote
<store> closeout-step --session-id "<session id>" --step knowledge-status
```

A step that could not run gets a skip with the real reason in at least three
words. Repeat it in the report. Looking and finding nothing to do (no duplicates, no promotion
candidates, clusters you declined to merge) counts as carrying the step out.

```bash
<store> closeout-skip --session-id "<session id>" --step <step> --reason "<what stopped it>"
```

## Step 7 — Acknowledge and report

If the user confirms that an armed closeout was unintended, record that
disposition with `<store> closeout-release --session-id "<session id>"`.
Use `closeout-status` to verify `request_disposition: not_requested`, then
report the release.

Run `closeout-status` again. `missing_steps`: do the step, then record it.
`stale_steps`: a write followed the receipt; record it again, and redo the
step only if that write changed what it would find. When it prints
`"ack_valid":true`:

```bash
<store> closeout-ack --session-id "<session id>"
```

Run `closeout-ack` once. If it refuses, it prints what is missing. If the
store cannot be opened, stop and tell the user; do not retry in a loop.

Then tell the user, in this order:

1. Lessons saved: text, namespace and signal for each. Or "none".
2. Memories retired or corrected, what replaced them, and the readback result.
3. Queue: reviewed N, saved M, rejected K, with the reason for each
   `remember:` item rejected. Convention-drift ids and what you did with each.
4. Merges made, or the clusters you declined to merge.
5. Promotion: candidates written and their paths; nothing is installed until
   the user approves.
6. Knowledge companion: only if enabled or unavailable (Step 5.5).
7. Steps skipped and why; redactions; whether the store was on keyword search
   only; whether the closeout was recorded.
8. What you chose not to save, and why.

Item 8 is the evidence that the bar was applied.
