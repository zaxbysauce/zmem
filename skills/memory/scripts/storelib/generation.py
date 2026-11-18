"""Store mutation generation + incarnation (issue #474).

The recall reuse layer keys cached work on an OBSERVABLE store mutation
generation: a monotonic counter maintained INSIDE the existing write
transactions (no new writer lock surface), bumped by every writer that
changes recall selection. Readers only ever SELECT — an absent key reads as
``0`` and NEVER triggers an INSERT on the read path (read-only / query_only
connections must stay writable-free).

The store INCARNATION breaks aliasing across wholesale store replacement
(backup restore, import-store staged swaps): those paths copy a snapshot
over the destination OUTSIDE any writer transaction, so a restored store can
carry an older — or absent — generation that would otherwise collide with
entries cached from the previous store incarnation. Replacement paths rotate
the incarnation TRANSACTIONALLY ON THE STAGED COPY BEFORE the atomic install:
a crash before install discards the staging (the old store and its cached
entries stay valid); a crash after install already carries the new value.

Table of contents:
- read_mutation_generation(conn)     -> str   (SELECT only; absent = "0")
- bump_mutation_generation(conn)     -> None  (call INSIDE a write txn)
- read_store_incarnation(conn)       -> str   (SELECT only; absent = "")
- rotate_incarnation_on_staging(path)-> str   (staged copy, pre-install)
"""

from __future__ import annotations

import os
import sqlite3
import uuid

GENERATION_KEY = "mutation_generation"
INCARNATION_KEY = "store_incarnation"


def read_mutation_generation(conn: sqlite3.Connection) -> str:
    """Read the mutation generation. SELECT-only: never writes, so it is
    safe on read-only connections and never itself invalidates reuse."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (GENERATION_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return "0"
    if not row or row[0] is None:
        return "0"
    return str(row[0])


def bump_mutation_generation(conn: sqlite3.Connection) -> None:
    """Advance the mutation generation. MUST be called inside an existing
    write transaction so the bump commits atomically with the mutation it
    describes (callers own the transaction; this helper never begins one).

    Uses UPSERT so the first bump on a fresh store needs no prior INSERT —
    and so concurrent first-bumps inside serialized transactions cannot
    fail. Failures propagate: a bump that cannot run inside a write
    transaction is a caller bug, not a degraded-ok state.
    """
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, '1') "
        "ON CONFLICT(key) DO UPDATE SET value = "
        "CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = ?",
        (GENERATION_KEY, GENERATION_KEY),
    )


def read_store_incarnation(conn: sqlite3.Connection) -> str:
    """Read the store incarnation (SELECT only). Empty string for a store
    that has never been wholesale-replaced — stable for its lifetime."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (INCARNATION_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return ""
    if not row or row[0] is None:
        return ""
    return str(row[0])


def rotate_incarnation_on_staging(staging_path) -> str:
    """Rotate the incarnation on a STAGED copy of the store, BEFORE the
    atomic install that replaces the live store.

    Opens the staging file in its own connection, sets a fresh uuid4
    incarnation, commits, and closes — a crash before the install leaves the
    live store (and every cached identity bound to it) untouched; after the
    install the new store already answers with the new incarnation, so no
    post-install window exists. Returns the new incarnation value.
    """
    new_value = uuid.uuid4().hex
    conn = sqlite3.connect(os.fspath(staging_path))
    try:
        # A staged copy may not yet carry the meta table (import lanes can
        # stage from a subset schema before the destination schema is
        # completed); create it idempotently with the canonical DDL so the
        # rotation never depends on the staging pipeline's intermediate
        # shape. (Found by tests/test_import_ns_control.py: the sqlite
        # import lane's staging lacked meta at rotation time.)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value "
            "WHERE key = ?",
            (INCARNATION_KEY, new_value, INCARNATION_KEY),
        )
        conn.commit()
    finally:
        conn.close()
    return new_value
