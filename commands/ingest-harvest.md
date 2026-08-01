---
description: Ingest a closeout-remote harvest (JSON file or pasted block) into the zmem store — dedup-checked against the real store, not blindly trusted from the remote session
---

Ingest a knowledge harvest produced by the **`closeout-remote`** skill on a
session that had no store access (cloud, CCR, another machine).

Argument: `$ARGUMENTS` — either a path to a harvest JSON file (for example
`zmem-harvest/abc12345.json`) or, if no path was given, a fenced JSON block
pasted directly into this conversation. If neither is present, ask the user
for the harvest before proceeding.

**The capture bar is applied here, by you, the ingesting agent — not just
trusted from the remote session.** The remote session could not recall what
the store already contains, so every item in the harvest is a *candidate*,
not a pre-cleared entry. Treat it the way `closeout`'s Step 1 treats a
locally-drafted lesson: check the store first, then decide.

## Step 0 — Locate the store and the ingest script

The SessionStart hook injects the `store.py` path into context (look for
`# Memory skill: invoke ...`). Use that exact path; fall back to
`${CLAUDE_PLUGIN_ROOT}/skills/memory/scripts/store.py` (Claude Code) or
`${ZCODE_PLUGIN_ROOT}/...` (ZCode). Set `S` to it for the commands below.

This command runs in the **user's project repo**, not the zmem plugin
checkout — a bare `scripts/ingest_harvest.py` will not resolve there. Derive
its path as the sibling of `S` in the plugin layout (`S` is always
`<plugin root>/skills/memory/scripts/store.py`, so the script is three
directories up from `S`, then into `scripts/`):

```bash
H="$(dirname "$S")/../../../scripts/ingest_harvest.py"
```

or equivalently `${CLAUDE_PLUGIN_ROOT}/scripts/ingest_harvest.py` /
`${ZCODE_PLUGIN_ROOT}/scripts/ingest_harvest.py` if those env vars are set in
this session. Set `H` to it for Step 4.

## Step 1 — Parse the harvest

Load `$ARGUMENTS` as a JSON array of objects. Each object must have exactly:
`namespace`, `type`, `content`, `tags`, `signal`, `why`. If the JSON is
malformed or an item is missing a required key, say so and stop rather than
guessing at intent — do not silently drop or repair a broken harvest.

## Step 2 — Dedup recall, per item

For **each** item, before deciding anything, check what the store already
believes — scoped to that item's own namespace:

```bash
python "$S" recall --query "<distinctive terms from the item's content>" \
  --namespace "<the item's namespace>" --limit 5 --hybrid --no-bump
```

Read the near-hits, don't just glance at scores. Three outcomes:

- **Already there, still true** → drop the item. Record it as "merged into
  existing <id>" in your report.
- **There, but this item corrects or updates it** → note it for supersession
  (run `python "$S" supersede --id <uuid> --reason "..."` yourself, outside
  this command's batch-add step, then let the corrected item survive to
  Step 3).
- **Not there / genuinely new** → the item survives to Step 3.

## Step 3 — Merge, trim, or drop overlapping items

Beyond store-vs-harvest dedup, also compare items **within the same harvest**
against each other (a remote session may have produced near-duplicate
phrasings of the same lesson) and re-apply the ordinary capture bar from the
`closeout` skill: reusable, not already in repo docs, not a one-off,
worth the cost of getting it wrong again. **Capturing nothing from an item is
valid** — do not pad the surviving set to make the harvest look useful.

For a surviving item that overlaps a store entry only partially, prefer
tightening its `content` over dropping it outright, so the ingested row adds
signal rather than restating what already exists.

## Step 4 — Ingest the survivors

For the items that survived Steps 2–3, run:

```bash
python "$H" <harvest-file> \
  --source-ref "session:<batch-tag>" \
  --store "$S"
```

using a harvest file trimmed to only the surviving rows (write a temp copy if
you dropped/merged any items — never feed the script items you already
decided not to keep). `<batch-tag>` should identify this ingestion batch
(e.g. the remote session id the harvest came from). If `--source-ref` is
omitted, `ingest_harvest.py` defaults it to `session:harvest-<file-stem>`
(the harvest file's name without its extension) — prefer passing it
explicitly with a batch tag that actually identifies the source session,
since the default is only as meaningful as the file name it derives from.

Alternatively, for a small number of survivors, call `python "$S" add
--namespace ... --type ... --content ... --tags ... --signal ... --source-ref
"session:<batch-tag>"` per item directly — either path is fine as long as
every surviving item actually gets written.

## Step 5 — Verify and report

After ingestion, spot-check with `python "$S" recall --query "..." --namespace
"..." --no-bump` for at least one surviving item to confirm it landed. Then
report plainly:

- **Added** — count and a one-line summary of each.
- **Merged** — count, and for each, which existing memory it merged into
  (id and namespace) and why.
- **Dropped** — count, and for each, why (already known, too narrow, one-off,
  already in docs, etc.).
- Any **superseded** entries and what replaced them.
- Confirm the script's summary line and exit code (0 = all surviving rows
  ingested cleanly; 1 = at least one row failed — report which and why).

That report is the evidence the bar was applied on ingest, not just relayed
from the remote session's own self-assessment.
