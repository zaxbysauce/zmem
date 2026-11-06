"""Check C1 — issue #443 AC1: `store.py list` withholds credential-shaped rows
on BOTH render arms (human and --json).

Law under test: the operator listing surface reduces a credential-shaped row
to the withhold treatment at RENDER time — the row is still LISTED (its id
survives), but the credential material never reaches stdout, and a withhold
marker (or an empty preview, on the JSON arm) discloses WHY. This is the same
read-time law the recall lanes already carry (`_withhold_credential_rows` in
storelib/recall.py): rows that BYPASS the writer's add-time redaction —
manual sqlite surgery, legacy stores — must not leak through `list` either.

The classification input is FULL content (plan F1 classifies the whole
content field and truncates for display only AFTER the withhold decision),
so these tests deliberately assert NOTHING about preview truncation width
for withheld rows — only token absence, id presence, and marker/empty
preview. The NAMESPACE itself is in scope: a row whose namespace carries
the credential must not re-leak it through the `ns=` field (human arm), the
entry's namespace field (JSON arm), or the top-level namespace echo of a
`--json --namespace <cred>` listing.

Discrimination (verified at the pre-fix worktree):
- The *_withheld / *_no_token tests FAIL at base — the raw AWS-key token is
  printed verbatim in both arms (preview, ns= field, and the JSON
  namespace echo).
- The *_shape_preserved tests are the preserving legs and PASS at base
  (exit 0, ids listed, benign row byte-shape unchanged, JSON count == 3).

Round-3 addition (InjectionTagWithheldTest): a credential-shaped row that
ALSO carries the repo's injection-risk tag (`prompt-injection-risk`) is
withheld on both arms (token absent, JSON tags blanked,
withheld_for_secret=true) while the human line's `⚠injection-risk` marker
still renders — the marker assertion passes in both worlds; the token-absent
and tags-blank assertions are the discriminating legs.

The credential row is seeded by DIRECT sqlite INSERT (the sanctioned way to
simulate a scrub-bypassing row; the CLI writer redacts credentials at add
time, so it cannot produce this fixture). A raw INSERT fires the FTS
triggers automatically — fine.

Run: python -X utf8 tests/test_operator_surface_withhold.py
"""

from __future__ import annotations

# -- env pinned BEFORE the issue-#244 isolation guard loads (the guard reads
# the ambient env at import time; an unpinned import aborts the whole file
# on a box with a real default store). Same ordering as
# tests/test_import_ns_control.py.
import json
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

_TMP = tempfile.mkdtemp(prefix="zmem-c1-")
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


NAMESPACE = "project:c1probe"
TOKEN = "AKIAIOSFODNN7EXAMPLE"
BENIGN_CONTENT = "benign operator surface row"
CRED_CONTENT = f"deploy note api_key={TOKEN} rotate quarterly"
CRED_NS = f"project:key={TOKEN}"
CRED_ID = "aaaa1111-2222-4333-8444-5555666677ff"
NSCRED_ID = "bbbb2222-3333-4444-8555-6666777788ff"
# Round-2 special rows (REVISION 3 F1: classification runs on FULL content —
# these rows push the credential past the 80-char preview window, ride the
# superseded tombstone column, or carry it in source_ref).
BEYOND80_ID = "aaaa1111-2222-4333-8444-555566667801"
BEYOND80_PREFIX = "b" * 90  # 90 benign chars; the credential starts past 80
SUPERSEDED_ID = "aaaa1111-2222-4333-8444-555566667802"
SOURCEREF_ID = "aaaa1111-2222-4333-8444-555566667803"
SOURCEREF_CRED = f"password={TOKEN}"  # matches the key=value credential registry
# Round-3 addition: the repo's injection-risk tag (the exact literal
# _has_injection_risk_tag checks in storelib/write.py, consumed at read time
# by storelib/recall.py:list_memory) and the human-line marker it renders
# (" \u26a0injection-risk" — recall.py's #36 M5 read-time surfacing).
INJECTIONTAG_ID = "aaaa1111-2222-4333-8444-555566667804"
INJECTION_RISK_TAG = "prompt-injection-risk"
INJECTION_MARKER = "⚠injection-risk"

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


def _excerpt(proc: subprocess.CompletedProcess, tail: int = 1200) -> str:
    return (f"rc={proc.returncode}\n--- stdout (tail) ---\n"
            f"{proc.stdout[-tail:]!r}\n--- stderr (tail) ---\n"
            f"{proc.stderr[-tail:]!r}")


class OperatorSurfaceWithholdBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-c1-leg-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = self.tmp / "store.sqlite"
        self.env = self._child_env()
        self._seed()

    def _child_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
        env.update({
            "ZMEM_STORE": str(self.store),
            "ZMEM_DATA": str(self.tmp),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "PYTHONUTF8": "1",
        })
        return env

    def _run(self, *args, timeout=120) -> subprocess.CompletedProcess:
        return subprocess.run(
            [PYTHON, "-X", "utf8", str(STORE_PY), *args],
            env=self.env, capture_output=True, text=True, timeout=timeout,
        )

    def _seed(self) -> None:
        # The standard 3-row census: one benign row through the REAL writer
        # plus two scrub-bypassing rows via direct sqlite INSERT.
        self._seed_benign()
        self._insert_row(CRED_ID, NAMESPACE, CRED_CONTENT)
        self._insert_row(NSCRED_ID, CRED_NS,
                         "benign row under credential-shaped namespace")

    def _seed_benign(self) -> None:
        # One benign row through the REAL writer (also materializes the
        # schema-complete store).
        r = self._run("add", "--namespace", NAMESPACE, "--type", "fact",
                      "--content", BENIGN_CONTENT, "--confidence", "0.9")
        self.assertEqual(r.returncode, 0, f"seed add failed:\n{_excerpt(r)}")

    def _insert_row(self, mid: str, ns: str, content: str, *,
                    source_ref: str = "session:c1",
                    superseded_at: str | None = None,
                    tags: str = "") -> None:
        """Direct sqlite INSERT — the sanctioned scrub-bypass simulation
        (the CLI writer redacts credentials at add time; the read-time law
        exists for rows that never saw the writer). A raw INSERT fires the
        FTS triggers automatically — fine."""
        conn = sqlite3.connect(str(self.store))
        try:
            conn.execute(
                "INSERT INTO memory (id, namespace, type, content, tags,"
                " source_ref, confidence, signal, ingestion_ts,"
                " superseded_at)"
                " VALUES (?, ?, 'fact', ?, ?, ?, 0.9, 'none',"
                " datetime('now'), ?)",
                (mid, ns, content, tags, source_ref, superseded_at))
            conn.commit()
        finally:
            conn.close()

    def _line_for(self, stdout: str, needle: str) -> str:
        for line in stdout.splitlines():
            if needle in line:
                return line
        return ""

    def _ns_field_of(self, line: str) -> str:
        m = re.search(r"\bns=(.*?) type=", line)
        return m.group(1) if m else None

    def _benign_id(self) -> str:
        conn = sqlite3.connect(str(self.store))
        try:
            row = conn.execute(
                "SELECT id FROM memory WHERE content=? AND namespace=?",
                (BENIGN_CONTENT, NAMESPACE)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row, "benign seed row not found in store")
        return row[0]


class HumanArmScopedTest(OperatorSurfaceWithholdBase):
    """`list --namespace project:c1probe --limit 10` (human render)."""

    def test_scoped_listing_shape_preserved(self):
        """PRESERVING leg — passes at base and must keep passing post-fix."""
        r = self._run("list", "--namespace", NAMESPACE, "--limit", "10")
        # a) exit 0 — the listing itself must succeed.
        self.assertEqual(r.returncode, 0,
                         "list must exit 0; observed:\n" + _excerpt(r))
        # c) the credential row is still LISTED (id present), not dropped.
        self.assertIn(
            CRED_ID, r.stdout,
            f"C1 human arm (scoped): the credential row's id must still be "
            f"listed (withheld, not dropped). Observed stdout:\n{r.stdout!r}")
        # e) the benign row renders byte-shape unchanged.
        benign_line = self._line_for(r.stdout, BENIGN_CONTENT)
        self.assertIn(
            ":: " + BENIGN_CONTENT, benign_line,
            f"C1 human arm (scoped): the benign row's line shape changed. "
            f"Expected ':: {BENIGN_CONTENT}' in its line; observed:\n"
            f"{benign_line!r}\nfull stdout:\n{r.stdout!r}")

    def test_scoped_credential_row_withheld(self):
        """DISCRIMINATING — fails at base (token leaks; no marker).

        Note: no assertion on preview truncation width here — the post-fix
        classification runs on FULL content (plan F1) and truncates only
        after the withhold decision, so width is not part of this law."""
        r = self._run("list", "--namespace", NAMESPACE, "--limit", "10")
        self.assertEqual(r.returncode, 0,
                         "list must exit 0; observed:\n" + _excerpt(r))
        # b) the credential token never reaches stdout.
        self.assertNotIn(
            TOKEN, r.stdout,
            f"C1 human arm (scoped): raw credential token leaked in list "
            f"stdout. The row was seeded via direct sqlite INSERT "
            f"(scrub-bypassing); the operator listing surface must reduce it "
            f"to the withhold treatment at render time. Observed "
            f"stdout:\n{r.stdout!r}")
        # d) the row's own line carries a withhold marker.
        line = self._line_for(r.stdout, CRED_ID)
        self.assertRegex(
            line, r"(?i)withheld",
            f"C1 human arm (scoped): the credential row's line carries no "
            f"withhold marker. Its observed line:\n{line!r}\n"
            f"full stdout:\n{r.stdout!r}")


class HumanArmUnscopedTest(OperatorSurfaceWithholdBase):
    """Unscoped `list --limit 10` must also cover the row whose NAMESPACE is
    itself credential-shaped (a scoped listing of project:c1probe would never
    show it)."""

    def test_unscoped_listing_shape_preserved(self):
        """PRESERVING leg — passes at base and must keep passing post-fix."""
        r = self._run("list", "--limit", "10")
        self.assertEqual(r.returncode, 0,
                         "list must exit 0; observed:\n" + _excerpt(r))
        for mid in (CRED_ID, NSCRED_ID):
            self.assertIn(
                mid, r.stdout,
                f"C1 human arm (unscoped): row {mid} missing from the "
                f"listing (withheld must not mean dropped). Observed "
                f"stdout:\n{r.stdout!r}")
        benign_line = self._line_for(r.stdout, BENIGN_CONTENT)
        self.assertIn(":: " + BENIGN_CONTENT, benign_line,
                      f"benign row line changed; observed:\n{benign_line!r}")

    def test_unscoped_no_token_anywhere(self):
        """DISCRIMINATING — fails at base (content preview AND ns= leak)."""
        r = self._run("list", "--limit", "10")
        self.assertEqual(r.returncode, 0,
                         "list must exit 0; observed:\n" + _excerpt(r))
        # f) no raw token anywhere in the listing — including the row whose
        # NAMESPACE carries the credential.
        self.assertNotIn(
            TOKEN, r.stdout,
            f"C1 human arm (unscoped): raw credential token leaked — either "
            f"the content preview of the credential row or the ns= field of "
            f"the credential-shaped-namespace row. Observed "
            f"stdout:\n{r.stdout!r}")
        # f, second assertion: the namespace-only row's ns= field must be
        # blanked (or the row's line carries a withhold marker) — the
        # withheld row must not re-leak through its own ns field.
        line = self._line_for(r.stdout, NSCRED_ID)
        self.assertNotIn(
            TOKEN, line,
            f"C1 human arm (unscoped): the credential-shaped NAMESPACE leaked "
            f"through its own row line. Observed line:\n{line!r}")
        ns_value = self._ns_field_of(line)
        marker = bool(re.search(r"(?i)withheld", line))
        self.assertTrue(
            ns_value == "" or marker,
            f"C1 human arm (unscoped): the namespace-only row's ns= field "
            f"must be blanked or its line must carry a withhold marker; "
            f"observed ns={ns_value!r} (marker={marker}) on line "
            f"{line!r}\nfull stdout:\n{r.stdout!r}")


class JsonArmTest(OperatorSurfaceWithholdBase):
    """`list --limit 10 --json` (machine render)."""

    def _payload(self, r):
        try:
            return json.loads(r.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"list --json stdout is not parseable JSON ({exc}); "
                      f"observed stdout:\n{r.stdout!r}")

    def test_json_payload_shape_preserved(self):
        """PRESERVING leg — passes at base and must keep passing post-fix."""
        r = self._run("list", "--limit", "10", "--json")
        self.assertEqual(r.returncode, 0,
                         "list --json must exit 0; observed:\n" + _excerpt(r))
        payload = self._payload(r)
        # c') count == number of seeded live rows (withheld rows stay listed).
        self.assertEqual(
            payload.get("count"), 3,
            f"C1 json arm: count must equal the 3 seeded live rows "
            f"(withheld rows stay listed). Observed count="
            f"{payload.get('count')!r}\npayload:\n"
            f"{json.dumps(payload, indent=2)[:2000]}")
        entries = {e.get("id"): e for e in payload.get("memories", [])}
        for mid in (CRED_ID, NSCRED_ID):
            self.assertIn(
                mid, entries,
                f"C1 json arm: entry for {mid} missing; payload:\n"
                f"{json.dumps(payload, indent=2)[:2000]}")
        # benign entry's preview is byte-identical.
        benign = entries.get(self._benign_id())
        self.assertIsNotNone(benign, "benign row entry missing")
        self.assertEqual(
            benign.get("preview"), BENIGN_CONTENT,
            f"C1 json arm: benign row preview changed. Observed:\n"
            f"{json.dumps(benign, indent=2)!s}")

    def test_json_credential_entry_withheld(self):
        """DISCRIMINATING — fails at base (preview and namespace leak).

        No preview-width assertions: classification is on FULL content
        (plan F1); display truncation happens after the withhold."""
        r = self._run("list", "--limit", "10", "--json")
        self.assertEqual(r.returncode, 0,
                         "list --json must exit 0; observed:\n" + _excerpt(r))
        # b') no raw token anywhere in the raw stdout.
        self.assertNotIn(
            TOKEN, r.stdout,
            f"C1 json arm: raw credential token leaked in list --json "
            f"stdout (content preview and/or namespace field). Observed "
            f"stdout:\n{r.stdout!r}")
        payload = self._payload(r)
        entries = {e.get("id"): e for e in payload.get("memories", [])}
        # d') the credential row's entry is withheld — preview free of the
        # token AND (empty preview or a withhold marker).
        entry = entries.get(CRED_ID)
        self.assertIsNotNone(
            entry, f"C1 json arm: credential row entry missing; "
                   f"payload:\n{json.dumps(payload, indent=2)[:2000]}")
        preview = str(entry.get("preview") or "")
        self.assertNotIn(
            TOKEN, preview,
            f"C1 json arm: credential row preview leaks the token. Observed "
            f"entry:\n{json.dumps(entry, indent=2)!s}")
        marker = bool(re.search(r"(?i)withheld|secret", json.dumps(entry)))
        self.assertTrue(
            preview == "" or marker,
            f"C1 json arm: the credential row's entry carries neither an "
            f"empty preview nor a withhold marker (any key/value like "
            f"withheld/secret). Observed entry:\n"
            f"{json.dumps(entry, indent=2)!s}")
        # f') the credential-shaped NAMESPACE entry leaks nowhere either.
        ns_entry = entries.get(NSCRED_ID)
        self.assertIsNotNone(
            ns_entry, f"C1 json arm: credential-shaped-namespace entry "
                      f"missing; payload:\n"
                      f"{json.dumps(payload, indent=2)[:2000]}")
        self.assertNotIn(
            TOKEN, json.dumps(ns_entry),
            f"C1 json arm: the credential-shaped NAMESPACE leaked through "
            f"its entry. Observed entry:\n"
            f"{json.dumps(ns_entry, indent=2)!s}")

    def test_json_withheld_entry_key_survival(self):
        """DISCRIMINATING (via the additive key) — REVISION 3 F1: every
        EXISTING JSON key survives on a withheld row (unsafe values
        blanked), and the row carries the additive withheld_for_secret
        marker. At base the keys exist but withheld_for_secret does not."""
        r = self._run("list", "--limit", "10", "--json")
        self.assertEqual(r.returncode, 0,
                         "list --json must exit 0; observed:\n" + _excerpt(r))
        payload = self._payload(r)
        entries = {e.get("id"): e for e in payload.get("memories", [])}
        entry = entries.get(CRED_ID)
        self.assertIsNotNone(
            entry, f"C1 json arm: credential row entry missing; payload:\n"
                   f"{json.dumps(payload, indent=2)[:2000]}")
        required = {"id", "namespace", "type", "preview", "confidence",
                    "signal", "tags", "superseded_at"}
        missing = required - set(entry.keys())
        self.assertEqual(
            missing, set(),
            f"C1 json arm: the withheld entry must keep EVERY existing key "
            f"(unsafe values blanked, keys never dropped); observed entry "
            f"missing {sorted(missing)}:\n{json.dumps(entry, indent=2)!s}")
        self.assertIs(
            entry.get("withheld_for_secret"), True,
            f"C1 json arm: the withheld entry must carry the additive "
            f"withheld_for_secret=true marker (REVISION 3 F1). Observed "
            f"entry:\n{json.dumps(entry, indent=2)!s}")

    def test_json_scoped_namespace_echo_withheld(self):
        """DISCRIMINATING — the top-level JSON `namespace` echo of a scoped
        listing over the credential-shaped namespace must not re-leak the
        token. At base the payload's "namespace" key echoes the filter
        verbatim."""
        r = self._run("list", "--namespace", CRED_NS, "--limit", "10",
                      "--json")
        self.assertEqual(r.returncode, 0,
                         "list --json must exit 0; observed:\n" + _excerpt(r))
        self.assertNotIn(
            TOKEN, r.stdout,
            f"C1 json arm (scoped echo): the top-level namespace echo leaked "
            f"the credential-shaped --namespace filter verbatim. Observed "
            f"stdout:\n{r.stdout!r}")
        # The scoped row itself is still listed (id present).
        self.assertIn(
            NSCRED_ID, r.stdout,
            f"C1 json arm (scoped echo): the namespace-only row must still "
            f"be listed under its own namespace filter. Observed "
            f"stdout:\n{r.stdout!r}")


class SpecialRowBase(OperatorSurfaceWithholdBase):
    """Base for the round-2 single-special-row legs: the benign row plus
    ONE scrub-bypassing row shaped for the law under test (REVISION 3 F1 —
    classification runs on FULL content, so the credential can live past
    the 80-char preview window, in the tombstone column's row, or in
    source_ref)."""

    SPECIAL_ID = ""
    SPECIAL_NS = NAMESPACE
    SPECIAL_CONTENT = ""
    SPECIAL_SOURCE_REF = "session:c1"
    SPECIAL_SUPERSEDED_AT: str | None = None
    SPECIAL_TAGS = ""

    def _seed(self) -> None:
        self._seed_benign()
        self._insert_row(
            self.SPECIAL_ID, self.SPECIAL_NS, self.SPECIAL_CONTENT,
            source_ref=self.SPECIAL_SOURCE_REF,
            superseded_at=self.SPECIAL_SUPERSEDED_AT,
            tags=self.SPECIAL_TAGS)


class BeyondEightyWithholdTest(SpecialRowBase):
    """The credential starts PAST the 80-char preview window: at base the
    preview hides the token, so the DISCRIMINATING assertion is the withhold
    marker / empty preview (classification must run on FULL content), not
    token absence."""

    SPECIAL_ID = BEYOND80_ID
    SPECIAL_CONTENT = f"{BEYOND80_PREFIX} api_key={TOKEN} rotate quarterly"

    def test_beyond_eighty_row_withheld_on_both_arms(self):
        # Sanity: the token really starts past the preview window.
        self.assertGreater(
            self.SPECIAL_CONTENT.index(TOKEN), 80,
            "fixture sanity: the credential must start past char 80")
        human = self._run("list", "--namespace", NAMESPACE, "--limit", "10")
        self.assertEqual(human.returncode, 0,
                         "list must exit 0; observed:\n" + _excerpt(human))
        line = self._line_for(human.stdout, BEYOND80_ID)
        self.assertIn(
            BEYOND80_ID, line,
            f"C1 beyond-80 (human): the row must still be listed; observed "
            f"stdout:\n{human.stdout!r}")
        self.assertNotIn(
            TOKEN, line,
            f"C1 beyond-80 (human): the token must not reach the line; "
            f"observed line:\n{line!r}")
        self.assertRegex(
            line, r"(?i)withheld",
            f"C1 beyond-80 (human): the row is credential-shaped on FULL "
            f"content (the token merely starts past the 80-char preview "
            f"window) — its line must carry the withhold marker. Observed "
            f"line:\n{line!r}\nfull stdout:\n{human.stdout!r}")
        js = self._run("list", "--namespace", NAMESPACE, "--limit", "10",
                       "--json")
        self.assertEqual(js.returncode, 0,
                         "list --json must exit 0; observed:\n"
                         + _excerpt(js))
        payload = json.loads(js.stdout)
        entry = {e.get("id"): e for e in payload.get("memories", [])}.get(
            BEYOND80_ID)
        self.assertIsNotNone(
            entry, f"C1 beyond-80 (json): entry missing; "
                   f"payload:\n{js.stdout!r}")
        preview = str(entry.get("preview") or "")
        self.assertNotIn(
            TOKEN, preview,
            f"C1 beyond-80 (json): preview must not carry the token; "
            f"observed entry:\n{json.dumps(entry, indent=2)!s}")
        marker = bool(re.search(r"(?i)withheld|secret", json.dumps(entry)))
        self.assertTrue(
            preview == "" or marker,
            f"C1 beyond-80 (json): classification is on FULL content, so "
            f"the row must still be withheld (empty preview or a withhold "
            f"marker) even though the 80-char preview window would have "
            f"hidden the token. Observed entry:\n"
            f"{json.dumps(entry, indent=2)!s}")


class SupersededWithheldTest(SpecialRowBase):
    """A TOMBSTONED credential row listed with --include-superseded: the
    status must keep rendering SUPERSEDED (the real column) while the
    credential is withheld."""

    SPECIAL_ID = SUPERSEDED_ID
    SPECIAL_CONTENT = CRED_CONTENT
    SPECIAL_SUPERSEDED_AT = "2026-01-01T00:00:00Z"

    def test_superseded_credential_row_withheld(self):
        r = self._run("list", "--namespace", NAMESPACE, "--limit", "10",
                      "--include-superseded")
        self.assertEqual(r.returncode, 0,
                         "list --include-superseded must exit 0; observed:\n"
                         + _excerpt(r))
        line = self._line_for(r.stdout, SUPERSEDED_ID)
        self.assertIn(
            SUPERSEDED_ID, line,
            f"C1 superseded: the tombstoned row must be listed under "
            f"--include-superseded; observed stdout:\n{r.stdout!r}")
        # PRESERVING: the status stays SUPERSEDED (rendered from the real
        # column), never 'live'.
        self.assertIn(
            "SUPERSEDED", line,
            f"C1 superseded: the row's status must render SUPERSEDED; "
            f"observed line:\n{line!r}")
        # DISCRIMINATING: token absent + withhold marker.
        self.assertNotIn(
            TOKEN, line,
            f"C1 superseded: the tombstoned credential row leaked the raw "
            f"token through --include-superseded. Observed line:\n{line!r}")
        self.assertRegex(
            line, r"(?i)withheld",
            f"C1 superseded: the tombstoned credential row must carry the "
            f"withhold marker (withhold applies to superseded rows too). "
            f"Observed line:\n{line!r}")


class SourceRefWithheldTest(SpecialRowBase):
    """Benign CONTENT but a credential-shaped source_ref: classification
    inputs are content/source_ref/tags, so the row must be withheld.
    source_ref is not rendered by list, so the DISCRIMINATING assertion is
    the withhold marker / empty preview (at base the benign preview renders
    with no marker)."""

    SPECIAL_ID = SOURCEREF_ID
    SPECIAL_CONTENT = "benign content with credential only in source_ref"
    SPECIAL_SOURCE_REF = SOURCEREF_CRED

    def test_source_ref_credential_row_withheld(self):
        human = self._run("list", "--namespace", NAMESPACE, "--limit", "10")
        self.assertEqual(human.returncode, 0,
                         "list must exit 0; observed:\n" + _excerpt(human))
        line = self._line_for(human.stdout, SOURCEREF_ID)
        self.assertIn(
            SOURCEREF_ID, line,
            f"C1 source_ref (human): the row must still be listed; observed "
            f"stdout:\n{human.stdout!r}")
        self.assertNotIn(
            TOKEN, human.stdout,
            f"C1 source_ref (human): the token must not reach stdout; "
            f"observed:\n{human.stdout!r}")
        self.assertRegex(
            line, r"(?i)withheld",
            f"C1 source_ref (human): source_ref is a classification input, "
            f"so a row whose source_ref carries a credential "
            f"({SOURCEREF_CRED!r}) must be withheld (marker on its line). "
            f"Observed line:\n{line!r}")
        js = self._run("list", "--namespace", NAMESPACE, "--limit", "10",
                       "--json")
        self.assertEqual(js.returncode, 0,
                         "list --json must exit 0; observed:\n"
                         + _excerpt(js))
        payload = json.loads(js.stdout)
        entry = {e.get("id"): e for e in payload.get("memories", [])}.get(
            SOURCEREF_ID)
        self.assertIsNotNone(
            entry, f"C1 source_ref (json): entry missing; "
                   f"payload:\n{js.stdout!r}")
        preview = str(entry.get("preview") or "")
        marker = bool(re.search(r"(?i)withheld|secret", json.dumps(entry)))
        self.assertTrue(
            preview == "" or marker,
            f"C1 source_ref (json): the row must be withheld (empty preview "
            f"or marker) because its source_ref matches the credential "
            f"registry. Observed entry:\n{json.dumps(entry, indent=2)!s}")


class InjectionTagWithheldTest(SpecialRowBase):
    """A credential-shaped row that ALSO carries the repo's injection-risk tag
    (`prompt-injection-risk` — the exact literal `_has_injection_risk_tag`
    checks in storelib/write.py, consumed at read time by recall.py's
    list_memory to render the `⚠injection-risk` marker, #36 M5).

    Law: the row gets the full WITHHOLD treatment (token never reaches
    stdout, JSON tags value blanked, withheld_for_secret=true) AND the
    injection marker still renders on the human line — classification may
    blank the tags VALUE but must not silently drop the read-time injection
    warning computed from it.

    DISCRIMINATING at base: the raw token leaks on the human line and the
    JSON entry renders the raw tag with no withheld_for_secret marker. The
    marker-still-renders assertion passes in BOTH worlds (at base the raw
    tags render, so the marker shows; post-fix it must survive the blanking)
    — the load-bearing cross-world pin."""

    SPECIAL_ID = INJECTIONTAG_ID
    SPECIAL_CONTENT = CRED_CONTENT  # api_key=... — credential-shaped
    SPECIAL_TAGS = INJECTION_RISK_TAG

    def test_injection_tag_marker_survives_on_withheld_row(self):
        human = self._run("list", "--namespace", NAMESPACE, "--limit", "10")
        self.assertEqual(human.returncode, 0,
                         "list must exit 0; observed:\n" + _excerpt(human))
        # The row is still listed (id present).
        line = self._line_for(human.stdout, INJECTIONTAG_ID)
        self.assertIn(
            INJECTIONTAG_ID, line,
            f"C1 injection-tag (human): the row must still be listed; "
            f"observed stdout:\n{human.stdout!r}")
        # DISCRIMINATING at base: the credential token leaks verbatim.
        self.assertNotIn(
            TOKEN, human.stdout,
            f"C1 injection-tag (human): the raw credential token leaked — a "
            f"credential-shaped row carrying the injection-risk tag gets the "
            f"withhold treatment at render time; the tag must not exempt it. "
            f"Observed stdout:\n{human.stdout!r}")
        # Cross-world pin (passes at base AND post-fix): the ⚠injection-risk
        # marker still renders on the row's own line.
        self.assertIn(
            INJECTION_MARKER, line,
            f"C1 injection-tag (human): the {INJECTION_MARKER!r} marker must "
            f"still render on the withheld row's line (withholding blanks "
            f"the tags VALUE, never the injection warning computed from it). "
            f"Observed line:\n{line!r}\nfull stdout:\n{human.stdout!r}")
        self.assertRegex(
            line, r"(?i)withheld",
            f"C1 injection-tag (human): the row's line carries no withhold "
            f"marker. Observed line:\n{line!r}\nfull stdout:\n{human.stdout!r}")
        # JSON arm: the entry is withheld (withheld_for_secret true) and its
        # tags field is BLANKED — the raw tag value must not ride along.
        js = self._run("list", "--namespace", NAMESPACE, "--limit", "10",
                       "--json")
        self.assertEqual(js.returncode, 0,
                         "list --json must exit 0; observed:\n" + _excerpt(js))
        self.assertNotIn(
            TOKEN, js.stdout,
            f"C1 injection-tag (json): the raw credential token leaked in "
            f"list --json stdout. Observed stdout:\n{js.stdout!r}")
        payload = json.loads(js.stdout)
        entry = {e.get("id"): e for e in payload.get("memories", [])}.get(
            INJECTIONTAG_ID)
        self.assertIsNotNone(
            entry, f"C1 injection-tag (json): entry missing; "
                   f"payload:\n{js.stdout!r}")
        self.assertIs(
            entry.get("withheld_for_secret"), True,
            f"C1 injection-tag (json): the entry must carry the additive "
            f"withheld_for_secret=true marker (the injection-risk tag must "
            f"not exempt a credential-shaped row from the withhold). "
            f"Observed entry:\n{json.dumps(entry, indent=2)!s}")
        self.assertEqual(
            entry.get("tags"), "",
            f"C1 injection-tag (json): the withheld entry's tags field must "
            f"be BLANKED (unsafe values blanked, keys kept) — not the raw "
            f"{INJECTION_RISK_TAG!r}. Observed entry:\n"
            f"{json.dumps(entry, indent=2)!s}")
        self.assertNotIn(
            TOKEN, json.dumps(entry),
            f"C1 injection-tag (json): the withheld entry leaks the token "
            f"somewhere. Observed entry:\n{json.dumps(entry, indent=2)!s}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
