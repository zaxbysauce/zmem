import importlib.util
from pathlib import Path

import _store_guard  # noqa: F401


def _load_hook():
    path = Path(__file__).resolve().parents[1] / "hooks" / "lib" / "zmem-recall-body.py"
    spec = importlib.util.spec_from_file_location("zmem_recall_body_issue546", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_store_child_lock_wait_fits_inside_remaining_hook_budget(monkeypatch):
    hook = _load_hook()
    captured = {}

    def fake_check_output(command, **kwargs):
        captured.update(kwargs)
        return b"{}"

    def assert_budget(timeout):
        env = captured["env"]
        schema_wait = float(env["ZMEM_SCHEMA_LOCK_WAIT_SECONDS"])
        maintenance_wait = float(env["ZMEM_MAINTENANCE_WAIT_SECONDS"])
        assert captured["timeout"] == timeout
        assert schema_wait >= 0 and maintenance_wait >= 0
        assert schema_wait + maintenance_wait <= timeout - min(0.50, timeout / 4) + 1e-9
        return schema_wait, maintenance_wait

    monkeypatch.setattr(hook.subprocess, "check_output", fake_check_output)
    monkeypatch.setenv("ZMEM_SCHEMA_LOCK_WAIT_SECONDS", "15")
    monkeypatch.setenv("ZMEM_MAINTENANCE_WAIT_SECONDS", "5")
    for timeout in (8.0, 1.0, 0.25):
        hook._run_store("store.py", ["recall"], timeout=timeout)
        assert_budget(timeout)

    monkeypatch.setattr(hook, "_store_timeout_s", lambda: 1.2)
    hook._run_store("store.py", ["recall"], timeout=None)
    assert_budget(1.2)

    monkeypatch.setenv("ZMEM_SCHEMA_LOCK_WAIT_SECONDS", "0.1")
    monkeypatch.setenv("ZMEM_MAINTENANCE_WAIT_SECONDS", "0.2")
    hook._run_store("store.py", ["recall"], timeout=8.0)
    assert assert_budget(8.0) == (0.1, 0.2)
