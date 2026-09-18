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
