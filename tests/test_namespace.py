"""Plain-unittest tests for host.py:resolve_namespace() and the v5 namespace
migration in store.py.

Run: python tests/test_namespace.py
No pytest / third-party test harness required — matches the repo convention
(tests/test_host.py).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import host  # noqa: E402


def _load_store_module(zmem_store_path: Path):
    """Load store.py as a fresh module instance pointed at zmem_store_path.
    store.py resolves STORE_PATH at import time from the environment, so
    each test that needs an isolated store loads its own module instance."""
    spec = importlib.util.spec_from_file_location(
        f"zmem_store_test_{id(zmem_store_path)}", SCRIPTS_DIR / "store.py"
    )
    with mock.patch.dict(os.environ, {"ZMEM_STORE": str(zmem_store_path)}, clear=False):
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


def _fresh_conn_at_v4(store_mod):
    """Return a connection with the full v1-v4 schema applied (so columns
    like `embedding` that add_memory() needs already exist) but with
    schema_version reset to 4, so a subsequent migrate() call re-runs the v5
    block fresh against whatever old-namespace rows the test seeds. This
    mirrors production: v5 always runs against an already-v4 store."""
    conn = store_mod.connect()
    store_mod.init_db(conn)
    store_mod.migrate(conn)
    conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    conn.commit()
    return conn


def _make_git_repo(tmp_path: Path, remote_url: str) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", remote_url], cwd=str(repo), check=True
    )
    return repo


class TestResolveNamespaceNormalization(unittest.TestCase):
    """git@ vs https vs trailing-slash vs case all collapse to one key."""

    def test_ssh_and_https_same_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ssh_repo = _make_git_repo(tmp_path / "a", "git@github.com:Org/Repo.git")
            https_repo = _make_git_repo(tmp_path / "b", "https://github.com/Org/Repo.git")
            self.assertEqual(
                host.resolve_namespace(ssh_repo), host.resolve_namespace(https_repo)
            )
            self.assertEqual(
                host.resolve_namespace(ssh_repo), "project:github.com/org/repo"
            )

    def test_trailing_slash_and_no_git_suffix_same_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            r1 = _make_git_repo(tmp_path / "a", "https://github.com/Org/Repo.git")
            r2 = _make_git_repo(tmp_path / "b", "https://github.com/Org/Repo/")
            r3 = _make_git_repo(tmp_path / "c", "https://github.com/Org/Repo")
            self.assertEqual(host.resolve_namespace(r1), host.resolve_namespace(r2))
            self.assertEqual(host.resolve_namespace(r1), host.resolve_namespace(r3))

    def test_case_insensitive_same_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            r1 = _make_git_repo(tmp_path / "a", "https://GitHub.com/Org/Repo.git")
            r2 = _make_git_repo(tmp_path / "b", "https://github.com/org/repo.git")
            self.assertEqual(host.resolve_namespace(r1), host.resolve_namespace(r2))

    def test_known_temp_git_repo_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(Path(tmp), "git@github.com:zaxbysauce/zmem.git")
            self.assertEqual(
                host.resolve_namespace(repo), "project:github.com/zaxbysauce/zmem"
            )

    def test_no_remote_falls_back_to_abspath(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            result = host.resolve_namespace(tmp_path)
            expected = "project:" + os.path.abspath(str(tmp_path)).replace("\\", "/").lower()
            self.assertEqual(result, expected)

    def test_not_a_git_repo_at_all_falls_back_to_abspath(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain_dir = Path(tmp) / "not_a_repo"
            plain_dir.mkdir()
            result = host.resolve_namespace(plain_dir)
            self.assertTrue(result.startswith("project:"))
            self.assertNotIn("github.com", result)

    def test_worktree_style_second_checkout_same_remote_same_key(self):
        # Two independent clones of the same remote (simulating worktrees /
        # a second checkout) must collapse to the same namespace key.
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            clone_a = _make_git_repo(tmp_path / "checkout1", "https://github.com/ZaxbyHub/opencode-swarm.git")
            clone_b = _make_git_repo(tmp_path / "checkout2", "https://github.com/ZaxbyHub/opencode-swarm.git")
            self.assertEqual(host.resolve_namespace(clone_a), host.resolve_namespace(clone_b))


class TestLoopbackProxyRemoteRewrite(unittest.TestCase):
    """CCR (Claude Code cloud/remote) sessions see their GitHub repo through a
    local HTTP proxy (`http://local_proxy@127.0.0.1:<port>/git/<org>/<repo>`).
    Without a rewrite, the ephemeral proxy port lands in the namespace key,
    fragmenting the same repo's memory across sessions and diverging from the
    key a local checkout of the same remote gets. These pin the collapse to
    `github.com/<org>/<repo>` (or `ZMEM_PROXY_FORGE_HOST` if set)."""

    def test_two_observed_proxy_urls_different_ports_collapse_to_same_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            repo_a = _make_git_repo(
                tmp_path / "a",
                "http://local_proxy@127.0.0.1:34567/git/ZaxbyHub/opencode-swarm",
            )
            repo_b = _make_git_repo(
                tmp_path / "b",
                "http://local_proxy@127.0.0.1:41999/git/ZaxbyHub/opencode-swarm",
            )
            expected = "project:github.com/zaxbyhub/opencode-swarm"
            self.assertEqual(host.resolve_namespace(repo_a), expected)
            self.assertEqual(host.resolve_namespace(repo_b), expected)

    def test_localhost_form_also_rewrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp), "http://x@localhost:8080/git/Org/Repo"
            )
            self.assertEqual(
                host.resolve_namespace(repo), "project:github.com/org/repo"
            )

    def test_git_suffix_on_proxy_path_is_stripped_before_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp),
                "http://local_proxy@127.0.0.1:34567/git/ZaxbyHub/repo.git",
            )
            self.assertEqual(
                host.resolve_namespace(repo), "project:github.com/zaxbyhub/repo"
            )

    def test_zmem_proxy_forge_host_env_var_overrides_forge_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp),
                "http://local_proxy@127.0.0.1:34567/git/ZaxbyHub/opencode-swarm",
            )
            with mock.patch.dict(
                os.environ, {"ZMEM_PROXY_FORGE_HOST": "gitlab.example.com"}, clear=False
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:gitlab.example.com/zaxbyhub/opencode-swarm")

    def test_empty_forge_host_env_disables_the_rewrite_entirely(self):
        """ZMEM_PROXY_FORGE_HOST has THREE states, not two. Set-but-empty is
        the OPT-OUT, distinct from unset: it is how a genuine local git server
        that serves repos under a `/git/` prefix (Gitea's default layout) says
        'I am not a CCR proxy -- keep my literal loopback key'. Collapsing it
        onto github.com would merge an unrelated local repo's memory into a
        public repo's namespace."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp), "http://gitea@127.0.0.1:3000/git/MyOrg/MyRepo"
            )
            with mock.patch.dict(
                os.environ, {"ZMEM_PROXY_FORGE_HOST": ""}, clear=False
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:127.0.0.1:3000/git/myorg/myrepo")

    def test_whitespace_only_forge_host_env_also_disables_the_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp), "http://gitea@localhost:3000/git/MyOrg/MyRepo"
            )
            with mock.patch.dict(
                os.environ, {"ZMEM_PROXY_FORGE_HOST": "   "}, clear=False
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:localhost:3000/git/myorg/myrepo")

    def test_unset_forge_host_env_still_defaults_to_github(self):
        """Guards the distinction the opt-out introduces: absent must NOT be
        treated as empty."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp), "http://local_proxy@127.0.0.1:34567/git/Org/Repo"
            )
            env = {k: v for k, v in os.environ.items()
                   if k != "ZMEM_PROXY_FORGE_HOST"}
            with mock.patch.dict(os.environ, env, clear=True):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:github.com/org/repo")

    def test_uppercase_git_path_prefix_still_rewrites(self):
        """The host check is case-insensitive; the path prefix must match it,
        or a proxy URL differing only in the case of `git/` fragments the same
        repo's memory into a second namespace."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            upper = _make_git_repo(
                tmp_path / "a", "http://local_proxy@127.0.0.1:34567/GIT/Org/Repo")
            mixed = _make_git_repo(
                tmp_path / "b", "http://local_proxy@127.0.0.1:41999/Git/Org/Repo")
            expected = "project:github.com/org/repo"
            self.assertEqual(host.resolve_namespace(upper), expected)
            self.assertEqual(host.resolve_namespace(mixed), expected)

    def test_non_git_path_loopback_remote_unchanged(self):
        # Pin today's (pre-existing) behavior for a loopback remote whose path
        # does NOT start with `git/` — a genuinely local git server keeps its
        # existing key; the proxy rewrite must not touch it.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp), "http://user@127.0.0.1:9000/some/other/path"
            )
            self.assertEqual(
                host.resolve_namespace(repo),
                "project:127.0.0.1:9000/some/other/path",
            )

    def test_loopback_with_only_one_git_path_segment_not_rewritten(self):
        # Fewer than two segments after `git/` (no repo, only an org-shaped
        # segment) falls through to the existing (unrewritten) behavior rather
        # than guessing.
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp), "http://u@127.0.0.1:1/git/onlyorg"
            )
            self.assertEqual(
                host.resolve_namespace(repo),
                "project:127.0.0.1:1/git/onlyorg",
            )

    def test_existing_ssh_and_https_forms_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            ssh_repo = _make_git_repo(tmp_path / "a", "git@github.com:ZaxbyHub/x.git")
            https_repo = _make_git_repo(tmp_path / "b", "https://github.com/a/b")
            self.assertEqual(
                host.resolve_namespace(ssh_repo), "project:github.com/zaxbyhub/x"
            )
            self.assertEqual(
                host.resolve_namespace(https_repo), "project:github.com/a/b"
            )


class TestV5MigrationEqualityInvariant(unittest.TestCase):
    """resolve_namespace(checkout) == migrated key, for real on-disk checkouts."""

    REAL_CHECKOUTS = {
        "project:opencode-swarm": r"E:\ZCode\opencode-swarm",
        "project:ragappv3": r"E:\ZCode\ragappv3",
        "project:trainingapp": r"E:\ZCode\trainingapp",
        "project:zmem": r"C:\Users\Brett\.graphify\repos\zaxbysauce\zmem",
    }

    def test_migration_map_matches_live_resolve_namespace(self):
        # Skip gracefully if this box's checkouts aren't present (e.g. CI).
        for old_ns, checkout in self.REAL_CHECKOUTS.items():
            if not Path(checkout).is_dir():
                self.skipTest(f"checkout not present on this box: {checkout}")

        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            # Seed v4 rows under each old namespace before migrating.
            for old_ns in self.REAL_CHECKOUTS:
                store_mod.add_memory(
                    conn, namespace=old_ns, type_="fact",
                    content=f"seed row for {old_ns}", signal="test",
                )
            store_mod.migrate(conn)

            for old_ns, checkout in self.REAL_CHECKOUTS.items():
                expected = host.resolve_namespace(checkout)
                row = conn.execute(
                    "SELECT namespace FROM memory WHERE content=?",
                    (f"seed row for {old_ns}",),
                ).fetchone()
                self.assertEqual(row["namespace"], expected)
            conn.close()

    def test_opencode_swarm_second_checkout_same_key_as_migrated(self):
        second_checkout = r"E:\ClaudeCode\opencode-swarm-dev"
        primary_checkout = r"E:\ZCode\opencode-swarm"
        if not Path(second_checkout).is_dir() or not Path(primary_checkout).is_dir():
            self.skipTest("opencode-swarm checkouts not present on this box")
        self.assertEqual(
            host.resolve_namespace(second_checkout),
            host.resolve_namespace(primary_checkout),
        )


class TestV5MigrationRefusesOnMissingCheckout(unittest.TestCase):
    def test_missing_checkout_leaves_namespace_unchanged_and_reports(self):
        # store.py hardcodes the {old_ns: checkout_path} map inline rather than
        # as an importable module-level constant, so we can't inject a fake
        # "missing" path into it directly. Instead, exercise the real refuse-
        # on-missing behavior the same way it will actually occur: call
        # migrate() and assert that whichever of the four mapped checkouts is
        # genuinely absent on this box is left with its namespace unchanged
        # (and, for the case actually seen on this box, that a present
        # checkout IS migrated) — this is the exact code path production runs.
        #
        # NOTE: this test is inherently machine-dependent — whichever branch
        # runs depends on whether E:\ZCode\opencode-swarm exists on the box
        # executing the suite, so it can pass without ever having exercised
        # the "checkout missing -> refuse and report" path (e.g. if that
        # checkout happens to exist wherever this runs). It is kept as an
        # opportunistic real-environment sanity check only. The deterministic,
        # box-independent, and therefore AUTHORITATIVE test for the
        # refuse-on-missing-checkout behavior is
        # test_synthetic_missing_checkout_via_relocated_map below, which
        # monkeypatches Path.is_dir so the missing-checkout branch is always
        # exercised regardless of what's on disk.
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            store_mod.add_memory(
                conn, namespace="project:opencode-swarm", type_="fact",
                content="row that should be refused/skipped", signal="test",
            )

            captured_warnings = []
            real_print = print

            def spy_print(*args, **kwargs):
                captured_warnings.append(" ".join(str(a) for a in args))
                real_print(*args, **kwargs)

            with mock.patch("builtins.print", side_effect=spy_print):
                store_mod.migrate(conn)

            row = conn.execute(
                "SELECT namespace FROM memory WHERE content=?",
                ("row that should be refused/skipped",),
            ).fetchone()
            if Path(r"E:\ZCode\opencode-swarm").is_dir():
                self.assertEqual(row["namespace"], host.resolve_namespace(r"E:\ZCode\opencode-swarm"))
            else:
                # Checkout genuinely absent -> refuse-and-report: namespace
                # left unchanged, and a loud warning was printed.
                self.assertEqual(row["namespace"], "project:opencode-swarm")
                self.assertTrue(any("opencode-swarm" in w and "not found" in w for w in captured_warnings))
            conn.close()

    def test_synthetic_missing_checkout_via_relocated_map(self):
        # A deterministic (box-independent) version of the refuse-on-missing
        # behavior: monkeypatch Path.is_dir so a mapped checkout path looks
        # absent regardless of what's actually on this box, and confirm that
        # specific namespace's rows are left untouched while the migration
        # still completes (schema_version still bumps to 5). Also asserts the
        # "report" half (a loud warning is printed) so this test alone is the
        # sole authoritative, deterministic check of the full refuse-and-
        # report contract — it does not depend on
        # test_missing_checkout_leaves_namespace_unchanged_and_reports above,
        # which only exercises the report assertion when the real checkout
        # happens to be absent on the machine running the suite.
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            store_mod.add_memory(
                conn, namespace="project:trainingapp", type_="fact",
                content="trainingapp row under synthetic absence", signal="test",
            )

            real_is_dir = Path.is_dir

            def fake_is_dir(self):
                if str(self) == r"E:\ZCode\trainingapp":
                    return False
                return real_is_dir(self)

            captured_warnings = []
            real_print = print

            def spy_print(*args, **kwargs):
                captured_warnings.append(" ".join(str(a) for a in args))
                real_print(*args, **kwargs)

            with mock.patch.object(Path, "is_dir", fake_is_dir), \
                    mock.patch("builtins.print", side_effect=spy_print):
                store_mod.migrate(conn)

            self.assertTrue(
                any("trainingapp" in w and "not found" in w for w in captured_warnings),
                "migrate() must print a loud warning when a mapped checkout is missing",
            )

            row = conn.execute(
                "SELECT namespace FROM memory WHERE content=?",
                ("trainingapp row under synthetic absence",),
            ).fetchone()
            self.assertEqual(row["namespace"], "project:trainingapp")

            version_row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            self.assertEqual(version_row["value"], "5")

            migration_map = json.loads(
                conn.execute(
                    "SELECT value FROM meta WHERE key='ns_migration_v5'"
                ).fetchone()["value"]
            )
            self.assertNotIn("project:trainingapp", migration_map)
            conn.close()

    def test_unmappable_namespace_project_zcode_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            store_mod.add_memory(
                conn, namespace="project:ZCode", type_="fact",
                content="unmappable spurious row", signal="test",
            )
            store_mod.add_memory(
                conn, namespace="user:global", type_="fact",
                content="global row", signal="test",
            )
            store_mod.migrate(conn)
            row_zcode = conn.execute(
                "SELECT namespace FROM memory WHERE content=?",
                ("unmappable spurious row",),
            ).fetchone()
            row_global = conn.execute(
                "SELECT namespace FROM memory WHERE content=?",
                ("global row",),
            ).fetchone()
            self.assertEqual(row_zcode["namespace"], "project:ZCode")
            self.assertEqual(row_global["namespace"], "user:global")
            conn.close()


class TestV5MigrationRollbackMapAndIdempotency(unittest.TestCase):
    def test_rollback_map_recorded_in_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = store_mod.connect()
            store_mod.init_db(conn)
            store_mod.migrate(conn)

            row = conn.execute(
                "SELECT value FROM meta WHERE key='ns_migration_v5'"
            ).fetchone()
            self.assertIsNotNone(row)
            migration_map = json.loads(row[0])
            self.assertIsInstance(migration_map, dict)
            # Every present-on-disk checkout must appear with its derived key.
            for old_ns, checkout in TestV5MigrationEqualityInvariant.REAL_CHECKOUTS.items():
                if Path(checkout).is_dir():
                    self.assertIn(old_ns, migration_map)
                    self.assertEqual(migration_map[old_ns], host.resolve_namespace(checkout))
            conn.close()

    def test_schema_version_bumped_to_5(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = store_mod.connect()
            store_mod.init_db(conn)
            store_mod.migrate(conn)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            self.assertEqual(row["value"], "5")
            conn.close()

    def test_second_migrate_run_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            store_mod.add_memory(
                conn, namespace="project:opencode-swarm", type_="fact",
                content="idempotency probe row", signal="test",
            )
            store_mod.migrate(conn)
            row_after_first = conn.execute(
                "SELECT namespace FROM memory WHERE content=?",
                ("idempotency probe row",),
            ).fetchone()
            map_after_first = conn.execute(
                "SELECT value FROM meta WHERE key='ns_migration_v5'"
            ).fetchone()["value"]

            # Second run: schema_version already 5, so the v5 block must be a
            # pure no-op — namespace and rollback map both unchanged.
            store_mod.migrate(conn)
            row_after_second = conn.execute(
                "SELECT namespace FROM memory WHERE content=?",
                ("idempotency probe row",),
            ).fetchone()
            map_after_second = conn.execute(
                "SELECT value FROM meta WHERE key='ns_migration_v5'"
            ).fetchone()["value"]

            self.assertEqual(row_after_first["namespace"], row_after_second["namespace"])
            self.assertEqual(map_after_first, map_after_second)
            version_row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            self.assertEqual(version_row["value"], "5")
            conn.close()


class TestRecallCompatAlias(unittest.TestCase):
    """A row migrated to its new namespace must still be recallable by its
    old (pre-migration) namespace for one release, and vice versa."""

    def test_recall_finds_row_by_old_namespace_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            try:
                store_mod.add_memory(
                    conn, namespace="project:opencode-swarm", type_="fact",
                    content="unique lesson about widget frobnication", signal="test",
                )
                store_mod.migrate(conn)

                new_ns = None
                row = conn.execute(
                    "SELECT namespace FROM memory WHERE content=?",
                    ("unique lesson about widget frobnication",),
                ).fetchone()
                new_ns = row["namespace"]

                if new_ns == "project:opencode-swarm":
                    self.skipTest("E:\\ZCode\\opencode-swarm checkout not present; namespace unchanged")

                # Recall using the OLD namespace must still find the row via the
                # compat alias, and must not double-count it.
                results = store_mod.recall_memory(
                    conn, query="widget frobnication", namespace="project:opencode-swarm",
                    limit=10, as_json=False,
                )
                ids = [r["id"] for r in results]
                self.assertEqual(len(ids), len(set(ids)), "row must not be double-counted")
                self.assertTrue(
                    any(r["content"] == "unique lesson about widget frobnication" for r in results),
                    "recall by old namespace should find the migrated row via the compat alias",
                )

                # Recall using the NEW namespace must also find it (direct match).
                results_new = store_mod.recall_memory(
                    conn, query="widget frobnication", namespace=new_ns,
                    limit=10, as_json=False,
                )
                self.assertTrue(
                    any(r["content"] == "unique lesson about widget frobnication" for r in results_new)
                )
            finally:
                # Must close before the enclosing TemporaryDirectory's __exit__
                # tries to rmtree the dir — otherwise an early exit via
                # skipTest() (or any other exception) leaves conn holding the
                # sqlite file open, which raises PermissionError on Windows
                # during cleanup.
                conn.close()

    def test_recall_synthetic_alias_finds_row_by_old_namespace(self):
        # Environment-independent version of the above: doesn't depend on any
        # real on-disk checkout (E:\ZCode\...), so it still exercises the
        # alias-matching logic on a box/CI leg where those paths don't exist
        # and the real-checkout migration test above would have skipped.
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = store_mod.connect()
            store_mod.init_db(conn)
            store_mod.migrate(conn)
            # Inject a synthetic pre-migration alias map and a row already
            # living under the "new" namespace side of it.
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('ns_migration_v5', ?)",
                (json.dumps({"project:widgetco": "project:github.com/example/widgetco"}),),
            )
            conn.commit()
            store_mod.add_memory(
                conn, namespace="project:github.com/example/widgetco", type_="fact",
                content="synthetic aliased lesson about gizmo assembly", signal="test",
            )

            # Recall by the OLD (pre-migration) namespace must find it.
            results_old = store_mod.recall_memory(
                conn, query="gizmo assembly", namespace="project:widgetco",
                limit=10, as_json=False,
            )
            ids_old = [r["id"] for r in results_old]
            self.assertEqual(len(ids_old), len(set(ids_old)))
            self.assertTrue(
                any(r["content"] == "synthetic aliased lesson about gizmo assembly" for r in results_old)
            )

            # Recall by the NEW namespace must also find it (direct match).
            results_new = store_mod.recall_memory(
                conn, query="gizmo assembly", namespace="project:github.com/example/widgetco",
                limit=10, as_json=False,
            )
            self.assertTrue(
                any(r["content"] == "synthetic aliased lesson about gizmo assembly" for r in results_new)
            )
            conn.close()

    def test_recall_with_no_migration_map_behaves_normally(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = store_mod.connect()
            store_mod.init_db(conn)
            store_mod.migrate(conn)
            # Simulate "no migration map recorded" (e.g. a store that never
            # went through the v5 migration on this box) by removing the
            # meta key add_memory/recall don't otherwise depend on.
            conn.execute("DELETE FROM meta WHERE key='ns_migration_v5'")
            conn.commit()
            store_mod.add_memory(
                conn, namespace="user:global", type_="fact",
                content="plain unmigrated memory", signal="test",
            )
            results = store_mod.recall_memory(
                conn, query="plain unmigrated", namespace="user:global",
                limit=10, as_json=False,
            )
            self.assertTrue(any(r["content"] == "plain unmigrated memory" for r in results))
            conn.close()


class TestV5MigrationRetryAfterCheckoutAppears(unittest.TestCase):
    """A namespace the v5 pass had to skip must NOT be stranded forever.

    The v5 block is version-gated, so it fires exactly once. Any namespace whose
    checkout happened to be absent at that instant (unmounted drive, repo not
    cloned yet) used to be skipped permanently — schema_version went to 5
    regardless, so the gate never let the block run again. migrate() now also
    runs a version-INDEPENDENT retry pass over the known old-style keys.
    """

    def _seed_v5_store_with_a_stranded_row(self, store_mod, old_ns: str):
        """A store already at schema_version 5 that still carries `old_ns` —
        exactly the shape the original migration leaves behind when the mapped
        checkout was missing at the time."""
        conn = store_mod.connect()
        store_mod.init_db(conn)
        store_mod.migrate(conn)
        self.assertEqual(
            conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                .fetchone()["value"], "5")
        store_mod.add_memory(
            conn, namespace=old_ns, type_="fact",
            content="stranded row under an old-style namespace", signal="test",
        )
        return conn

    def _namespace_of_the_row(self, conn) -> str:
        return conn.execute(
            "SELECT namespace FROM memory WHERE content=?",
            ("stranded row under an old-style namespace",),
        ).fetchone()["namespace"]

    def test_row_is_rekeyed_once_the_checkout_appears(self):
        old_ns = "project:trainingapp"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            checkout = _make_git_repo(tmp_path / "late", "https://github.com/Org/LateRepo.git")
            store_mod = _load_store_module(tmp_path / "store.sqlite")
            conn = self._seed_v5_store_with_a_stranded_row(store_mod, old_ns)
            self.assertEqual(self._namespace_of_the_row(conn), old_ns)

            # The checkout "appears": point the known map at it and re-run
            # migrate() on the ALREADY-v5 store. Under the old
            # version-gated-only code this was a guaranteed no-op.
            with mock.patch.object(store_mod, "_NS_MIGRATION_CHECKOUTS",
                                   {old_ns: str(checkout)}):
                store_mod.migrate(conn)

            expected = host.resolve_namespace(checkout)
            self.assertEqual(expected, "project:github.com/org/laterepo")
            self.assertEqual(self._namespace_of_the_row(conn), expected)

            recorded = json.loads(
                conn.execute("SELECT value FROM meta WHERE key='ns_migration_v5'")
                    .fetchone()["value"])
            self.assertEqual(recorded[old_ns], expected)
            # schema_version is untouched by the retry.
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                    .fetchone()["value"], "5")
            conn.close()

    def test_retry_rekeys_tombstones_too(self):
        """Supersession is a tombstone UPDATE, never a DELETE. A superseded row
        left behind under a dead key would be cut off from its own namespace's
        history, so the re-key covers every row, live or not (same semantics as
        the original v5 UPDATE)."""
        old_ns = "project:ragappv3"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            checkout = _make_git_repo(tmp_path / "late", "https://github.com/Org/Tomb.git")
            store_mod = _load_store_module(tmp_path / "store.sqlite")
            conn = self._seed_v5_store_with_a_stranded_row(store_mod, old_ns)
            dead_id = store_mod.add_memory(
                conn, namespace=old_ns, type_="fact",
                content="already superseded row", signal="test",
            )
            store_mod.supersede_memory(conn, dead_id, "test tombstone")

            with mock.patch.object(store_mod, "_NS_MIGRATION_CHECKOUTS",
                                   {old_ns: str(checkout)}):
                store_mod.migrate(conn)

            expected = host.resolve_namespace(checkout)
            row = conn.execute(
                "SELECT namespace, superseded_at FROM memory WHERE id=?", (dead_id,)
            ).fetchone()
            self.assertEqual(row["namespace"], expected)
            self.assertIsNotNone(row["superseded_at"])
            self.assertEqual(
                conn.execute("SELECT count(*) FROM memory WHERE namespace=?",
                             (old_ns,)).fetchone()[0], 0)
            conn.close()

    def test_still_refuses_to_guess_while_the_checkout_is_absent(self):
        """The retry keeps the original safety property: never invent a key.
        An absent checkout leaves the rows alone and reports, and the SAME store
        is picked up on a later run once the path exists."""
        old_ns = "project:trainingapp"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing = tmp_path / "not-cloned-yet"
            store_mod = _load_store_module(tmp_path / "store.sqlite")
            conn = self._seed_v5_store_with_a_stranded_row(store_mod, old_ns)

            captured = []
            real_print = print

            def spy_print(*args, **kwargs):
                captured.append(" ".join(str(a) for a in args))
                real_print(*args, **kwargs)

            with mock.patch.object(store_mod, "_NS_MIGRATION_CHECKOUTS",
                                   {old_ns: str(missing)}), \
                    mock.patch("builtins.print", side_effect=spy_print):
                store_mod.migrate(conn)

            self.assertEqual(self._namespace_of_the_row(conn), old_ns)
            self.assertTrue(any("trainingapp" in c and "not found" in c for c in captured),
                            "an absent checkout must still be reported loudly")

            # ...and the very same store re-keys on the next run once the
            # checkout is there. That is the whole point of decoupling the retry
            # from the version gate.
            checkout = _make_git_repo(tmp_path / "arrived", "https://github.com/Org/Arrived.git")
            with mock.patch.object(store_mod, "_NS_MIGRATION_CHECKOUTS",
                                   {old_ns: str(checkout)}):
                store_mod.migrate(conn)
            self.assertEqual(self._namespace_of_the_row(conn),
                             host.resolve_namespace(checkout))
            conn.close()

    def test_retry_is_a_no_op_when_nothing_is_stranded(self):
        """Nothing left under an old-style key => no re-derivation and no meta
        write, so the unconditional pass costs one SELECT. resolve_namespace
        must not even be called."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store_mod = _load_store_module(tmp_path / "store.sqlite")
            conn = store_mod.connect()
            store_mod.init_db(conn)
            store_mod.migrate(conn)
            store_mod.add_memory(
                conn, namespace="user:global", type_="fact",
                content="nothing stranded here", signal="test",
            )
            before = conn.execute(
                "SELECT value FROM meta WHERE key='ns_migration_v5'").fetchone()

            with mock.patch.object(host, "resolve_namespace") as resolver:
                store_mod.migrate(conn)
                resolver.assert_not_called()

            after = conn.execute(
                "SELECT value FROM meta WHERE key='ns_migration_v5'").fetchone()
            self.assertEqual(
                before["value"] if before else None,
                after["value"] if after else None,
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
