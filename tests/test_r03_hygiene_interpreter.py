"""Issue #259 companion pins (Phase 4.2 fifth site + feedback round, NOT in
the frozen manifest).

The frozen census (tests/test_r03_interpreter_in_commands.py
InterpreterBindingCensusTest) pins the four hook-injected command sites; its
test file is byte-locked by the red-checkpoint manifest, so companion pins
live in this file. Covers: the store-hygiene upgrade action (must carry the
running process's own quoted interpreter, never a PATH-resolved literal
python), the payload note's binding source, and the storelib-wide operator
command prefix (schema/backup hints via storelib.opcmds).

Run: python tests/test_r03_hygiene_interpreter.py
"""

import os
import re
import shlex
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HYGIENE = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "hygiene.py"
OPCMDS = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "opcmds.py"
PAYLOAD = REPO_ROOT / "hooks" / "lib" / "zmem-session-start-payload.py"
SCHEMA = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "schema.py"
BACKUP = REPO_ROOT / "skills" / "memory" / "scripts" / "storelib" / "backup.py"
DOCTOR = REPO_ROOT / "skills" / "memory" / "scripts" / "doctor.py"


def first_token(cmd):
    """First shell token of a rendered command, parsed with the same
    posix=False + quote-strip rule as the frozen behavioral checks (safe for
    interpreter paths containing spaces)."""
    tokens = shlex.split(cmd, posix=False)
    tok = tokens[0] if tokens else ""
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
        tok = tok[1:-1]
    return tok


class HygieneInterpreterPin(unittest.TestCase):
    def test_upgrade_action_names_running_interpreter(self):
        text = HYGIENE.read_text(encoding="utf-8")
        self.assertIn("from storelib.opcmds import command_prefix", text,
                      "hygiene must source the command prefix from opcmds")
        self.assertIn("command_prefix()", text,
                      "the upgrade action must use the opcmds prefix")
        self.assertNotIn('"python skills/memory/scripts/store.py', text,
                         "the literal PATH-python form must not return")

    def test_rendered_command_starts_with_quoted_interpreter(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        import storelib.hygiene as hygiene
        rendered = hygiene._upgrade_command(
            none_id="x", content="y", signal="none", proof_ref="z")
        first = first_token(rendered)
        self.assertTrue(os.path.isfile(first), first)

    def test_payload_note_binds_sys_executable(self):
        # Strengthens the frozen census (C7 pins only the format literal for
        # the payload site): the interpreter token must be bound from
        # sys.executable, not hardcoded.
        text = PAYLOAD.read_text(encoding="utf-8")
        self.assertIn("shlex.quote(sys.executable)", text,
                      "the payload note must bind the interpreter from "
                      "sys.executable")


class OperatorCommandPrefixPin(unittest.TestCase):
    """schema.py + backup.py render operator-facing store.py hints (review
    round: PRR-004); they must go through the same opcmds prefix."""

    def test_opcmds_binds_running_interpreter(self):
        text = OPCMDS.read_text(encoding="utf-8")
        self.assertIn("shlex.quote(sys.executable)", text,
                      "opcmds.command_prefix must bind sys.executable")
        self.assertIn('parents[1] / "store.py"', text,
                      "opcmds must resolve the store path from its location")

    def test_schema_and_backup_use_the_prefix(self):
        schema = SCHEMA.read_text(encoding="utf-8")
        self.assertIn("command_prefix()", schema,
                      "schema reembed hint must use the opcmds prefix")
        backup = BACKUP.read_text(encoding="utf-8")
        self.assertEqual(backup.count("command_prefix()"), 4,
                         "all four backup rollback hints must use the prefix")

    def test_doctor_hints_use_the_prefix(self):
        doctor = DOCTOR.read_text(encoding="utf-8")
        self.assertGreaterEqual(doctor.count("{_prefix()} "), 10,
                                "doctor command hints must use the prefix")
        # Structural: ANY backticked store.py / <store.py> hint outside a
        # comment line must route through _prefix() (round-2 review: the
        # f-anchored form was blind to plain-string and <store.py> hints).
        bad = []
        for ln, line in enumerate(doctor.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"`store\.py ", line) or re.search(
                    r"`python <store\.py> ", line):
                bad.append(ln)
        self.assertEqual(bad, [],
                         "bare backticked store hints at lines %s" % bad)

    def test_rendered_prefix_is_existing_executable_and_path(self):
        sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
        import storelib.opcmds as opcmds
        prefix = opcmds.command_prefix()
        tokens = shlex.split(prefix, posix=False)
        stripped = []
        for tok in tokens:
            if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ("'", '"'):
                tok = tok[1:-1]
            stripped.append(tok)
        self.assertTrue(os.path.isfile(stripped[0]), stripped[0])
        self.assertTrue(os.path.isfile(stripped[1]), stripped[1])
        # The resolved store path must be the real CLI shim (no shebang, so
        # it must never be suggested as a bare token).
        self.assertTrue(stripped[1].replace("\\", "/").endswith(
            "skills/memory/scripts/store.py"), stripped[1])

    def test_no_bare_or_literal_python_hint_returns(self):
        # The backticked copy-paste form is pinned everywhere; the plain
        # f"store.py form is schema-specific (its prose f"store.py run ...
        # mentions in doctor.py/backup.py are not copy-paste hints).
        for path in (SCHEMA, BACKUP, DOCTOR):
            text = path.read_text(encoding="utf-8")
            with self.subTest(site=str(path.relative_to(REPO_ROOT))):
                self.assertNotRegex(
                    text, r'f"`store\.py ',
                    "bare backticked store.py hint must not return")

    def test_schema_hint_plain_form_does_not_return(self):
        schema = SCHEMA.read_text(encoding="utf-8")
        self.assertNotRegex(schema, r'f"store\.py ',
                            "bare f-string store.py hint must not return")


if __name__ == "__main__":
    unittest.main()
