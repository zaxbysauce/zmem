# Training exporter fixtures

This directory contains a small tracked contract fixture for the governed
training exporter. `cases.json` describes an isolated SQLite store with one
reviewer correction pair, one exact duplicate pair, and one semantic duplicate
pair. The fixture builder seeds that store and invokes the production exporter;
it does not hand-write the expected rows.

Regenerate all goldens from the repository root with:

    python tests/fixtures/training/build_fixtures.py --output tests/fixtures/training

The focused golden test seeds the same input independently, runs the exporter,
and byte-compares `sft-000.parquet`, `preferences-000.parquet`, and
`deletion-map.json` with the tracked `expected-*.parquet` and
`expected-deletion-map.json` files. The readable JSON projections are also
checked against the Parquet rows. A PyArrow writer-version upgrade may change
Parquet bytes; regenerate these files with the command above, inspect the
projection diff and deletion map, and commit the updated binary goldens with
the dependency change.
