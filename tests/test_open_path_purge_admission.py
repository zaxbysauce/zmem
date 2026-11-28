import _store_guard  # noqa: F401

from _issue546_helpers import SCRIPTS


def test_purge_drains_admitted_connection_before_taking_schema(monkeypatch, tmp_path, capsys):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from storelib import purge, schema

    store = tmp_path / "store.sqlite"
    monkeypatch.setattr(schema, "STORE_PATH", store)
    monkeypatch.setattr(schema, "SCHEMA_LOCK_WAIT_SECONDS", 0.0)
    schema_probes = []

    def refuse_schema(**kwargs):
        assert schema._lock_path("maintenance").is_file()
        schema_probes.append(kwargs)
        return None

    monkeypatch.setattr(purge, "_acquire_schema_lock_with_liveness", refuse_schema)
    admitted = schema._acquire_open_lease("add")
    try:
        assert schema._cleanup_stale_writer_leases() == []
        assert purge.cmd_purge(ids=["fixture"]) == 4
        assert "a store connection is active" in capsys.readouterr().err
        assert schema_probes == []
        assert not schema._lock_path("maintenance").exists()
        assert not store.exists()

        writer = schema._acquire_writer_lease("add", open_lease=admitted)
        try:
            assert purge.cmd_purge(ids=["fixture"]) == 4
            assert "a normal writer is currently active" in capsys.readouterr().err
            assert schema_probes == []
        finally:
            schema._release_writer_lease(writer)
    finally:
        schema._release_writer_lease(admitted)

    assert purge.cmd_purge(ids=["fixture"]) == 4
    assert "store initialization or migration is running" in capsys.readouterr().err
    assert len(schema_probes) == 1
    assert not schema._lock_path("maintenance").exists()
    assert not store.exists()
