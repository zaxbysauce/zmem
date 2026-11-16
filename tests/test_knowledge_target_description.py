"""Arm's-length acceptance checks: the OPTIONAL knowledge target
``description`` field (issue trace 480-481-482, Fix 6, rebuild r3).

Published selectors: class TargetDescriptionTest with
  - test_description_renders_in_policy_and_status
  - test_invalid_description_refused

NEW file (tests/test_knowledge_policy.py is 869 lines, over the 500-line
test-file cap — appending disallowed; pre-approved deviation).

FROZEN contract (the implementer must satisfy exactly this):
- BOTH kinds (markdown, kb-http) accept OPTIONAL ``description``: a JSON
  string of 1..200 CODEPOINTS (exactly 200 valid, 201 refused), no
  control characters (CR/LF/TAB/C0 refused), not empty/whitespace-only,
  never null/bool/object/array.
- Invalid forms: config.load_knowledge_config raises typed
  KnowledgeConfigError naming 'description' and NEVER the unknown-key
  text; the policy.py display lane renders EMPTY (strict-suppress) in
  BOTH capture modes.
- Rendering (BOTH kinds): the policy roster shows
  ``target-id — description`` on the SAME line (frozen separator
  " — " EM DASH); ``knowledge status`` HUMAN output prints it;
  ``knowledge status --json`` reports the exact per-target field
  ``targets.<id>.description``.
- No-description output is byte-identical to the base shape, pinned by
  FULL-output goldens: the policy text is pinned as an EXACT literal
  (fully deterministic); the human status and the status --json outputs
  are pinned by FULL-output equality after masking ONLY the two
  nondeterministic tokens (row uuids and ISO timestamps) — everything
  else byte-identical.
- Aggregate safety: descriptions each individually valid whose AGGREGATE
  would exceed the renderer's word budget still render — deterministic
  truncation, per-target display cut to the largest prefix that keeps
  the total under the budget, frozen marker "..." — never empty.

Base: both methods RED (description is an unknown key; the strict
display lane suppresses the whole render for a described config).

Run: python -X utf8 tests/test_knowledge_target_description.py
"""

from __future__ import annotations
import os
import re
import sys
import tempfile

# Scratch env pinned BEFORE the isolation guard and any knowledge import.
_TMP = tempfile.mkdtemp(prefix="zmem-kndesc-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ["ZMEM_DATA"] = _TMP
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
os.environ.setdefault("ZMEM_MODELS_DIR", os.path.join(_TMP, "no-models"))
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

import json
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
POLICY_PY = SCRIPTS_DIR / "knowledge" / "policy.py"
PYTHON = sys.executable
sys.path.insert(0, str(SCRIPTS_DIR))

from knowledge.config import (  # noqa: E402
    KnowledgeConfigError,
    load_knowledge_config,
)

_DESCRIPTION = "Deployment runbook for the fleet"
_MAX = 200                       # frozen bound (codepoints)
_TRUNCATION_MARKER = "..."       # frozen deterministic display truncation

# ---------------------------------------------------------------------------
# FIXED LITERAL no-description goldens, captured verbatim from the BASE
# renderer/status output at commit 0d9bd0c7 (empty store: zero captures,
# so there are NO uuid/timestamp tokens at all — the bytes are fully
# deterministic).  These are constants, NOT runtime captures: the head
# leg proves byte-identity against a fixed base-derived string.

_GOLDEN_POLICY_TEXT = (
    "# ZMem knowledge companion (operator-enabled external notes)\n"
    "\n"
    "Configured target ids: notes.\n"
    "\n"
    "Targets hold durable reference notes you explicitly author, curate,\n"
    "and attach evidence to - never raw transcripts, never secrets. Keep\n"
    "routine session lessons in zmem memory (compact, private); targets\n"
    "store long-lived notes outside zmem (Markdown vault or KB endpoint;\n"
    "access follows the target's own permissions; Obsidian is optional,\n"
    "never required).\n"
    "\n"
    "Invoke via the configured Python interpreter and memory store CLI.\n"
    "Read: `knowledge read --target <id> --key <key> --namespace <ns>`,\n"
    "verify with `knowledge check`; `knowledge status` shows pointers.\n"
    "Derive the namespace per the memory skill; always pass `--namespace`\n"
    "(the default puts project pointers in user:global).\n"
    "\n"
    "Publish explicitly, never automatically: screen for secrets (capture\n"
    "rejects them), then `knowledge capture --target <id> --key <key>\n"
    "--namespace <ns> --content <text|->`; at a closeout the user\n"
    "requested in this session, store relevant explicit notes\n"
    "(agent/reminder-initiated closeouts draft only, no capture). Exit 3\n"
    "= pending: inspect `knowledge status`; `knowledge flush` drains the\n"
    "whole pending queue (no session filter), else re-run the same capture\n"
    "input (idempotent).\n"
    "\n"
    'Full workflow and limits: the "knowledge" skill (memory skill '
    "knowledge section).\n"
)

_GOLDEN_STATUS_HUMAN = (
    "knowledge companion: enabled=True mode=local targets=notes\n"
)

_GOLDEN_STATUS_JSON = (
    '{"ok": true, "mode": "local", "enabled": true, "targets": '
    '{"notes": {"kind": "markdown"}}, "namespace": "user:global", '
    '"journal": [], "delivery": {"queue": [], "receipts": []}, '
    '"pointers": []}\n'
)


def _aggregate_oracle_roster(target_ids, descriptions):
    """INDEPENDENT ORACLE for the frozen aggregate truncation rule: the
    WHOLE policy must stay under the exclusive 200-word cap
    (MAX_POLICY_WORDS=200; base renders "" at >= 200), so each target's
    display is cut to the LARGEST per-target prefix (uniform across
    targets, deterministic) that keeps the TOTAL under the cap; truncated
    units end with the frozen marker '...'.  The oracle SEARCHES the
    prefix downward and VERIFIES its own output satisfies the budget —
    the roster literal and the arithmetic cohere by construction."""
    template = _GOLDEN_POLICY_TEXT.replace(
        "Configured target ids: notes.\n", "")
    template_words = len(template.split())
    max_prefix = max(len(d.split()) for d in descriptions)
    for prefix in range(max_prefix, -1, -1):
        units = []
        for tid, desc in zip(target_ids, descriptions):
            words = desc.split()
            if prefix < len(words):
                units.append("%s — %s %s"
                             % (tid, " ".join(words[:prefix]),
                                _TRUNCATION_MARKER))
            else:
                units.append("%s — %s" % (tid, desc))
        roster = "Configured target ids: %s." % ", ".join(units)
        total = template_words + len(roster.split())
        if total < 200:
            assert total < 200, "oracle self-check: budget satisfied"
            return roster
    raise AssertionError(
        "no feasible truncation prefix for %d targets (template=%d words)"
        % (len(target_ids), template_words))


def _write_config(path: Path, targets: dict) -> Path:
    path.write_text(json.dumps(
        {"knowledge": {"enabled": True, "targets": targets}}),
        encoding="utf-8")
    return path


def _markdown(description=None, root=None) -> dict:
    target = {"kind": "markdown", "root": root or os.path.join(_TMP, "vault")}
    if description is not None:
        target["description"] = description
    return target


def _kbhttp(description=None) -> dict:
    target = {
        "kind": "kb-http",
        "endpoint": "https://hub.internal.example/api/notes",
        "credential_env": "ZMEM_KB_TEST_TOKEN",
    }
    if description is not None:
        target["description"] = description
    return target


class TargetDescriptionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-kndesc-case-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.store_dir = self.tmp / "store"
        self.store_dir.mkdir()
        self.vault = self.store_dir / "vault"
        self.vault.mkdir()
        self.env = {**os.environ}
        self.env["ZMEM_STORE"] = str(
            self.store_dir / "store.sqlite").replace("\\", "/")
        self.env["ZMEM_DATA"] = str(self.tmp).replace("\\", "/")
        self.env["ZMEM_MODELS_DIR"] = str(self.tmp / "no-models")
        self.env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
        self.env.pop("ZMEM_REMOTE_URL", None)
        init = subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "stats"],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO_ROOT), timeout=180)
        self.assertEqual(init.returncode, 0, init.stderr)

    # -- helpers --------------------------------------------------------

    def _policy_text(self, config: Path, *extra: str) -> str:
        proc = subprocess.run(
            [PYTHON, "-X", "utf8", str(POLICY_PY),
             "--config", str(config), "--text", *extra],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO_ROOT), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-400:])
        return proc.stdout

    def _status(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), "knowledge", "status",
             *extra],
            capture_output=True, text=True, encoding="utf-8",
            env=self.env, cwd=str(REPO_ROOT), timeout=120)

    def _install_config(self, targets: dict) -> None:
        (self.store_dir / "config.json").write_text(json.dumps(
            {"knowledge": {"enabled": True, "targets": targets}}),
            encoding="utf-8")

    # -- acceptance ------------------------------------------------------

    def test_description_renders_in_policy_and_status(self):
        # The no-description goldens are FIXED LITERAL BYTES captured from
        # the BASE output (constants above) — the head leg proves
        # byte-identity against a constant, never against the code under
        # test.  (Empty store: zero captures, zero uuid/timestamp tokens.)
        # The policy comparison is TRUE byte equality — no rstrip; the
        # golden carries the CLI's exact trailing newline.
        self._install_config(
            {"notes": _markdown(root=str(self.vault))})
        golden_policy = _GOLDEN_POLICY_TEXT
        golden_human = _GOLDEN_STATUS_HUMAN
        golden_json = _GOLDEN_STATUS_JSON

        # MARKDOWN kind: policy "target-id — description" same line;
        # human status prints it; status --json exact field.
        self._install_config(
            {"notes": _markdown(_DESCRIPTION, root=str(self.vault))})
        rendered = self._policy_text(self.store_dir / "config.json")
        self.assertTrue(rendered.strip(), "the policy must render")
        same_line = [line for line in rendered.splitlines()
                     if "notes" in line and _DESCRIPTION in line]
        self.assertTrue(same_line,
                        "the description must render next to the target id "
                        "(same line, separator ' — '): %r" % (rendered,))
        self.assertIn(" — ", same_line[0])
        human = self._status()
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn(_DESCRIPTION, human.stdout,
                      "human status must print the description: %r"
                      % (human.stdout,))
        js = self._status("--json")
        self.assertEqual(js.returncode, 0, js.stdout + js.stderr)
        self.assertEqual(
            (json.loads(js.stdout).get("targets") or {})
            .get("notes", {}).get("description"),
            _DESCRIPTION,
            "status --json must report targets.notes.description exactly")

        # KB-HTTP kind through the SAME three outputs (not just load).
        self._install_config({"fleet": _kbhttp(_DESCRIPTION)})
        kb_rendered = self._policy_text(self.store_dir / "config.json")
        self.assertTrue(kb_rendered.strip(),
                        "a described kb-http target must render")
        self.assertTrue(
            any("fleet" in line and _DESCRIPTION in line
                for line in kb_rendered.splitlines()),
            "the kb-http description renders next to its target id: %r"
            % (kb_rendered,))
        kb_human = self._status()
        self.assertEqual(kb_human.returncode, 0, kb_human.stderr)
        self.assertIn(_DESCRIPTION, kb_human.stdout,
                      "human status must print the kb-http description")
        kb_js = self._status("--json")
        self.assertEqual(
            (json.loads(kb_js.stdout).get("targets") or {})
            .get("fleet", {}).get("description"),
            _DESCRIPTION)

        # NO-DESCRIPTION goldens: FULL-output byte equality against the
        # FIXED LITERAL constants (empty store — no uuid/timestamp
        # tokens exist, so the comparison is byte-exact with no masking).
        self._install_config(
            {"notes": _markdown(root=str(self.vault))})
        self.assertEqual(self._policy_text(
            self.store_dir / "config.json"), golden_policy,
            "no-description policy text must be BYTE-identical (no "
            "rstrip; trailing newline included) to the base-derived "
            "literal golden")
        self.assertIn("Configured target ids: notes.", golden_policy)
        plain_human = self._status()
        self.assertEqual(plain_human.returncode, 0, plain_human.stderr)
        self.assertEqual(plain_human.stdout, golden_human,
                         "no-description human status must be byte-"
                         "identical to the base-derived literal golden")
        plain_json = self._status("--json")
        self.assertEqual(plain_json.stdout, golden_json,
                         "no-description status --json must be byte-"
                         "identical to the base-derived literal golden")
        self.assertEqual(json.loads(golden_json)["targets"],
                         {"notes": {"kind": "markdown"}},
                         "the base golden's targets object is the bare "
                         "shape (no description key)")

        # AGGREGATE safety + EXACTNESS (r5 arithmetic): 3 targets (the
        # base template alone is 176 words, so an 8-target roster can
        # NEVER fit under the exclusive 200-word cap — the fixture is
        # sized for feasibility), each description individually valid
        # (199 codepoints) and jointly over the per-target display
        # budget.  The policy STILL renders, and the roster equals the
        # INDEPENDENT-ORACLE output EXACTLY; the oracle's own output is
        # EXECUTED and verified to satisfy the <200-word assertion.
        words = " ".join("word%d" % i for i in range(30))  # 199 codepoints
        self.assertLessEqual(len(words), _MAX)
        heavy_ids = ["t%d" % i for i in range(3)]
        heavy = {tid: _markdown(words, root=str(self.vault))
                 for tid in heavy_ids}
        heavy_cfg = _write_config(self.tmp / "cfg-heavy.json", heavy)
        heavy_rendered = self._policy_text(heavy_cfg)
        self.assertTrue(
            heavy_rendered.strip(),
            "an individually-valid over-budget aggregate must STILL render "
            "with truncated descriptions, not empty")
        for tid in heavy_ids:
            self.assertIn(tid, heavy_rendered,
                          "every target id renders")
        self.assertLess(len(heavy_rendered.split()), 200,
                        "the truncated aggregate stays under the word cap")
        expected_roster = _aggregate_oracle_roster(heavy_ids, [words] * 3)
        # Oracle self-check (executed in-test): the expected roster plus
        # the fixed template satisfies the same <200-word assertion the
        # render is held to — the roster literal and the budget cohere.
        template_words = len(_GOLDEN_POLICY_TEXT.replace(
            "Configured target ids: notes.\n", "").split())
        self.assertLess(template_words + len(expected_roster.split()), 200,
                        "the ORACLE's own output satisfies the word cap "
                        "(roster=%r)" % (expected_roster,))
        self.assertIn(
            expected_roster, heavy_rendered,
            "the roster must equal the ORACLE's exact truncated output "
            "(frozen allocation rule, marker '...'); expected roster "
            "line: %r" % (expected_roster,))

        # Unicode boundary (r5): padding DERIVED from len(prefix) so the
        # accepted case is EXACTLY 200 codepoints and the refused case
        # EXACTLY 201.
        uni_prefix = "déploiment-ünïcode-"
        uni200 = uni_prefix + "é" * (_MAX - len(uni_prefix))
        self.assertEqual(len(uni200), _MAX)
        uni = _write_config(self.tmp / "cfg-uni.json",
                            {"notes": _markdown(uni200, root=str(self.vault))})
        load_knowledge_config(uni)  # must NOT raise (exactly 200)
        uni201 = uni_prefix + "é" * (_MAX - len(uni_prefix) + 1)
        self.assertEqual(len(uni201), _MAX + 1)
        cfg201 = _write_config(
            self.tmp / "cfg-uni201.json",
            {"notes": _markdown(uni201, root=str(self.vault))})
        with self.assertRaises(KnowledgeConfigError) as ctx:
            load_knowledge_config(cfg201)
        self.assertIn("description", str(ctx.exception))

    def test_invalid_description_refused(self):
        invalid_values = [
            None,                       # null
            True,                       # bool
            {"text": "object"},         # object
            ["array"],                  # array
            "",                         # empty string
            "   \t ",                   # whitespace-only
            "x" * (_MAX + 1),           # 201 codepoints
            "line1\rline2",             # CR
            "line1\nline2",             # LF
            "col\ttab",                 # TAB
            "ctl\x01char",              # control char
        ]
        for i, value in enumerate(invalid_values):
            for name, base_target in (
                    ("markdown", _markdown(root=str(self.vault))),
                    ("kb-http", _kbhttp())):
                target = dict(base_target)
                target["description"] = value   # the RAW invalid JSON value
                cfg = _write_config(
                    self.tmp / ("cfg-invalid-%d-%s.json" % (i, name)),
                    {"notes": target})
                with self.assertRaises(KnowledgeConfigError) as ctx:
                    load_knowledge_config(cfg)
                message = str(ctx.exception)
                self.assertIn("description", message,
                              "%s=%r refusal must name 'description': %r"
                              % (name, value, message))
                self.assertNotIn("unknown key", message,
                                 "a present-but-invalid description is not "
                                 "an unknown key: %r" % (message,))

        # Exactly 200 valid on BOTH kinds (both sides of the boundary).
        ok200 = "y" * _MAX
        for name, target_factory in (("markdown", _markdown),
                                     ("kb-http", _kbhttp)):
            cfg = _write_config(
                self.tmp / ("cfg-max-%s.json" % name),
                {"notes": target_factory(ok200, root=str(self.vault))
                 if name == "markdown" else target_factory(ok200)})
            config = load_knowledge_config(cfg)  # must NOT raise
            self.assertIn("notes", config.targets)

        # The policy.py display lane renders EMPTY for an invalid
        # description, in BOTH capture modes (strict-suppress).
        for value in (12345, "x" * (_MAX + 1)):
            cfg = _write_config(self.tmp / "cfg-display-invalid.json",
                                {"notes": _markdown(value,
                                                    root=str(self.vault))})
            for extra in ((), ("--no-capture",)):
                text = self._policy_text(cfg, *extra)
                self.assertEqual(
                    text.strip(), "",
                    "the strict display lane must suppress the whole render "
                    "for an invalid description (mode=%r): %r"
                    % (extra or "capture", text))


if __name__ == "__main__":
    unittest.main()
