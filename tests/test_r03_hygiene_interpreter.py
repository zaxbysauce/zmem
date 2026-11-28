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
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import os
import re
import shlex
import sys
import ast
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


def _operator_hints(tree):
    """Find the operator command hints in a parsed module.

    An operator instruction is a literal that STARTS with a command (the
    "[zmem] " prefix is stripped) and carries one of that command's flags --
    the shape every operator hint in this repo has ("restore --from X",
    "doctor --json"). PROSE that merely names a command mid-sentence ("For
    diagnostics, see doctor --json in the manual") is NOT a hint.

    Returns (fstring_hints, bare_hints): the f-strings that render a command
    (each must call `command_prefix()`), and the plain string literals that
    read as a command with no prefix at all (each is a bare hint).
    """
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    fstring_parts = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr):
            for v in n.values:
                if isinstance(v, ast.Constant):
                    fstring_parts.add(id(v))
    fstring_hints = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.JoinedStr):
            continue
        parts = [v.value for v in n.values
                 if isinstance(v, ast.Constant) and isinstance(v.value, str)]
        if any(_is_operator_instruction(p) for p in parts):
            fstring_hints.append(n)
    bare_hints = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Constant) and isinstance(n.value, str)):
            continue
        if id(n) in docstrings or id(n) in fstring_parts:
            continue
        if _is_operator_instruction(n.value):
            bare_hints.append(n)
    return fstring_hints, bare_hints


_OPERATOR_COMMANDS = ("restore", "backup", "doctor", "reindex", "reembed",
                       "import-store", "export", "list", "recall", "consolidate")
# An operator instruction RENDERS a copyable command: the command word
# comes FIRST, optionally behind the repo's own "[zmem] " prefix, an
# executable prefix ("zmem ", "python .../store.py ") or an interpreter path.
_CMD_RE = re.compile(
    r"^\s*(?:(?P<label>(?:run|try|use)\s*:?\s+)?"
    r"(?P<prefix>\[zmem\]\s*)?)"
    r"(?P<exec>(?:zmem|python)\s+\S*store\.py\s+|[\w./:\\-]*store\.py\s+"
    r"|zmem\s+)?"
    r"(?:" + "|".join(_OPERATOR_COMMANDS) + r")\b", re.IGNORECASE)


def _is_operator_instruction(text: str) -> bool:
    """True when the literal READS as an operator instruction.

    An instruction (a) STARTS with the command, (b) carries one of that
    command's flags, (c) is short, and (d) does not end like a sentence.
    Prose that merely names a command -- mid-sentence ("For diagnostics, see
    doctor --json in the manual") or as a sentence ("restore --from is the
    option that selects the source.") -- is NOT an instruction.
    """
    stripped = text.strip()
    if len(stripped) > 120:
        return False
    if re.search(r"[.!?]\s*$", stripped):
        return False
    match = _CMD_RE.match(text)
    if not match:
        return False
    # A copyable instruction either carries one of the command's flags, or
    # is EXECUTABLE on its own: "zmem restore", "python .../store.py
    # restore" and "C:/x/store.py restore" are all pasteable, flagless or
    # not. Every executable form the pattern accepts counts.
    return bool(re.search(r"\s--[a-z]", stripped)) or bool(
        match.group("exec"))


class OperatorHintDetectorTest(unittest.TestCase):
    """TG-2: the detector must flag an operator instruction and leave prose
    alone -- both are pinned here so the pin above cannot silently rot."""

    def test_flags_a_bare_operator_instruction(self):
        tree = ast.parse('hint = "doctor --json --rebuild-fts"\n')
        _fstrings, bare = _operator_hints(tree)
        self.assertEqual(len(bare), 1, bare)

    def test_flags_an_unprefixed_fstring_hint(self):
        tree = ast.parse('hint = f"restore --from {path}"\n')
        fstrings, bare = _operator_hints(tree)
        self.assertEqual(len(fstrings), 1, fstrings)
        self.assertEqual(bare, [])

    def test_flags_a_second_unprefixed_hint_in_one_fstring(self):
        # TG-2 (the closeout reviewer's round 30): an f-string that carries
        # a PREFIXED command and a second, UNPREFIXED command literal is
        # two hints, and the unprefixed one is a bare hint.
        tree = ast.parse(
            'hint = f"{command_prefix()} restore --from {p} doctor --json"\n')
        fstrings, bare = _operator_hints(tree)
        self.assertEqual(len(fstrings), 1, fstrings)
        self.assertEqual(bare, [])

    def test_leaves_prose_alone(self):
        prose = ('note = "For diagnostics, see doctor --json in the manual"\n'
                 'doc = """\nrestore --from is the option that selects the source.\n"""\n')
        tree = ast.parse(prose)
        fstrings, bare = _operator_hints(tree)
        self.assertEqual(bare, [], bare)
        self.assertEqual(fstrings, [], fstrings)

    def test_leaves_a_flagless_mention_alone(self):
        tree = ast.parse('note = "the restore path is guarded"\n')
        _fstrings, bare = _operator_hints(tree)
        self.assertEqual(bare, [], bare)

    def test_flags_a_flagless_bare_command(self):
        tree = ast.parse('hint = "zmem restore"\n')
        _fstrings, bare = _operator_hints(tree)
        self.assertEqual(len(bare), 1, bare)

    def test_flags_an_executable_prefixed_hint(self):
        tree = ast.parse('hint = "python C:/x/store.py restore --from Y"\n')
        _fstrings, bare = _operator_hints(tree)
        self.assertEqual(len(bare), 1, bare)

    def test_flags_a_flagless_interpreter_path_hint(self):
        tree = ast.parse('hint = "C:/x/store.py restore"\n')
        _fstrings, bare = _operator_hints(tree)
        self.assertEqual(len(bare), 1, bare)

    def test_flags_a_labelled_unprefixed_hint(self):
        tree = ast.parse('hint = "Run: zmem restore --from X"\n')
        _fstrings, bare = _operator_hints(tree)
        self.assertEqual(len(bare), 1, bare)


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
        # PR #541 (#504) consolidated the old five-branch rollback into one
        # _finish_failed_restore; the empty/missing-destination arms advise
        # file deletion, which carries no command. There are now TWO
        # hand-rollback hint sites — the automatic-rollback failure arm and
        # the put-back failure arm (F-007: the operator must be told how to
        # re-install the pre-restore backup BY HAND) — and BOTH render
        # through the prefix. The rule's intent — every operator COMMAND
        # hint renders through the prefix — is unchanged, and the AST checks
        # below are what enforce it.
        self.assertEqual(backup.count("command_prefix()"), 2,
                         "both backup hand-rollback hints must use the prefix "
                         "(the automatic-rollback arm and the put-back arm; "
                         "the pre-#504 five-branch shape is gone)")
        # TG-2 (the round-1 review comment): the count above is a sentinel
        # that cannot catch a NEW unprefixed hint -- a second hint added
        # without the prefix would leave both counts at one. Pin the FORM,
        # over EXECUTABLE strings only: the AST gives the hint literals
        # (a comment is not a node; a docstring is excluded explicitly), so
        # a harmless prose mention is not a false positive and a bare
        # operator hint is.
        tree = ast.parse(backup, filename="backup.py")
        fstring_hints, bare_hints = _operator_hints(tree)
        # (the detector itself is `_operator_hints`, unit-tested with
        # positive and negative fixtures below)
        self.assertTrue(fstring_hints or bare_hints,
                        "the backup hand-rollback hint disappeared")
        for n in fstring_hints:
            prefixed = [v for v in n.values
                        if isinstance(v, ast.FormattedValue)
                        and isinstance(v.value, ast.Call)
                        and isinstance(v.value.func, ast.Name)
                        and v.value.func.id == "command_prefix"]
            self.assertTrue(
                prefixed,
                "an operator command hint must render through "
                "command_prefix(): %r" % (ast.dump(n),))
            # TG-2 (the closeout reviewer's round 30): the check above only
            # asks whether a prefix call appears SOMEWHERE in the f-string,
            # so a SECOND, UNPREFIXED operator command literal beside a
            # prefixed one used to pass. EVERY operator instruction literal
            # inside the f-string must be rendered through the prefix --
            # the interpolation that PRECEDES it must be the prefix call
            # (the command word itself is a literal; the interpreter is
            # the prefixed part).
            values = list(n.values)
            for idx, part in enumerate(values):
                if not (isinstance(part, ast.Constant)
                        and isinstance(part.value, str)):
                    continue
                if not _is_operator_instruction(part.value):
                    continue
                prev = values[idx - 1] if idx > 0 else None
                self.assertTrue(
                    isinstance(prev, ast.FormattedValue)
                    and isinstance(prev.value, ast.Call)
                    and isinstance(prev.value.func, ast.Name)
                    and prev.value.func.id == "command_prefix",
                    "an operator command literal must be rendered through "
                    "command_prefix(): %r" % (part.value,))
        self.assertEqual(
            bare_hints, [],
            "a bare operator command hint must render through "
            "command_prefix(): %r" % ([n.value for n in bare_hints],))
        prefix_calls = [n for n in ast.walk(tree)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name)
                        and n.func.id == "command_prefix"]
        self.assertEqual(
            len(fstring_hints), len(prefix_calls),
            "every operator command hint in backup.py must use "
            "command_prefix()")

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
        # The resolved store path must be the real CLI shim (its shebang
        # exists for POSIX direct exec, but it must still never be
        # suggested as a bare token — issue #259/#294).
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
