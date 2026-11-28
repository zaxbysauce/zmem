import sqlite3

import _store_guard  # noqa: F401
import pytest

from _issue546_helpers import SCRIPTS


@pytest.mark.parametrize("released", [False, True])
def test_store_open_event_requires_schema_release(monkeypatch, released):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from storelib import schema

    events = []
    monkeypatch.setattr(schema, "_acquire_schema_lock_with_liveness", lambda **_: "test-token")
    monkeypatch.setattr(schema, "_release_named_lock", lambda *args: released)
    monkeypatch.setattr(schema, "_check_connection_schema_version", lambda *args: None)
    monkeypatch.setattr(schema, "init_db", lambda conn: None)
    monkeypatch.setattr(schema, "migrate", lambda conn: None)
    monkeypatch.setattr(schema, "_record_store_open", lambda *args: events.append(args))

    conn = sqlite3.connect(":memory:")
    try:
        schema._prepare_store(conn)
    finally:
        conn.close()
    assert len(events) == int(released)
