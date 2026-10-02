"""CLI, selector, bootstrap, and maintenance refusal tests for #138."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parent.parent
STORE_SCRIPT = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "pages"
PAGE_ID = "fixture-page"
NAMESPACE = "project:test"
NOW = "2026-09-10T00:01:00Z"


def _load_lifecycle_helpers():
    spec = importlib.util.spec_from_file_location("issue138_pages_lifecycle", REPO_ROOT / "tests" / "test_pages.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load lifecycle helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIFECYCLE = _load_lifecycle_helpers()


def _env(root: Path, **extra: str) -> dict[str, str]:
    env = os.environ.copy()
    for key in ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_PAGE_ADAPTER_ACTIONS",
                "ZMEM_HOME", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA"):
        env.pop(key, None)
    env.update({
        "ZMEM_STORE": str(root / "store.sqlite"),
        "ZMEM_DATA": str(root / "data"),
        "ZMEM_MODELS_DIR": str(root / "missing-models"),
        "ZMEM_MODEL_AUTODOWNLOAD": "0",
        "ZMEM_HOME": str(root / "home"),
        "HOME": str(root / "home"),
        "USERPROFILE": str(root / "home"),
    })
    env.update(extra)
    return env


def _run(root: Path, *args: str, env: dict[str, str] | None = None,
         timeout: int = 45) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(STORE_SCRIPT), *args], cwd=REPO_ROOT,
        env=env or _env(root), capture_output=True, text=True,
        check=False, timeout=timeout,
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): path.read_bytes()
        for path in sorted(root.rglob("*")) if path.is_file()
    } if root.exists() else {}


def _digest(root: Path) -> str:
    h = hashlib.sha256()
    for name, data in _tree_bytes(root).items():
        h.update(name.encode("utf-8")); h.update(b"\0"); h.update(data)
    return h.hexdigest()


def _seed_page(root: Path):
    old = os.environ.copy()
    os.environ.clear()
    os.environ.update(_env(root))
    try:
        db, pages = LIFECYCLE._seed_store(root)
        page_dir = _page_dir(root)
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "page.md").write_bytes((FIXTURES / "page-base.md").read_bytes())
        pages.page_refresh(
            db, data_dir=str(root), page_id=PAGE_ID, query="fixture topic",
            namespace=NAMESPACE, tags=("fixture-topic",), now=NOW,
        )
        db.close()
    finally:
        os.environ.clear()
        os.environ.update(old)


def _page_dir(root: Path) -> Path:
    # The CLI selector resolves an omitted data_dir beside the explicit store;
    # keep this helper on that canonical one-root path even though lifecycle
    # API tests pass an explicit data_dir under root/data.
    return root / "pages" / PAGE_ID


def _current(root: Path) -> dict:
    return json.loads((_page_dir(root) / "current.json").read_text(encoding="utf-8"))


class PageCliRefusalTests(unittest.TestCase):

    def test_refresh_refusal_stderr_exit_and_unchanged_artifacts(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-cli-refusal-") as tmp:
            root = Path(tmp)
            _seed_page(root)
            before = _digest(_page_dir(root))
            actions = root / "bad-actions.json"
            cases = [
                {"operations": [{"op": "replace_section", "section_id": "unknown",
                                  "markdown": "bad", "citations": ["ev-502"]}]},
                {},
                {"operations": [{"op": "replace_section", "section_id": "refresh",
                                  "markdown": "bad", "citations": ["ev-missing"]}]},
            ]
            for action in cases:
                actions.write_text(json.dumps(action), encoding="utf-8", newline="\n")
                env = _env(root, ZMEM_PAGE_ADAPTER_ACTIONS=str(actions))
                result = _run(root, "page", "refresh", "--id", PAGE_ID,
                               "--query", "fixture topic", "--namespace", NAMESPACE,
                               "--tag", "fixture-topic", "--llm-local", env=env)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(result.stderr, "[zmem] page refresh: refused\n")
                self.assertEqual(before, _digest(_page_dir(root)))

    def test_missing_recorded_adapter_refuses_before_store_and_lock(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-cli-missing-adapter-") as tmp:
            root = Path(tmp)
            result = _run(root, "page", "refresh", "--id", PAGE_ID,
                           "--query", "fixture topic", "--namespace", NAMESPACE,
                           "--llm-local", env=_env(root))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr.splitlines()[-1],
                             "store.py: error: --llm-local requires the recorded maintenance adapter")
            self.assertFalse((root / "store.sqlite").exists())
            self.assertFalse((root / ".zmem-maintenance.lock").exists())
            self.assertFalse((root / ".zmem-consolidate.lock").exists())

    def test_page_adapter_passes_precheck_and_consolidate_still_refuses(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-cli-precheck-") as tmp:
            root = Path(tmp)
            _seed_page(root)
            actions = root / "bad-actions.json"
            actions.write_text(json.dumps({"operations": []}), encoding="utf-8", newline="\n")
            page = _run(
                root, "page", "refresh", "--id", PAGE_ID, "--query", "fixture topic",
                "--namespace", NAMESPACE, "--tag", "fixture-topic", "--llm-local",
                env=_env(root, ZMEM_PAGE_ADAPTER_ACTIONS=str(actions)),
            )
            self.assertEqual(page.returncode, 1)
            self.assertEqual(page.stderr, "[zmem] page refresh: refused\n")
            consolidate = _run(root, "consolidate", "--llm-local")
            self.assertEqual(consolidate.returncode, 2)
            self.assertIn("--llm-local requires --belief-heads", consolidate.stderr)

    def test_page_discovery_matches_represented_source_and_rejects_foreign_namespace(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-cli-discovery-") as tmp:
            root = Path(tmp)
            _seed_page(root)
            common = ("recall", "--for-injection", "--json", "--namespace", NAMESPACE,
                      "--session-id", "cli-discovery", "--moment", "user_prompt",
                      "--lane", "codex", "--no-hybrid")
            matched = _run(root, *common, "--query", "source alpha")
            self.assertEqual(matched.returncode, 0, matched.stderr)
            payload = json.loads(matched.stdout)
            page_id = f"page:{PAGE_ID}:{_current(root)['version_id']}"
            self.assertIn(page_id, payload["candidate_ids"])
            unrelated = _run(root, *common, "--query", "unrelated words")
            self.assertEqual(unrelated.returncode, 0, unrelated.stderr)
            self.assertFalse(any(row.get("type") == "page"
                                 for row in json.loads(unrelated.stdout)["results"]))
            foreign = _run(root, *common, "--namespace", "project:foreign",
                           "--query", "source alpha")
            self.assertEqual(foreign.returncode, 0, foreign.stderr)
            self.assertFalse(any(row.get("type") == "page"
                                 for row in json.loads(foreign.stdout)["results"]))

    def test_excluded_page_never_excludes_represented_source(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-cli-exclusion-") as tmp:
            root = Path(tmp)
            _seed_page(root)
            page_id = f"page:{PAGE_ID}:{_current(root)['version_id']}"
            baseline = _run(
                root, "recall", "--for-injection", "--json", "--namespace", NAMESPACE,
                "--session-id", "cli-exclusion-baseline", "--moment", "user_prompt",
                "--lane", "codex", "--query", "source alpha", "--no-hybrid",
            )
            self.assertEqual(baseline.returncode, 0, baseline.stderr)
            baseline_payload = json.loads(baseline.stdout)
            self.assertTrue(any(row.get("id") == "belief:fixture-501"
                                for row in baseline_payload["results"]), baseline_payload)
            self.assertFalse(any(row.get("id", "").endswith("000000000502")
                                 for row in baseline_payload["results"]), baseline_payload)
            result = _run(
                root, "recall", "--for-injection", "--json", "--namespace", NAMESPACE,
                "--session-id", "cli-exclusion", "--moment", "user_prompt",
                "--lane", "codex", "--query", "source alpha", "--exclude", page_id,
                "--exclude", "belief:fixture-501", "--no-hybrid",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertNotIn(page_id, [row.get("id") for row in payload["results"]])
            self.assertTrue(any(row.get("id", "").endswith("000000000502")
                                for row in payload["results"]), payload)

    def test_bootstrap_definition_and_conflicting_definition_refusal(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-cli-bootstrap-") as tmp:
            root = Path(tmp)
            old = os.environ.copy()
            os.environ.clear(); os.environ.update(_env(root))
            try:
                db, pages = LIFECYCLE._seed_store(root)
                page_dir = _page_dir(root)
                page_dir.mkdir(parents=True, exist_ok=True)
                (page_dir / "page.md").write_bytes((FIXTURES / "page-base.md").read_bytes())
                result = pages.page_refresh(
                    db, data_dir=str(root), page_id=PAGE_ID,
                    query="fixture topic", namespace=NAMESPACE,
                    tags=("fixture-topic",), now=NOW,
                )
                self.assertEqual(result["version_id"], "v000001")
                definition = json.loads((page_dir / "definition.json").read_text())
                self.assertEqual(definition["creation_policy"], "explicit")
                before = _tree_bytes(page_dir)
                with self.assertRaises(getattr(pages, "PageError", ValueError)):
                    pages.page_refresh(
                        db, data_dir=str(root), page_id=PAGE_ID,
                        query="different query", namespace=NAMESPACE,
                        tags=("fixture-topic",), now=NOW,
                    )
                self.assertEqual(before, _tree_bytes(page_dir))
                db.close()
            finally:
                os.environ.clear(); os.environ.update(old)

    def test_pointer_authority_with_missing_or_lagging_projection(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-cli-pointer-") as tmp:
            root = Path(tmp)
            _seed_page(root)
            page_dir = _page_dir(root)
            committed = json.loads((page_dir / "versions" / "v000001.json").read_text())
            original_tree = _tree_bytes(page_dir)
            (page_dir / "page.md").unlink()
            with self.assertRaises(FileNotFoundError):
                (page_dir / "page.md").read_bytes()
            old = os.environ.copy(); os.environ.clear(); os.environ.update(_env(root))
            try:
                _beliefs, pages, _schema, _meta = LIFECYCLE._runtime()
                read = pages.page_read(data_dir=str(root), page_id=PAGE_ID)
                self.assertEqual(read["content"], committed["content"])
                self.assertFalse((page_dir / "page.md").exists())
                (page_dir / "page.md").write_text("lagging projection\n", encoding="utf-8")
                read_again = pages.page_read(data_dir=str(root), page_id=PAGE_ID)
                self.assertEqual(read_again["content"], committed["content"])
                (page_dir / "current.json").write_bytes(b"{}\n")
                with self.assertRaises(getattr(pages, "PageError", ValueError)):
                    pages.page_read(data_dir=str(root), page_id=PAGE_ID)
            finally:
                os.environ.clear(); os.environ.update(old)
            self.assertEqual(_tree_bytes(page_dir)["current.json"], b"{}\n")

    def test_maintenance_lock_is_actual_conn_store_adjacent_and_not_consolidate(self):
        with tempfile.TemporaryDirectory(prefix="zmem-pages-cli-lock-") as tmp:
            root = Path(tmp)
            _seed_page(root)
            old = os.environ.copy(); os.environ.clear(); os.environ.update(_env(root))
            try:
                _beliefs, pages, schema, _meta = LIFECYCLE._runtime()
                actual = root / "store.sqlite"
                decoy = root / "decoy"
                decoy.mkdir()
                os.environ["ZMEM_STORE"] = str(decoy / "cached.sqlite")
                conn = sqlite3.connect(actual)
                with self.assertRaises(getattr(pages, "PageError", ValueError)):
                    conn.execute("BEGIN")
                    pages.page_refresh(
                        conn, data_dir=str(root), page_id=PAGE_ID,
                        query="fixture topic", namespace=NAMESPACE,
                        tags=("fixture-topic",), now=NOW,
                    )
                conn.rollback()
                conn.close()
                self.assertFalse((decoy / ".zmem-maintenance.lock").exists())
                self.assertFalse((root / ".zmem-consolidate.lock").exists())

                memory = sqlite3.connect(":memory:")
                with self.assertRaises(getattr(pages, "PageError", ValueError)):
                    pages.page_refresh(
                        memory, data_dir=str(root), page_id=PAGE_ID,
                        query="fixture topic", namespace=NAMESPACE,
                        tags=("fixture-topic",), now=NOW,
                    )
                memory.close()

                leases = root / ".zmem-writers"; leases.mkdir(exist_ok=True)
                held = leases / "held.lease"; held.write_text("held", encoding="utf-8")
                with patch.object(schema, "MAINTENANCE_WAIT_SECONDS", 0.15), \
                     patch.object(schema, "MAINTENANCE_POLL_SECONDS", 0.03):
                    conn = sqlite3.connect(actual)
                    started = time.monotonic()
                    with self.assertRaises(getattr(pages, "PageError", ValueError)):
                        pages.page_refresh(
                            conn, data_dir=str(root), page_id=PAGE_ID,
                            query="fixture topic", namespace=NAMESPACE,
                            tags=("fixture-topic",), now=NOW,
                        )
                    conn.close()
                    self.assertLess(time.monotonic() - started, 2.0)
                held.unlink()

                # Two independent writers must serialize on the store-adjacent
                # maintenance lock and publish two distinct committed versions.
                # Keep the worker in the disposable root so this probe cannot
                # import a cached operator store or leak a lease into the host.
                worker = root / "page-refresh-worker.py"
                worker.write_text(
                    "import json, os, sqlite3, sys\n"
                    "from pathlib import Path\n"
                    f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})\n"
                    "from storelib import pages\n"
                    "root = Path(os.environ['ZMEM_TEST_ROOT'])\n"
                    "conn = sqlite3.connect(root / 'store.sqlite')\n"
                    "conn.row_factory = sqlite3.Row\n"
                    "try:\n"
                    "    result = pages.page_refresh(\n"
                    "        conn, data_dir=str(root), page_id='fixture-page',\n"
                    "        query='fixture topic', namespace='project:test',\n"
                    "        tags=('fixture-topic',), now='2026-09-10T00:01:00Z')\n"
                    "    print(json.dumps(result, sort_keys=True))\n"
                    "except Exception as exc:\n"
                    "    print(json.dumps({'error': type(exc).__name__, 'message': str(exc)}))\n"
                    "    raise\n"
                    "finally:\n"
                    "    conn.close()\n",
                    encoding="utf-8", newline="\n",
                )
                child_env = _env(root, ZMEM_TEST_ROOT=str(root))
                children = [
                    subprocess.Popen(
                        [sys.executable, str(worker)], cwd=REPO_ROOT,
                        env=child_env, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True,
                    )
                    for _ in range(2)
                ]
                completed = []
                try:
                    completed = [child.communicate(timeout=30) for child in children]
                finally:
                    for child in children:
                        if child.poll() is None:
                            child.kill()
                            child.communicate()
                for child, (stdout, stderr) in zip(children, completed):
                    self.assertEqual(child.returncode, 0, stderr or stdout)
                    self.assertEqual(stderr, "", stderr)
                versions = {json.loads(stdout.strip())["version_id"]
                            for stdout, _stderr in completed}
                self.assertEqual(versions, {"v000002", "v000003"})
                self.assertFalse((root / ".zmem-maintenance.lock").exists())
                self.assertFalse(any((root / ".zmem-writers").glob("*.lease")))
                self.assertFalse(any(_page_dir(root).glob(".staging-*")))
            finally:
                os.environ.clear(); os.environ.update(old)

    def test_cloud_docs_page_contract_present(self):
        docs = (REPO_ROOT / "docs" / "CLOUD.md").read_text(encoding="utf-8")
        for phrase in ("page read", "page list", "page refresh", "zmem_page_adapter_actions",
                       "source", "freshness", "selector", "bootstrap", "projection"):
            self.assertIn(phrase.lower(), docs.lower())


if __name__ == "__main__":
    unittest.main()
