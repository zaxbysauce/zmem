---
description: End-of-session closeout for sessions WITHOUT store access — emits a portable JSON harvest for later /ingest-harvest, instead of writing to the store
---

Run the end-of-session knowledge closeout now, for a session that cannot
reach the zmem store.

Invoke the **`closeout-remote` skill** from the zmem plugin (via the Skill
tool) and follow it exactly. The skill is the single source of truth for
this routine — do not improvise a shortened version of it here, and do not
restate its steps from memory: read it and execute it.

Two reminders that matter more than speed:

- **Capturing nothing is a valid outcome.** The bar in the skill is
  deliberately high, and higher still here than in a local closeout, because
  this harvest will be ingested without the ingesting agent having seen this
  session unfold. A harvest that emits an empty array because nothing
  generalized is a success, not a failure to be padded out.
- **Report what you deliberately did not capture, and why.** That line is the
  evidence the bar was actually applied rather than skipped, even though the
  result is a harvest handed off for later ingestion rather than a store
  write you can verify yourself.

One more thing, specific to this command: if this session **also** has
working store access — that is, the SessionStart hook injected a `store.py`
path into context and it actually resolves — then `/closeout` is the right
command to run instead, not this one. This variant deliberately skips
recall-before-write dedup against the real store (it can't reach it), and
its output requires a second ingestion step (`/ingest-harvest`) before
anything actually lands. If you can see a working store path, say so and ask
the user to confirm before proceeding with `/closeout-remote` anyway — don't
silently pick the lower-fidelity path when the better one is available.

If the user passed arguments, treat them as scope hints for what to reflect
on (for example a specific task, file, or subsystem) — not as permission to
lower the capture bar.
