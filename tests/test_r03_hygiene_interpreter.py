"""Issue #259 companion pin (Phase 4.2 fifth site, NOT in the frozen manifest).

The frozen census (tests/test_r03_interpreter_in_commands.py
InterpreterBindingCensusTest) pins the four hook-injected command sites; its
test file is byte-locked by the red-checkpoint manifest, so this sibling-site
pin lives in its own file. The store hygiene report renders a suggested
`store.py update` command in its action plan; it must carry the running
process's own interpreter (quoted), never a PATH-resolved literal `python`
(the Windows Store stub hazard).

Run: python tests/test_r03_hygiene_interpreter.py
"""

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HYGIENE = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "hygiene.py"


class HygieneInterpreterPin(unittest.TestCase):
    def test_upgrade_action_names_running_interpreter(self):
        text = HYGIENE.read_text(encoding="utf-8")
        self.assertIn("shlex.quote(sys.executable)", text,
                      "hygiene _UPGRADE_COMMAND must bind the running "
                      "interpreter from sys.executable")
        self.assertNotIn('"python skills/memory/scripts/store.py', text,
                         "the literal PATH-python form must not return")

    def test_rendered_command_starts_with_quoted_interpreter(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        import storelib.hygiene as hygiene
        rendered = hygiene._UPGRADE_COMMAND.format(
            none_id="x", content="y", signal="none", proof_ref="z")
        first = rendered.split(" ", 1)[0].strip("'\"")
        self.assertTrue(os.path.isfile(first), first)

    def test_payload_note_binds_sys_executable(self):
        # Strengthens the frozen census (C7 pins only the format literal for
        # the payload site): the interpreter token must be bound from
        # sys.executable, not hardcoded.
        text = (REPO_ROOT / "hooks" / "lib" /
                "zmem-session-start-payload.py").read_text(encoding="utf-8")
        self.assertIn("shlex.quote(sys.executable)", text,
                      "the payload note must bind the interpreter from "
                      "sys.executable")


if __name__ == "__main__":
    unittest.main()
