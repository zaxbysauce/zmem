"""Issue #535 guardrail: the URL-scheme credential family (applied by the
_URLCredentialScannerPattern sentinel occupying the fourth
SECRET_COMMAND_PATTERNS slot) preserves the legacy regex's match semantics
EXACTLY while being linear on adversarial input.

Coverage (plan-critic rounds 1-4 inventory):
- exact password-only-span output through the full redact_secret_like_text
  pipeline, plus the boundary matrix (long/short passwords, punctuation
  scheme prefixes, non-letter scheme starts that legacy left unredacted,
  raw-colon passwords that legacy REDACTS, nested :// inside a password);
- the registry gates: URL-only auto-capture redacts, a credential-bearing
  URL source_ref is refused, and the passive recall classifiers see the
  shape (the round-3 critic's executed bypass simulation, pinned);
- positions mapping through _redact_secret_like_text_with_positions for
  prefix/password/suffix positions, multiple URLs, earlier-pattern length
  deltas, and already-marker no-ops;
- a linearity smoke: three adversarial no-terminating-`@` shapes sized so a
  quadratic regression exceeds the bound by >=8x even on a fast CI runner,
  while linear behavior sits <1/10 of the bound (no flaky CI timing). The
  two `@`-terminated shapes were dropped — a trailing `@` ends the password
  scan immediately, so the legacy quadratic regex clears them too and they
  did not guard against a regression.

Run: python -X utf8 tests/test_redaction_url_linearity.py  (no pytest required)
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

# Hermeticity floor BEFORE any storelib import (STORE_PATH freeze hazard):
# pin the store env to a scratch area and strip ambient project pins.
_SCRATCH = os.path.abspath(tempfile.mkdtemp(prefix="zmem-535-url-"))
_SAVED_ENV = {k: os.environ.get(k) for k in (
    "ZMEM_STORE", "ZMEM_DATA", "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
    "ZMEM_NAMESPACE")}
os.environ["ZMEM_STORE"] = os.path.join(_SCRATCH, "store.sqlite")
os.environ["ZMEM_DATA"] = _SCRATCH
for _marker in ("ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR", "ZMEM_NAMESPACE"):
    os.environ.pop(_marker, None)

import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "skills", "memory", "scripts"))

import redaction as R  # noqa: E402
from storelib import write as W  # noqa: E402
from storelib import recall as RC  # noqa: E402


class PipelineParityMatrixTest(unittest.TestCase):
    """Exact-output matrix through the full pipeline (value-span semantics)."""

    CASES = [
        # (input, expected output)
        ("https://user:pass@host/x",
         "https://user:[REDACTED_SECRET]@host/x"),
        ("see https://al:pw180I@example.test/repo end",
         "see https://al:[REDACTED_SECRET]@example.test/repo end"),
        (".https://u:p@h", ".https://u:[REDACTED_SECRET]@h"),
        ("+https://u:p@h", "+https://u:[REDACTED_SECRET]@h"),
        ("-https://u:p@h", "-https://u:[REDACTED_SECRET]@h"),
        ("foo.https://u:p@h", "foo.https://u:[REDACTED_SECRET]@h"),
        ("HTTP://U:PW@H/", "HTTP://U:[REDACTED_SECRET]@H/"),
        ("a+b-c://u:pw@h", "a+b-c://u:[REDACTED_SECRET]@h"),
        ("(https://u:pw@h)", "(https://u:[REDACTED_SECRET]@h)"),
        # Long passwords stay redacted (the bounded-run anti-pattern would
        # leak these — plan-critic round 1).
        ("https://u:" + "p" * 200 + "@h",
         "https://u:[REDACTED_SECRET]@h"),
        ("https://u:" + "p" * 5000 + "@h",
         "https://u:[REDACTED_SECRET]@h"),
        # Raw-colon passwords stay redacted (round-2 BLOCKER case).
        ("x://u:pa:ss@h", "x://u:[REDACTED_SECRET]@h"),
        # Nested :// inside the password: one span, like the legacy regex.
        ("x://u:a://b@h", "x://u:[REDACTED_SECRET]@h"),
        # Non-letter scheme starts legacy left UNREDACTED (fail-open to
        # redact would be a behavior change in the over-redaction
        # direction; the scanner keeps legacy exactness).
        ("x_https://u:pw@h", "x_https://u:pw@h"),
        ("123://u:pw@h", "123://u:pw@h"),
        ("://u:pw@h", "://u:pw@h"),
        ("9https://u:pw@h", "9https://u:pw@h"),
        # Unicode case-fold schemes legacy REDACTED (round-3 BLOCKER).
        ("\u017f://u:pw@h", "\u017f://u:[REDACTED_SECRET]@h"),
        ("\u0130://u:pw@h", "\u0130://u:[REDACTED_SECRET]@h"),
        ("\u0131://u:pw@h", "\u0131://u:[REDACTED_SECRET]@h"),
        ("\u212a://u:pw@h", "\u212a://u:[REDACTED_SECRET]@h"),
        # ...and the lookbehind fold blocks the same way legacy did.
        ("x_\u017fhttps://u:pw@h", "x_\u017fhttps://u:pw@h"),
        # Empty username: legacy rejects (round-3 MINOR).
        ("x://:pw@h", "x://:pw@h"),
        # Multiple URLs, one token apart.
        ("two https://a:b@h and x://c:d@i end",
         "two https://a:[REDACTED_SECRET]@h and x://c:[REDACTED_SECRET]@i end"),
        # Already-marker idempotence: byte-identical, still counted.
        ("https://u:[REDACTED_SECRET]@h",
         "https://u:[REDACTED_SECRET]@h"),
    ]

    def test_matrix_exact_outputs_and_counts(self):
        for text, expected in self.CASES:
            with self.subTest(text=text[:40]):
                out, count = R.redact_secret_like_text(text)
                self.assertEqual(out, expected)
                if "[REDACTED_SECRET]" not in expected:
                    self.assertEqual(count, 0, "unredacted case must count 0")

    def test_detection_count_idempotence(self):
        once = R.redact_secret_like_text("https://u:pw@h")
        twice = R.redact_secret_like_text(once[0])
        self.assertEqual(twice[0], once[0])
        self.assertEqual(twice[1], 1,
                         "an already-marker span counts once, no rewrite")

    def test_keyvalue_overlap_single_detection(self):
        out, count = R.redact_secret_like_text("password=https://u:pw@h")
        self.assertEqual(out, "password=[REDACTED_SECRET]")
        self.assertEqual(count, 1,
                         "the key=value pattern fires first; the sentinel "
                         "then sees only the marker (sequence position "
                         "preserved)")


class RegistryGateTest(unittest.TestCase):
    """The sentinel lives IN the registries: the capture gates, the
    source_ref refusal, and the passive recall classifiers all see the URL
    credential shape (round-3 BLOCKER: wiring only the redactor would have
    bypassed these)."""

    def test_check_secrets_sees_url_content(self):
        warnings = W._check_secrets("visit https://u:pw@h/x", "")
        self.assertTrue(
            any("pattern" in w for w in warnings),
            "the advisory gate must still flag URL credentials")

    def test_check_credential_shapes_sees_url_source_ref(self):
        hits = W._check_credential_shapes("https://u:pw@h/x")
        self.assertTrue(hits, "the allowlisted source_ref defense must "
                              "still flag credential-bearing URLs")

    def test_auto_capture_redacts_url_only_content(self):
        content, source_ref, tags, warns = W.apply_capture_policy(
            content="visit https://u:pw@h/x",
            source_ref="",
            tags="",
            capture_mode="auto",
        )
        self.assertNotIn("pw@h", content)
        self.assertIn("[REDACTED_SECRET]", content)

    def test_auto_capture_refuses_credential_url_source_ref(self):
        with self.assertRaises(Exception) as raised:
            W.apply_capture_policy(
                content="body",
                source_ref="https://u:pw@h/x",
                tags="",
                capture_mode="auto",
            )
        self.assertIn("source_ref_secret_like", str(raised.exception))

    def test_recall_classifiers_see_url_shape(self):
        self.assertTrue(RC._credential_shaped_text("https://u:pw@h/x"))
        self.assertTrue(RC._classify_credential({"content": "https://u:pw@h"}))


class PositionsMappingTest(unittest.TestCase):
    """Positions mapping through _redact_secret_like_text_with_positions
    (the production consumer is storelib/source.py)."""

    def _map(self, text, positions):
        out, count, mapped = R._redact_secret_like_text_with_positions(
            text, list(positions))
        return out, count, mapped

    def test_prefix_password_suffix_positions(self):
        text = "https://u:pw@host/x"
        # positions 0 (prefix), 10 (inside 'pw'), 13 (suffix 'h' of host)
        out, count, mapped = self._map(text, [0, 10, 13])
        self.assertEqual(out, "https://u:[REDACTED_SECRET]@host/x")
        self.assertEqual(count, 1)
        # prefix maps to itself; the password position maps to the marker
        # start; the suffix position shifts by the marker-vs-'pw' delta
        # (17 - 2 = +15).
        self.assertEqual(
            mapped, [0, 10, 13 + len("[REDACTED_SECRET]") - 2])

    def test_multiple_urls(self):
        text = "https://a:bb@h and https://c:dd@i"
        out, count, mapped = self._map(text, [11, 30])
        self.assertEqual(count, 2)
        # 11 is the second 'b' of the first password -> first marker start
        # (10). 30 is the second 'd' of the second password: positions
        # inside a replaced span map to that marker's start — the second
        # password starts at 29, shifted by the first marker's delta
        # (+15): 29 + 15 = 44.
        self.assertEqual(mapped, [10, 44])

    def test_earlier_pattern_length_delta(self):
        # An earlier pattern (key=value) changes the length BEFORE the URL:
        # later positions must shift by the accumulated delta. The key=value
        # value span is 'hunter2see' (>= 8 chars), so the whole token is
        # replaced before the URL is scanned.
        text = "password=hunter2see https://u:pw@h"
        out, count, mapped = self._map(text, [26])
        self.assertEqual(count, 2)
        self.assertIn("password=[REDACTED_SECRET] https", out)
        # Position 26 is the 'u' user prefix: delta from the key=value
        # replacement is len(marker) - len('hunter2see') = 17 - 10 = +7.
        self.assertEqual(mapped, [26 + len("[REDACTED_SECRET]") - 10])

    def test_already_marker_position_noop(self):
        text = "https://u:[REDACTED_SECRET]@h"
        out, count, mapped = self._map(text, [10])
        self.assertEqual(out, text)
        self.assertEqual(count, 1)
        self.assertEqual(mapped, [10])


class LinearitySmokeTest(unittest.TestCase):
    """Reduced-size timing smoke: bounds a quadratic regression would
    exceed by >=8x even on a fast CI runner while linear behavior sits
    <1/10 of the bound. The frozen C2 check drives the full-cap shapes;
    this guards the shipped pipeline in CI without a flaky wall-clock.
    Only no-terminating-`@` shapes are included: an `@` ends the password
    scan immediately, so `@`-terminated shapes do not exercise the
    quadratic path and pass under the legacy regex too."""

    BOUND_S = 0.5

    def _assert_linear(self, text):
        start = time.perf_counter()
        R.redact_secret_like_text(text)
        elapsed = time.perf_counter() - start
        self.assertLess(
            elapsed, self.BOUND_S,
            "quadratic regression: %d chars took %.3fs (the legacy shape "
            "scales ~n^2/1e9; linear is sub-millisecond at this size)"
            % (len(text), elapsed))

    def test_dot_alternating(self):
        self._assert_linear("a." * 16384)

    def test_scheme_chain_no_at(self):
        self._assert_linear("x://u:" * 8192)

    def test_scheme_chain_discriminates_the_barrier_memo(self):
        # A memo-less scan (the barrier check disabled) measures ~2.2s at
        # this size (measured on the M3 mutant, 2026-11-22: 852ms at 36k,
        # 2173ms at 60k, 4032ms at 84k chars) while the memo'd scanner sits
        # in single-digit milliseconds — the 1.0s bound sits between them
        # with >2x margin on both sides.
        start = time.perf_counter()
        R.redact_secret_like_text("x://u:" * 10000)
        elapsed = time.perf_counter() - start
        self.assertLess(
            elapsed, 1.0,
            "barrier-memo regression: 60k-char scheme chain took %.3fs "
            "(memo'd scanner is single-digit ms; the memo-less mutant "
            "measures ~2.2s here)" % elapsed)


def tearDownModule():
    for _key, _prior in _SAVED_ENV.items():
        if _prior is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _prior
    import shutil
    shutil.rmtree(_SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
