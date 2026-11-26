"""AC1 (#502) frozen acceptance check - redaction shape/context calibration.

CONTRACT (public seam: ``redaction.redact_secret_like_text``, UNCHANGED
signature ``(text) -> (redacted_text, detection_count)``; the corpus lives in
tests/fixtures/redaction_calibration/corpus.json):

1. PRESERVATION: every corpus entry labeled ``benign``/``preserved`` passes
   through redaction BYTE-IDENTICAL. These are verifiable-identifier forms in
   realistic contexts: a 40-hex release SHA in a sentence, a 40-hex commit SHA,
   a 32-hex MD5-styled value behind an ``md5:`` label, a version string, a
   short hex id below 32 chars, a 64-hex ``sha256:`` checksum of a public
   artifact, a credential-free URL, and a 40-hex git tree object behind the
   word ``object``. The calibration may use these contexts; it may NOT keep
   destroying the identifiers.
2. REDACTION: every corpus entry labeled ``credential``/``redacted`` comes back
   changed, with ``detection_count >= 1`` and the entry's
   ``credential_value`` substring ABSENT from the output. The corpus includes
   the two confirmed base survivors (``Authorization: Basic <short base64>``
   and ``DB_PASS=<value>``) plus vendor/command/URL/key=value shapes.
3. FAIL-SAFE NET (root cause RC-1): the ``unlabeled-hex-run`` credential entry
   pins that a 32+ hex run with NO verifiable-identifier context is still
   redacted - the fix calibrates by shape/context, it never drops the generic
   patterns.
4. PRESERVING invariants (#535/#537): Bearer tokens stay redacted; the URL
   credential scanner still replaces ONLY the password value span
   (scheme/user/host preserved); the key=value pattern still replaces ONLY the
   value for ``password=<value>`` input.

Base status: this file COLLECTS and RUNS against base 0ada1b83 and is
deliberately RED in both directions - benign hex entries are destroyed by
SECRET_GENERIC_PATTERNS and the Basic/DB_PASS credentials survive undetected.
"""
from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from redaction import redact_secret_like_text  # noqa: E402

CORPUS_PATH = (Path(__file__).resolve().parents[1] / "tests" / "fixtures"
               / "redaction_calibration" / "corpus.json")


def _corpus() -> list[dict]:
    with CORPUS_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    entries = payload["corpus"]
    assert entries and isinstance(entries, list)
    return entries


class CredentialCalibrationTests(unittest.TestCase):
    maxDiff = None

    def test_benign_identifiers_survive_byte_identical(self) -> None:
        for entry in _corpus():
            if entry["kind"] != "benign":
                continue
            with self.subTest(label=entry["label"]):
                redacted, _count = redact_secret_like_text(entry["text"])
                self.assertEqual(redacted, entry["text"])

    def test_credential_fixtures_are_redacted(self) -> None:
        for entry in _corpus():
            if entry["kind"] != "credential":
                continue
            with self.subTest(label=entry["label"]):
                redacted, count = redact_secret_like_text(entry["text"])
                self.assertGreaterEqual(count, 1)
                self.assertNotEqual(redacted, entry["text"])
                self.assertNotIn(entry["credential_value"], redacted)

    def test_preserving_invariants_bearer_url_and_keyvalue_spans(self) -> None:
        # #537-family invariant: the Bearer detector stays ahead of any
        # calibration and still swallows the token.
        bearer = "Authorization: Bearer ff00ddee1122334455aabbcc"
        bearer_redacted, bearer_count = redact_secret_like_text(bearer)
        self.assertGreaterEqual(bearer_count, 1)
        self.assertNotIn("ff00ddee1122334455aabbcc", bearer_redacted)

        # #535 invariant: the URL credential sentinel replaces ONLY the
        # password value span; scheme, user and host survive verbatim.
        url = "postgres://deploy:hunter2-secret@db.internal:5432/prod"
        url_redacted, url_count = redact_secret_like_text(url)
        self.assertGreaterEqual(url_count, 1)
        self.assertEqual(url_redacted,
                         "postgres://deploy:[REDACTED_SECRET]@db.internal:5432/prod")

        # #180/#502 invariant: key=value span semantics replace only the value.
        pair = "password=CorrectHorse9!"
        pair_redacted, pair_count = redact_secret_like_text(pair)
        self.assertGreaterEqual(pair_count, 1)
        self.assertEqual(pair_redacted, "password=[REDACTED_SECRET]")


if __name__ == "__main__":
    unittest.main()
