---
name: closeout-remote
description: >
  End-of-session knowledge closeout for sessions WITHOUT access to the zmem
  store — cloud sessions, Claude Code Remote (CCR), or any other machine that
  cannot reach this box's store.py. Reflects on the session with the same
  capture bar as the local `closeout` skill, then emits a portable JSON
  harvest instead of writing to the store directly. Use when the user says
  "closeout", "wrap up", "end of session", "capture what we learned" in a
  session that has no local zmem store, or when `store.py`/`ZMEM_DATA` is
  unreachable. Pair with the `/ingest-harvest` command, run later on a
  machine that does have the store, to actually write the results.
---

# Session Knowledge Closeout (Remote / No-Store Variant)

This session cannot read or write the zmem store — no `store.py`, no
`ZMEM_DATA`, no local box to connect to. You cannot recall what is already
known and you cannot supersede anything. Do not attempt either; do not
fabricate a namespace derivation you cannot verify. Instead, reflect on the
session and hand off a harvest that a session WITH store access will dedup,
trim, and ingest later via `/ingest-harvest`.

## The capture bar (same bar as local closeout — restated, not skipped)

The store compounds only if what goes in is **true, general, and
non-redundant**. A store that only grows becomes a store that lies.
**Capturing nothing is a perfectly good outcome; capturing five mediocre
items is a bad one**, because retrieved-wrong costs more than
retrieved-nothing. Because this harvest will be ingested *without* your
having seen what the store already contains, err toward a smaller, higher-bar
list than you would in a local closeout — the ingesting agent is your only
backstop against redundancy, not a rubber stamp.

An item earns a row only if **all** of these hold:

1. A future session facing a *different but similar* task would act
   differently because of it.
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

Write each item's content as an actionable claim, not a story. Include the
trigger condition ("when X, do Y, because Z") so recall can match a future
situation.

**Aim for 0–5 items.** If you have more than five, you are probably capturing
narrative or duplicating docs — re-apply the bar.

**Signal honesty is load-bearing** — signal sets confidence, confidence gates
recall. Never inflate:

| Signal | Means | Only if |
|---|---|---|
| `test` / `compile` / `lint` | a tool verified it | that tool actually ran and passed/failed accordingly |
| `reviewer` | an independent review confirmed it | a reviewer/critic actually said so |
| `user` | the user stated it | they actually did |
| `none` | your own inference | everything else — including "it seems right" |

## Namespace rule

You cannot run `host.py:resolve_namespace` here (no store checkout to import
from, and even if the code is visible, git remote resolution needs to match
what the ingesting box would derive). Default to `user:global` for anything
portable. Only propose a `project:github.com/<org>/<repo>` namespace when you
can verify it from a **real github.com remote URL**:

```bash
git remote get-url origin
```

- If that prints `https://github.com/<org>/<repo>.git`, `git@github.com:<org>/<repo>.git`,
  or an equivalent GitHub URL, use `project:github.com/<org>/<repo>` (lowercase).
- **Cloud / CCR sessions see a loopback proxy remote instead**, of the form
  `http://local_proxy@127.0.0.1:<port>/git/<org>/<repo>` — this is NOT a real
  GitHub host, it is a local port-forwarded proxy standing in for one. If you
  see this shape, you may derive `<org>/<repo>` from the path segments after
  `git/` and propose `project:github.com/<org>/<repo>` — but you MUST say so
  explicitly in that item's `why` field (e.g. "derived org/repo from the CCR
  loopback proxy remote path, not a verified github.com URL"), so the
  ingesting agent can decide whether to trust it.
- If the remote is anything else, unreadable, or you are not confident in the
  derivation, fall back to `user:global` and say why in `why`.

Never hand-write a `project:` namespace from a guessed folder name — an
unverified namespace writes somewhere nothing ever queries.

## Output format

Emit results as a **single fenced JSON array**, one object per captured item,
with exactly these keys (no extras, no omissions):

```json
[
  {
    "namespace": "user:global",
    "type": "lesson",
    "content": "Specific, actionable claim including the trigger condition.",
    "tags": "comma,separated,tags",
    "signal": "test",
    "why": "One sentence: which signal, and what concretely verified it."
  }
]
```

Field rules:

- `namespace` — `user:global` or a verified `project:github.com/<org>/<repo>`
  (see above).
- `type` — exactly one of `fact`, `lesson`, `convention`, `preference`.
- `content` — the actionable claim itself, not narrative.
- `tags` — a comma-separated string (not a JSON array).
- `signal` — exactly one of `test`, `compile`, `lint`, `reviewer`, `user`,
  `none`.
- `why` — one sentence of grounding evidence: why this signal was chosen and
  what concretely verified it (e.g. "pytest run passed after the fix",
  "user explicitly stated this preference in chat", "no tool ran; this is my
  own inference").

If nothing clears the bar, emit an empty array `[]` — do not omit the fenced
block, and do not pad it with narrative to look productive.

## Never

- Never include secrets, credentials, tokens, API keys, or PII in `content`
  — the store is local plaintext and the write-time scanner is advisory only.
  If a lesson is only meaningful with a secret embedded (e.g. "the deploy key
  starts with X"), drop the item entirely rather than redact-and-keep.
- Never inflate a signal to make an item look more grounded than it is.
- Never capture in-trajectory narrative ("first tried X, then Y") — capture
  only the conclusion that would help next time.
- Never claim a namespace derivation you did not actually check.

## Delivery

1. Paste the fenced JSON array directly in your reply, along with a short
   plain-language report: what you captured (or didn't, and why not).
2. **If this session has a working branch**, also write the same JSON to
   `zmem-harvest/<short-session-id>.json` in the repo (create the
   `zmem-harvest/` directory if absent) and include that file in the branch
   (stage/commit per the session's normal workflow) so the harvest survives
   after the conversation ends. Use a short, stable session identifier for
   `<short-session-id>` (e.g. the first 8 characters of the session/task id
   you have available) — this filename is only a delivery handle, not a
   namespace or an identity the store reasons about.
3. Do not attempt to write to any zmem store, and do not invoke `store.py`
   from this skill — that is the ingesting agent's job, via `/ingest-harvest`,
   applying its own recall-before-write dedup pass against the real store.

## Report

State plainly, same as local closeout:

- Items captured, with signal and namespace for each (or "none").
- What you deliberately did **not** capture, and why — this is the evidence
  the bar was actually applied, not skipped.
- Where the harvest was delivered (pasted only, or also written to
  `zmem-harvest/<short-session-id>.json`).
