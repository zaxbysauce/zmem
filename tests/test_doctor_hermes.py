"""Issue #71 B: doctor's hermes-plugin surface check + dual-mode doc pins.

Pins the `hermes-plugin` doctor check (manifest parity with the MemoryProvider
ABC hooks actually implemented, provider/register entry point, the three shell
hook scripts, MCP server importability, remote-mode token/mcp requirements)
and the docs contract for remote prefetch (the README "planned v2" stub must
never come back; CUTOVER must carry the gateway-restart sentence, the
gateway-mode caveat, and the mcp-lib-required line).

Runs standalone: python tests/test_doctor_hermes.py
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import doctor  # noqa: E402


class HermesPluginCheckTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("ZMEM_MCP_URL", "ZMEM_MCP_TOKEN",
                        "ZMEM_MCP_TOKEN_FILE")}
        self.addCleanup(self._restore)
        # Open the skip gate: the check skips when neither ~/.hermes nor
        # ZMEM_MCP_URL exists. Point Path.home at a temp dir containing a
        # .hermes marker so the surface checks run without depending on the
        # dev box having Hermes installed.
        import tempfile
        self._home = tempfile.mkdtemp(prefix="zmem-doc-hd-")
        self.addCleanup(shutil.rmtree, self._home, True)
        os.makedirs(os.path.join(self._home, ".hermes"), exist_ok=True)
        self._home_patch = mock.patch.object(
            doctor.Path, "home", return_value=Path(self._home))
        self._home_patch.start()
        self.addCleanup(self._home_patch.stop)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_repo_tree_passes(self):
        check = doctor._check_hermes_plugin(REPO_ROOT)
        self.assertEqual(check["status"], "pass", check["summary"])

    def test_mcp_absent_degrades_to_pass_with_note(self):
        """CI environment (stdlib-only, no mcp package): the missing lib must
        NOT fail the surface check on a non-remote box — only the importability
        probe degrades to 'unverified'. (This exact case failed ubuntu CI on
        the first push.)"""
        from unittest import mock
        import importlib.util
        with mock.patch.object(importlib.util, "find_spec",
                               return_value=None):
            check = doctor._check_hermes_plugin(REPO_ROOT)
        self.assertEqual(check["status"], "pass", check["summary"])
        self.assertEqual(check["details"].get("mcp_server_importable"),
                         "unverified")

    def test_remote_mode_mcp_absent_still_fails(self):
        """Remote mode keeps the hard requirement: with ZMEM_MCP_URL set and
        no mcp package, prefetch can never work — that must fail."""
        from unittest import mock
        import importlib.util
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:8765/mcp"
        os.environ.setdefault("ZMEM_MCP_TOKEN", "x")
        with mock.patch.object(importlib.util, "find_spec",
                               return_value=None), \
             mock.patch.object(doctor.Path, "home",
                               return_value=Path(self._home)):
            check = doctor._check_hermes_plugin(REPO_ROOT)
        self.assertEqual(check["status"], "fail")
        self.assertIn("mcp", check["summary"])

    def test_stub_manifest_fails(self):
        with mock.patch.object(doctor, "_parse_simple_yaml",
                               return_value={"name": "zmem"}):
            check = doctor._check_hermes_plugin(REPO_ROOT)
        self.assertEqual(check["status"], "fail")
        self.assertIn("missing", check["summary"].lower())

    def test_invented_hook_name_fails(self):
        with mock.patch.object(
                doctor, "_parse_simple_yaml",
                return_value={"name": "zmem", "version": "0.13.1",
                              "description": "x",
                              "hooks": ["prefetch", "made_up_hook"]}):
            check = doctor._check_hermes_plugin(REPO_ROOT)
        self.assertEqual(check["status"], "fail")
        self.assertIn("made_up_hook", check["summary"])

    def test_missing_hook_script_fails(self):
        real_is_file = Path.is_file

        def fake_is_file(self, *a, **k):
            if self.name == "zmem-hermes-verify.py":
                return False
            return real_is_file(self, *a, **k)

        with mock.patch.object(Path, "is_file", fake_is_file):
            check = doctor._check_hermes_plugin(REPO_ROOT)
        self.assertEqual(check["status"], "fail")
        self.assertIn("zmem-hermes-verify.py", check["summary"])

    def test_remote_mode_without_token_fails(self):
        os.environ["ZMEM_MCP_URL"] = "http://127.0.0.1:8765/mcp"
        os.environ.pop("ZMEM_MCP_TOKEN", None)
        os.environ.pop("ZMEM_MCP_TOKEN_FILE", None)
        check = doctor._check_hermes_plugin(REPO_ROOT)
        self.assertEqual(check["status"], "fail")
        self.assertIn("token", check["summary"].lower())


class RemoteDocsPinTest(unittest.TestCase):
    """The remote-prefetch docs contract (critic #9): the caveat trio must
    stay documented — the planned-v2 stub must never return."""

    def test_readme_has_no_planned_v2_stub(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertNotIn("planned v2", readme,
                         "remote passive prefetch SHIPPED (#71 A); the stub "
                         "must not return")
        self.assertIn("ZMEM_MCP_URL", readme)

    def test_cutover_documents_gateway_caveats(self):
        cutover = (REPO_ROOT / "CUTOVER.md").read_text(encoding="utf-8")
        self.assertIn("gateway", cutover.lower())
        self.assertIn("pre_llm_call", cutover)

    def test_readme_documents_mcp_lib_requirement(self):
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("requirements.txt", readme,
                      "the remote recipe must say the mcp lib is required or "
                      "prefetch fails open")


if __name__ == "__main__":
    unittest.main(verbosity=2)
