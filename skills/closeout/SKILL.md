---
name: closeout
description: >
  End-of-session knowledge closeout for the zmem store. Recall what is already
  known, capture only generalizable lessons with honest signals, supersede
  anything this session proved WRONG, consolidate near-duplicates, and review
  skill-promotion candidates. Use when the user says "closeout", "wrap up",
  "end of session", "capture what we learned", or after finishing a substantial
  task whose lessons would otherwise be lost. Not for mid-task capture — the
  PostToolUseFailure and Stop hooks already handle that.
---

# Session Knowledge Closeout

The store compounds only if what goes in is **true, general, and non-redundant**.
A store that only grows becomes a store that lies. Capturing nothing is a
perfectly good outcome; capturing five mediocre lessons is a bad one, because
retrieved-wrong costs more than retrieved-nothing.

## Step 0 — Locate the store and resolve the namespace

The SessionStart hook injects the `store.py` path into context each session
(look for `# Memory skill: invoke ...`). Use that exact path. Fallback:
`${CLAUDE_PLUGIN_ROOT}/skills/memory/scripts/store.py` (Claude Code) or
`${ZCODE_PLUGIN_ROOT}/...` (ZCode). Set `S` to it for the commands below.

**Never hand-write a namespace.** Keys are derived from the git remote, so a
guessed `project:<foldername>` writes somewhere nothing ever queries. Derive it:

```bash
python -c "import sys;sys.path.insert(0,r'$(dirname "$S")');import host;print(host.resolve_namespace('.'))"
```

Choose the scope deliberately:

| Scope | Use when |
|---|---|
| the derived `project:...` | the lesson is only true inside this repo (its build, its conventions, its gotchas) |
| `user:global` | the lesson holds anywhere — a language footgun, a tool behaviour, a workflow rule |

When in doubt prefer `user:global` for genuinely portable knowledge and the
project key for anything that references this repo's structure. Worktrees and
second clones of the same remote resolve to the same key automatically.

## Step 0.5 — Review captured correction candidates

The live-capture hook (`capture-correction`, issue #47) queues corrections the
user typed mid-session ("no, use X", "don't refactor unrelated code",
"remember: ...") into a namespace-scoped sidecar queue — it NEVER writes the
store (hooks only queue; this skill writes). If this session-start hook surface
mentioned a pending count, or you want to check, review the queue now:

```bash
python "$S" queue-list --namespace "<derived namespace>" --json
```

The `items[]` shape is a superset of transcript-mining (`corrections`) items, so
the same review discipline applies. For each item, apply the adapted
claude-reflect rubric — executed by YOU (the session's agent) reading this
skill, never by shelling out to an LLM CLI:

- **Keep only corrections reusable across sessions** — reject questions,
  one-time task instructions, context-specific requests, and vague feedback
  ("fix it", "wrong").
- **`remember:` items are always presented, never silently dropped** — they are
  explicit, highest-confidence capture requests.
- **Trust user corrections as authoritative** for model names, API versions,
  tool availability, and flag values — do not second-guess them against your
  training data.
- **Rewrite accepted items** as actionable imperative claims with trigger
  conditions (Step 2 guidance below).

Accepted items then flow through the existing pipeline unchanged: Step 1
recall-before-write (dedupe/supersede check) → Step 2 `add` with
`--signal user` (never higher — SIGNAL_CONFIDENCE maps `user` → 0.6) and
`--source-ref "session:<id>"`.

**Cold-start (bootstrap) candidates:** the `mine-history` command (issue #48)
can also queue mined candidates with `source: "history-mine"` — salvaged from
HISTORICAL Claude Code transcripts (`~/.claude/projects/**/*.jsonl`), not
live-captured. They appear in `queue-list` exactly like live items; apply the
same rubric. Two queue-item `kind`s exist:

- `kind: "correction"` (`source: "history-mine"`) — a mined user correction.
  Treat exactly like a live correction above. Its `occurrences` field says how
  many transcripts contained the same (near-identical) message; corrections do
  NOT carry `review_priority` (that flag is exclusive to `error_pattern` items),
  so give the row its honest signal-derived confidence like any other correction.
- `kind: "error_pattern"` (`source: "history-mine"`) — a recurring tool error
  aggregated across sessions (grouped by `error_type` + `project_folder`, count
  `N`). It is NOT a corrective claim yet. Rewrite its `suggested_guideline` as a
  starting draft into a **`lesson`** with the ACTUAL trigger condition you
  observed ("when X fails with `error_type`, do Y"), then write it via Step 2
  `add --type lesson`. Signal honesty applies: repeated tool errors do NOT
  automatically qualify as `test`/`compile` grounding — assign the signal the
  evidence actually supports. `review_priority` (their ordering weight) must not
  become the row's confidence; give the row the honest signal-derived one.

Rejections mined into the report (not queued) are context for judging whether a
feature/tool is being misused; they are a #46 report surface, not a queue
candidate. `mine-history` never writes the store in any mode.

**Secrets:** an item flagged `secret_warning: true` carried secret-like text at
capture time. Render the warning to yourself, and write that item via
`add --capture-mode auto` so any remaining secret-like text is redacted before
it reaches the store (the default manual mode would keep the original wording).

After processing, clear the processed items from the queue (leave explicitly
deferred items in place), and prune stale low-confidence candidates:

```bash
# remove the specific processed item ids
python "$S" queue-clear --namespace "<derived namespace>" --id <id> --id <id>

# prune stale (past decay) items with confidence < 0.6
python "$S" queue-clear --namespace "<derived namespace>" --drop-stale
```

## Step 1 — Recall before you write

For each candidate lesson, check what the store already believes:

```bash
python "$S" recall --query "<the lesson in a few words>" --limit 5 --hybrid --no-bump
```

Three outcomes, and they lead to different actions:

- **Already there, still true** → capture nothing. Redundancy dilutes recall.
- **There, but this session proved it WRONG or outdated** → supersede it (Step 3).
  This is the step most closeouts skip, and it is the one that keeps the store
  honest.
- **Not there** → capture it (Step 2).

`--no-bump` keeps this audit from inflating retrieval counts (it records a passive
*surface* on `surfaced_count`, not a retrieval — issue #21). `--hybrid` blends vector
and keyword matching so you find near-misses phrased differently from your query.

## Step 2 — Capture, with a hard bar

A lesson earns a row only if **all** of these hold:

1. A future session facing a *different but similar* task would act differently
   because of it.
2. It is not already discoverable in the repo (README, CLAUDE.md/AGENTS.md,
   docstrings). Don't mirror documentation into memory.
3. It is not a one-off — not a typo, a transient network failure, or a
   now-fixed bug in code you already corrected.
4. Getting it wrong again would cost real time.

Good: *"vec0 KNN is namespace-blind; the recall path now over-fetches by
`ZMEM_VEC_NS_OVERFETCH` (default 8) and post-filters by namespace in a single
helper shared with the dedup window. The footgun is mitigated, still
over-fetch; consolidate escalates `k` until a below-threshold row appears,
capped at 500."*
Bad: *"Fixed the consolidate bug."* (narrative, not reusable)
Bad: *"Use pytest for tests."* (already in the repo docs)

Write the content as an actionable claim, not a story. Include the trigger
condition ("when X, do Y, because Z") so recall can match a future situation.

```bash
python "$S" add \
  --namespace "<derived namespace or user:global>" \
  --type <lesson|convention|fact|preference|decision|constraint> \
  --content "<specific, actionable, includes the trigger condition>" \
  --tags "comma,separated" \
  --signal <test|compile|lint|reviewer|user|none> \
  --source-ref "session:<session-id>"
```

**Signal honesty is load-bearing** — signal sets confidence, confidence gates
recall, and only grounded signals are promotable. Never inflate:

| Signal | Means | Only if |
|---|---|---|
| `test` / `compile` / `lint` | a tool verified it | that tool actually ran and passed/failed accordingly |
| `reviewer` | an independent review confirmed it | a reviewer/critic actually said so |
| `user` | the user stated it | they actually did |
| `none` | your own inference | everything else — including "it seems right" |

Re-running `add` with **identical** content in the same namespace refreshes the
existing row rather than duplicating it. **Paraphrases** dedup at ≥0.85 cosine;
the dedup window now uses the same shared `ZMEM_VEC_NS_OVERFETCH`-based
helper as recall, so a same-namespace paraphrase cannot be crowded out by
other namespaces on a busy multi-namespace store — the footgun is
mitigated, still over-fetch. Step 4's `consolidate` is the backstop, which
is why it is part of this routine and not optional.

**Aim for 0–5 rows.** If you have more than five, you are probably capturing
narrative or duplicating docs — re-apply the bar.

## Step 3 — Supersede what is now wrong

If this session disproved, replaced, or outdated a stored memory, tombstone it.
This preserves history while removing it from recall (issue #59):

```bash
python "$S" invalidate --id <full-uuid> --reason "<why the fact is no longer true>"
```

`invalidate` REQUIRES a reason — it is the preferred form for "this fact is no
longer true" because the correction is auditable. For a revision that keeps the
same topic (wrong details, now corrected) use `update` instead, which is
append-only and preserves point-in-time recall:

```bash
python "$S" update --id <full-uuid> --content "<the corrected lesson>"
```

`update` tombstones the old row, creates a NEW live row, and links the new row
back via `update_of` — `--as-of` before the update still returns the OLD
content, so the correction never destroys history. Plain `supersede` remains
for general tombstones (consolidated/pruned rows) where no reason is required.

Then capture the corrected lesson as a new row if `update` was not the right
shape. A store whose wrong entries are never retired will confidently mislead
a future session.

## Step 4 — Consolidate near-duplicates

```bash
python "$S" consolidate --dry-run
```

Review the proposed clusters. Merging is namespace-scoped — it will not fold one
project's memory into another's — but the *keeper* choice still deserves a
glance. The dry run models the cadence gate, so if it reports `would merge N`
you can trust a real run will merge; if it reports `would skip by cadence gate`, the
store was consolidated recently and has not grown enough to warrant another pass.
If the clusters look right:

```bash
python "$S" consolidate
```

A real run that the cadence gate declines prints `[zmem] consolidate: skipped by
cadence gate (...)` (it is never silent) and changes nothing. If you want to
consolidate anyway — e.g. you just imported a large batch of near-duplicates —
pass `--force`:

```bash
python "$S" consolidate --force
```

**Contested clusters are never auto-merged — not even by `--force`.** Similarity
alone cannot tell "always X" from "never X", so when a cluster's members differ
in negation polarity (a negator like *never / don't / not / avoid* on one side
only) consolidate reports it as a `CONTESTED cluster ... NOT merged` block and
leaves every member live. Resolve a contested pair with Step 3 (`supersede` the
wrong side, then recapture the corrected lesson) — do not merge contradictions;
merging would absorb a memory's own refutation into the row it contradicts.
Pass `--merge-contested` only when you have confirmed the contest is a heuristic
false positive (both sides mean the same thing). For machine-readable output
(including the contested list), pass `--json`: stdout then carries only the JSON
run report, with human output moved to stderr.

Pruning low-value, never-surfaced, never-retrieved rows is opt-in and destructive-ish; inspect
first and only proceed if they are genuinely noise:

```bash
python "$S" consolidate --prune --dry-run
```

**`retrieval_count = 0` is NOT evidence a memory is unused.** Since hook-driven recall is
passive (`--no-bump`) and records the surface on `surfaced_count` (issue #21), a memory
surfaced into context on every prompt still shows `retrieval_count = 0`. `consolidate --prune`
only retires rows with BOTH `retrieval_count = 0` AND `surfaced_count = 0` (plus low
confidence, old age, `signal = none`) — do not hand-prune on `retrieval_count` alone, and do
not read `retrieval_count = 0` as "dead weight".

## Step 5 — Review promotion candidates

```bash
python "$S" promote --dry-run
```

Promotion turns a lesson into a `SKILL.md` in **both** `~/.claude/skills` and
`~/.zcode/skills`. Be selective — every promoted skill costs trigger-matching
attention in every future session.

Promote only when the lesson is (a) grounded (`test`/`compile`/`lint`),
(b) repeatedly retrieved, and (c) genuinely a *reusable procedure* rather than a
stored fact. **Promote at most 1–3 per closeout**, newest-highest-value first,
even when the candidate list is long.

The `description` is the entire trigger surface — a vague one means the skill
never fires and the promotion was wasted. Always write it yourself:

```bash
python "$S" promote --id <uuid> --description "Use when <explicit trigger context> — <what it prevents>" --confirm
```

`--confirm` is required to actually write; `--dry-run` alone changes nothing.

## Step 6 — Report

State plainly:

- Lessons captured, with signal and namespace for each
- Anything **superseded**, and what replaced it
- Whether consolidation merged anything
- Correction candidates reviewed (from Step 0.5's queue): reviewed N, captured M,
  rejected K (and why)
- Skills promoted (and why those, not the others)
- What you deliberately did **not** capture, and why

That last line matters most: it is the evidence the bar was actually applied,
not skipped.

## Never

- Never put secrets, credentials, tokens, or PII in the store — it is local
  plaintext and the write-time scanner is advisory only.
- Never inflate a signal to make a lesson look promotable.
- Never treat `retrieval_count = 0` as evidence a memory is unused — hook-surfaced
  memories carry their count on `surfaced_count`; base prune/rank decisions on both
  (issue #21).
- Never capture in-trajectory refinement ("first tried X, then Y") — capture
  only the conclusion that would help next time.
- Never write to `tasks/<slug>/*.md` or `issue-traces/<issue>/*.md` from here;
  the memory store wraps durable session state, it does not replace it.
