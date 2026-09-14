# Host-lane decision attribution

## What changed

Injection decision logs now preserve the host lane (`claude`, `codex`, `zcode`,
or the Hermes lanes), release version, and measured store-attempt time. The
classifier distinguishes `already-delivered` from an actually empty candidate
pool, and miss/false-injection reports expose a deterministic lane-by-moment
matrix while retaining legacy aggregate data.

## Why

Telemetry previously collapsed delivery-ledger exclusions and omitted the
runtime lane and timing fields, making cross-host diagnosis and attribution
unreliable.

## Migration

No migration is required. Existing decision lines remain parseable; enriched
fields are additive. Persistent schema version 13 is unchanged.

## Breaking changes

None.

## Known caveats

The reserved `expired` reason has no producer yet. `subagent` and
`session_start_compact` remain aggregate-only report moments by design.
