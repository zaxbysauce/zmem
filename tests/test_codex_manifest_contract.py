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


if __name__ == "__main__":
    unittest.main()
