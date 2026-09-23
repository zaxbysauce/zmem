"""Supplemental namespace control-character guardrail for issue #166.

This post-checkpoint check keeps the fleet scope grammar safe when a namespace
contains a C0 byte or DEL.  It reuses the stdlib-only validator loader from the
frozen namespace matrix, so it remains runnable without the optional MCP SDK.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

try:
    from tests import test_namespace_schema as _namespace_schema
except ModuleNotFoundError:  # ``python tests/test_namespace_controls.py``
    import test_namespace_schema as _namespace_schema


BAD_FLEET_SCOPES = (
    "fleet:x" + chr(0x00) + "y",
    "fleet:x" + chr(0x7F) + "y",
)


class NamespaceControlCharacterTest(unittest.TestCase):
    """All namespace admission surfaces reject C0 and DEL fleet scopes."""

    @classmethod
    def setUpClass(cls):
        cls.writer, cls.mcp, cls.auth = (
            _namespace_schema.NamespaceSchemaTest()._load_scope_validators()
        )

    def test_writer_rejects_c0_and_del_fleet_scopes(self):
        conn = sqlite3.connect(":memory:")
        try:
            for namespace in BAD_FLEET_SCOPES:
                with self.subTest(namespace=repr(namespace)):
                    with self.assertRaises(self.writer.CapturePolicyRefusal):
                        self.writer._validate_namespace(conn, namespace)
        finally:
            conn.close()

    def test_mcp_rejects_c0_and_del_fleet_scopes(self):
        for namespace in BAD_FLEET_SCOPES:
            with self.subTest(namespace=repr(namespace)):
                self.assertFalse(self.mcp._valid_mcp_namespace(namespace))

    def test_auth_rejects_c0_and_del_fleet_scopes(self):
        for namespace in BAD_FLEET_SCOPES:
            with self.subTest(namespace=repr(namespace)):
                self.assertFalse(self.auth._valid_scope_namespace(namespace))

    def test_scope_resolver_rejects_c0_and_del_identities(self):
        scripts = Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"
        sys.path.insert(0, str(scripts))
        try:
            import host

            for bad in ("x" + chr(0x00) + "y", "x" + chr(0x7F) + "y"):
                with self.subTest(identity=repr(bad)):
                    with self.assertRaises(ValueError):
                        host.resolve_scopes(None, bad, {}, {})
                    with self.assertRaises(ValueError):
                        host.resolve_scopes(None, None, {"ZMEM_FLEET": bad}, {})
                    with self.assertRaises(ValueError):
                        host.resolve_scopes(None, None, {}, {"agent_identity": bad})
        finally:
            sys.path.remove(str(scripts))

    def test_selected_checkout_without_schema_fails_closed(self):
        """An explicit incomplete checkout cannot widen noncanonical scopes."""
        old_home = os.environ.get("ZMEM_HOME")
        with tempfile.TemporaryDirectory(prefix="zmem-no-schema-") as checkout:
            os.environ["ZMEM_HOME"] = checkout
            try:
                _, mcp, auth = (
                    _namespace_schema.NamespaceSchemaTest()._load_scope_validators()
                )
            finally:
                if old_home is None:
                    os.environ.pop("ZMEM_HOME", None)
                else:
                    os.environ["ZMEM_HOME"] = old_home

        self.assertFalse(auth._valid_scope_namespace("fleet:dgx-spark"))
        self.assertFalse(mcp._valid_mcp_namespace("fleet:dgx-spark"))
        self.assertTrue(mcp._valid_mcp_namespace("user:global"))


if __name__ == "__main__":
    unittest.main()
