"""Agent surfaces can create and update state memories (#326).

The #173 stopgap de-advertised `state` on hermes zmem_add and MCP add because
neither surface took the mandatory expiry params. This driver pins the real
wiring end to end: hermes add/update, REAL MCP add/update (driven through
build_server + _tool_manager.call_tool, the test_session_tools pattern),
apply_ops add/update carrying expiry through the ops applier (the #405-round
silent-drop regression), the remote transport forwarding both fields, and the
boundary refusals for a state add without expiry_class.

CI runs this file directly (no pytest); the __main__ block executes every
test so a plain `python tests/test_agent_state_writes.py` is a real gate.
"""
import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _store_guard  # noqa: F401  (issue #244 isolation guard)

NS = "project:github.com/zaxbysauce/zmem"


def _load_under(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class AgentStateWritesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.mkdtemp(prefix="zmem-agent-state-")
        cls._saved = {k: os.environ.get(k) for k in (
            "ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MCP_TOKEN",
            "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
            "ZMEM_REMOTE_URL", "ZMEM_MCP_URL",
        )}
        os.environ["ZMEM_HOME"] = str(REPO_ROOT)
        os.environ["ZMEM_STORE"] = os.path.join(cls._tmp, "store.sqlite")
        os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        os.environ["ZMEM_MODELS_DIR"] = os.path.join(cls._tmp, "no-models")
        os.environ["ZMEM_MCP_TOKEN"] = "agent-state-test-token"
        os.environ.pop("ZMEM_DATA", None)
        # A developer box with remote mode configured would silently reroute
        # the hermes legs to a hub (PRR-020).
        os.environ.pop("ZMEM_REMOTE_URL", None)
        os.environ.pop("ZMEM_MCP_URL", None)
        cls.store_path = os.environ["ZMEM_STORE"]

        # -- hermes plugin under stub agent modules (house pattern) --------
        vendor = REPO_ROOT / "hermes-plugin" / "vendor" / "agent"
        agent = types.ModuleType("agent")
        sys.modules["agent"] = agent
        try:
            provider = _load_under("agent.memory_provider",
                                   vendor / "memory_provider.py")
            provider.MemoryProvider
        except Exception:
            provider = types.ModuleType("agent.memory_provider")

            class MemoryProvider:
                pass
            provider.MemoryProvider = MemoryProvider
            sys.modules["agent.memory_provider"] = provider
        agent.memory_provider = provider
        sc = types.ModuleType("agent.skill_commands")
        sc.extract_user_instruction_from_skill_message = lambda m: m
        agent.skill_commands = sc
        sys.modules["agent.skill_commands"] = sc
        cls.hermes = _load_under("zmem_agent_state_hermes",
                                 REPO_ROOT / "hermes-plugin" / "__init__.py")
        cls.provider = cls.hermes.ZmemMemoryProvider()

        # -- real MCP server (in-process drive) -----------------------------
        cls.mcp_mod = _load_under("zmem_agent_state_server",
                                  SERVER_DIR / "mcp_server.py")
        cls.server = cls.mcp_mod.build_server(host="127.0.0.1", port=0,
                                              use_tls=False)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)
        for k, v in cls._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # -- helpers -----------------------------------------------------------

    def _call(self, name, **args):
        return asyncio.run(
            self.server._tool_manager.call_tool(name, args, context=None))

    def _live_id(self, prefix):
        import sqlite3
        c = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True)
        try:
            row = c.execute(
                "select id from memory where content like ? "
                "and superseded_at is null order by rowid desc limit 1",
                (prefix + "%",)).fetchone()
            return row[0] if row else None
        finally:
            c.close()

    def _side(self, memory_id):
        import sqlite3
        c = sqlite3.connect(f"file:{self.store_path}?mode=ro", uri=True)
        try:
            return c.execute(
                "select expiry_class, inject_until from memory_expiry "
                "where memory_id=?", (memory_id,)).fetchone()
        finally:
            c.close()

    # -- hermes --------------------------------------------------------------

    def test_hermes_add_lands_state_with_parent_keyed_record(self):
        out = self.provider.handle_tool_call("zmem_add", {
            "type": "state", "namespace": NS,
            "content": "agent probe: hermes pool size is 8",
            "expiry_class": "setting",
            "inject_until": "2031-06-30T12:00:00Z"})
        self.assertNotIn('"error"', str(out)[:80], str(out)[:300])
        mid = self._live_id("agent probe: hermes pool size is 8")
        self.assertIsNotNone(mid)
        row = self._side(mid)
        self.assertIsNotNone(row, "the side record must land with the row")
        self.assertEqual(row[0], "setting")
        self.assertTrue(str(row[1]).startswith("2031-06-30"),
                        f"inject_until must round-trip: {row[1]!r}")

    def test_hermes_update_carries_expiry_to_replacement(self):
        self.provider.handle_tool_call("zmem_add", {
            "type": "state", "namespace": NS,
            "content": "agent probe: hermes queue depth is 3",
            "expiry_class": "setting"})
        mid = self._live_id("agent probe: hermes queue depth is 3")
        out = self.provider.handle_tool_call("zmem_update", {
            "id": mid,
            "content": "agent probe: hermes queue depth is 4",
            "expiry_class": "metric"})
        self.assertNotIn('"error"', str(out)[:80], str(out)[:300])
        live = self._live_id("agent probe: hermes queue depth is 4")
        row = self._side(live)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "metric")

    def test_hermes_state_add_without_expiry_class_refused(self):
        out = self.provider.handle_tool_call("zmem_add", {
            "type": "state", "namespace": NS,
            "content": "agent probe: hermes missing expiry"})
        self.assertIn("requires expiry_class", str(out))

    # -- MCP (real execution) -------------------------------------------------

    def test_mcp_add_lands_state_with_parent_keyed_record(self):
        res = self._call("add", type="state",
                         content="agent probe: mcp pool size is 8",
                         namespace=NS, expiry_class="metric")
        mid = self._live_id("agent probe: mcp pool size is 8")
        self.assertIsNotNone(mid)
        row = self._side(mid)
        self.assertIsNotNone(row, str(res)[:300])
        self.assertEqual(row[0], "metric")

    def test_mcp_update_carries_expiry_to_replacement(self):
        self._call("add", type="state",
                   content="agent probe: mcp queue depth is 3",
                   namespace=NS, expiry_class="metric")
        mid = self._live_id("agent probe: mcp queue depth is 3")
        upd = self._call("update", id=mid,
                         content="agent probe: mcp queue depth is 4",
                         expiry_class="setting")
        upd_txt = str(upd).lower()
        self.assertNotIn("error", upd_txt[:80], str(upd)[:300])
        live = self._live_id("agent probe: mcp queue depth is 4")
        row = self._side(live)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "setting")

    def test_mcp_state_add_without_expiry_class_refused(self):
        res = self._call("add", type="state",
                         content="agent probe: mcp missing expiry",
                         namespace=NS)
        self.assertIn("requires expiry_class", str(res))

    # -- apply_ops (the ops applier path) --------------------------------------

    def test_apply_ops_add_carries_expiry(self):
        import uuid
        res = self._call("apply_ops", ops=[{
            "op_id": f"agent-state-add-{uuid.uuid4().hex[:8]}",
            "op": "add",
            "args": {"type": "state", "namespace": NS,
                     "content": "agent probe: ops pool size is 8",
                     "expiry_class": "setting",
                     "inject_until": "2031-01-01T00:00:00Z"}}])
        res_txt = str(res)
        self.assertNotIn("error", res_txt[:120].lower(), res_txt[:400])
        mid = self._live_id("agent probe: ops pool size is 8")
        self.assertIsNotNone(mid)
        row = self._side(mid)
        self.assertIsNotNone(
            row,
            "the applier must forward the expiry pair (the #405-round "
            "silent-drop regression)")
        self.assertEqual(row[0], "setting")

    def test_apply_ops_add_dedup_fold_with_expiry_is_honest(self):
        """FB-2: a state add that byte-folds into an existing row must not
        report committed while dropping its expiry. A true replay (stored
        record matches) commits; a missing/differing record is rejected
        naming the reason."""
        import uuid
        content = "agent probe: dedup fold honesty pool is 8"
        # Seed a state row WITH its record through the surface under test.
        r1 = self._call("apply_ops", ops=[{
            "op_id": f"fb2-seed-{uuid.uuid4().hex[:8]}",
            "op": "add",
            "args": {"type": "state", "namespace": NS, "content": content,
                     "expiry_class": "setting"}}])
        mid = self._live_id(content)
        self.assertIsNotNone(mid)
        row = self._side(mid)
        self.assertIsNotNone(row)
        # Replay: identical content + identical record -> committed (replay).
        r2 = self._call("apply_ops", ops=[{
            "op_id": f"fb2-replay-{uuid.uuid4().hex[:8]}",
            "op": "add",
            "args": {"type": "state", "namespace": NS, "content": content,
                     "expiry_class": "setting"}}])
        self.assertIn("committed", str(r2), str(r2)[:300])
        # DIFFERING record on the same content -> rejected, not a silent
        # committed-with-drop.
        r3 = self._call("apply_ops", ops=[{
            "op_id": f"fb2-conflict-{uuid.uuid4().hex[:8]}",
            "op": "add",
            "args": {"type": "state", "namespace": NS, "content": content,
                     "expiry_class": "metric"}}])
        self.assertIn("rejected", str(r3), str(r3)[:300])

    def test_apply_ops_update_without_expiry_inherits(self):
        """PRR-014: an ops-applier update that OMITS expiry_class forwards
        None so the writer inherits the target's stored record."""
        import uuid
        self._call("add", type="state",
                   content="agent probe: inherit pool is 3",
                   namespace=NS, expiry_class="setting")
        mid = self._live_id("agent probe: inherit pool is 3")
        res = self._call("apply_ops", ops=[{
            "op_id": f"agent-inherit-{uuid.uuid4().hex[:8]}",
            "op": "update",
            "args": {"id": mid,
                     "content": "agent probe: inherit pool is 4"}}])
        self.assertNotIn("error", str(res)[:120].lower(), str(res)[:400])
        live = self._live_id("agent probe: inherit pool is 4")
        row = self._side(live)
        self.assertIsNotNone(row,
                             "omitted expiry must inherit the stored record")
        self.assertEqual(row[0], "setting")

    def test_apply_ops_update_carries_expiry(self):
        import uuid
        self._call("add", type="state",
                   content="agent probe: ops queue depth is 3",
                   namespace=NS, expiry_class="setting")
        mid = self._live_id("agent probe: ops queue depth is 3")
        res = self._call("apply_ops", ops=[{
            "op_id": f"agent-state-upd-{uuid.uuid4().hex[:8]}",
            "op": "update",
            "args": {"id": mid,
                     "content": "agent probe: ops queue depth is 4",
                     "expiry_class": "metric"}}])
        res_txt = str(res)
        self.assertNotIn("error", res_txt[:120].lower(), res_txt[:400])
        live = self._live_id("agent probe: ops queue depth is 4")
        row = self._side(live)
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "metric")

    # -- remote transport forwarding -------------------------------------------

    def test_transport_forwards_expiry_on_add_and_update(self):
        tpath = (REPO_ROOT / "skills" / "memory" / "scripts" / "fleet" /
                 "transport.py")
        tspec = importlib.util.spec_from_file_location(
            "zmem_agent_state_transport", tpath)
        tmod = importlib.util.module_from_spec(tspec)
        sys.modules["zmem_agent_state_transport"] = tmod
        tspec.loader.exec_module(tmod)
        captured = []

        def fake_call(tool, args, fallback):
            captured.append((tool, dict(args)))
            return {"ok": True, "result": "fake"}

        http = tmod.McpHttp(url="http://127.0.0.1:1/mcp", executor=object())
        http._explicit_call = fake_call
        http.add("agent probe: transport row", type_="state",
                 expiry_class="setting", inject_until="2031-06-30T12:00:00Z")
        http.update("00000000-0000-0000-0000-000000000000",
                    "agent probe: transport row 2",
                    expiry_class="metric",
                    inject_until="2032-01-01T00:00:00Z")
        add_args = next((a for t, a in captured if t == "add"), {})
        upd_args = next((a for t, a in captured if t == "update"), {})
        self.assertEqual(add_args.get("expiry_class"), "setting")
        self.assertEqual(add_args.get("inject_until"), "2031-06-30T12:00:00Z")
        self.assertEqual(upd_args.get("expiry_class"), "metric")
        self.assertEqual(upd_args.get("inject_until"), "2032-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main(verbosity=2)
