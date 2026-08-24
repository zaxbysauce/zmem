"""Regression + guardrail tests for issue #21 (surfaced_count telemetry).

Issue #21: `retrieval_count` never increments for hook-driven recall, so promotion
ranking and prune decisions rest on a metric biased toward manual/explicit use. The fix
adds a `surfaced_count`/`last_surfaced` counter that passive (`--no-bump`) recall IS
allowed to bump, and blends it with `retrieval_count` everywhere the latter was the sole
"usefulness" signal.

This module covers:
  - adapter-scan guardrail: every PASSIVE recall/recent consumer (`hooks/*.sh`, Hermes
    `prefetch`) carries a literal `--no-bump`, and every EXPLICIT consumer (MCP
    `recall`/`search`, Hermes `_tool_search`) does NOT — codifying the single rule across
    all adapters so a future adapter cannot silently introduce a fourth rule.
  - `consolidate --prune` protection: a memory that has been SURFACED but never explicitly
    retrieved must NOT be pruned by the never-used rule.
  - `compute_score` popularity blends surfaced + retrieval (a surfaced-only row scores
    above an inert row; equivalent surface totals score equal regardless of which counter
    carried the events).

Run: python tests/test_surface_consistency.py   (no pytest required — repo convention)
"""

from __future__ import annotations

import ast
import json
import os
import re
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
STORE_PY = SCRIPTS_DIR / "store.py"
PYTHON = sys.executable

NS = "project:surfaceconsistencytest"

# --- Point the import-time STORE_PATH at a throwaway location before importing store.py ---
_IMPORT_TMP = tempfile.mkdtemp(prefix="zmem-surface-import-")
os.environ["ZMEM_STORE"] = os.path.join(_IMPORT_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")

sys.path.insert(0, str(SCRIPTS_DIR))
import store  # noqa: E402


class AdapterScanTest(unittest.TestCase):
    """Guardrail: one rule across all adapters (the issue-comment ask)."""

    # Passive consumers: automatic / hook / prefetch recall paths. Each MUST
    # carry the literal --no-bump flag (directly or via the shared body) so
    # only surfaced_count (+last_surfaced), never retrieval_count, advances
    # on the passive path. PRR-025 fix: zmem-precompact.sh is a passive
    # recall consumer too (SubagentStart-adjacent PreCompact re-inject).
    PASSIVE = {
        "hooks/zmem-recall.sh",
        "hooks/zmem-subagent-recall.sh",
        "hooks/zmem-session-start.sh",
        "hooks/zmem-precompact.sh",
    }

    def _method_body(self, text: str, method: str) -> str:
        """Return the body of `def method(...)` up to the next `def ` at col 0."""
        m = re.search(rf"^\s*def {method}\(.*$", text, re.MULTILINE)
        self.assertIsNotNone(m, f"def {method} not found")
        rest = text[m.end():]
        nxt = re.search(r"^\s*def ", rest, re.MULTILINE)
        return rest if nxt is None else rest[:nxt.start()]

    def test_passive_hooks_carry_no_bump(self):
        # Issue #58, 3.9: recall (UserPromptSubmit) and precompact share a
        # single Python body (hooks/lib/zmem-recall-body.py) — the literal
        # ``--no-bump`` lives in the body, not the .sh wrapper. The session-
        # start hook still inlines its own store.py invocation, so it must
        # carry the literal too.
        body_text = (REPO_ROOT / "hooks" / "lib" / "zmem-recall-body.py").read_text(
            encoding="utf-8"
        )
        for rel in sorted(self.PASSIVE):
            text = (REPO_ROOT / rel).read_text(encoding="utf-8")
            # session-start still inlines; recall AND precompact source the
            # shared body (the literal --no-bump lives in the body's argv).
            if rel in ("hooks/zmem-recall.sh", "hooks/zmem-precompact.sh"):
                self.assertIn(
                    "lib/zmem-recall-body.py", text,
                    f"{rel} must source the shared recall body to honor "
                    f"--no-bump (issue #58, 3.5/3.8/3.9)",
                )
                combined = text + "\n" + body_text
            else:
                combined = text
            self.assertIn(
                "--no-bump", combined,
                f"{rel} is a passive recall path and MUST pass --no-bump so it records "
                "a surface event, not a retrieval")

    def test_passive_hermes_prefetch_carries_no_bump(self):
        text = (REPO_ROOT / "hermes-plugin" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("--no-bump", self._method_body(text, "prefetch"),
                      "Hermes prefetch is passive and MUST pass --no-bump")

    def test_explicit_hermes_tool_search_omits_no_bump(self):
        text = (REPO_ROOT / "hermes-plugin" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotIn("--no-bump", self._method_body(text, "_tool_search"),
                         "Hermes _tool_search is EXPLICIT and must NOT pass --no-bump")

    def test_explicit_mcp_recall_omits_no_bump(self):
        text = (REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py").read_text(
            encoding="utf-8")
        self.assertNotIn("--no-bump", text,
                         "MCP recall/search are EXPLICIT and must NOT pass --no-bump")

    def test_explicit_mcp_recall_docstring_documents_bump(self):
        # I2 (#38 / #56): the explicit-vs-passive bump rule is tested design,
        # but the MCP recall tool never STATED it, so it kept being
        # re-discovered as a bug. The docstring must document the intentional
        # retrieval_count bump. Scoped to the docstring via ast (a body comment
        # must not mask a docstring regression — same discipline as
        # test_readonly_invariant_docstring_lists_all_hooks above). The
        # prohibition on the passive flag literal in this file is pinned
        # separately by test_explicit_mcp_recall_omits_no_bump.
        source = (REPO_ROOT / "hermes-plugin" / "server" / "mcp_server.py").read_text(
            encoding="utf-8")
        tree = ast.parse(source)
        doc = None
        for node in ast.walk(tree):
            # The MCP tools are `async def` closures, so the recall tool is an
            # ast.AsyncFunctionDef, not an ast.FunctionDef.
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == "recall"):
                doc = ast.get_docstring(node)
                break
        self.assertIsNotNone(doc, "MCP recall tool must have a docstring")
        self.assertIn("retrieval_count", doc,
                      "the docstring must name the counter that explicit recall bumps")
        self.assertIn("passive", doc,
                      "the docstring must contrast explicit recall with the passive surfaces")

    def test_readonly_invariant_docstring_lists_all_hooks(self):
        # The passive-recall read-only contract (the recall_memory DOCSTRING) must
        # name ALL THREE automatic hook sources — UserPromptSubmit, SubagentStart,
        # and SessionStart — so the read-only invariant stays checkable and a
        # comment cannot silently drop one hook again (issue #23). SessionStart was
        # the one omitted here after it was made --no-bump by PR #29.
        #
        # Scope the assertion to the docstring ONLY (via ast.get_docstring), not
        # the whole function body: a body comment or refactor mention of a hook
        # name must NOT mask a docstring regression (PRR-002).
        # Post-split (issue #57) `recall_memory` lives in storelib/recall.py.
        source = (SCRIPTS_DIR / "storelib" / "recall.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        doc = None
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "recall_memory":
                doc = ast.get_docstring(node)
                break
        self.assertIsNotNone(doc, "recall_memory must have a docstring")
        self.assertIn("UserPromptSubmit", doc)
        self.assertIn("SubagentStart", doc)
        self.assertIn("SessionStart", doc)


class SurfaceTempStoreTest(unittest.TestCase):
    """Subprocess tests driving the REAL store.py CLI against a temp store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store,
                    "ZMEM_MODEL_AUTODOWNLOAD": "0"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run([PYTHON, str(STORE_PY), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def _conn(self):
        return sqlite3.connect(self.store)

    def _counts(self, content):
        c = self._conn()
        try:
            return c.execute(
                "SELECT retrieval_count, surfaced_count, superseded_at FROM memory "
                "WHERE content LIKE ?", (f"%{content}%",)
            ).fetchone()
        finally:
            c.close()

    def test_prune_does_not_prune_surfaced_but_unretrieved(self):
        # Two old, low-confidence, signal=none rows: one really never surfaced, one
        # surfaced only by hooks. Only the never-surfaced one may be pruned.
        inert = "definitely never surfaced nor retrieved row alpha"
        surfaced = "surfaced many times by hooks but never fetched row beta"
        for content, sc in ((inert, 0), (surfaced, 3)):
            r = self._run("add", "--namespace", NS, "--type", "fact",
                          "--content", content, "--signal", "none", "--confidence", "0.2")
            self.assertEqual(r.returncode, 0, r.stderr)
        c = self._conn()
        try:
            for content, sc in ((inert, 0), (surfaced, 3)):
                c.execute(
                    "UPDATE memory SET surfaced_count=?, confidence=0.2, "
                    "ingestion_ts=datetime('now','-60 days') WHERE content LIKE ?",
                    (sc, f"%{content}%"))
            c.commit()
        finally:
            c.close()

        r = self._run("consolidate", "--prune")
        # consolidate prints a summary regardless; embeddings may be absent (lexical
        # fallback) but the run must still succeed or at least not abort the prune.
        self.assertEqual(r.returncode, 0, r.stderr)
        # Issue #44: a REAL run (no --dry-run) must report the prune in the
        # past tense ("pruned N"), symmetric with the dry-run "would prune 1"
        # coverage in test_consolidate_lossy.py. Pin the count too (the prune
        # clause is appended whenever --prune is set, even at N=0, so asserting
        # the exact "pruned 1" is stronger than the bare verb). Real-run stdout
        # carries no verbatim content echo (the preview prints only under
        # --dry-run), so a whole-buffer assertIn is safe here.
        self.assertIn("pruned 1", r.stdout, r.stdout)

        inert_counts = self._counts(inert)
        surfaced_counts = self._counts(surfaced)
        self.assertIsNotNone(inert_counts, "prune target row should exist")
        self.assertIsNotNone(surfaced_counts, "surfaced row should exist")
        self.assertIsNotNone(
            inert_counts[2],
            f"never-surfaced, never-retrieved row MUST be pruned (superseded); got {inert_counts}")
        self.assertIsNone(
            surfaced_counts[2],
            f"surfaced-but-unretrieved row must NOT be pruned; got {surfaced_counts}")


class GetExitContractTest(unittest.TestCase):
    """I7 (#38 / #56): `store.py get --id` has a documented not-found exit
    contract — exit 1 plus the stable stderr line, never a traceback; a found
    row exits 0 with JSON on stdout. Drives the REAL CLI against a temp store."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-get-contract-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store,
                    "ZMEM_MODEL_AUTODOWNLOAD": "0"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run([PYTHON, str(STORE_PY), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def test_missing_id_exits_1_with_stable_stderr_token(self):
        missing = "no-such-id-00000000"
        r = self._run("get", "--id", missing)
        self.assertEqual(r.returncode, 1, (r.stdout, r.stderr))
        self.assertIn(f"[zmem] no memory with id {missing}", r.stderr)
        self.assertNotIn("Traceback", r.stdout + r.stderr)

    def test_existing_id_exits_0_with_json_on_stdout(self):
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", "get exit contract probe row", "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        m = re.search(r"added memory ([0-9a-f-]{36})", r.stdout)
        self.assertIsNotNone(m, r.stdout)
        mid = m.group(1)
        g = self._run("get", "--id", mid)
        self.assertEqual(g.returncode, 0, g.stderr)
        parsed = json.loads(g.stdout)
        self.assertEqual(parsed["id"], mid)
        self.assertEqual(parsed["content"], "get exit contract probe row")

    def test_help_documents_the_exit_contract(self):
        # The contract only exists if it is documented at the surface a caller
        # can discover (--help) — the original #38 I7 gap was exactly this.
        r = self._run("get", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no memory with id", r.stdout)
        self.assertIn("exit", r.stdout.lower())

    def test_blob_columns_render_as_marker_and_never_crash(self):
        # PRR-009: the bytes→"<N-byte blob>" fix must be provable WITHOUT the
        # embedding runtime — CI is model-absent and never creates BLOBs
        # organically, so seed one directly and pin the rendered marker, the
        # exit code, and the absence of a traceback.
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", "blob marker probe row", "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        mid = re.search(r"added memory ([0-9a-f-]{36})", r.stdout).group(1)
        conn = sqlite3.connect(self.store)
        try:
            conn.execute("UPDATE memory SET embedding=? WHERE id=?",
                         (sqlite3.Binary(b"\x00" * 16), mid))
            conn.commit()
        finally:
            conn.close()
        g = self._run("get", "--id", mid)
        self.assertEqual(g.returncode, 0, g.stderr)
        parsed = json.loads(g.stdout)
        self.assertEqual(parsed["embedding"], "<16-byte blob>")
        self.assertNotIn("Traceback", g.stdout + g.stderr)

    def test_superseded_row_still_gettable_by_id(self):
        # Pins get's forensic-read semantics: `get` intentionally returns
        # tombstoned rows too (its SELECT has no superseded_at filter, unlike
        # recall/search/list) — history stays inspectable by id. Documented
        # behavior, previously unpinned (PRR-009a).
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", "superseded get probe row", "--signal", "test")
        self.assertEqual(r.returncode, 0, r.stderr)
        mid = re.search(r"added memory ([0-9a-f-]{36})", r.stdout).group(1)
        s = self._run("supersede", "--id", mid, "--reason", "probe")
        self.assertEqual(s.returncode, 0, s.stderr)
        g = self._run("get", "--id", mid)
        self.assertEqual(g.returncode, 0, g.stderr)
        parsed = json.loads(g.stdout)
        self.assertIsNotNone(parsed.get("superseded_at"),
                             "get by id is a forensic read: tombstoned rows "
                             "must still be inspectable")


class ComputeScorePopularityBlendTest(unittest.TestCase):
    """compute_score popularity must blend surfaced + retrieval (defect-class fix)."""

    def _row(self, retrieval: int, surfaced: int) -> dict:
        return {
            "retrieval_count": retrieval,
            "surfaced_count": surfaced,
            "confidence": 0.5,
            "ingestion_ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

    def test_surfaced_only_outranks_inert(self):
        now = time.time()
        surfaced = store.compute_score(self._row(0, 5), None, now, vec_sim=0.5)
        inert = store.compute_score(self._row(0, 0), None, now, vec_sim=0.5)
        self.assertGreater(surfaced, inert,
                           "a surfaced-only memory should outrank a never-surfaced one")

    def test_equivalent_totals_score_equal(self):
        now = time.time()
        via_surfaced = store.compute_score(self._row(0, 5), None, now, vec_sim=0.5)
        via_retrieval = store.compute_score(self._row(5, 0), None, now, vec_sim=0.5)
        self.assertAlmostEqual(
            via_surfaced, via_retrieval, places=12,
            msg="popularity counts total surface events regardless of which counter carried them")


if __name__ == "__main__":
    unittest.main(verbosity=2)
