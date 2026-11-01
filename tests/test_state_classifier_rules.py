"""classify-state rule narrowing regression pins (issue #348).

The original expressions matched any dotted token (config.json, README.md,
node.js, US.East — any file extension passes `[a-z]{2,}` case-insensitively)
and bare SETTING words in ordinary prose ("the current default", "version 3
of the plan"), so nearly every row received a proposal and the advisory
`changed` count carried no signal. These pins hold the narrowed contract:
the filename-collision policy (a TLD is bare-host-eligible only when it is
not a common filename extension; scheme-ful URLs accept any host; retained
collisions com/svc carry documented host-family exceptions), the SETTING
vocabulary-shape rule, the endpoint > metric > setting precedence, and the
issue-#174 golden vocabulary. RED at base, GREEN after the fix.
"""
import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import _store_guard  # noqa: F401  (issue #244 isolation guard)

from storelib import state


def _matched_rule(text):
    for name, pattern in state._RULES:
        if pattern.search(text):
            return name
    return None


class ClassifierNarrowingTest(unittest.TestCase):
    # The issue's own negative vocabulary plus the collision corpus: every
    # entry must match NO rule after the narrowing.
    NEGATIVES = (
        "config.json", "README.md notes", "node.js runtime", "US.East region",
        "file.txt backup", "the current default", "version 3 of the plan",
        "deploy.sh", "read.me", "main.in", "index.co", "build.pl",
        "image.ai", "application.app", "sound.au", "archive.br",
    )

    # Golden vocabulary (classify-expected.json rows) plus the policy's
    # bare-host allowlist members and SETTING shape arms.
    POSITIVES = (
        ("api.example.com", "endpoint"),
        ("request latency p95", "metric"),
        ("configured timeout", "setting"),
        ("https://internal.svc:8443/health", "endpoint"),
        ("cache.local", "endpoint"),
        ("db.internal", "endpoint"),
        ("kubernetes.default.svc", "endpoint"),
        ("https://example.me", "endpoint"),
        ("https://myapp.ai", "endpoint"),
        ("default timeout", "setting"),
        ("timeout=30", "setting"),
        ("version: 3", "setting"),
    )

    def test_issue_vocabulary_and_collision_corpus_match_nothing(self):
        for text in self.NEGATIVES:
            self.assertIsNone(
                _matched_rule(text),
                f"negative vocabulary must not match a rule: {text!r}")

    def test_golden_and_policy_positives_keep_matching(self):
        for text, want in self.POSITIVES:
            self.assertEqual(
                _matched_rule(text), want,
                f"positive vocabulary must keep matching {want}: {text!r}")

    def test_precedence_endpoint_beats_metric_beats_setting(self):
        # The golden rows never exercise competing matches; the documented
        # endpoint > metric > setting order must hold when several match.
        self.assertEqual(
            _matched_rule("https://api.example.com latency p95"), "endpoint")
        self.assertEqual(
            _matched_rule("metric configured timeout"), "metric")

    def test_changed_counter_contract_unchanged(self):
        # The #174 frozen golden pins changed=3 over its four rows; the
        # narrowed expressions must leave that mapping byte-stable (pinned
        # end-to-end by test_store_characterization.StateClassifierTest).
        self.assertEqual(
            _matched_rule("memory note"), None,
            "the golden's negative row must stay unmatched")


if __name__ == "__main__":
    unittest.main(verbosity=2)
