# Counterbalanced Reviewer Passes (issue #2906)

The review protocol presents candidate findings to the reviewer phase in two
independent batches over the same full inventory whenever that inventory holds
more than one item. The controller assigns presentation orders to those
batches deterministically — the first reviewer batch in the window renders its
candidates in canonical (lexicographically sorted) order (`presentation_order:
A`) and the second renders the exact reverse (`presentation_order: B`),
alternating for any further batches. A retry batch receives the NEXT ordinal,
so a re-dispatch of a failed pass observes the opposite order by design.

## Where the signal lives

- **Prompt**: each reviewer lane's controller contract carries the
  `presentation_order:` line and renders `assigned_item_ids` in that pass's
  order; the lane is instructed to order its candidate prose identically (the
  mechanical reordering covers the controller-owned id list — prose-order
  compliance is instruction-mediated).
- **Receipts**: the per-batch coherence record stamps `presentationOrder`
  ('A' | 'B'), and the lane collection receipt footer names it when gate state
  is readable at collection time.
- **Merged verdicts**: every composed reviewer claim carries `order_agreement`
  — `agree` when both the A and B passes produced the same
  (classification, severity) pair for the item, `disagree` when the verdict
  flipped with presentation order, `single` when fewer than two marked orders
  observed the item (including single-pass runs and pre-#2906 batches).
- **Critic routing**: order-unstable items are named on the critic contract's
  `order_unstable_item_ids:` line so the critic prioritizes them; the critic
  batch's coherence record keeps an audit-only snapshot of that list.
- **Findings and report**: findings rows carry `order_agreement` (validated
  against the composed verdicts at write time — a contradictory or unbacked
  affirmative label is rejected); the rendered PR-review report marks
  order-unstable findings with the `order-unstable` token and counts them in a
  summary line.

## How to read the labels

- `agree` — order-stable: both passes reached the same classification and
  severity. The strongest reviewer signal.
- `disagree` — order-unstable: the verdict flipped with presentation order.
  Not automatically wrong — it means the finding's verdict is sensitive to
  candidate ordering and deserves critic priority and explicit evidence.
- `single` — only one presentation order observed the item; no order-effect
  evidence either way.

A single-item inventory (or single-item lane) renders identical A and B
sequences; its labels remain computed but are descriptive only — they cannot
implicate order effects at n=1.

## Operator notes

- The presentation-order ordinal counts retained reviewer batches in the
  reviewer window (batches after the latest council batch), excluding the
  batch being recorded. Both the dispatch-time derivation and the record
  guard apply the SAME pure capacity prune before counting, so at the
  ≥128-batch cap the derived ordinal already reflects the post-prune window
  and the two sides cannot disagree about prune parity. A dispatch whose
  derived order still diverges from its recorded receipt — state genuinely
  changed between derivation and recording — fails closed; re-derive and
  re-dispatch. That retry cannot wedge: the derivation is deterministic over
  the current state, so it re-derives exactly the value the guard will stamp.
- The capacity prune never drops a reviewer batch that contributed order
  observations: an older counterbalanced pass is retained even when the newer
  pass claimed every item (its observations ride the agreement-only
  continuation), so `agree`/`disagree` labels survive GC and never silently
  degrade to `single`.
- The wire `[REVIEWED]` row grammar is unchanged: presentation order is a
  property of the pass (batch), not of any individual row.
