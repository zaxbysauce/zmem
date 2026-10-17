# Contributing to ZMem (zmem)

How to file issues. This repository originated the workstream convention; the per-AC check binding below is the codified upgrade of that practice.

<!-- CANONICAL-VERSION: ZaxbyHub/opencode-swarm@78a4c546bb (PR #3185, follow-up section from PRs #3371 and #3377). Verbatim-copy core; repo notes are repo-specific. Update by re-copying from the canonical and bumping this stamp. -->


The issue-format contract below is the canonical version (see the stamp). The "Repo notes" section at the end is specific to this repository.

## Filing issues — issues are executable contracts

Issues in this repository are not prose requests: they are **executable contracts**. A well-formed issue can be frozen into acceptance checks, proven RED at the base commit, and proven GREEN at the head of its PR, with independent reviewer and critic gates in between. Every criterion that can be executable must be bound to a check — a criterion with a fake or missing binding is miswritten. Criteria that genuinely cannot be executable take the NON-EXECUTABLE path below instead. This section defines the contract; the tracer workflow consumes this shape. Existing issues filed before this contract are grandfathered; it governs new issues.

**Where this sits in the field.** The acceptance-criteria grammar below is [EARS](https://alistairmavin.com/ears/) (Easy Approach to Requirements Syntax — Mavin/Rolls-Royce, 2009; used across aerospace and heavyweight systems engineering). The overall shape — specification before plan, plan before tasks, human gates around implementation — is the same family as [GitHub Spec Kit](https://github.com/github/spec-kit)'s constitution → specify → plan → tasks → implement → converge. Our differentiator: the specification lives in the tracker (the issue itself), and acceptance is **proven executably** (frozen checks run RED at base, GREEN at head) rather than by implementation review alone. The one-AC-one-check rule is classical requirements traceability: every requirement gets exactly one chosen verification method.

### Two issue types

1. **Standalone issues** (feature, bug, investigation): the required core below, no title convention beyond a clear outcome-oriented statement.
2. **Workstream program issues**: when a program spans multiple PRs, it is decomposed as one issue per PR with the title ``[Workstream <letter>] PR <n> of <m>: <outcome statement>`` — workstreams are parallel-capable tracks, PRs within a workstream are sequential, and **one issue = one PR**. Each body carries a `## Sequence` section naming the full chain (`#A -> #B -> ...`) and its depends-on/blocks relationships. The umbrella tracker/epic issue (workstream table, closure ledger, maintainer-gated items, checklist) is repo-specific; see the repo notes at the end of this file.

### Body anatomy (required core)

Every verifiable issue has, in this order:

1. **Provenance preamble** (italic, first line): where the issue was generated from (audit run, session, plan), the exact commit every claim was re-checked against (full SHA), the one-issue-one-PR statement, and a disclaimer for any referenced local artifacts that are not in this repository.
2. `## Summary` — what will be true after this PR that is not true today. Outcome-framed, not task-framed.
3. `## Problem (claims to verify)` or `## Current state (evidence)` — numbered claims, each carrying:
   - a file:line reference, as a permalink anchored to the full SHA named in the preamble;
   - a code excerpt for load-bearing lines (the lines a fix will touch or a check will read);
   - an **epistemic label** — `VERIFIED` (re-checked at the anchor commit; say how) or `INFERENCE` (motivating but unproven) — so the implementer knows which premises still need proving;
   - a **prior-art reconciliation** note when a claim overlaps existing implementations (name them and state which one this PR extends rather than duplicates);
   - corrections discovered during review are **preserved inline** (`CORRECTED during review: ...`), never silently rewritten — the issue is a living record of what was claimed and what was verified.
4. `## Scope` — `In:` and `Out:` (out-of-scope items reference their owning issue when they have one).
5. `## Acceptance criteria` — the contract (next subsection). Non-negotiable.
6. `## Sequence` — programs only (above).

### The acceptance-criteria contract

Each criterion is a checkbox line in **EARS grammar**:

```
- [ ] ACn: WHEN <trigger>, THE SYSTEM SHALL <observable response>
```

- Sequential from `AC1`, nothing between the id and the colon, each AC appears exactly once. One AC binds to exactly **one** frozen check — never share a check across ACs.
- EARS patterns, in order of preference for this repo: **event-driven** (`WHEN <trigger>, THE SYSTEM SHALL <response>` — the default), **ubiquitous** (`THE SYSTEM SHALL <response>` — always-true properties; the natural shape for PRESERVING criteria), **unwanted-behavior** (`IF <undesired trigger>, THEN THE SYSTEM SHALL <response>` — error paths), **state-driven** (`WHILE <state>, THE SYSTEM SHALL <response>`), **optional-feature** (`WHERE <feature>, THE SYSTEM SHALL <response>`).
- The response must be **observable** — a test can assert it. "The system shall be more robust" is not a response.

Each AC carries exactly five sub-bullets:

```
- [ ] AC1: WHEN <trigger>, THE SYSTEM SHALL <observable response>.
  - class: DISCRIMINATING | NEW-SURFACE | PRESERVING
  - argv: <one runnable command from the repository root — the frozen check driver>
  - expect: <the signature the BASE leg shows before the fix>
  - files: <files the check reads or creates>
  - why: <why this is red at base — or green at both ends for PRESERVING>
```

| class | meaning | base leg | head leg |
|---|---|---|---|
| `DISCRIMINATING` | the defect exists today | **RED** (the failure signature) | GREEN |
| `NEW-SURFACE` | the fix creates a surface that cannot exist at base (new spec, new tool) | deterministic **absence** signature — the runner's literal no-matches output (e.g. vitest's `No test files found`, bun's `The following filters did not match any test files:`) | GREEN |
| `PRESERVING` | a regression floor | GREEN | GREEN |

Honest-class rules:

- Do not label an AC `DISCRIMINATING` unless it is genuinely red at the anchor commit — run the check mentally (or for real) against base before claiming it.
- A `NEW-SURFACE` AC must fail at base with a **deterministic** absence signature, never an environmental error (missing runtime, network). If the driver cannot exist at base, the driver itself prints a sentinel line and exits nonzero.
- `PRESERVING` criteria are regression floors: they must be green at base and stay green at head. A PRESERVING check that regresses at head is a fix bug, not a check bug.
- **No vacuous checks.** An AC shaped "do X and observe failure" that cannot fail on the primary dev/test runtime is a false contract — route the probe through a context where it can genuinely fail, or reshape the AC.

**NON-EXECUTABLE criteria.** Some criteria have no meaningful executable check — docs-only changes, host-only behavior (GUI/TUI surfaces a check cannot drive), maintainer product decisions, unavailable external services. For those, write `class: NON-EXECUTABLE(<reason>)` with the closed reason enum `DOCS_ONLY | HOST_ONLY | PRODUCT_DECISION | EXTERNAL_SERVICE_UNAVAILABLE`, replace `argv`/`expect` with **named substitute evidence** (a review artifact, a decision recorded on the issue, a captured external state), and get plan-critic approval for the substitution — never force an artificial executable check; that is the vacuous shape the rule above forbids.
- `argv` is one command, runnable from the repository root, POSIX-safe, deterministic (use the repo's test-runner flags for hermetic runs — see repo notes). `expect` describes what the **base** leg shows; the head leg's pass condition lives in the check's own exit logic.

### Evidence rules

- Every claim is anchored: permalinks embed the full SHA; before filing, re-verify each claim at that commit and say so in the preamble.
- Quotes and excerpts must exist verbatim in the cited file at the cited lines — grep each one before filing (a fabricated quote propagates into every downstream ledger).
- When line numbers matter, confirm the cited file is unchanged between the anchor commit and filing (or re-anchor).
- Label every non-trivial claim `VERIFIED` or `INFERENCE`.

### Extended-section menu (recommended for design-heavy and program issues)

The core is the required minimum. For issues whose fix has real design surface, add any of these (they **inform**, the ACs **bind** — if a Design section and an AC disagree, the AC wins and the Design section gets corrected):

`## Why (evidence)` · `## Current behaviour` · `## Dependencies` (`Depends on:` / `Gated on:` / `Blocks:`) · `## Design` (numbered, with exact signatures) · `## Files to change` · `## Tests` (named spec files) · `## Fixtures to add` · `## Local verification recipe` (commands) · `## Release rules` · `## Definition of done`

### Filing checklist (self-review before Create)

- [ ] Preamble names the source, the anchor SHA, and re-verification
- [ ] Every claim has a full-SHA permalink; every quote greps clean at that SHA; VERIFIED/INFERENCE labeled
- [ ] Scope has both In and Out
- [ ] ACs sequential, each exactly five sub-bullets, one-AC-one-check
- [ ] Classes honest (DISCRIMINATING genuinely red at base; NEW-SURFACE deterministic absence; PRESERVING green-both-ends)
- [ ] No vacuous checks; argv runnable from repo root; expect describes the base leg
- [ ] Program issues: title `[Workstream <letter>] PR n of m`, Sequence section, one issue = one PR
- [ ] Any NON-EXECUTABLE criterion carries a closed-enum reason and named substitute evidence (not a forced check)
- [ ] Follow-up issues: passed the necessity gate below, collated into at most two issues per source PR (more needs the maintainer's go-ahead, Priority items excepted), titled `Follow-up from PR #N: <theme>`

### Filing follow-up issues

This section covers work found during a PR review or a PR-feedback round that the PR does not fix. Agents and contributors handle it the same way. Issue-tracer closeouts keep their own full-resolution contract (the issue-tracer skill's `references/full-resolution-contract.md`): a separable concern there is fixed or filed with the user's acknowledgment, and this section does not waive any of its clauses. Collation and the title convention below still apply to issues filed from a trace. Anything the PR introduced is fixed in the PR, not routed here; a small pre-existing item inside the PR's files and scope is fixed only with the maintainer's go-ahead. Nothing is silently dropped. Equally, not every leftover becomes an issue: most review findings are never acted on, and filing all of them turns review noise into backlog noise.

**1. Give every unfixed item exactly one disposition.**

| Disposition | When |
|---|---|
| `ABSORB` | An open issue already covers it. Comment on that issue with the new evidence instead of opening a new one. |
| `FILE` | It passes the necessity gate (step 2) and is Priority, or the maintainer approved it. |
| `RECORD` | It fails gate 1 or gate 2, or the maintainer declined it. Add a row to the PR body's "Considered, not filed" table (inside `## Summary`) with: the item, the reason, which gate test failed, and a re-evaluation trigger. This is a complete closure. |
| `ASK` | It needs a product or maintainer decision, it fails only gate 3, or it is an Improvement item awaiting approval. Ask before filing anything; a decline makes it `RECORD` (quote it); an approval makes a gate-3 or Improvement item `FILE`, lands a gate-4 fix in the PR, and sends an unverified item back to investigation. |

`ASK` takes precedence over fixing in the PR when the fix itself needs a product or compatibility decision. `ABSORB` takes precedence over `RECORD` when an open issue already covers the item.

**2. Apply the necessity gate.** `FILE` only when all four hold, or when the maintainer approves an `ASK` proposal. Failing gate 1 or 2 is `RECORD`, except that an unverified item claiming a wedge, fail-open gate, security, or data-loss consequence is `ASK`. Failing only gate 3 makes the item an Improvement proposal (`ASK`). Failing gate 4 means `ABSORB` (already tracked) or `ASK` to fix it in the PR (fixable here), never `RECORD` without the maintainer's decline. A wedge, fail-open gate, security, or data-loss consequence satisfies gate 3.

1. **Verified.** An executed probe or an exact `path:line` source trace proves it on the current head. An `INFERENCE` is never filed.
2. **Consequential.** There is a concrete, reachable trigger with a correctness, safety or security, data-loss, or workflow-blocking consequence. Coverage gaps, docs, wording, and "could harden" items fail unless they hide a real defect.
3. **Material.** It is on a hot path, invalidates a gate or contract, or blocks planned work.
4. **Untracked and not fixable here.** No open issue covers it, and it cannot be fixed in the PR without leaving the PR's files or scope.

**3. Collate the survivors.**

- All `FILE` items from one PR go into one issue titled `Follow-up from PR #<N>: <theme>`, in the executable-contract format above, with at least one acceptance criterion per item, each bound to its own check.
- Split into a second issue only when the items genuinely need separate PRs. A HIGH-severity item (fail-open gate, security, data loss) may stand alone.
- More than two follow-up issues from one PR needs the maintainer's go-ahead before filing (Priority items excepted). A standalone HIGH item counts toward the two. Priority items over the budget are still filed, and the overflow is reported.

**4. Respect the two tiers.**

- **Priority** items (a wedge, fail-open gate, security, or data loss) are filed after the closeout review, once the push is published.
- **Improvement** items that pass the gate are proposed as a list in the final response to the maintainer and filed only once the maintainer approves.
- Findings about how the review or feedback round was conducted, such as an incomplete ledger or a missed disposition, are `RECORD` unless they block a workflow. This never covers defects in any code, skill text, or gate, changed by the PR or not: PR-introduced ones are fixed in the PR, and a fail-open gate is Priority.

**5. Keep the record true.** A disposition is planned before the closeout review and executed after the push. The published record states it only after it is true on the live artifact: the issue is filed, the comment is posted, or the table row is in the PR body. The swarm-pr-feedback ledger form of this rule is the swarm-pr-feedback skill's `references/followup-dispositions.md`.

### Repo notes (zmem)

- **This is the format's origin repo**: the extended-section menu below (Design/Files/Tests/Fixtures/Dependencies/DoD) is native here — keep using it for design-heavy issues. The per-AC `argv/expect` binding is the new required element going forward; existing open issues are grandfathered.
- **Tracker convention**: program trackers are titled `[EPIC] ...` (e.g. Workstreams P-S, T); issue labels use `severity:high|medium|info|low` plus type labels (bug/documentation/enhancement).
- **argv conventions for this repo**: `python -X utf8 skills/memory/scripts/store.py <subcommand>` for CLI-surface checks; pytest for suite checks; lightweight runs under the 512 MiB guard use `python -X utf8 -S` (see the degraded-lexical-mode lesson — never report a guarded-import failure as a pass).
- A `NEW-SURFACE` argv naming a spec absent at base fails with pytest's file-not-found class — write the real signature verbatim in `expect`.
