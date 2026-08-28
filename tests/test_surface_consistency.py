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
        # v13 (issue #65, 10.5): session_start is the D4 PASSIVE path and MUST
        # pass --no-bump — the scan is scoped to the EXPLICIT tool bodies so
        # the passive exception is pinned, not forbidden.
        import ast as _ast
        tree = _ast.parse(text)
        bodies = {}
        for node in _ast.walk(tree):
            if isinstance(node, _ast.AsyncFunctionDef) and node.name in (
                "recall", "search", "recent", "session_start", "session_end",
            ):
                bodies[node.name] = _ast.get_source_segment(text, node) or ""
        for explicit in ("recall", "search", "recent"):
            self.assertIn(explicit, bodies, f"tool {explicit} missing?")
            self.assertNotIn(
                "--no-bump", bodies[explicit],
                f"MCP {explicit} is EXPLICIT and must NOT pass --no-bump")
        self.assertIn(
            "--no-bump", bodies.get("session_start", ""),
            "MCP session_start is the D4 passive path and MUST pass --no-bump "
            "(issue #65, 10.5)")

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


class TaintAutoInjectSurfaceTest(unittest.TestCase):
    """Issue #59, 4.7: the taint model must be visible and SAFE on the
    auto-inject surface. A web-sourced row (untrusted_web) is omitted on the
    passive --no-bump path exactly like an injection-risk row; an
    untrusted_tool row is trusted enough to surface passively; and the
    EXPLICIT recall text path PREFIXES both ranks so an agent/operator sees
    the untrusted provenance without reading JSON. (Final critic blocker:
    this is the regression pin the fix plan promised and the suite lacked.)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-taint-surface-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store,
                    "ZMEM_MODEL_AUTODOWNLOAD": "0"}

    def _run(self, *args):
        r = subprocess.run([PYTHON, str(STORE_PY), *args],
                           env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    def _seed(self, marker: str) -> dict:
        """One row per taint rank, all grounded (conf 0.9, above the recall /
        recent floors); contents unique to `marker`. Returns
        ``{taint: id, "ns": ns}`` so a test holds direct references."""
        ns = f"project:taint-surface-{marker}"
        by_taint = {}
        for taint, word in (("untrusted_web", "webey"),
                            ("untrusted_tool", "tooly"),
                            ("trusted_internal", "trusty")):
            r = self._run("add", "--namespace", ns, "--type", "fact",
                          "--content", f"{taint} {word} {marker} quokka",
                          "--signal", "test", "--taint", taint)
            mid = re.search(r"added memory ([0-9a-f-]{36})", r.stdout)
            self.assertIsNotNone(mid, r.stdout)
            by_taint[taint] = mid.group(1)
        by_taint["ns"] = ns
        return by_taint

    def test_no_bump_recall_omits_untrusted_web_keeps_tool(self):
        seeded = self._seed("alpha")
        r = self._run("recall", "--query", "quokka", "--namespace", seeded["ns"],
                      "--no-bump", "--json")
        _parsed = json.loads(r.stdout)
        # v13 (issue #65, 10.8): read --json emits the envelope.
        _rows = _parsed["results"] if isinstance(_parsed, dict) else _parsed
        ids = {x["id"] for x in _rows}
        self.assertNotIn(seeded["untrusted_web"], ids,
                         "passive recall must OMIT untrusted_web (same path as "
                         "injection-risk)")
        self.assertIn(seeded["untrusted_tool"], ids,
                      "passive recall must KEEP untrusted_tool: it is trusted "
                      "enough to surface passively and gets flagged on the "
                      "explicit path")
        self.assertIn(seeded["trusted_internal"], ids)
        # The payload still carries taint so a --json consumer can filter.
        tool_item = next(x for x in _rows
                         if x["id"] == seeded["untrusted_tool"])
        self.assertEqual(tool_item["taint"], "untrusted_tool")

    def test_no_bump_recent_omits_untrusted_web(self):
        seeded = self._seed("bravo")
        r = self._run("recent", "--namespace", seeded["ns"], "--no-bump", "--json")
        _parsed = json.loads(r.stdout)
        # v13 (issue #65, 10.8): read --json emits the envelope.
        ids = {x["id"] for x in (_parsed["results"] if isinstance(_parsed, dict) else _parsed)}
        self.assertNotIn(seeded["untrusted_web"], ids,
                         "passive recent must OMIT untrusted_web")
        self.assertIn(seeded["untrusted_tool"], ids)
        self.assertIn(seeded["trusted_internal"], ids)

    def test_explicit_recall_keeps_all_and_prefixes_untrusted_taints(self):
        seeded = self._seed("charlie")
        r = self._run("recall", "--query", "quokka", "--namespace", seeded["ns"])
        self.assertIn("[UNTRUSTED WEB]", r.stdout,
                      "explicit recall text must prefix untrusted_web")
        self.assertIn("[UNTRUSTED TOOL]", r.stdout,
                      "explicit recall text must prefix untrusted_tool")
        # Every rank still surfaces on the explicit path (a deliberate fetch —
        # the operator sees the marker, not an omission).
        self.assertIn("trusty", r.stdout)
        self.assertIn("webey", r.stdout)
        self.assertIn("tooly", r.stdout)

    def test_explicit_json_includes_all_ranks_with_taint_field(self):
        seeded = self._seed("delta")
        r = self._run("recall", "--query", "quokka", "--namespace", seeded["ns"],
                      "--json")
        _parsed = json.loads(r.stdout)
        # v13 (issue #65, 10.8): read --json emits the envelope.
        items = _parsed["results"] if isinstance(_parsed, dict) else _parsed
        ids = {x["id"] for x in items}
        self.assertIn(seeded["untrusted_web"], ids)
        self.assertIn(seeded["untrusted_tool"], ids)
        self.assertIn(seeded["trusted_internal"], ids)
        taint_by_id = {x["id"]: x["taint"] for x in items}
        for taint in ("untrusted_web", "untrusted_tool", "trusted_internal"):
            self.assertEqual(taint_by_id[seeded[taint]], taint,
                             f"explicit JSON must carry the real taint for {taint}")

class AgentWriteSurfaceParityTest(unittest.TestCase):
    """PR-review PRR-L/M/P/Y (issue #59 review round): the agent write
    surfaces (Hermes + MCP add/update) must share ONE contract — secret
    redaction (`--capture-mode auto`), sanitized (never raw-stderr) errors,
    and a stdin fallback for content too large for Windows argv. Source-scan
    ratchets so CI (which cannot import mcp/hermes internals) still guards
    the parity."""

    def _src(self, rel: str) -> str:
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    # -- v13 (issue #65): surface-completeness parity -----------------------

    def test_session_tools_exist_on_both_surfaces(self):
        """10.3/10.5: session_start/session_end, search, update, invalidate
        are listed on BOTH the MCP server and the Hermes provider — one
        contract, two surfaces (source-scan so CI guards it too)."""
        mcp = self._src("hermes-plugin/server/mcp_server.py")
        hermes = self._src("hermes-plugin/__init__.py")
        for tool in ("session_start", "session_end", "update", "invalidate",
                     "search", "recall", "recent", "add"):
            self.assertIn(f"async def {tool}(", mcp, f"MCP missing tool {tool}")
        for tool in ("zmem_session_start", "zmem_session_end", "zmem_update",
                     "zmem_invalidate", "zmem_search"):
            self.assertIn(f'"{tool}"', hermes, f"Hermes missing tool {tool}")
        schemas = [f'"{t}"' in hermes for t in
                   ("zmem_session_start", "zmem_session_end")]
        self.assertTrue(all(schemas), "Hermes session tool schemas missing")

    def test_hermes_search_never_expands_links(self):
        """10.4 (plan-critic 13): Hermes zmem_search pins --link-hops 0 — the
        CLI/MCP keyword-only, never-expanded search contract. A future refactor
        dropping the flag would silently append linked neighbors past --limit."""
        src = self._src("hermes-plugin/__init__.py")
        start = src.find("def _tool_search")
        window = src[start:start + 2500]
        self.assertIn('"--link-hops", "0"', window,
                      "hermes _tool_search must pin --link-hops 0 (parity "
                      "with the CLI search subcommand and MCP search)")

    def test_both_write_surfaces_use_structured_json_result(self):
        """10.8: MCP and Hermes add/update consume the structured --json write
        result so remote write warnings are structured data on both surfaces."""
        mcp = self._src("hermes-plugin/server/mcp_server.py")
        hermes = self._src("hermes-plugin/__init__.py")
        self.assertIn('_write_response(r, ok_result="stored"', mcp)
        self.assertIn('_write_response(r, ok_result="updated"', mcp)
        self.assertIn('_structured_write_response(r, ok_result="stored")', hermes)
        self.assertIn('_structured_write_response(r, ok_result="updated")', hermes)

    def test_update_namespace_override_on_both_surfaces(self):
        """10.3: the namespace override is exposed on BOTH remote update
        surfaces (CLI already had --namespace)."""
        mcp = self._src("hermes-plugin/server/mcp_server.py")
        hermes = self._src("hermes-plugin/__init__.py")
        mcp_start = mcp.find("async def update(")
        self.assertIn('"--namespace", ns_override', mcp[mcp_start:mcp_start + 4000])
        h_start = hermes.find("def _tool_update")
        self.assertIn('"--namespace", ns_override', hermes[h_start:h_start + 4000])

    def test_agent_write_paths_pass_capture_mode_auto(self):
        src = self._src("hermes-plugin/__init__.py")
        for m in ("_tool_add", "_tool_update"):
            start = src.find(f"def {m}")
            self.assertGreater(start, 0, f"def {m} not found in hermes source")
            window = src[start:start + 5000]
            self.assertIn('"--capture-mode", "auto"', window,
                          f"hermes {m} must pass --capture-mode auto so secrets "
                          "are redacted like the MCP path (PRR-L)")
        mcp = self._src("hermes-plugin/server/mcp_server.py")
        self.assertGreaterEqual(mcp.count('"--capture-mode", "auto"'), 2,
                                "MCP add AND update must keep --capture-mode auto")

    def test_remote_error_paths_never_echo_raw_stderr(self):
        for rel in ("hermes-plugin/server/mcp_server.py",
                    "hermes-plugin/__init__.py"):
            src = self._src(rel)
            self.assertIn("_sanitize_store_error", src,
                          f"{rel} must route remote error payloads through the "
                          "sanitizer (PRR-M)")
            self.assertNotIn('_error(r["stderr"]', src,
                             f"{rel} must not return raw stderr to remote clients")
            self.assertNotIn("r['stderr'] or r['stdout'][:200]", src,
                             f"{rel} must not splice raw stderr into tool errors")
        # The invalidate/supersede remote paths are covered by the file-wide
        # checks above (critic nit): their failure payloads are built by the
        # same sanitized call sites, so no separate unwired path exists.
        mcp = self._src("hermes-plugin/server/mcp_server.py")
        self.assertGreaterEqual(mcp.count("_sanitize_store_error(r)"), 4,
                                "sanitize every remote failure site incl. "
                                "supersede/invalidate/update")

    def test_oversize_content_uses_stdin_not_argv(self):
        for rel in ("hermes-plugin/__init__.py",
                    "hermes-plugin/server/mcp_server.py"):
            src = self._src(rel)
            self.assertIn("_ARGV_SAFE_CONTENT_CHARS", src,
                          f"{rel} must gate content-by-argv on the safe "
                          "threshold and pipe larger content via stdin (PRR-P)")
            self.assertIn('.index("--content")', src,
                          f"{rel} must switch to the stdin content path for "
                          "oversize payloads")
        cli = self._src("skills/memory/scripts/storelib/cli.py")
        self.assertIn('== "-"', cli,
                      "the CLI must honor `--content -` as read-from-stdin")


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


class UnrecalledPruneExtensionTest(unittest.TestCase):
    """Issue #62, 7.6: the unrecalled-prune extension. ``consolidate --prune``
    may additionally qualify a live row whose ``last_surfaced`` is older than
    ZMEM_UNRECALLED_DAYS (default 30) — a surfaced-but-stale row loses its
    issue #21 protection once the surface event itself has gone stale. A
    recently-surfaced row stays protected; a never-surfaced row (NULL
    last_surfaced, surfaced_count=0) still qualifies via the surgeed_count=0
    branch; signal!=none is never pruned.

    Uses the REAL store.py CLI (subprocess) exactly like the prune test above.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-unrecalled-")
        self.store = os.path.join(self.tmp, "store.sqlite")
        self.env = {**os.environ, "ZMEM_STORE": self.store,
                    "ZMEM_MODEL_AUTODOWNLOAD": "0"}
        # cubic#76 / Claude Code round 4 (env isolation): a host-leaked
        # ZMEM_UNRECALLED_DAYS would silently change every expectation here —
        # including test_default_unrecalled_days_is_30's premise. Pop it so
        # the suite is hermetic (pattern: test_session_cadence._base_env).
        self.env.pop("ZMEM_UNRECALLED_DAYS", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *args):
        return subprocess.run([PYTHON, str(STORE_PY), *args],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def _add_old_row(self, content, *, surfaced_count, last_surfaced, days_old=60,
                     signal="none", confidence="0.2", retrieval=0):
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", content, "--signal", signal,
                      "--confidence", confidence)
        self.assertEqual(r.returncode, 0, r.stderr)
        c = sqlite3.connect(self.store)
        try:
            if last_surfaced is None:
                c.execute(
                    "UPDATE memory SET retrieval_count=?, surfaced_count=?, "
                    "ingestion_ts=datetime('now', ?) WHERE content LIKE ?",
                    (retrieval, surfaced_count, f"-{days_old} days", f"%{content}%"))
            else:
                c.execute(
                    "UPDATE memory SET retrieval_count=?, surfaced_count=?, "
                    "last_surfaced=datetime('now', ?), "
                    "ingestion_ts=datetime('now', ?) WHERE content LIKE ?",
                    (retrieval, surfaced_count, f"-{last_surfaced} days",
                     f"-{days_old} days", f"%{content}%"))
            c.commit()
        finally:
            c.close()

    def _superseded(self, content):
        c = sqlite3.connect(self.store)
        try:
            row = c.execute(
                "SELECT superseded_at FROM memory WHERE content LIKE ?",
                (f"%{content}%",)).fetchone()
            return row[0] if row else None
        finally:
            c.close()

    def _add_old_row_iso(self, content, *, surfaced_count, last_surfaced_days,
                         days_old=60):
        """Seed a row whose timestamps are the REAL store format — ISO-8601
        ``YYYY-MM-DDTHH:MM:SSZ`` from ``now_iso()`` — unlike ``_add_old_row``,
        which writes SQLite's space-form ``datetime('now', ...)``. PRR-005:
        the prune WHERE used to compare the TEXT columns against a space-form
        cutoff, and 'T' (0x54) > ' ' (0x20) in byte order, so genuinely-stale
        ISO rows silently NEVER qualified — the space-form seeds below were
        structurally blind to the inversion."""
        r = self._run("add", "--namespace", NS, "--type", "fact",
                      "--content", content, "--signal", "none",
                      "--confidence", "0.2")
        self.assertEqual(r.returncode, 0, r.stderr)

        def iso(days: float) -> str:
            return time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days * 86400))

        c = sqlite3.connect(self.store)
        try:
            c.execute(
                "UPDATE memory SET retrieval_count=0, surfaced_count=?, "
                "last_surfaced=?, ingestion_ts=? WHERE content LIKE ?",
                (surfaced_count, iso(last_surfaced_days), iso(days_old),
                 f"%{content}%"))
            c.commit()
        finally:
            c.close()

    def test_iso_form_timestamps_prune_correctly(self):
        """PRR-005 regression: both boundary comparisons must be
        format-agnostic (``julianday`` on both sides). A row last surfaced
        60d ago in the store's real ISO form IS pruned; one surfaced 10d ago
        IS NOT — under the old TEXT-vs-space-form comparison, the stale ISO
        row's 'T' made it byte-GREATER than the cutoff so BOTH survived. The
        two probes are deliberately lexically DISSIMILAR so consolidation
        never merges them (a merged keeper would inherit one row's
        last_surfaced and make the assertion nondeterministic)."""
        stale = "ancient lighthouse sentinel sixty"
        fresh = "modern harbor beacon ten"
        self._add_old_row_iso(stale, surfaced_count=3, last_surfaced_days=60)
        self._add_old_row_iso(fresh, surfaced_count=3, last_surfaced_days=10)
        r = self._run("consolidate", "--prune")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(
            self._superseded(stale),
            f"60-day-old ISO-form last_surfaced must be pruned; "
            f"out={r.stdout!r} {r.stderr!r}")
        self.assertIsNone(
            self._superseded(fresh),
            f"10-day-old ISO-form last_surfaced must stay protected; "
            f"out={r.stdout!r} {r.stderr!r}")

    def test_surfaced_beyond_unrecalled_days_is_pruned(self):
        """surfaced_count=3 with last_surfaced OLDER than ZMEM_UNRECALLED_DAYS:
        the row's protection lapses and it IS pruned."""
        stale = "surfaced long ago and now stale row eta"
        self._add_old_row(stale, surfaced_count=3, last_surfaced=60)
        self.env = {**self.env, "ZMEM_UNRECALLED_DAYS": "30"}
        r = self._run("consolidate", "--prune")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(
            self._superseded(stale),
            f"stale-surfaced row must be pruned when last_surfaced > 30d; "
            f"out={r.stdout!r} {r.stderr!r}")

    def test_recently_surfaced_stays_protected(self):
        """last_surfaced 10 days ago < ZMEM_UNRECALLED_DAYS=30: protected."""
        fresh = "surfaced recently so protected row zeta"
        self._add_old_row(fresh, surfaced_count=3, last_surfaced=10)
        self.env = {**self.env, "ZMEM_UNRECALLED_DAYS": "30"}
        r = self._run("consolidate", "--prune")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(
            self._superseded(fresh),
            f"recently-surfaced row must NOT be pruned (last_surfaced < 30d); "
            f"out={r.stdout!r} {r.stderr!r}")

    def test_default_unrecalled_days_is_30(self):
        """With no ZMEM_UNRECALLED_DAYS, the default 30 applies: a row surfaced
        29 days ago is protected, one surfaced 31 days ago is pruned."""
        keep = "default 30 protects 29-day-surfaced row iota"
        drop = "default 30 prunes 31-day-surfaced row kappa"
        self._add_old_row(keep, surfaced_count=2, last_surfaced=29)
        self._add_old_row(drop, surfaced_count=2, last_surfaced=31)
        r = self._run("consolidate", "--prune")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(self._superseded(keep),
                          f"29-day-surfaced row must survive the default-30 gate")
        self.assertIsNotNone(self._superseded(drop),
                             f"31-day-surfaced row must be pruned under default-30")

    def test_never_surfaced_null_last_surfaced_still_prunable(self):
        """NULL last_surfaced + surfaced_count=0 keeps qualifying (the existing
        issue #21 branch) — the extension must not regress the base rule."""
        inert = "never surfaced so plainly prunable row lambda"
        self._add_old_row(inert, surfaced_count=0, last_surfaced=None)
        self.env = {**self.env, "ZMEM_UNRECALLED_DAYS": "30"}
        r = self._run("consolidate", "--prune")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNotNone(self._superseded(inert),
                             f"never-surfaced row must still be pruned")

    def test_signal_not_none_never_pruned(self):
        """signal!=none rows are never pruned even when every numeric gate
        (old, unretrieved, low-confidence, stale-surfaced) would qualify them."""
        kept = "belief signal old unretrieved row mu"
        self._add_old_row(kept, surfaced_count=3, last_surfaced=60, signal="user")
        self.env = {**self.env, "ZMEM_UNRECALLED_DAYS": "30"}
        r = self._run("consolidate", "--prune")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIsNone(
            self._superseded(kept),
            f"signal!=none row must NEVER be pruned, regardless of staleness; "
            f"out={r.stdout!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
