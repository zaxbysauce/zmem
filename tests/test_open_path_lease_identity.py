import os
import sqlite3
import sys

import _store_guard  # noqa: F401
import pytest

from _issue546_helpers import SCRIPTS

sys.path.insert(0, str(SCRIPTS))

from storelib import schema


def test_restore_only_discards_a_proven_stale_open_lease(tmp_path, monkeypatch):
    monkeypatch.setattr(schema, "STORE_PATH", tmp_path / "store.sqlite")
    monkeypatch.setattr(schema, "SCHEMA_LOCK_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(schema._host, "process_alive", lambda pid: True)
    monkeypatch.setattr(schema._host, "process_birth_identity", lambda pid: "w0000000000000002")

    live = schema._acquire_open_lease("stats")
    stale = live.with_name(live.name.replace("w0000000000000002", "w0000000000000001"))
    stale.touch()
    legacy = live.with_name(f"{os.getpid()}-legacy.lease")
    legacy.touch()

    assert schema._wait_for_open_connections() is False
    assert live.is_file() and legacy.is_file()
    assert not stale.exists()
    live.unlink()
    assert schema._wait_for_open_connections() is False
    legacy.unlink()
    assert schema._wait_for_open_connections() is True


def test_current_process_birth_identity_on_supported_hosts():
    if os.name == "nt" or sys.platform.startswith("linux"):
        identity = schema._host.process_birth_identity(os.getpid())
        if os.name == "nt" or os.path.isfile("/proc/sys/kernel/random/boot_id"):
            assert identity is not None
        assert schema._host.process_birth_identity(os.getpid()) == identity


def test_failed_embedding_check_closes_connection_and_releases_open_lease(tmp_path, monkeypatch):
    from storelib import cli

    monkeypatch.setattr(schema, "STORE_PATH", tmp_path / "store.sqlite")
    monkeypatch.setattr(sys, "argv", [
        "store.py", "add", "--content", "fixture", "--namespace", "project:fixture", "--type", "fact",
    ])
    monkeypatch.setattr(cli, "_prepare_store", lambda conn: None)
    monkeypatch.setattr(cli, "_auto_near_miss_rekey", lambda conn, **kwargs: None)

    def fail_check(conn, **kwargs):
        assert len(list(schema._open_lease_dir().glob("*.lease"))) == 1
        raise KeyError("embedding check failed unexpectedly")

    monkeypatch.setattr(cli, "assert_embedding_compatible", fail_check)
    conn = sqlite3.connect(":memory:")
    monkeypatch.setattr(cli, "connect", lambda: conn)
    try:
        with pytest.raises(KeyError, match="embedding check failed unexpectedly"):
            cli.main()
        assert list(schema._open_lease_dir().glob("*.lease")) == []
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            conn.execute("SELECT 1")
    finally:
        conn.close()
