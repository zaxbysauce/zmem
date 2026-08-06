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


def _synthetic_migration_checkouts(tmp_path: Path) -> dict[str, Path]:
    return {
        "project:opencode-swarm": _make_git_repo(
            tmp_path / "opencode",
            "https://github.com/Org/OpenCode-Swarm.git",
        ),
        "project:ragappv3": _make_git_repo(
            tmp_path / "ragapp",
            "https://github.com/Org/RagAppV3.git",
        ),
        "project:trainingapp": _make_git_repo(
            tmp_path / "training",
            "https://github.com/Org/TrainingApp.git",
        ),
    }


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
    `github.com/<org>/<repo>` (or `ZMEM_PROXY_FORGE_HOST` if set AND valid --
    an unset var, an empty/whitespace var, and a set-but-unparseable var are
    three distinct non-rewrite-to-that-value cases covered below)."""

    def setUp(self):
        # Deterministic regardless of the ambient test-runner environment: if
        # ZMEM_PROXY_FORGE_HOST happens to be set in the shell running the
        # suite, every "collapses to github.com" assertion below would fail
        # for a reason that has nothing to do with the code under test. Save
        # the whole environ (mock.patch.dict's own restore mechanism, the
        # convention already used elsewhere in this class) and drop the var
        # for the duration of each test; the dedicated override tests below
        # keep setting it explicitly via their own nested patch.dict.
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("ZMEM_PROXY_FORGE_HOST", None)

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

    def test_forge_host_env_containing_slash_disables_the_rewrite(self):
        """A malformed ZMEM_PROXY_FORGE_HOST (e.g. containing a path) must not
        be concatenated verbatim into the namespace key -- that yields a
        malformed `host/org/repo` key -- and must NOT silently fall back to
        github.com either, since that would wrongly attribute a private
        forge's repos to the public one's namespace. It disables the rewrite
        entirely, exactly like the set-but-empty case: the loopback remote
        keeps its legacy `127.0.0.1:<port>/git/...` key."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp),
                "http://local_proxy@127.0.0.1:34567/git/ZaxbyHub/opencode-swarm",
            )
            with mock.patch.dict(
                os.environ,
                {"ZMEM_PROXY_FORGE_HOST": "evil.example.com/../inject"},
                clear=False,
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(
                result, "project:127.0.0.1:34567/git/zaxbyhub/opencode-swarm"
            )

    def test_forge_host_env_uppercase_is_lowercased(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp),
                "http://local_proxy@127.0.0.1:34567/git/ZaxbyHub/opencode-swarm",
            )
            with mock.patch.dict(
                os.environ,
                {"ZMEM_PROXY_FORGE_HOST": "GitLab.Example.COM"},
                clear=False,
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:gitlab.example.com/zaxbyhub/opencode-swarm")

    def test_forge_host_env_with_port_is_kept_verbatim(self):
        """A `host:port` value is valid and must be used VERBATIM (port
        included) -- ordinary, non-proxy remote normalization also keeps
        host:port in the key, so the proxy override must too, or the same
        forge would key differently depending on whether it was reached
        through the proxy or cloned directly."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp),
                "http://local_proxy@127.0.0.1:34567/git/org/repo",
            )
            with mock.patch.dict(
                os.environ, {"ZMEM_PROXY_FORGE_HOST": "gitlab.internal:8443"},
                clear=False,
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:gitlab.internal:8443/org/repo")

    def test_forge_host_env_with_underscore_label_is_accepted(self):
        """An underscore in a host label (e.g. an internal DNS name like
        `my_forge.internal`) is a legitimate value that the old bare-hostname
        regex rejected, silently falling back to github.com. It must now be
        accepted."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp),
                "http://local_proxy@127.0.0.1:34567/git/org/repo",
            )
            with mock.patch.dict(
                os.environ, {"ZMEM_PROXY_FORGE_HOST": "my_forge.internal"},
                clear=False,
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:my_forge.internal/org/repo")

    def test_forge_host_env_with_empty_label_disables_the_rewrite(self):
        """`a..b` has an empty label between the two dots -- unparseable, so
        the rewrite is disabled entirely (never falls back to github.com)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp),
                "http://local_proxy@127.0.0.1:34567/git/org/repo",
            )
            with mock.patch.dict(
                os.environ, {"ZMEM_PROXY_FORGE_HOST": "a..b"}, clear=False
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:127.0.0.1:34567/git/org/repo")

    def test_forge_host_env_with_leading_hyphen_label_disables_the_rewrite(self):
        """`-a.com` has a label starting with a hyphen -- unparseable, so the
        rewrite is disabled entirely (never falls back to github.com)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp),
                "http://local_proxy@127.0.0.1:34567/git/org/repo",
            )
            with mock.patch.dict(
                os.environ, {"ZMEM_PROXY_FORGE_HOST": "-a.com"}, clear=False
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:127.0.0.1:34567/git/org/repo")

    def test_forge_host_env_with_trailing_hyphen_label_disables_the_rewrite(self):
        """`a-.com` has a label ending with a hyphen -- unparseable, so the
        rewrite is disabled entirely (never falls back to github.com)."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp),
                "http://local_proxy@127.0.0.1:34567/git/org/repo",
            )
            with mock.patch.dict(
                os.environ, {"ZMEM_PROXY_FORGE_HOST": "a-.com"}, clear=False
            ):
                result = host.resolve_namespace(repo)
            self.assertEqual(result, "project:127.0.0.1:34567/git/org/repo")

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
    """resolve_namespace(checkout) == migrated key, from a portable configured map."""

    def test_migration_map_matches_live_resolve_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            checkouts = _synthetic_migration_checkouts(tmp_path)
            store_path = tmp_path / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            # Seed v4 rows under each old namespace before migrating.
            for old_ns in checkouts:
                store_mod.add_memory(
                    conn, namespace=old_ns, type_="fact",
                    content=f"seed row for {old_ns}", signal="test",
                )
            with mock.patch.object(
                store_mod,
                "_NS_MIGRATION_CHECKOUTS",
                {k: str(v) for k, v in checkouts.items()},
            ):
                store_mod.migrate(conn)

            for old_ns, checkout in checkouts.items():
                expected = host.resolve_namespace(checkout)
                row = conn.execute(
                    "SELECT namespace FROM memory WHERE content=?",
                    (f"seed row for {old_ns}",),
                ).fetchone()
                self.assertEqual(row["namespace"], expected)
            conn.close()

    def test_opencode_swarm_second_checkout_same_key_as_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            primary_checkout = _make_git_repo(
                tmp_path / "primary", "https://github.com/Org/OpenCode-Swarm.git"
            )
            second_checkout = _make_git_repo(
                tmp_path / "second", "git@github.com:Org/OpenCode-Swarm.git"
            )
            self.assertEqual(
                host.resolve_namespace(second_checkout),
                host.resolve_namespace(primary_checkout),
            )


class TestV5MigrationRefusesOnMissingCheckout(unittest.TestCase):
    def test_missing_checkout_leaves_namespace_unchanged_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store_path = tmp_path / "store.sqlite"
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

            missing = tmp_path / "missing-opencode-checkout"
            with mock.patch.object(
                store_mod,
                "_NS_MIGRATION_CHECKOUTS",
                {"project:opencode-swarm": str(missing)},
            ), mock.patch("builtins.print", side_effect=spy_print):
                store_mod.migrate(conn)

            row = conn.execute(
                "SELECT namespace FROM memory WHERE content=?",
                ("row that should be refused/skipped",),
            ).fetchone()
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
            tmp_path = Path(tmp)
            store_path = tmp_path / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            try:
                store_mod.add_memory(
                    conn, namespace="project:trainingapp", type_="fact",
                    content="trainingapp row under synthetic absence", signal="test",
                )

                captured_warnings = []
                real_print = print

                def spy_print(*args, **kwargs):
                    captured_warnings.append(" ".join(str(a) for a in args))
                    real_print(*args, **kwargs)

                with mock.patch.object(
                    store_mod,
                    "_NS_MIGRATION_CHECKOUTS",
                    {"project:trainingapp": str(tmp_path / "not-there")},
                ), mock.patch("builtins.print", side_effect=spy_print):
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
                self.assertEqual(version_row["value"], str(store_mod.SUPPORTED_SCHEMA_VERSION))

                migration_map = json.loads(
                    conn.execute(
                        "SELECT value FROM meta WHERE key='ns_migration_v5'"
                    ).fetchone()["value"]
                )
                self.assertNotIn("project:trainingapp", migration_map)
            finally:
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
            tmp_path = Path(tmp)
            checkouts = _synthetic_migration_checkouts(tmp_path)
            store_path = tmp_path / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = store_mod.connect()
            store_mod.init_db(conn)
            with mock.patch.object(
                store_mod,
                "_NS_MIGRATION_CHECKOUTS",
                {k: str(v) for k, v in checkouts.items()},
            ):
                store_mod.migrate(conn)

            row = conn.execute(
                "SELECT value FROM meta WHERE key='ns_migration_v5'"
            ).fetchone()
            self.assertIsNotNone(row)
            migration_map = json.loads(row[0])
            self.assertIsInstance(migration_map, dict)
            for old_ns, checkout in checkouts.items():
                self.assertIn(old_ns, migration_map)
                self.assertEqual(migration_map[old_ns], host.resolve_namespace(checkout))
            conn.close()

    def test_schema_version_bumped_after_migrate(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_path = Path(tmp) / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = store_mod.connect()
            store_mod.init_db(conn)
            store_mod.migrate(conn)
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            # migrate() runs every version block up to the current supported
            # version (v6 adds the merged_from consolidation-provenance column;
            # see store.py migrate). The v5 namespace migration it includes is
            # verified separately via the ns_migration_v5 meta key + namespace
            # rekeying in the surrounding tests.
            self.assertEqual(row["value"], str(store_mod.SUPPORTED_SCHEMA_VERSION))
            conn.close()

    def test_second_migrate_run_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            checkout = _make_git_repo(
                tmp_path / "opencode", "https://github.com/Org/OpenCode-Swarm.git"
            )
            store_path = tmp_path / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            store_mod.add_memory(
                conn, namespace="project:opencode-swarm", type_="fact",
                content="idempotency probe row", signal="test",
            )
            with mock.patch.object(
                store_mod,
                "_NS_MIGRATION_CHECKOUTS",
                {"project:opencode-swarm": str(checkout)},
            ):
                store_mod.migrate(conn)
            row_after_first = conn.execute(
                "SELECT namespace FROM memory WHERE content=?",
                ("idempotency probe row",),
            ).fetchone()
            map_after_first = conn.execute(
                "SELECT value FROM meta WHERE key='ns_migration_v5'"
            ).fetchone()["value"]

            # Second run: the v5 block must be a pure no-op — namespace and
            # rollback map both unchanged. (schema_version is already at the
            # final supported version after the first run.)
            with mock.patch.object(
                store_mod,
                "_NS_MIGRATION_CHECKOUTS",
                {"project:opencode-swarm": str(checkout)},
            ):
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
            self.assertEqual(version_row["value"], str(store_mod.SUPPORTED_SCHEMA_VERSION))
            conn.close()


class TestRecallCompatAlias(unittest.TestCase):
    """A row migrated to its new namespace must still be recallable by its
    old (pre-migration) namespace for one release, and vice versa."""

    def test_recall_finds_row_by_old_namespace_after_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            checkout = _make_git_repo(
                tmp_path / "opencode", "https://github.com/Org/OpenCode-Swarm.git"
            )
            store_path = tmp_path / "store.sqlite"
            store_mod = _load_store_module(store_path)
            conn = _fresh_conn_at_v4(store_mod)
            try:
                store_mod.add_memory(
                    conn, namespace="project:opencode-swarm", type_="fact",
                    content="unique lesson about widget frobnication", signal="test",
                )
                with mock.patch.object(
                    store_mod,
                    "_NS_MIGRATION_CHECKOUTS",
                    {"project:opencode-swarm": str(checkout)},
                ):
                    store_mod.migrate(conn)

                new_ns = None
                row = conn.execute(
                    "SELECT namespace FROM memory WHERE content=?",
                    ("unique lesson about widget frobnication",),
                ).fetchone()
                new_ns = row["namespace"]

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
        # configured migration checkout, so it still exercises the
        # alias-matching logic on a box/CI leg where those paths don't exist
        # and the portable migration test above would otherwise be bypassed.
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
        """A store that has run the full migration suite (so it is at the
        current supported schema_version) but still carries `old_ns` — exactly
        the shape the original v5 migration leaves behind when the mapped
        checkout was missing at the time. The version-INDEPENDENT retry pass
        picks such rows up later."""
        conn = store_mod.connect()
        store_mod.init_db(conn)
        store_mod.migrate(conn)
        self.assertEqual(
            conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                .fetchone()["value"], str(store_mod.SUPPORTED_SCHEMA_VERSION))
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
            # schema_version is untouched by the retry (it stays at whatever the
            # last full migrate() left it at — the current supported version).
            self.assertEqual(
                conn.execute("SELECT value FROM meta WHERE key='schema_version'")
                    .fetchone()["value"], str(store_mod.SUPPORTED_SCHEMA_VERSION))
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
