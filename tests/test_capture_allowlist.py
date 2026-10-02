"""Issue #71 F: capture-mode `auto` allowlists structured provenance schemes.

Field report (#71): `ingest-jsonl --capture-mode auto` refused valid rows
because `source_ref` looked secret-like (32-hex db ids, hash-shaped file
names). The generic hex/base64 SECRET_PATTERNS were written for free text but
source_ref is STRUCTURED provenance. This pins the allowlist contract:

- `db:` / `hindsight:` / `session:` / `zmem-queue:` / `file:<relative>` refs
  are stored in auto mode even when they carry hex/base64-shaped text;
- CREDENTIAL shapes (key=value, PEM headers, gh*_ tokens, AKIA) still refuse
  on allowlisted refs — defense in depth;
- `file:` absolute remainders (drive letter, POSIX-absolute, UNC, ~) refuse;
- content/tags scanning is UNCHANGED in auto mode; since issue #180 the
  `reviewed` mode RAISES on credential-shaped source_refs (exit 2) while
  `manual` stays advisory-only — see test_reviewed_mode_never_refuses;
- the write result surfaces a structured `source_ref_allowlisted` warning so
  the redacted/refused/warnings triad stays honest.

Run: python tests/test_capture_allowlist.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(SCRIPTS_DIR / "storelib"))

STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

from storelib.write import CapturePolicyRefusal, apply_capture_policy  # noqa: E402


def _base_env(tmp: str) -> dict:
    env = {**os.environ}
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env["ZMEM_DATA"] = tmp
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["PYTHONUTF8"] = "1"
    return env


def _ingest_row(tmp: str, source_ref: str, content: str = "fleet provenance row"):
    """Run the real ingest-jsonl auto path and return (returncode, stderr)."""
    env = _base_env(tmp)
    subprocess.run([PYTHON, str(STORE_PY), "stats"], capture_output=True,
                   text=True, env=env, check=True)
    row = {"id": str(uuid.uuid4()), "namespace": "user:global", "type": "fact",
           "content": content, "signal": "none", "confidence": 0.7,
           "source_ref": source_ref}
    jl = Path(tmp) / "rows.jsonl"
    jl.write_text(json.dumps(row) + "\n", encoding="utf-8")
    r = subprocess.run([PYTHON, str(STORE_PY), "ingest-jsonl", "--in", str(jl),
                        "--capture-mode", "auto"],
                       capture_output=True, text=True, env=env)
    return r


def _row_stored(tmp: str, source_ref: str) -> bool:
    conn = __import__("sqlite3").connect(os.path.join(tmp, "store.sqlite"))
    try:
        n = conn.execute("SELECT COUNT(*) FROM memory WHERE source_ref=?",
                         (source_ref,)).fetchone()[0]
        return n > 0
    finally:
        conn.close()


class AllowlistedSchemeStoredTest(unittest.TestCase):
    """The issue's exact refusal shapes must now store in auto mode."""

    def _stored(self, source_ref):
        tmp = tempfile.mkdtemp(prefix="zmem-allow-")
        self.addCleanup(shutil.rmtree, tmp, True)
        r = _ingest_row(tmp, source_ref)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("refused", r.stderr.lower(), r.stderr)
        self.assertTrue(_row_stored(tmp, source_ref), source_ref)

    def test_db_scheme_with_hex_row_id(self):
        self._stored("db:memory:0123456789abcdef0123456789abcdef")

    def test_hindsight_scheme_with_run_id(self):
        self._stored("hindsight:run-8f14e45fceea167a5a36dedd4bea2543")

    def test_session_scheme_with_uuid(self):
        self._stored("session:d3b07384d9ed4bd4b4e5c0e2f6a1b2c3d4e5f6a7")

    def test_zmem_queue_scheme(self):
        self._stored("zmem-queue:item-7f3a2b9c4d5e6f807172737475767778")

    def test_file_relative_path_with_hex_name(self):
        self._stored("file:build/abcdef0123456789abcdef0123456789.md")

    def test_file_well_known_stem(self):
        self._stored("file:codex-MEMORY.md")


class CredentialShapeStillRefusedTest(unittest.TestCase):
    """Defense in depth: allowlisted schemes never carry real credentials."""

    def _refused(self, source_ref, mode="auto"):
        tmp = tempfile.mkdtemp(prefix="zmem-allow-cred-")
        self.addCleanup(shutil.rmtree, tmp, True)
        content, _, _, warnings = apply_capture_policy(
            content="probe", source_ref=source_ref, tags="",
            capture_mode=mode)
        return content, warnings

    def test_db_ref_with_pem_header_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("db:keys:-----BEGIN PRIVATE KEY-----")

    def test_db_ref_with_ghp_token_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("db:tokens:ghp_" + "a" * 36)

    def test_db_ref_with_akia_key_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("db:aws:AKIA" + "B2C3D4F5G6H7" + "AB" + "12")

    def test_session_ref_with_api_key_pair_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("session:config api_key=supersecretvalue123")

    def test_allowlisted_ref_with_github_fine_grained_pat_refuses(self):
        # PRR-001: github_pat_ is a distinct shape from ghp_/gho_/… and was
        # stored verbatim on an allowlisted ref before the fix.
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("db:tokens:github_pat_" + "A1b2C3d4E5" * 4)

    def test_allowlisted_ref_with_slack_token_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("db:slack:xoxb-" + "a1B2c3D4e5F6" * 2)

    def test_allowlisted_ref_with_anthropic_token_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("db:anthropic:sk-ant-" + "A1b2C3d4E5f6" * 3)

    def test_allowlisted_ref_with_google_key_refuses(self):
        # Real Google API keys are 39 chars total: AIza + 35.
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("db:gcp:AIza" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r")

    def test_allowlisted_ref_with_jwt_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("db:jwt:eyJ" + "A1b2C3d4" * 8 + ".eyJ" + "x9Y8z7W6" * 4)

    def test_reviewed_mode_never_refuses(self):
        # Issue #180 retarget (the method name is historical — the DoD forbids
        # renames): the issue's Design step 1 governs — `reviewed` RAISES on a
        # credential-shaped ref where `manual` stays advisory-only, so an
        # explicit review claim can never carry a secret-shaped source_ref.
        with self.assertRaises(CapturePolicyRefusal) as ctx:
            self._refused("db:tokens:ghp_" + "a" * 36, mode="reviewed")
        self.assertEqual(ctx.exception.reason, "source_ref_secret_like")
        # Manual keeps the original advisory contract.
        content, _, _, warnings = apply_capture_policy(
            content="probe", source_ref="db:tokens:ghp_" + "a" * 36,
            tags="", capture_mode="manual")
        self.assertEqual(content, "probe")
        self.assertTrue(any(w["type"] == "advisory" for w in warnings),
                        warnings)


class FileAbsoluteRefusedTest(unittest.TestCase):
    """`file:` is the one scheme with a shape rule: relative only."""

    def _refused(self, source_ref):
        apply_capture_policy(content="probe", source_ref=source_ref,
                              tags="", capture_mode="auto")

    def test_windows_drive_absolute_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("file:C:/Users/<user>/secrets.txt")

    def test_windows_backslash_absolute_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("file:C:\\Users\\<user>\\secrets.txt")

    def test_posix_absolute_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("file:/home/<user>/secrets.txt")

    def test_unc_path_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("file://srv/share/secrets.txt")

    def test_home_relative_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("file:~/secrets.txt")


class ContentScanningUnchangedTest(unittest.TestCase):
    """The allowlist relaxes ONLY source_ref scanning. Content with a secret
    still redacts in auto mode even when the source_ref is allowlisted."""

    def test_content_secret_redacted_with_allowlisted_ref(self):
        content, _, _, warnings = apply_capture_policy(
            content="the token is ghp_" + "a" * 36,
            source_ref="db:memory:0123456789abcdef0123456789abcdef",
            tags="", capture_mode="auto")
        self.assertIn("[REDACTED_SECRET]", content)
        self.assertNotIn("ghp_", content)
        self.assertTrue(any(w["type"] == "redacted" for w in warnings))

    def test_allowlist_warning_surfaced(self):
        _, _, _, warnings = apply_capture_policy(
            content="clean content", tags="",
            source_ref="db:memory:0123456789abcdef0123456789abcdef",
            capture_mode="auto")
        by_type = {w["type"] for w in warnings}
        self.assertIn("source_ref_allowlisted", by_type)
        warn = next(w for w in warnings if w["type"] == "source_ref_allowlisted")
        self.assertEqual(warn["scheme"], "db:")

    def test_unallowlisted_hex_ref_still_refuses(self):
        # The 32-hex shape OUTSIDE an allowlisted scheme refuses as before —
        # the relaxation is scoped to the provenance schemes only.
        with self.assertRaises(CapturePolicyRefusal):
            apply_capture_policy(content="probe", tags="",
                                  source_ref="build 0123456789abcdef0123456789abcdef",
                                  capture_mode="auto")


class IngestEndToEndContractTest(unittest.TestCase):
    """End-to-end: the exact row from the issue's field report now stores."""

    def test_field_report_row_stores_in_auto(self):
        tmp = tempfile.mkdtemp(prefix="zmem-allow-e2e-")
        self.addCleanup(shutil.rmtree, tmp, True)
        ref = "db:memory:0123456789abcdef0123456789abcdef"
        r = _ingest_row(tmp, ref)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("refused 1 row", r.stderr)
        self.assertTrue(_row_stored(tmp, ref))


class FileTraversalRefusedTest(unittest.TestCase):
    """PRR-008: `file:../` parent-traversal refs are refused — a relative
    ref is otherwise allowlisted, and _source_hash reads CWD-relative
    bytes, so traversal would hash files outside the project."""

    def _refused(self, source_ref):
        apply_capture_policy(content="probe", source_ref=source_ref,
                              tags="", capture_mode="auto")

    def test_posix_traversal_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("file:../secrets.txt")

    def test_nested_traversal_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("file:docs/../../etc/passwd")

    def test_backslash_traversal_refuses(self):
        with self.assertRaises(CapturePolicyRefusal):
            self._refused("file:..\secrets.txt")

    def test_inner_stays_allowed(self):
        # A directory merely NAMED with dots is not traversal.
        _, _, _, warnings = apply_capture_policy(
            content="probe", tags="", source_ref="file:docs/v1.2/notes.md",
            capture_mode="auto")
        self.assertTrue(any(w["type"] == "source_ref_allowlisted"
                            for w in warnings))


class CaptureAllowlistTest(unittest.TestCase):
    """Issue #180: the capture policy is importable ONLY under its public
    name — the pre-#180 private spelling is gone from the package surface.
    The private name is CONSTRUCTED so this file carries no literal."""

    def test_only_public_policy_is_imported(self):
        private_name = "_" + "apply_capture_policy"
        import storelib
        import storelib.write
        self.assertTrue(hasattr(storelib, "apply_capture_policy"))
        self.assertTrue(hasattr(storelib.write, "apply_capture_policy"))
        self.assertFalse(hasattr(storelib, private_name),
                         "the private policy symbol must not survive on the "
                         "storelib package surface")
        self.assertFalse(hasattr(storelib.write, private_name),
                         "the private policy symbol must not survive on the "
                         "storelib.write module surface")


if __name__ == "__main__":
    unittest.main(verbosity=2)
