"""Check C3 — issue #443 AC3: `store.py export-pack` withholds
credential-shaped rows from the rendered Tier 1 markdown pack.

Law under test: the export-pack renderer reduces a credential-shaped row to
the withhold treatment — the credential material never reaches the pack
stdout. The pack is read as CONTEXT by other agents (docs/CLOUD.md Tier 1
contract), so a scrub-bypassing row (direct sqlite surgery / legacy store)
must not leak through the bullet any more than through `list`. The pack
TITLE echoes the --namespace filter, so a credential-shaped NAMESPACE must
not re-leak through the title either.

Discrimination (verified at the pre-fix worktree):
- test_pack_credential_row_withheld FAILs at base — the credential row's
  bullet renders `api_key=AKIAIOSFODNN7EXAMPLE` verbatim.
- test_pack_credential_namespace_title_withheld FAILs at base — the
  `# Memory pack: user:key=AKIAIOSFODNN7EXAMPLE` title echoes the filter
  verbatim.
- test_withheld_bullet_carries_marker FAILs at base — the token renders
  verbatim AND no `[WITHHELD: SECRET]` marker exists anywhere (the row is
  never silently dropped post-fix: the withheld bullet must carry a visible
  marker).
- test_global_section_credential_withheld FAILs at base — a credential row
  INSERTed directly into `user:global` renders its token verbatim in the
  `## Cross-project lessons (user:global)` section of another namespace's
  pack.
- test_empty_pack_refusal_sanitized FAILs at base — the exit-2 empty-pack
  refusal echoes `namespace=user:key=AKIAIOSFODNN7EXAMPLE` (raw token) into
  stderr.
- test_pack_structure_preserved is the preserving leg and PASSes at base
  (exit 0, benign bullet present, header/section/bullet markers intact).

`export-pack --namespace` is REQUIRED (no unscoped variant exists — see the
parser registration), so both drives are scoped. The credential rows are
seeded by DIRECT sqlite INSERT (the sanctioned scrub-bypass simulation; the
CLI writer redacts at add time). A raw INSERT fires the FTS triggers
automatically — fine.

Run: python -X utf8 tests/test_export_pack_withhold.py
"""

from __future__ import annotations

# -- env pinned BEFORE the issue-#244 isolation guard loads (the guard reads
# the ambient env at import time; an unpinned import aborts the whole file
# on a box with a real default store). Same ordering as
# tests/test_import_ns_control.py.
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
PYTHON = sys.executable

_TMP = tempfile.mkdtemp(prefix="zmem-c3-")
os.environ.update({
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
})
for _k in ("ZMEM_BACKUP_DIR", "ZMEM_HOME", "CLAUDE_PLUGIN_DATA",
           "ZCODE_PLUGIN_DATA", "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
           "ZMEM_SESSION", "ZMEM_SESSION_ID"):
    os.environ.pop(_k, None)
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

_PINNED_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODEL_AUTODOWNLOAD",
                "PYTHONIOENCODING", "ZMEM_BACKUP_DIR", "ZMEM_HOME",
                "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
                "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
                "ZMEM_SESSION", "ZMEM_SESSION_ID")
_SAVED_ENV = {key: os.environ.get(key) for key in _PINNED_KEYS}


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the ambient env (pytest co-run citizenship)."""
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(_TMP, ignore_errors=True)


NAMESPACE = "user:c3probe"
CRED_NS = "user:key=AKIAIOSFODNN7EXAMPLE"
TOKEN = "AKIAIOSFODNN7EXAMPLE"
BENIGN_CONTENT = "benign export-pack row"
CRED_CONTENT = f"deploy note api_key={TOKEN} rotate quarterly"
CRED_ID = "eeee5555-6666-7777-8888-999900001111"
NSCRED_ID = "eeee5555-6666-7777-8888-999900001112"
GLOBAL_NS = "user:global"
GLOBAL_CRED_ID = "eeee5555-6666-7777-8888-999900001113"
GLOBAL_CRED_CONTENT = f"global lesson api_key={TOKEN} rotate now"
GLOBAL_SECTION_HEADING = f"## Cross-project lessons ({GLOBAL_NS})"
WITHHELD_MARKER = "[WITHHELD: SECRET]"
UNRELATED_NS = "user:unrelated-c3"

# Ambient zmem/project markers are stripped from every child process so the
# subprocess resolves exactly the per-test scratch store (storelib freezes
# STORE_PATH at first import — an unpinned child could touch the shared
# operator store). Mirrors tests/test_add_namespace_hostless.py's pattern.
_STRIP_ENV = (
    "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_HOST",
    "ZMEM_QUERY_CONTEXT", "ZMEM_INJECT", "ZMEM_INJECT_TOKEN_BUDGET",
    "ZMEM_INJECT_FLOOR_LEX", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODELS_DIR",
    "ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_REMOTE_URL", "ZMEM_AUTO_REKEY",
    "CLAUDE_SESSION_ID", "ZCODE_SESSION_ID", "ZMEM_BACKUP_DIR",
    "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT",
    "ZCODE_PLUGIN_ROOT", "PLUGIN_ROOT", "PLUGIN_DATA",
    "ZCODE_PROJECT_DIR", "CLAUDE_PROJECT_DIR",
)


def _excerpt(proc: subprocess.CompletedProcess, tail: int = 1500) -> str:
    return (f"rc={proc.returncode}\n--- stdout (tail) ---\n"
            f"{proc.stdout[-tail:]!r}\n--- stderr (tail) ---\n"
            f"{proc.stderr[-tail:]!r}")


class ExportPackWithholdBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-c3-leg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = self.tmp / "store.sqlite"
        self.env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        self.env.update({
            "ZMEM_STORE": str(self.store),
            "ZMEM_DATA": str(self.tmp),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONUTF8": "1",
        })
        self._seed()

    def _seed(self) -> None:
        # One benign row through the REAL writer (also materializes the
        # schema-complete store).
        r = self._run("add", "--namespace", NAMESPACE, "--type", "fact",
                      "--content", BENIGN_CONTENT, "--confidence", "0.9")
        self.assertEqual(r.returncode, 0,
                         f"seed add failed:\n{_excerpt(r)}")
        # Two scrub-bypassing rows via direct sqlite INSERT.
        conn = sqlite3.connect(str(self.store))
        try:
            for mid, ns, content in (
                    (CRED_ID, NAMESPACE, CRED_CONTENT),
                    (NSCRED_ID, CRED_NS,
                     "benign row under credential-shaped namespace")):
                conn.execute(
                    "INSERT INTO memory (id, namespace, type, content, tags,"
                    " source_ref, confidence, signal, ingestion_ts)"
                    " VALUES (?, ?, 'fact', ?, '', 'session:c3', 0.9,"
                    " 'none', datetime('now'))",
                    (mid, ns, content))
            conn.commit()
        finally:
            conn.close()

    def _run(self, *args, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=timeout,
        )

    def _pack(self, namespace: str) -> subprocess.CompletedProcess:
        return self._run("export-pack", "--namespace", namespace)


class PackStructurePreservedTest(ExportPackWithholdBase):
    """PRESERVING leg — the pack's structure stays intact at base and must
    keep staying intact post-fix (the withhold only touches the rows that
    carry credential material)."""

    def test_pack_structure_preserved(self):
        r = self._pack(NAMESPACE)
        self.assertEqual(r.returncode, 0,
                         f"export-pack must exit 0 with live rows; "
                         f"observed:\n{_excerpt(r)}")
        # Header marker.
        self.assertIn(
            "# Memory pack: ", r.stdout,
            f"C3 preserving: the pack header must still render. Observed "
            f"stdout:\n{r.stdout!r}")
        # Section marker.
        self.assertIn(
            "## Project knowledge", r.stdout,
            f"C3 preserving: the Project knowledge section heading must "
            f"still render. Observed stdout:\n{r.stdout!r}")
        # Bullet row marker.
        self.assertRegex(
            r.stdout, re.escape("- **["),
            f"C3 preserving: bullet rows must keep their '- **[type/signal]**'"
            f" shape. Observed stdout:\n{r.stdout!r}")
        # The benign row's content renders.
        self.assertIn(
            BENIGN_CONTENT, r.stdout,
            f"C3 preserving: the benign row's content must render in the "
            f"pack. Observed stdout:\n{r.stdout!r}")


class PackCredentialWithheldTest(ExportPackWithholdBase):
    """DISCRIMINATING legs — at base the credential row's bullet and the
    credential-shaped namespace's pack title both leak the raw token."""

    def test_pack_credential_row_withheld(self):
        r = self._pack(NAMESPACE)
        self.assertEqual(r.returncode, 0,
                         f"export-pack must exit 0 with live rows; "
                         f"observed:\n{_excerpt(r)}")
        self.assertNotIn(
            TOKEN, r.stdout,
            f"C3: export-pack leaked the raw credential token — the row "
            f"seeded via direct sqlite INSERT (scrub-bypassing) rendered its "
            f"content verbatim in a pack bullet. The pack is agent-read "
            f"context (Tier 1), so the read-time withhold law must apply "
            f"here too. Observed stdout:\n{r.stdout!r}")

    def test_pack_credential_namespace_title_withheld(self):
        r = self._pack(CRED_NS)
        self.assertEqual(r.returncode, 0,
                         f"export-pack must exit 0 with live rows; "
                         f"observed:\n{_excerpt(r)}")
        # The ns-only row: benign content, credential-shaped NAMESPACE. The
        # token must not appear anywhere — at base the pack TITLE
        # (`# Memory pack: user:key=AKIA...`) echoes the filter verbatim.
        self.assertNotIn(
            TOKEN, r.stdout,
            f"C3: export-pack leaked the raw credential token through the "
            f"credential-shaped --namespace filter (the pack title echoes "
            f"the namespace verbatim at base). Observed "
            f"stdout:\n{r.stdout!r}")

    def test_withheld_bullet_carries_marker(self):
        # Same seed as test_pack_credential_row_withheld (the scrub-bypassing
        # CRED row lives in the project namespace). The withhold treatment
        # must be VISIBLE: the withheld row's bullet renders a
        # `[WITHHELD: SECRET]` marker in the pack body — the row is never
        # silently dropped (an operator reading the pack must be able to see
        # a memory was withheld, and why). At base the token renders verbatim
        # and no marker exists anywhere.
        r = self._pack(NAMESPACE)
        self.assertEqual(r.returncode, 0,
                         f"export-pack must exit 0 with live rows; "
                         f"observed:\n{_excerpt(r)}")
        self.assertNotIn(
            TOKEN, r.stdout,
            f"C3: export-pack leaked the raw credential token — the withheld "
            f"bullet must never carry the credential material. Observed "
            f"stdout:\n{r.stdout!r}")
        self.assertIn(
            WITHHELD_MARKER, r.stdout,
            f"C3: a withheld row must render a visible {WITHHELD_MARKER!r} "
            f"marker in the pack body — silently dropping the row hides that "
            f"a memory exists at all. Observed stdout (marker absent):\n"
            f"{r.stdout!r}")

    def test_global_section_credential_withheld(self):
        # A credential row planted DIRECTLY in user:global (scrub-bypassing
        # sqlite INSERT) rides the global section of ANOTHER namespace's
        # pack: `## Cross-project lessons (user:global)` (see _render_pack in
        # storelib/sync.py). The token must not reach stdout and the global
        # section must carry the withhold marker. At base the global section
        # bullet renders the token verbatim.
        conn = sqlite3.connect(str(self.store))
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " source_ref, confidence, signal, ingestion_ts)"
                " VALUES (?, ?, 'fact', ?, '', 'session:c3', 0.9,"
                " 'none', datetime('now'))",
                (GLOBAL_CRED_ID, GLOBAL_NS, GLOBAL_CRED_CONTENT))
            conn.commit()
        finally:
            conn.close()
        r = self._pack(NAMESPACE)
        self.assertEqual(r.returncode, 0,
                         f"export-pack must exit 0 with live rows; "
                         f"observed:\n{_excerpt(r)}")
        self.assertNotIn(
            TOKEN, r.stdout,
            f"C3: export-pack leaked the raw credential token through the "
            f"global ({GLOBAL_NS}) section — a scrub-bypassing user:global "
            f"row rides every project namespace's pack. Observed "
            f"stdout:\n{r.stdout!r}")
        idx = r.stdout.find(GLOBAL_SECTION_HEADING)
        self.assertNotEqual(
            idx, -1,
            f"C3: the global section heading {GLOBAL_SECTION_HEADING!r} must "
            f"still render in the pack. Observed stdout:\n{r.stdout!r}")
        self.assertIn(
            WITHHELD_MARKER, r.stdout[idx:],
            f"C3: the global section must carry the {WITHHELD_MARKER!r} "
            f"marker for the withheld user:global row (never a silent "
            f"drop). Observed global section:\n{r.stdout[idx:]!r}")


class PackEmptyRefusalSanitizedTest(ExportPackWithholdBase):
    """DISCRIMINATING — at base the exit-2 empty-pack refusal echoes the
    credential-shaped --namespace (raw token included) verbatim into
    stderr (cmd_export_pack's refusal line interpolates `namespace=`)."""

    def _seed(self) -> None:
        # Materialize the schema-complete store via a benign add in an
        # UNRELATED third namespace only: both the requested namespace
        # (CRED_NS) and user:global must hold ZERO qualifying rows so
        # cmd_export_pack takes its empty-pack refusal branch (exit 2). The
        # standard _seed is deliberately NOT used (it plants a benign row
        # in CRED_NS, which would make the pack non-empty).
        r = self._run("add", "--namespace", UNRELATED_NS, "--type", "fact",
                      "--content", BENIGN_CONTENT, "--confidence", "0.9")
        self.assertEqual(r.returncode, 0,
                         f"seed add failed:\n{_excerpt(r)}")

    def test_empty_pack_refusal_sanitized(self):
        r = self._pack(CRED_NS)
        self.assertEqual(
            r.returncode, 2,
            f"C3: the empty-pack refusal must keep exiting 2 (no live rows "
            f"in {CRED_NS!r} or {GLOBAL_NS!r}). Observed:\n{_excerpt(r)}")
        combined = r.stdout + "\n--stderr--\n" + r.stderr
        self.assertNotIn(
            TOKEN, combined,
            f"C3: the exit-2 empty-pack refusal leaked the raw credential "
            f"token by echoing the credential-shaped --namespace verbatim "
            f"(the refusal line interpolates namespace={CRED_NS!r}). The "
            f"refusal text must be sanitized. Observed combined "
            f"output:\n{combined!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
