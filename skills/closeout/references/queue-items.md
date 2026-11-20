# Queue items that need extra care

Read this only when `queue-list` shows an item with `source: "history-mine"`,
`kind: "error_pattern"`, or `source: "closeout-note"`.

## Mined corrections

`kind: "correction"` with `source: "history-mine"` is a user correction
recovered from an older transcript by `mine-history`. Judge it exactly like a
live correction. Its `occurrences` field counts the transcripts that contained
the same message; that is context, not evidence. Save with `--signal user`.

## Error patterns

`kind: "error_pattern"` is a tool error that recurred across sessions, grouped
by `error_type` and project folder. It is not a lesson yet.

- Treat `suggested_guideline` as a starting draft. Rewrite it with the trigger
  you can actually name: "when X fails with <error_type>, do Y".
- Save it as `--type lesson`.
- A repeated error is not proof of the fix. Use `test`, `compile` or `lint`
  only if that tool ran in this session and supports the lesson; otherwise
  `none`.
- `review_priority` orders the queue. It is not the row's confidence and must
  not influence the signal you choose.

## Closeout notes

`source: "closeout-note"` is a note an earlier closeout left for review with:

```bash
<store> queue-add --namespace "<namespace>" --message "<note>" --type lesson
```

It gets no fast track: apply the same bar as any other item. Use `queue-add`
yourself only for something worth a later look that does not meet the bar
today.

## What is never queued

Rejections that `mine-history` reports are context for judging whether a tool
is being misused. They are not queue items, and `mine-history` never writes to
the store.
