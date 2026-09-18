"""Wave D1 tests for the pure ambiguity/rewrite and evidence-read helpers."""

from __future__ import annotations

import atexit
import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "memory" / "scripts"
FIXTURES = ROOT / "tests" / "fixtures" / "ambiguity"
sys.path.insert(0, str(SCRIPTS))

_ROUTE_ENV_KEYS = (
    "ZMEM_STORE", "ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA",
    "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD", "ZMEM_MODEL_URL",
    "ZMEM_EMBED_PROFILE", "ZMEM_CROSS_ENCODER_MODEL", "HOME", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA",
)
_IMPORT_ENV = {key: os.environ.get(key) for key in _ROUTE_ENV_KEYS}
_IMPORT_SANDBOX = Path(tempfile.mkdtemp(prefix="zmem-query-ambiguity-import-"))
atexit.register(shutil.rmtree, _IMPORT_SANDBOX, ignore_errors=True)
for _key in _ROUTE_ENV_KEYS:
    os.environ.pop(_key, None)
os.environ.update({
    "ZMEM_STORE": str(_IMPORT_SANDBOX / "store.sqlite"),
    "ZMEM_DATA": str(_IMPORT_SANDBOX / "data"),
    "ZMEM_MODELS_DIR": str(_IMPORT_SANDBOX / "models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "HOME": str(_IMPORT_SANDBOX / "home"),
    "USERPROFILE": str(_IMPORT_SANDBOX / "home"),
    "APPDATA": str(_IMPORT_SANDBOX / "appdata"),
    "LOCALAPPDATA": str(_IMPORT_SANDBOX / "localappdata"),
})
try:
    from storelib.query_ambiguity import (  # noqa: E402
        AMBIG_MIN_TERMS_ENV,
        EDIT_BASENAME_LIMIT,
        REWRITE_MAX_CHARS,
        is_ambiguous_prompt,
        read_recent_edit_basenames,
        rewrite_ambiguous_query,
    )
finally:
    for _key, _value in _IMPORT_ENV.items():
        if _value is None:
            os.environ.pop(_key, None)
        else:
            os.environ[_key] = _value


class AmbiguityClassifierTest(unittest.TestCase):
    def test_twenty_prompt_flags(self):
        cases = json.loads((FIXTURES / "prompts.json").read_text(encoding="utf-8"))
        results = [
            {"id": case["id"], "ambiguous": is_ambiguous_prompt(case["prompt"])}
            for case in cases
        ]
        actual = json.dumps(results, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        self.assertEqual(actual, (FIXTURES / "prompts.expected.json").read_bytes())
        self.assertEqual(
            [case["ambiguous"] for case in cases],
            [result["ambiguous"] for result in results],
        )

    def test_threshold_is_positive_and_read_at_call_time(self):
        original = os.environ.get(AMBIG_MIN_TERMS_ENV)
        try:
            os.environ[AMBIG_MIN_TERMS_ENV] = "3"
            self.assertFalse(is_ambiguous_prompt("one two three"))
            os.environ[AMBIG_MIN_TERMS_ENV] = "0"
            self.assertTrue(is_ambiguous_prompt("one two three"))
            os.environ[AMBIG_MIN_TERMS_ENV] = "not-an-int"
            self.assertTrue(is_ambiguous_prompt("one two three"))
            self.assertFalse(is_ambiguous_prompt("one two three", min_terms=3))
            self.assertTrue(is_ambiguous_prompt("one two three", min_terms=0))
        finally:
            if original is None:
                os.environ.pop(AMBIG_MIN_TERMS_ENV, None)
            else:
                os.environ[AMBIG_MIN_TERMS_ENV] = original

    def test_malformed_controls_and_overlong_tokens_fail_open(self):
        self.assertFalse(is_ambiguous_prompt(None))  # type: ignore[arg-type]
        self.assertFalse(is_ambiguous_prompt("please\x00help"))
        self.assertTrue(is_ambiguous_prompt("x" * (REWRITE_MAX_CHARS + 1)))
        self.assertFalse(is_ambiguous_prompt("x" * 4097))


class RewriteFixtureTest(unittest.TestCase):
    def test_continue_fixture_rewrites_exactly(self):
        fixture = json.loads(
            (FIXTURES / "continue-from-yesterday.json").read_text(encoding="utf-8")
        )
        query, rewritten = rewrite_ambiguous_query(
            fixture["prompt"],
            ops_tokens=fixture["ops_tokens"],
            edited_basenames=fixture["edited_basenames"],
        )
        actual = json.dumps(
            {"query": query, "rewrite": int(rewritten)},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode() + b"\n"
        self.assertEqual(actual, (FIXTURES / "continue-from-yesterday.expected.json").read_bytes())
        self.assertLessEqual(len(query), REWRITE_MAX_CHARS)

    def test_exact_tokens_and_empty_context_do_not_rewrite(self):
        for prompt in (
            "skills/memory/scripts/store.py", "storelib.schema", "memory_entity",
            "project::alpha", "foo::", "::foo", "::", "--dry-run", "ValueError", "RuntimeException",
            "Error,", "Exception.",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(
                    rewrite_ambiguous_query(
                        prompt, ops_tokens=["git"], edited_basenames=["recall.py"]
                    ),
                    (prompt, False),
                )
        for prompt in ("project:alpha", "user:global", "(project:alpha)"):
            with self.subTest(prompt=prompt):
                self.assertFalse(is_ambiguous_prompt(prompt))
        self.assertEqual(
            rewrite_ambiguous_query(
                "continue from yesterday", ops_tokens=[], edited_basenames=[]
            ),
            ("continue from yesterday", False),
        )
        original = "  project::alpha  "
        self.assertEqual(
            rewrite_ambiguous_query(
                original, ops_tokens=["git"], edited_basenames=["recall.py"]
            ),
            ("project::alpha", False),
        )

    def test_context_uses_first_twelve_ops_and_newest_three_basenames(self):
        query, rewritten = rewrite_ambiguous_query(
            "continue",
            ops_tokens=[f"op-{index}" for index in range(14)],
            edited_basenames=["one.py", "two.py", "three.py", "four.py"],
        )
        self.assertTrue(rewritten)
        self.assertIn("op-0", query)
        self.assertIn("op-11", query)
        self.assertNotIn("op-12", query)
        self.assertNotIn("op-13", query)
        self.assertIn("three.py", query)
        self.assertNotIn("four.py", query)

    def test_long_prompt_reserves_context_and_deduplicates_source_order(self):
        query, rewritten = rewrite_ambiguous_query(
            "please help " + ("context " * 200),
            ops_tokens=["git", "status", "git", "bad token", "-m"],
            edited_basenames=["recall.py", "RECALL.py", "ops_tokens.py", "../unsafe"],
        )
        self.assertTrue(rewritten)
        self.assertLessEqual(len(query), REWRITE_MAX_CHARS)
        self.assertIn("git status -m recall.py ops_tokens.py", query)
        self.assertNotIn("bad token", query)
        self.assertNotIn("../unsafe", query)

    def test_malformed_context_fails_open_without_unsafe_values(self):
        original = "please help"
        query, rewritten = rewrite_ambiguous_query(
            original,
            ops_tokens=["git", None],  # type: ignore[list-item]
            edited_basenames=["../secret", "line\nfeed"],
        )
        self.assertEqual((query, rewritten), ("please help git", True))
        self.assertNotIn("../secret", query)
        self.assertNotIn("line", query)
        long_prompt = "please help " + ("x " * 300)
        query, rewritten = rewrite_ambiguous_query(
            long_prompt, ops_tokens=["git"], edited_basenames=["recall.py"]
        )
        self.assertTrue(rewritten)
        self.assertLessEqual(len(query), REWRITE_MAX_CHARS)
        self.assertTrue(query.endswith("git recall.py"))


class EvidenceReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="zmem-query-evidence-")
        self.root = Path(self.tmp.name)
        self.old_env = os.environ.copy()
        for key in _ROUTE_ENV_KEYS:
            os.environ.pop(key, None)
        os.environ.update({
            "ZMEM_STORE": str(self.root / "store.sqlite"),
            "ZMEM_DATA": str(self.root),
            "ZMEM_MODELS_DIR": str(self.root / "models"),
            "ZMEM_MODEL_AUTODOWNLOAD": "0",
            "HOME": str(self.root / "home"),
            "USERPROFILE": str(self.root / "home"),
            "APPDATA": str(self.root / "appdata"),
            "LOCALAPPDATA": str(self.root / "localappdata"),
        })
        self.conn = sqlite3.connect(self.root / "store.sqlite", timeout=0.05)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.assertEqual(self.conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def tearDown(self) -> None:
        self.conn.close()
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def test_recent_edit_order_filters_kind_session_and_dedupes(self):
        self.conn.execute(
            "CREATE TABLE evidence(id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, "
            "ts TEXT, ref_path TEXT)"
        )
        rows = [
            ("a", "session-183", "edit", "2026-09-10T00:00:01Z", r"C:\work\recall.py"),
            ("b", "session-183", "edit", "2026-09-10T00:00:03Z", "/work/ops_tokens.py"),
            ("c", "session-183", "edit", "2026-09-10T00:00:04Z", "/work/recall.py"),
            ("d", "session-183", "tool_call", "2026-09-10T00:00:05Z", "/work/new.py"),
            ("e", "other", "edit", "2026-09-10T00:00:06Z", "/work/other.py"),
            ("f", "session-183", "edit", "2026-09-10T00:00:02Z", "../unsafe/../safe.py"),
            ("g", "session-183", "edit", "2026-09-10T00:00:00Z", "bad\nname.py"),
        ]
        self.conn.executemany("INSERT INTO evidence VALUES (?, ?, ?, ?, ?)", rows)
        self.conn.commit()
        before = self.conn.total_changes
        self.assertEqual(
            read_recent_edit_basenames(self.conn, "session-183"),
            ["recall.py", "ops_tokens.py", "safe.py"],
        )
        self.assertEqual(self.conn.total_changes, before)

    def test_missing_table_malformed_limit_and_sql_failure_fail_open(self):
        self.assertEqual(read_recent_edit_basenames(self.conn, "session-183"), [])
        self.assertEqual(read_recent_edit_basenames(self.conn, "session-183", limit=0), [])
        self.assertEqual(read_recent_edit_basenames(self.conn, "session-183", limit=True), [])
        self.assertEqual(read_recent_edit_basenames(self.conn, None), [])  # type: ignore[arg-type]
        closed = sqlite3.connect(self.root / "closed.sqlite")
        closed.close()
        self.assertEqual(read_recent_edit_basenames(closed, "session-183"), [])

        self.conn.execute(
            "CREATE TABLE evidence(id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, "
            "ts TEXT, ref_path TEXT)"
        )
        self.conn.execute(
            "INSERT INTO evidence VALUES ('a', 'session-183', 'edit', '2026-09-10T00:00:00Z', NULL)"
        )
        self.conn.commit()
        self.assertEqual(read_recent_edit_basenames(self.conn, "session-183"), [])

        locked = sqlite3.connect(self.root / "store.sqlite", timeout=0.01)
        locked.execute("BEGIN EXCLUSIVE")
        try:
            self.assertEqual(read_recent_edit_basenames(self.conn, "session-183"), [])
        finally:
            locked.rollback()
            locked.close()

    def test_recent_edit_overfetches_before_deduping_and_truncating(self):
        self.conn.execute(
            "CREATE TABLE evidence(id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, "
            "ts TEXT, ref_path TEXT)"
        )
        rows = [
            ("a", "session-183", "edit", "2026-09-10T00:00:05Z", "bad\nname.py"),
            ("b", "session-183", "edit", "2026-09-10T00:00:04Z", "/work/recall.py"),
            ("c", "session-183", "edit", "2026-09-10T00:00:03Z", "/other/RECALL.py"),
            ("d", "session-183", "edit", "2026-09-10T00:00:02Z", "/work/ops_tokens.py"),
            ("e", "session-183", "edit", "2026-09-10T00:00:01Z", "/work/schema.py"),
        ]
        self.conn.executemany("INSERT INTO evidence VALUES (?, ?, ?, ?, ?)", rows)
        self.conn.commit()
        self.assertEqual(
            read_recent_edit_basenames(self.conn, "session-183", limit=3),
            ["recall.py", "ops_tokens.py", "schema.py"],
        )

    def test_strict_sql_failure_can_be_audited_without_changing_default(self):
        self.assertEqual(
            read_recent_edit_basenames(self.conn, "session-183"), []
        )
        with self.assertRaises(sqlite3.OperationalError):
            read_recent_edit_basenames(
                self.conn, "session-183", strict_errors=True
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
