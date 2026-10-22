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

The SessionStart hook injects a ready-to-run store command into context
(look for `# Memory skill: invoke ...`): the shell-quoted interpreter the
hook resolved, then the `store.py` path. Set `P` to that interpreter token
and `S` to the `store.py` path for the commands below — and prefer running
the injected command verbatim. Fallback: resolve an interpreter explicitly
(`py -3` on Windows) and set
`S=${CLAUDE_PLUGIN_ROOT}/skills/memory/scripts/store.py` (Claude Code) or
`${ZCODE_PLUGIN_ROOT}/...` (ZCode).

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
"$P" "$S" recall --query "<distinctive terms from the item's content>" \
  --namespace "<the item's namespace>" --limit 5 --hybrid --no-bump
```

Read the near-hits, don't just glance at scores. Three outcomes:

- **Already there, still true** → drop the item. Record it as "merged into
  existing <id>" in your report.
- **There, but this item corrects or updates it** → note it for supersession
  (run `"$P" "$S" supersede --id <uuid> --reason "..."` yourself, outside
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
"$P" "$H" <harvest-file> \
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

### Untrusted-peer mode (issue #373)

When the harvest comes from a machine or session you do NOT fully trust
(a peer box's closeout, a remote agent's self-assessment), ingest it behind
the same trust boundary `ingest-jsonl` uses (issue #282):

```bash
"$P" "$H" <harvest-file> \
  --trust untrusted-peer \
  --allow-namespace "project:<owner>/<repo>" \
  --attribute-host "<originating-host>" \
  --source-ref "session:<batch-tag>" \
  --store "$S"
```

Peer mode clamps every trust-bearing field a row could self-declare
(signal grounding is demoted to a `claimed_signal:` tag, so taint and
confidence stay peer-grade — note: confidence 0.2 sits below the 0.25
default-recall floor, so peer rows surface via `--min-confidence` or
keyword search), strips row-declared `host:`/`agent:` attribution (only
`--attribute-host` is stamped; peer provenance is always the caller's
`--source-ref` or the `session:harvest-<stem>` default), makes the caller's
`--source-ref` win over any row-carried ref, and refuses every namespace
outside `--allow-namespace` (counted as `namespace_refused=` in the summary,
never fatal; the allow-list is EXACT string match, not a prefix — list the
precise namespaces your rows use). Peer rows also carry `--no-dedup`, so a
peer row never folds into (and mutates) one of your existing local rows.
Flag-contract violations exit 2 at parse time — before the harvest file is
read. Default mode (`--trust authoritative`, the pre-#373 behavior) trusts
the harvest file — use it only for your own exports or files you curated
row by row; note that in default mode a row's own `source_ref` still wins
over `--source-ref` (the long-standing precedence the memory SKILL.md
documents).

Alternatively, for a small number of survivors, call `"$P" "$S" add
--namespace ... --type ... --content ... --tags ... --signal ... --source-ref
"session:<batch-tag>"` per item directly — either path is fine as long as
every surviving item actually gets written.

## Step 5 — Verify and report

After ingestion, spot-check with `"$P" "$S" recall --query "..." --namespace
"..." --no-bump` for at least one surviving item to confirm it landed. Then
report plainly:

- **Added** — count and a one-line summary of each.
- **Merged** — count, and for each, which existing memory it merged into
  (id and namespace) and why.
- **Dropped** — count, and for each, why (already known, too narrow, one-off,
  already in docs, etc.).
- Any **superseded** entries and what replaced them.
- Confirm the script's summary line and exit code (0 = every row stored,
  deduped, quarantined, or — in peer mode — namespace-refused cleanly;
  1 = at least one row failed/was rejected, or a file-level error: missing
  file, unreadable file, invalid JSON, or store.py not found).

That report is the evidence the bar was applied on ingest, not just relayed
from the remote session's own self-assessment.
