from contextlib import redirect_stderr
import io

import _store_guard  # noqa: F401

from _issue546_helpers import SCRIPTS


def test_release_failure_is_reported_and_returns_failure(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    from storelib import schema

    host = schema._host
    path = tmp_path / "schema.lock"
    path.write_text("token", encoding="utf-8")
    attempts = []

    def deny_rename(*args):
        attempts.append(args)
        error = PermissionError("lock is still open")
        error.winerror = 32
        raise error

    monkeypatch.setattr(host, "_RELEASE_RETRY_IS_WINDOWS", True)
    monkeypatch.setattr(host.os, "rename", deny_rename)
    assert host.release_lock(path, "token") is False
    assert len(attempts) == host._RELEASE_RETRY_ATTEMPTS
    assert path.read_text(encoding="utf-8") == "token"

    monkeypatch.setattr(schema, "_lock_path", lambda name: path)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        assert schema._release_named_lock("schema", "token") is False
    lines = stderr.getvalue().splitlines()
    assert len(lines) == 1
    assert "schema" in lines[0].lower() and "release" in lines[0].lower()

    path.write_text("successor-token", encoding="utf-8")
    before = len(attempts)
    stderr = io.StringIO()
    with redirect_stderr(stderr):
        assert schema._release_named_lock("schema", "token") is True
    assert len(attempts) == before
    assert stderr.getvalue() == ""
    assert path.read_text(encoding="utf-8") == "successor-token"
