#!/usr/bin/env python3
"""Source-contract guardrail for issue #109's defect class.

Defect class: a store mutation reachable from the authenticated MCP surface
WITHOUT namespace confinement of the caller's token. Issue #109 shipped
exactly this shape for the two tombstone tools — `supersede` and
`invalidate` were the only mutating @mcp.tool() handlers whose bodies never
referenced `_guard_namespace`, so a scoped fleet token could tombstone rows
in any namespace by id.

The contract pinned here: EVERY @mcp.tool() handler in
hermes-plugin/server/mcp_server.py whose store argv carries a MUTATING
subcommand (add / update / supersede / invalidate) must reference
`_guard_namespace` in its handler body. On the pre-#109 tree this fails for
exactly `supersede` + `invalidate` (the demonstrated bite); any future
mutating tool added without the guard trips it the same way.

Residual weakness (deliberate, documented): this scan proves the guard is
CALLED, not that its denial is honored — a handler that called
`_guard_namespace` and ignored the returned denial dict would still pass
this source contract. The behavioral denial tests in tests/test_mcp_auth.py
(scoped-token denied on foreign/own/global rows, unscoped keeps powers)
carry that load. This file exists to catch the ADDITION of a new mutating
tool that forgets the guard entirely — the #109 shape.

Run: python tests/test_mcp_mutating_tools_guard.py   (no pytest -- repo convention)
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PY = REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py"

# Subcommands that mutate store rows (the store.py CLI names the handlers
# build argv for). Anything new that writes must be added here AND guarded.
MUTATING_SUBCOMMANDS = ("add", "update", "supersede", "invalidate")
GUARD_SYMBOL = "_guard_namespace"


def _tool_segments(source: str):
    """Yield (tool_name, segment) for each @mcp.tool() decorated handler."""
    # Handlers are nested functions inside build_server, indented one level.
    # Split on the decorator line; each segment runs to the next decorator
    # (or EOF) and therefore contains the entire handler body.
    parts = re.split(r"(?m)^    @mcp\.tool\(\)\s*$", source)
    for part in parts[1:]:
        m = re.search(r"async def (\w+)\(", part)
        if not m:
            continue
        yield m.group(1), part


class MutatingToolsGuardContractTest(unittest.TestCase):
    def test_every_mutating_tool_handler_references_the_namespace_guard(self):
        source = SERVER_PY.read_text(encoding="utf-8")
        self.assertIn(GUARD_SYMBOL, source,
                      "the guard closure itself disappeared from mcp_server.py")
        mutating = {}
        for name, segment in _tool_segments(source):
            argv = {
                f'"{sub}"' for sub in MUTATING_SUBCOMMANDS
                if f'"{sub}"' in segment
            }
            if argv:
                mutating[name] = (argv, GUARD_SYMBOL in segment)
        self.assertTrue(mutating,
                        "no mutating tool handlers found — the scanner rotted; "
                        "update it alongside any tool-registration refactor")
        unguarded = sorted(n for n, (_, ok) in mutating.items() if not ok)
        self.assertEqual(
            [], unguarded,
            f"mutating MCP tools whose handlers never check {GUARD_SYMBOL} "
            f"(issue #109 defect class): {unguarded}")

    def test_tombstone_handlers_pin_expected_namespace(self):
        # The guard being CALLED is not enough: the id-addressed tombstone
        # handlers must also PIN the verified namespace on the store mutation
        # by APPENDING the --expected-namespace argv pair. The behavioral
        # denial tests stay green when only the pin is dropped (the
        # server-side check denies first), so this source contract is the
        # ONLY automated tripwire for a dropped pin — the pin is the TOCTOU
        # defense for the get->tombstone subprocess pair (issue #109 scope
        # item 1). The needle is the executable argv-append line, NOT the
        # bare flag name: both handler DOCSTRINGS legitimately mention
        # --expected-namespace, and matching prose would not bite a drop.
        pin_line = 'args += ["--expected-namespace", ns_pin]'
        source = SERVER_PY.read_text(encoding="utf-8")
        missing = []
        for name, segment in _tool_segments(source):
            if name not in ("supersede", "invalidate"):
                continue
            if pin_line not in segment:
                missing.append(name)
        self.assertEqual(
            [], missing,
            f"tombstone handlers not pinning --expected-namespace "
            f"(expected the executable argv append): {missing}")

    def test_update_handler_pins_expected_old_namespace(self):
        # PRR-002 closure: update's OLD-row tombstone is also a namespace-
        # confined destructive write. The behavioral tests cannot catch a
        # dropped pin (the server-side guard denies first), so the source
        # contract is the tripwire here too — same rationale as
        # test_tombstone_handlers_pin_expected_namespace.
        pin_line = 'args += ["--expected-old-namespace", ns_pin]'
        source = SERVER_PY.read_text(encoding="utf-8")
        segments = dict(_tool_segments(source))
        self.assertIn("update", segments,
                      "update handler not found — scanner rotted")
        self.assertIn(
            pin_line, segments["update"],
            "update handler no longer pins --expected-old-namespace on the "
            "scoped no-override path (issue #109 follow-up)")

    def test_update_handler_maps_store_guard_refusal_to_structured_shape(self):
        # Final-critic revision: update's not-ok branch must remap the
        # store-level namespace-guard refusal to the structured
        # namespace_not_allowed shape (same uniformity as supersede /
        # invalidate); otherwise the rekey-race denial surfaces as prose.
        needle = "_namespace_guard_denial("
        source = SERVER_PY.read_text(encoding="utf-8")
        for name in ("supersede", "invalidate", "update"):
            segment = dict(_tool_segments(source))[name]
            self.assertIn(
                needle, segment,
                f"{name} handler does not map store-level guard refusals "
                "through _namespace_guard_denial (uniform denial shape)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
