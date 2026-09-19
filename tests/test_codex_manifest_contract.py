"""Host manifest hooks-path contract (issue #108, RC2).

codex-cli 0.152.1+ silently ignores a plugin's hooks whose manifest path does
not start with ``./`` relative to the plugin root — its own debug log says
``ignoring hooks: path must start with `./` relative to plugin root`` — so on
2026-09-02/05 every non-interactive ``codex exec`` run loaded zero zmem hooks,
wrote zero bg-log lines, and injected nothing. This pins the contract per host:

- codex   MUST use ``./hooks/hooks.codex.json`` (the fixed form);
- claude  stays ``./hooks/hooks.claude.json`` (already compliant);
- zcode   stays ``hooks/hooks.zcode.json`` — UNPREFIXED on purpose: ZCode loads
  the unprefixed form today (its parser differs from Codex's), so "normalizing"
  it to ``./`` without a ZCode probe would be churn, not a fix.

Runs standalone: python tests/test_codex_manifest_contract.py
"""

import json
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED = {
    "codex": ("./hooks/hooks.codex.json", "hooks/hooks.codex.json"),
    "claude": ("./hooks/hooks.claude.json", "hooks/hooks.claude.json"),
    "zcode": ("hooks/hooks.zcode.json", "hooks/hooks.zcode.json"),
}
MANIFEST_PATHS = {
    "codex": ".codex-plugin/plugin.json",
    "claude": ".claude-plugin/plugin.json",
    "zcode": ".zcode-plugin/plugin.json",
}


class CodexManifestContractTest(unittest.TestCase):
    def test_hooks_paths_match_host_contracts(self):
        for host, (want, _) in EXPECTED.items():
            with self.subTest(host=host):
                manifest = json.loads(
                    (REPO_ROOT / MANIFEST_PATHS[host]).read_text(encoding="utf-8")
                )
                self.assertEqual(
                    manifest.get("hooks"),
                    want,
                    "%s manifest hooks path drifted from the %s host contract "
                    "(issue #108 RC2: codex requires the ./ prefix; zcode requires "
                    "the unprefixed form it loads today)" % (host, host),
                )

    def test_referenced_hooks_files_exist(self):
        for host, (_, fallback) in EXPECTED.items():
            with self.subTest(host=host):
                manifest = json.loads(
                    (REPO_ROOT / MANIFEST_PATHS[host]).read_text(encoding="utf-8")
                )
                for candidate in (manifest.get("hooks"), fallback):
                    if candidate:
                        target = REPO_ROOT / candidate
                        if target.exists():
                            break
                else:
                    self.fail(
                        "%s manifest hooks target missing on disk (neither %r nor %r)"
                        % (host, manifest.get("hooks"), fallback)
                    )

    def test_each_hooks_file_declares_session_start(self):
        for host in EXPECTED:
            with self.subTest(host=host):
                manifest = json.loads(
                    (REPO_ROOT / MANIFEST_PATHS[host]).read_text(encoding="utf-8")
                )
                target = REPO_ROOT / manifest["hooks"]
                spec = json.loads(target.read_text(encoding="utf-8"))
                self.assertIn(
                    "SessionStart",
                    spec.get("hooks", {}),
                    "%s hooks file must declare a SessionStart entry" % host,
                )

    # Issue #188 (Workstream N PR 5 of 6): every Codex entry carries a literal
    # quote-free Windows command (no shell wrapper, no nested quotes) and the
    # seven context-bearing event families declare the 2,000-token
    # additionalContextLimit — PreCompact is omitted because upstream Codex
    # drops additionalContext on PreCompact.
    CONTEXT_FAMILIES = {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "Stop",
        "SubagentStart",
        "SubagentStop",
    }
    EXPECTED_VERBS = [
        "session-start",
        "recall",
        "capture-correction",
        "pretool-recall",
        "convention-capture",
        "capture-failure",
        "reflect",
        "subagent-recall",
        "subagent-reflect",
        "precompact",
    ]

    @staticmethod
    def _codex_entries():
        spec = json.loads(
            (REPO_ROOT / "hooks" / "hooks.codex.json").read_text(encoding="utf-8")
        )
        entries = []
        for event, groups in spec.get("hooks", {}).items():
            for group in groups:
                for hook in group.get("hooks", []):
                    entries.append((event, hook))
        return entries

    def test_codex_entries_have_windows_commands_and_context_limits(self):
        entries = self._codex_entries()
        self.assertEqual(
            len(entries), 10,
            "hooks.codex.json must declare exactly ten hook entries, got %d"
            % len(entries),
        )
        verbs = []
        for event, entry in entries:
            command = entry.get("command", "")
            verb = command.rsplit(" ", 1)[-1] if command else ""
            verbs.append(verb)
            with self.subTest(verb=verb):
                expected = "node ${PLUGIN_ROOT}/hooks/zmem-launch.js %s" % verb
                self.assertEqual(
                    entry.get("commandWindows"), expected,
                    "entry %r commandWindows must be the exact quote-free "
                    "launcher invocation %r" % (verb, expected),
                )
                self.assertNotIn(
                    '"', entry.get("commandWindows", ""),
                    "commandWindows must contain no nested double quotes",
                )
                if event in self.CONTEXT_FAMILIES:
                    self.assertEqual(
                        entry.get("additionalContextLimit"), 2000,
                        "entry %r under event family %s must declare "
                        "additionalContextLimit 2000" % (verb, event),
                    )
                else:
                    self.assertNotIn(
                        "additionalContextLimit", entry,
                        "entry %r under event family %s (not a context-bearing "
                        "family) must omit additionalContextLimit — upstream "
                        "Codex drops additionalContext there" % (verb, event),
                    )
        self.assertEqual(
            verbs, self.EXPECTED_VERBS,
            "the ten entries must keep their existing verbs in manifest order",
        )

    def test_codex_limit_matches_launcher_constant(self):
        probe = (
            'const l=require("./hooks/zmem-launch.js"); '
            "process.stdout.write(JSON.stringify(["
            "l.CODEX_ENVELOPE_CAP_BYTES, l.CHARS_PER_TOKEN, "
            "l.CODEX_ADDITIONAL_CONTEXT_LIMIT]));"
        )
        result = subprocess.run(
            ["node", "-e", probe],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode, 0,
            "launcher constant probe failed: %s" % result.stderr[-400:],
        )
        values = json.loads(result.stdout)
        self.assertEqual(
            values, [8000, 4, 2000],
            "launcher must export [CODEX_ENVELOPE_CAP_BYTES, "
            "CHARS_PER_TOKEN, CODEX_ADDITIONAL_CONTEXT_LIMIT] = "
            "[8000, 4, 2000]",
        )
        self.assertEqual(
            values[2], values[0] // values[1],
            "CODEX_ADDITIONAL_CONTEXT_LIMIT must equal "
            "floor(CODEX_ENVELOPE_CAP_BYTES / CHARS_PER_TOKEN)",
        )


# Issue #187: every ZCode hook entry must be a direct `process` executor entry —
# the executable and its two arguments are fully known, so the host must not own
# shell parsing. Event/matcher/verb/timeout structure is preserved verbatim from
# the shell-form manifest this conversion replaced.
ZCODE_LAUNCHER_ARG0 = "${ZCODE_PLUGIN_ROOT}/hooks/zmem-launch.js"
ZCODE_VERB_BY_EVENT = {
    "SessionStart": "session-start",
    "UserPromptSubmit": "recall",  # first UserPromptSubmit group
    "PostToolUseFailure": "capture-failure",
    "PreToolUse": "pretool-recall",
    "PostToolUse": "convention-capture",
    "Stop": "reflect",
}
ZCODE_MATCHER_BY_EVENT = {
    "SessionStart": ".*",
    "PreToolUse": "Edit|Write|MultiEdit|NotebookEdit|Bash|apply_patch",
    "PostToolUse": "Edit|Write|MultiEdit|NotebookEdit|Bash|apply_patch",
}
ZCODE_SHELL_METACHARACTERS = "|&;`><()"


def _load_zcode_manifest():
    return json.loads(
        (REPO_ROOT / "hooks" / "hooks.zcode.json").read_text(encoding="utf-8")
    )


def _zcode_nested_entries():
    spec = _load_zcode_manifest()
    entries = []
    for event, groups in spec.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                entries.append((event, group, hook))
    return entries


class ZcodeManifestProcessTest(unittest.TestCase):
    def test_zcode_entries_are_process_hooks_with_args(self):
        entries = _zcode_nested_entries()
        self.assertEqual(
            len(entries), 7,
            "hooks.zcode.json must declare exactly seven nested hook entries, "
            "got %d" % len(entries),
        )
        for event, _group, hook in entries:
            with self.subTest(event=event):
                self.assertEqual(
                    hook.get("type"), "process",
                    "%s entry must be type process (issue #187)" % event,
                )
                self.assertEqual(hook.get("command"), "node",
                                 "%s entry must run node directly" % event)
                args = hook.get("args")
                self.assertIsInstance(args, list,
                                      "%s entry must carry an args list" % event)
                self.assertEqual(len(args), 2,
                                 "%s entry args must be [launcher, verb]" % event)
                self.assertEqual(
                    args[0], ZCODE_LAUNCHER_ARG0,
                    "%s entry args[0] must be the plugin-root launcher placeholder"
                    % event,
                )
                stripped = " ".join(
                    arg.replace("${ZCODE_PLUGIN_ROOT}", "") for arg in args
                )
                for ch in ZCODE_SHELL_METACHARACTERS:
                    self.assertNotIn(
                        ch, stripped,
                        "%s entry args must not contain shell metacharacter %r"
                        % (event, ch),
                    )
                self.assertNotIn("sh ", stripped,
                                 "%s entry must not wrap a shell" % event)
                self.assertIsInstance(
                    hook.get("timeout"), int,
                    "%s entry timeout must be a JSON integer" % event,
                )
                self.assertEqual(
                    hook.get("timeout"), 15,
                    "%s entry must keep its timeout of 15" % event,
                )

    def test_zcode_args_preserve_verbs(self):
        events_seen = []
        for event, _group, hook in _zcode_nested_entries():
            events_seen.append(event)
            expected_verb = ZCODE_VERB_BY_EVENT.get(event)
            if expected_verb and events_seen.count(event) == 1:
                self.assertEqual(
                    hook["args"][1], expected_verb,
                    "%s entry must keep its existing verb %s" % (event, expected_verb),
                )
        # The second UserPromptSubmit group keeps capture-correction.
        user_prompt_verbs = [
            hook["args"][1]
            for event, _group, hook in _zcode_nested_entries()
            if event == "UserPromptSubmit"
        ]
        self.assertEqual(
            user_prompt_verbs, ["recall", "capture-correction"],
            "UserPromptSubmit entries must keep recall + capture-correction",
        )
        self.assertEqual(
            sorted(events_seen),
            sorted(list(ZCODE_VERB_BY_EVENT) + ["UserPromptSubmit"]),
            "the seven entries must bind one-to-one to the existing events",
        )
        # Matchers survive the conversion unchanged.
        for event, group, _hook in _zcode_nested_entries():
            with self.subTest(event=event):
                self.assertEqual(
                    group.get("matcher"), ZCODE_MATCHER_BY_EVENT.get(event),
                    "%s matcher must be preserved verbatim" % event,
                )


if __name__ == "__main__":
    unittest.main()
