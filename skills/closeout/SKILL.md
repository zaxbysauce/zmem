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

`--no-bump` keeps this audit from inflating retrieval counts, which feed
promotion ranking. `--hybrid` blends vector and keyword matching so you find
near-misses phrased differently from your query.

## Step 2 — Capture, with a hard bar

A lesson earns a row only if **all** of these hold:

1. A future session facing a *different but similar* task would act differently
   because of it.
2. It is not already discoverable in the repo (README, CLAUDE.md/AGENTS.md,
   docstrings). Don't mirror documentation into memory.
3. It is not a one-off — not a typo, a transient network failure, or a
   now-fixed bug in code you already corrected.
4. Getting it wrong again would cost real time.

Good: *"vec0 KNN is namespace-blind — filtering after a fixed `k` silently
starves same-namespace neighbours; escalate `k` until a below-threshold row
appears."*
Bad: *"Fixed the consolidate bug."* (narrative, not reusable)
Bad: *"Use pytest for tests."* (already in the repo docs)

Write the content as an actionable claim, not a story. Include the trigger
condition ("when X, do Y, because Z") so recall can match a future situation.

```bash
python "$S" add \
  --namespace "<derived namespace or user:global>" \
  --type <lesson|convention|fact|preference> \
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
existing row rather than duplicating it. **Paraphrases** dedup at ≥0.85 cosine,
but that lookup uses a fixed `k=5` namespace-blind window — on a busy
multi-namespace store a same-namespace paraphrase can be crowded out by other
namespaces and land as a duplicate anyway. Step 4's `consolidate` is the
backstop, which is why it is part of this routine and not optional.

**Aim for 0–5 rows.** If you have more than five, you are probably capturing
narrative or duplicating docs — re-apply the bar.

## Step 3 — Supersede what is now wrong

If this session disproved, replaced, or outdated a stored memory, tombstone it.
This preserves history while removing it from recall:

```bash
python "$S" supersede --id <full-uuid> --reason "<what changed and why>"
```

Then capture the corrected lesson as a new row. A store whose wrong entries are
never retired will confidently mislead a future session.

## Step 4 — Consolidate near-duplicates

```bash
python "$S" consolidate --dry-run
```

Review the proposed clusters. Merging is namespace-scoped — it will not fold one
project's memory into another's — but the *keeper* choice still deserves a
glance. If the clusters look right:

```bash
python "$S" consolidate
```

Pruning low-value, never-retrieved rows is opt-in and destructive-ish; inspect
first and only proceed if they are genuinely noise:

```bash
python "$S" consolidate --prune --dry-run
```

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
- Skills promoted (and why those, not the others)
- What you deliberately did **not** capture, and why

That last line matters most: it is the evidence the bar was actually applied,
not skipped.

## Never

- Never put secrets, credentials, tokens, or PII in the store — it is local
  plaintext and the write-time scanner is advisory only.
- Never inflate a signal to make a lesson look promotable.
- Never capture in-trajectory refinement ("first tried X, then Y") — capture
  only the conclusion that would help next time.
- Never write to `tasks/<slug>/*.md` or `issue-traces/<issue>/*.md` from here;
  the memory store wraps durable session state, it does not replace it.
