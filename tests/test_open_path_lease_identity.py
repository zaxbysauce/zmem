import os
import sys

import _store_guard  # noqa: F401

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
