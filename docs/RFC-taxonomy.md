# Taxonomy RFC — Workstream K PR 1 of 8 (issue #172)

## Status

This RFC reopens #126's six-type rule and records one decision per taxonomy
candidate, as required by issue #172. It is documentation plus experiment
evidence only: no production schema change, no runtime behavior change, no
store write, and no release-version bump ship with it. The `schema_version: 1`
below is the RFC document version, not a database schema version
(`SUPPORTED_SCHEMA_VERSION` is unchanged).

## Experiment

Each candidate is judged against the #155 replay snapshot using the
contract's three-way rule: `accept` only when the replay and the insufficiency
statement prove the claim; `reject` when the replay shows tags or a side table
is sufficient; `defer` when the required evidence or migration decision is not
available. The evidence is the committed #155 fixture set
(`tests/fixtures/replay/store.sqlite`, `decisions.log`, `expected.json`); the
input digest is sha256 over the raw snapshot bytes followed by the raw
decision-log bytes with no separator, and it equals the `input_digest` recorded
in `expected.json`.

Read-only inspection of the snapshot (a temp copy opened with sqlite3
`mode=ro`; the committed fixture is never opened for writing) shows:

- `memory`: 70 rows, types `fact` (64) and `lesson` (6) only; temporal columns
  `valid_from` (70 nonempty), `valid_until` (15), `update_of` (3),
  `superseded_at` (15) carry the snapshot's two-step as-of release-policy
  transitions.
- `entity`: 25 rows across the current five kinds — `project` (10), `other`
  (10), `tool` (4), `person` (1) — with aliases in `entity_alias` (25 rows),
  `contradicts` relations in `memory_link` (16 rows), and membership in
  `memory_entity` (180 rows).
- `evidence`, `memory_evidence`, `episode`, `episode_evidence`: 0 rows — no
  provenance chain or episode workload exists in this snapshot.
- `decisions.log`: 8 ordered lane-by-moment injection decisions; the replay
  report is an 8-row delivery measurement, not a taxonomy workload. No as-of
  bucket, event query, runbook procedure, entity-card query, or observation
  provenance query is exercised.

## Decisions

The decisions below are the literal product of that evidence. There are no
`accept` decisions: nothing in an injection-delivery measurement proves any
candidate's insufficiency clause, and the contract forbids accepting on any
lesser showing. Each rationale keeps the decisive fact explicit so the
reject-versus-defer boundary stays auditable.

- `state` — **defer**. The replay preserves its as-of transitions through
  temporal columns on `memory` itself; columns are neither tags nor a side
  table, so the claim's named alternative was never exercised. Necessity is
  unproven (not accept) and the alternative's sufficiency is untested (not
  reject); the schema-15 migration decision is owned by #173/#174.
- `event` — **defer**. Zero event-shaped memory rows exist and the report
  measures no ordered-occurrence query; the decision log's own ordered
  timestamped rows are the replay input, not a stored workload whose
  preservation could be tested. Required evidence unavailable.
- `observation` — **defer**. The evidence tables are empty (0 rows), so no
  sourced-measurement-with-provenance workload is exercised and neither
  necessity nor sufficiency can be shown. Provenance design belongs to the
  #137 handoff.
- `runbook` — **defer**. No ordered executable procedure with preconditions
  exists anywhere in the snapshot or report; required evidence unavailable.
- `entity_card` — **reject**. The claim is directly falsified by the
  snapshot's own tables: all 25 aliases live in the `entity_alias` side
  table, all 16 `contradicts` relations in `memory_link`, all 180 membership
  links in `memory_entity`. Aliases and relationships are preserved by side
  tables today, so tags plus a side table are sufficient.
- `host` — **reject**. 0 of the 25 entities is host-shaped; the existing five
  kinds absorbed the whole snapshot workload with no residue and no
  preservation failure.
- `service` — **reject**. 0 of the 25 entities is service-shaped; same
  sufficiency evidence.
- `model` — **reject**. 0 of the 25 entities is model-shaped; same
  sufficiency evidence.
- `repo` — **reject**. 0 of the 25 entities is repo-shaped; the `eval-*`
  names are `project`-kind and preserved exactly.
- `file` — **reject**. 0 of the 25 entities is file-shaped; same sufficiency
  evidence.
- `command` — **reject**. The command-like names `pytest`, `rg`, `fzf`, and
  `hyperfine` are `tool`-kind with exact canonical names and alias rows
  preserved; no query needing a command-versus-tool distinction failed.
  Classification preference is not preservation failure.
- `error_class` — **reject**. 0 of the 25 entities is error-class-shaped;
  `prompt-injection-risk` and its peers are `other`-kind abstract concepts,
  preserved exactly.

Every candidate's `migration` is therefore `none in #172`. The `handoff`
mapping is exact and decision-independent: later implementation is gated to
#173/#174 (`state`), #178 (`event`), #137 (`observation`), #138 (`runbook`),
#175/#179 (`entity_card`), and #175 (the seven entity kinds). A future
revision of this contract may evaluate further candidates — for example an
`environment` type proposed in the issue discussion — but this artifact
covers exactly the twelve candidates the contract fixes.

## Evidence artifact

The sole machine-readable decision object follows. Its `replay_command` for
each candidate is the exact recorded command that projects the #155 evidence
into `tests/fixtures/taxonomy/delta/<candidate>.json`; all twelve commands are
deterministic and read-only with respect to the replay inputs.

```json
{
  "schema_version": 1,
  "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
  "candidates": [
    {
      "name": "state",
      "claim": "A dedicated state type is necessary only if the #155 replay shows an as-of state transition or latest-state invariant that tags plus a side table cannot preserve without losing validity semantics.",
      "tags_or_side_tables_insufficient": "Insufficiency: A dedicated state type is necessary only if the #155 replay shows an as-of state transition or latest-state invariant that tags plus a side table cannot preserve without losing validity semantics.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate state --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/state.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/state.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "defer",
      "handoff": "#173 and #174"
    },
    {
      "name": "event",
      "claim": "A dedicated event type is necessary only if the #155 replay shows an ordered immutable occurrence query that tags plus a side table cannot preserve with event time and identity.",
      "tags_or_side_tables_insufficient": "Insufficiency: A dedicated event type is necessary only if the #155 replay shows an ordered immutable occurrence query that tags plus a side table cannot preserve with event time and identity.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate event --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/event.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/event.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "defer",
      "handoff": "#178"
    },
    {
      "name": "observation",
      "claim": "A dedicated observation type is necessary only if the #155 replay shows a sourced measurement with observation time and provenance that tags plus a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: A dedicated observation type is necessary only if the #155 replay shows a sourced measurement with observation time and provenance that tags plus a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate observation --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/observation.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/observation.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "defer",
      "handoff": "#137"
    },
    {
      "name": "runbook",
      "claim": "A dedicated runbook type is necessary only if the #155 replay shows an ordered executable procedure with preconditions that tags plus a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: A dedicated runbook type is necessary only if the #155 replay shows an ordered executable procedure with preconditions that tags plus a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate runbook --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/runbook.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/runbook.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "defer",
      "handoff": "#138"
    },
    {
      "name": "entity_card",
      "claim": "A dedicated entity_card type is necessary only if the #155 replay shows a canonical entity summary with aliases and relationships that tags plus a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: A dedicated entity_card type is necessary only if the #155 replay shows a canonical entity summary with aliases and relationships that tags plus a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate entity_card --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/entity_card.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/entity_card.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "reject",
      "handoff": "#175 and #179"
    },
    {
      "name": "host",
      "claim": "A host entity kind is necessary only if the #155 replay shows host-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: A host entity kind is necessary only if the #155 replay shows host-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate host --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/host.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/host.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "reject",
      "handoff": "#175"
    },
    {
      "name": "service",
      "claim": "A service entity kind is necessary only if the #155 replay shows service-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: A service entity kind is necessary only if the #155 replay shows service-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate service --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/service.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/service.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "reject",
      "handoff": "#175"
    },
    {
      "name": "model",
      "claim": "A model entity kind is necessary only if the #155 replay shows model-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: A model entity kind is necessary only if the #155 replay shows model-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate model --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/model.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/model.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "reject",
      "handoff": "#175"
    },
    {
      "name": "repo",
      "claim": "A repo entity kind is necessary only if the #155 replay shows repository-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: A repo entity kind is necessary only if the #155 replay shows repository-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate repo --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/repo.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/repo.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "reject",
      "handoff": "#175"
    },
    {
      "name": "file",
      "claim": "A file entity kind is necessary only if the #155 replay shows file-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: A file entity kind is necessary only if the #155 replay shows file-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate file --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/file.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/file.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "reject",
      "handoff": "#175"
    },
    {
      "name": "command",
      "claim": "A command entity kind is necessary only if the #155 replay shows command-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: A command entity kind is necessary only if the #155 replay shows command-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate command --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/command.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/command.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "reject",
      "handoff": "#175"
    },
    {
      "name": "error_class",
      "claim": "An error_class entity kind is necessary only if the #155 replay shows error-class-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "tags_or_side_tables_insufficient": "Insufficiency: An error_class entity kind is necessary only if the #155 replay shows error-class-specific identity or relationships that the existing kinds plus tags or a side table cannot preserve.",
      "replay_command": "python tests/fixtures/taxonomy/replay_candidate.py --candidate error_class --snapshot tests/fixtures/replay/store.sqlite --decisions tests/fixtures/replay/decisions.log --expected tests/fixtures/replay/expected.json --out tests/fixtures/taxonomy/delta/error_class.json",
      "input_digest": "8529a1210681f53b602ad32070a88c97d84402d419bf136a040f1842b3e367b0",
      "observed_delta": "tests/fixtures/taxonomy/delta/error_class.json",
      "migration": "none in #172",
      "compatibility": "unchanged: no database schema or runtime behavior changes in #172",
      "rollback": "delete the RFC and delta artifacts; no store rollback required",
      "decision": "reject",
      "handoff": "#175"
    }
  ]
}
```

