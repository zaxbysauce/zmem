"""Pure checkpoint-query contract tests for issue #99.

The store-side integration and transport checks live with the pretool selector
tests.  This module keeps the matcher, canonical slice, deterministic fixture,
and query-budget contract executable without a host hook or ambient store.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "checkpoint_queries"
RECALL_BODY = REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py"
sys.path.insert(0, str(SCRIPTS / "storelib"))

import ops_tokens  # noqa: E402


def _fixture_generator():
    path = FIXTURES / "generate.py"
    spec = importlib.util.spec_from_file_location("issue99_fixture_generator", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CheckpointQueryTest(unittest.TestCase):
    def test_fixture_digest(self):
        expected = {
            "cases.json": "bb4e5eab5d8380639461bbf0f94e32bc8c36c1f15cce6eb06ecdee351cf9d9b8",
            "expected.json": "27bf6e9d2adf22bac06752f984f8c828b9346c3bd2c79d52e4d99adabd52d5d7",
        }
        generator = FIXTURES / "generate.py"
        with tempfile.TemporaryDirectory(prefix="zmem-issue99-fixture-") as tmp:
            subprocess.run([sys.executable, str(generator), "--out-dir", tmp],
                           check=True, capture_output=True, text=True,
                           timeout=60)
            for name, digest in expected.items():
                self.assertEqual(
                    hashlib.sha256(Path(tmp, name).read_bytes()).hexdigest(),
                    digest)
                self.assertEqual(
                    Path(tmp, name).read_bytes(), Path(FIXTURES, name).read_bytes())

        cases = json.loads((FIXTURES / "cases.json").read_text(encoding="utf-8"))
        expected_rows = json.loads(
            (FIXTURES / "expected.json").read_text(encoding="utf-8")
        )["rows"]
        actual_rows = []
        for case in cases["rows"]:
            tokens, checkpoint = ops_tokens.compose_pretool_query(case["tool_input"])
            actual_rows.append({"id": case["id"], "tokens": tokens,
                                "checkpoint": checkpoint})
        self.assertEqual(actual_rows, expected_rows)

    def test_phrase_table(self):
        self.assertIsInstance(ops_tokens.CHECKPOINT_PHRASES, tuple)
        self.assertEqual(
            ops_tokens.CHECKPOINT_PHRASES,
            (
                ("stash-consume", "foreign-stash conflict verify stash list"),
                ("reset", "stale tree fetch main rebase verify diff"),
                ("force-push", "stale tree fetched base force-with-lease"),
                ("branch-publication", "stale tree fetched base force-with-lease"),
                ("base-rewrite", "base drift citation re-pin"),
                ("path-test", "basename ratchet citation re-pin local battery"),
            ))
        self.assertEqual(len({phrase for _, phrase in ops_tokens.CHECKPOINT_PHRASES}), 5)

    def test_first_150_characters(self):
        left = {"z": "git stash pop", "a": "x" * 124}
        right = {"a": "x" * 124, "z": "git stash pop"}
        canonical = json.dumps(left, sort_keys=True, separators=(",", ":"))
        self.assertEqual(canonical, json.dumps(right, sort_keys=True,
                                                separators=(",", ":")))
        self.assertEqual(canonical.index("git stash pop") + len("git stash pop"), 150)
        self.assertEqual(
            ops_tokens.checkpoint_query_expansion(left),
            ops_tokens.CHECKPOINT_PHRASES[0][1])

        beyond = {"a": "x" * 125, "z": "git stash pop"}
        self.assertEqual(
            json.dumps(beyond, sort_keys=True, separators=(",", ":"))
            .index("git stash pop") + len("git stash pop"), 151)
        self.assertEqual(ops_tokens.checkpoint_query_expansion(beyond), "")

        insertion_trap = {"z": "x" * 137, "a": "git stash pop"}
        sorted_json = json.dumps(insertion_trap, sort_keys=True,
                                 separators=(",", ":"))
        insertion_json = json.dumps(insertion_trap, sort_keys=False,
                                    separators=(",", ":"))
        self.assertLess(sorted_json.index("git stash pop"), 20)
        self.assertGreaterEqual(insertion_json.index("git stash pop"), 150)
        self.assertEqual(
            ops_tokens.checkpoint_query_expansion(insertion_trap),
            ops_tokens.CHECKPOINT_PHRASES[0][1])

        # The canonical contract uses stdlib's default ensure_ascii=True;
        # non-ASCII text therefore occupies escaped JSON characters in the
        # inspected window, while matching remains case-insensitive.
        unicode_payload = {"description": "café", "command": "GIT STASH POP"}
        canonical_unicode = json.dumps(unicode_payload, sort_keys=True,
                                       separators=(",", ":"))
        self.assertIn(r"\u00e9", canonical_unicode)
        self.assertEqual(
            ops_tokens.checkpoint_query_expansion(unicode_payload),
            ops_tokens.CHECKPOINT_PHRASES[0][1])

    def test_query_bound(self):
        tokens, checkpoint = ops_tokens.compose_pretool_query(
            {"command": "git push --force-with-lease origin topic"})
        query = ops_tokens.compose_inject_query("P" * 1000, " ".join(tokens),
                                                checkpoint)
        self.assertLessEqual(len(query), 500)
        self.assertIn("git push", query)
        self.assertIn(checkpoint, query)
        self.assertEqual(len(query), 500)
        expected_prose = 500 - len("git push " + checkpoint) - 1
        self.assertTrue(query.startswith("P" * expected_prose + " "))

        # Only table-approved phrases can consume the additive budget; an
        # arbitrary oversized value must not sever the output contract.
        self.assertEqual(
            ops_tokens.compose_inject_query("prompt", "", "x" * 1000),
            "prompt")

    def test_stash_list(self):
        for payload in (
            {"command": "git stash list"},
            {"cmd": "git stash --list"},
            {"command": "git status"},
            {},
            None,
            "git stash pop",
            [],
        ):
            self.assertEqual(ops_tokens.checkpoint_query_expansion(payload), "")
        self.assertEqual(
            ops_tokens.checkpoint_query_expansion({"command": "git stash apply"}),
            ops_tokens.CHECKPOINT_PHRASES[0][1])

    def test_legacy_bytes(self):
        vectors = (
            ("", "", ""),
            ("  hello world  ", "", "hello world"),
            ("P" * 700, "", "P" * 500),
            ("prompt", "git stash pop", "prompt git stash pop"),
            ("P" * 700, "git stash pop", "P" * 349 + " git stash pop"),
        )
        for prompt, ops, expected in vectors:
            with self.subTest(prompt_len=len(prompt), ops=ops):
                self.assertEqual(ops_tokens.compose_inject_query(prompt, ops), expected)
                self.assertEqual(ops_tokens.compose_inject_query(prompt, ops, ""),
                                 expected)

    def test_every_option_and_phrase_field(self):
        cases = (
            ({"command": "git stash apply"}, 0),
            ({"cmd": "git reset --soft origin/main"}, 1),
            ({"command": "git reset --hard HEAD~1"}, 1),
            ({"command": "git push --force origin topic"}, 2),
            ({"command": "git push -f origin topic"}, 2),
            ({"command": "git push --force-with-lease origin topic"}, 2),
            ({"command": "git push origin topic"}, 3),
            ({"command": "git merge --squash topic"}, 4),
            ({"file_path": "tests/test_ops_tokens.py"}, 5),
            ({"notebook_path": "docs/citation-ratchet.ipynb"}, 5),
            ({"path": "local/battery-test.txt"}, 5),
            ({"description": "run tests/test_checkpoint_queries.py"}, 5),
        )
        for payload, index in cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    ops_tokens.checkpoint_query_expansion(payload),
                    ops_tokens.CHECKPOINT_PHRASES[index][1])

        self.assertEqual(
            ops_tokens._PRETOOL_FIELDS,
            ("command", "cmd", "file_path", "notebook_path", "path",
             "description"),
        )
        for field in ops_tokens._PRETOOL_FIELDS:
            tokens, _ = ops_tokens.compose_pretool_query(
                {field: "tests/test_checkpoint_queries.py"})
            self.assertTrue(tokens, field)

        spec = importlib.util.spec_from_file_location(
            "issue99_recall_body_fields", RECALL_BODY)
        assert spec and spec.loader
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        for field in ops_tokens._PRETOOL_FIELDS:
            value = f"tests/{field}-checkpoint.py"
            self.assertEqual(
                hook._query_for("pretool", {"tool_input": {field: value}}),
                value,
                field,
            )
        self.assertEqual(
            hook._query_for("pretool", {"tool_input": {"unknown": "git reset --hard"}}),
            "",
        )

        # Ordered matching: branch publication precedes a later path-test
        # match when both appear inside the canonical 150-character window.
        overlap = {"command": "git push origin topic",
                   "path": "tests/test_checkpoint_queries.py"}
        self.assertEqual(
            ops_tokens.checkpoint_query_expansion(overlap),
            ops_tokens.CHECKPOINT_PHRASES[3][1],
        )

    def test_kill_switch_and_pretool_shape(self):
        saved = os.environ.pop("ZMEM_QUERY_CONTEXT", None)
        try:
            self.assertEqual(
                ops_tokens.compose_pretool_query({"command": "git stash pop"})[1],
                ops_tokens.CHECKPOINT_PHRASES[0][1])
            os.environ["ZMEM_QUERY_CONTEXT"] = "0"
            self.assertEqual(ops_tokens.compose_pretool_query({"command": "git stash pop"}),
                             ([], ""))
        finally:
            if saved is None:
                os.environ.pop("ZMEM_QUERY_CONTEXT", None)
            else:
                os.environ["ZMEM_QUERY_CONTEXT"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
