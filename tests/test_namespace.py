"""Plain-unittest tests for host.py:resolve_namespace() and the v5 namespace
migration in store.py.

Run: python tests/test_namespace.py
No pytest / third-party test harness required — matches the repo convention
(tests/test_host.py).
"""

from __future__ import annotations

import atexit  # noqa: E402
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

# Issue #97: pin the store/env family ONLY for the import-time freeze (storelib
# resolves STORE_PATH once at first import) and restore the caller's env right
# after — module-level assignments that never restore would silently redirect
# every sibling module imported later in a shared-process co-run (PR #199
# review 199-c). The per-test cases patch env explicitly.
import uuid as _uuid  # noqa: E402

_PIN_KEYS = ("ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR", "ZMEM_MODEL_AUTODOWNLOAD")
_prior_env = {k: os.environ.get(k) for k in _PIN_KEYS}
_test_scratch = Path(tempfile.gettempdir()) / (
    "zmem-namespace-tests-" + _uuid.uuid4().hex
)
_test_scratch.mkdir(parents=True, exist_ok=True)
atexit.register(shutil.rmtree, _test_scratch, True)
os.environ["ZMEM_STORE"] = str(_test_scratch / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_test_scratch)
os.environ["ZMEM_MODELS_DIR"] = str(_test_scratch / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"

import host  # noqa: E402


# storelib submodules (issue #57): the store shim cannot forward
# attribute writes, so tests that mock a mutable global patch the owning submodule.
sys.path.insert(0, str(SCRIPTS_DIR))
import importlib as _ii
_schema_mod = _ii.import_module("storelib.schema")

# Both host and storelib have resolved their frozen paths — hand the env back.
for _k, _v in _prior_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


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

    def test_resolve_scopes_defaults_and_environment_selection(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(host.resolve_scopes(), {})
            self.assertEqual(host.resolve_scopes(hostname="spark1"), {
                "host": "host:spark1",
            })
            self.assertEqual(host.resolve_scopes(env={}), {})
        with mock.patch.dict(os.environ, {"ZMEM_FLEET": "ambient"}, clear=False):
            self.assertEqual(host.resolve_scopes(env=None), {
                "fleet": "fleet:ambient",
            })

    def test_resolve_scopes_without_project(self):
        with mock.patch.dict(os.environ, {"ZMEM_FLEET": "ambient-must-not-win"}, clear=False):
            scopes = host.resolve_scopes(
                None,
                "spark1",
                {"ZMEM_FLEET": "dgx"},
                {"agent_identity": "ops"},
            )
        self.assertEqual(
            scopes,
            {
                "host": "host:spark1",
                "fleet": "fleet:dgx",
                "agent": "agent:ops",
            },
        )
        with mock.patch.dict(os.environ, {"ZMEM_FLEET": "dgx"}, clear=False):
            env_none_scopes = host.resolve_scopes(
                None, "spark1", None, {"agent_identity": "ops"}
            )
        self.assertEqual(
            env_none_scopes,
            {
                "host": "host:spark1",
                "fleet": "fleet:dgx",
                "agent": "agent:ops",
            },
        )

    def test_resolve_scopes_with_project_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _make_git_repo(
                Path(tmp), "https://github.com/O/R.git"
            )
            scopes = host.resolve_scopes(
                repo,
                "spark1",
                {"ZMEM_FLEET": "dgx"},
                {"agent_identity": "ops"},
            )
        self.assertEqual(
            scopes,
            {
                "project": "project:github.com/o/r",
                "host": "host:spark1",
                "fleet": "fleet:dgx",
                "agent": "agent:ops",
            },
        )

    def test_resolve_scopes_rejects_invalid_injected_value(self):
        cases = (
            (None, "spark:1", {}, None),
            (None, None, {"ZMEM_FLEET": "dgx:spark"}, None),
            (None, None, {}, {"agent_identity": "ops:1"}),
        )
        for project_dir, hostname, env, hermes_kwargs in cases:
            with self.subTest(hostname=hostname, env=env, hermes_kwargs=hermes_kwargs):
                with self.assertRaises(ValueError):
                    host.resolve_scopes(
                        project_dir, hostname, env, hermes_kwargs
                    )

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
                _schema_mod,
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
                _schema_mod,
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
                    _schema_mod,
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
                _schema_mod,
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
                _schema_mod,
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
                _schema_mod,
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
                    _schema_mod,
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
            with mock.patch.object(_schema_mod, "_NS_MIGRATION_CHECKOUTS",
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

            with mock.patch.object(_schema_mod, "_NS_MIGRATION_CHECKOUTS",
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

            with mock.patch.object(_schema_mod, "_NS_MIGRATION_CHECKOUTS",
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
            with mock.patch.object(_schema_mod, "_NS_MIGRATION_CHECKOUTS",
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


class NamespaceCacheTest(unittest.TestCase):
    """Issue #97: the three git statuses and the namespace cache contract.

    A git ERROR inside a checkout that HAS an origin must resolve to the
    cached remote key (warm) or exactly ``user:global`` (cold) — never a
    path-shaped key. ``absent`` keeps the historical path-key behavior.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="zmem-ns-cache-")
        self.addCleanup(self._tmp.cleanup)
        self.data_dir = Path(self._tmp.name) / "data"
        # Pin BOTH ZMEM_STORE and ZMEM_DATA: _resolve_data_dir() prefers an
        # explicit ZMEM_STORE's parent (PR #199 review F1), so a test run
        # under an ambient ZMEM_STORE (e.g. the frozen C8 check's env) would
        # otherwise write the cache beside that store while assertions read
        # self.data_dir — non-hermetic (crank-proven failure).
        self.patcher = mock.patch.dict(
            os.environ,
            {"ZMEM_STORE": str(self.data_dir / "store.sqlite"),
             "ZMEM_DATA": str(self.data_dir)},
            clear=False,
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _healthy_repo(self, name: str) -> Path:
        repo = Path(self._tmp.name) / name
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
            cwd=str(repo), check=True,
        )
        return repo

    def _break_git(self, repo: Path) -> None:
        """Force `git -C <repo> remote get-url origin` to exit non-zero while
        the directory still looks like a checkout (worktree-style pointer to
        a missing gitdir)."""
        git_path = repo / ".git"
        if git_path.is_dir():
            def _reset_ro(func, path, _exc):
                os.chmod(path, 0o777)
                func(path)

            shutil.rmtree(git_path, onerror=_reset_ro)
        git_path.write_text("gitdir: /nonexistent/CorruptGitDir\n", encoding="utf-8")

    def test_error_uses_cached_remote(self):
        repo = self._healthy_repo("warm")
        self.assertEqual(
            host.resolve_namespace(repo), "project:github.com/example/repo"
        )
        self._break_git(repo)
        status = host._get_git_remote_status(repo)
        self.assertEqual(status[1], "error")
        self.assertIsNone(status[0])
        self.assertEqual(
            host.resolve_namespace(repo), "project:github.com/example/repo"
        )

    def test_error_cache_miss_uses_global(self):
        repo = self._healthy_repo("cold")
        self._break_git(repo)
        result = host.resolve_namespace(repo)
        self.assertEqual(result, "user:global")
        self.assertFalse(result.startswith("project:"))

    def test_absent_uses_path(self):
        repo = Path(self._tmp.name) / "absent"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        self.assertEqual(
            host.resolve_namespace(repo),
            f"project:{host._norm_abspath_key(repo)}",
        )

    def test_corrupt_cache_fails_open(self):
        repo = self._healthy_repo("corrupt")
        self.assertEqual(
            host.resolve_namespace(repo), "project:github.com/example/repo"
        )
        from storelib.namespace_cache import get_cached_namespace

        cache_files = list((self.data_dir / "namespace-cache").glob("*.json"))
        self.assertEqual(len(cache_files), 1)
        cache_files[0].write_bytes(b"\x00not json at all")
        self._break_git(repo)
        # Fail open: corrupt cache behaves like a cold cache, no exception.
        self.assertEqual(
            get_cached_namespace(
                self.data_dir, repo, now=10_000.0, ttl_seconds=3600
            ),
            None,
        )
        self.assertEqual(host.resolve_namespace(repo), "user:global")

    def test_success_refreshes_cache(self):
        from storelib.namespace_cache import (
            NAMESPACE_CACHE_TTL_SECONDS,
            get_cached_namespace,
            put_cached_namespace,
        )

        self.assertEqual(NAMESPACE_CACHE_TTL_SECONDS, 3600)
        repo = self._healthy_repo("refresh")
        self.assertEqual(
            host.resolve_namespace(repo), "project:github.com/example/repo"
        )
        cached = get_cached_namespace(
            self.data_dir, repo, now=10_000.0, ttl_seconds=3600
        )
        self.assertEqual(cached, "project:github.com/example/repo")
        # Direct roundtrip + expiry.
        put_cached_namespace(
            self.data_dir, repo, "project:github.com/other/repo", now=10_000.0
        )
        self.assertEqual(
            get_cached_namespace(
                self.data_dir, repo, now=10_000.0 + 3600, ttl_seconds=3600
            ),
            "project:github.com/other/repo",
        )
        self.assertIsNone(
            get_cached_namespace(
                self.data_dir, repo, now=10_000.0 + 3600 + 0.5, ttl_seconds=3600
            )
        )
        # Re-resolving a healthy checkout refreshes the cached key back.
        self.assertEqual(
            host.resolve_namespace(repo), "project:github.com/example/repo"
        )
        self.assertEqual(
            get_cached_namespace(
                self.data_dir, repo, now=99_999.0, ttl_seconds=3600
            ),
            "project:github.com/example/repo",
        )

    def test_entry_valid_at_exact_ttl(self):
        from storelib.namespace_cache import get_cached_namespace, put_cached_namespace

        repo = Path(self._tmp.name) / "exact"
        repo.mkdir()
        put_cached_namespace(
            self.data_dir, repo, "project:github.com/exact/repo", now=1_000.0
        )
        self.assertEqual(
            get_cached_namespace(
                self.data_dir, repo, now=1_000.0 + 3600, ttl_seconds=3600
            ),
            "project:github.com/exact/repo",
        )

    def test_subdirectory_resolves_remote_key(self):
        """PR #199 review F3: hook capture resolves os.getcwd(), which is
        often a SUBDIRECTORY of the checkout. git walks up to the repo root,
        so a subdir must classify exactly like the root (remote key), never
        fall back to a path key."""
        repo = self._healthy_repo("monorepo")
        sub = repo / "packages" / "app"
        sub.mkdir(parents=True)
        self.assertEqual(
            host.resolve_namespace(sub), "project:github.com/example/repo"
        )
        self.assertEqual(
            host._get_git_remote_status(sub)[1], "remote"
        )

    def test_remote_removed_invalidates_cache(self):
        """PR #199 review F2: after `git remote remove origin` the absent
        resolution must DROP the cached remote key, so a later git error
        resolves to user:global — never the removed origin's identity."""
        repo = self._healthy_repo("removed-origin")
        self.assertEqual(
            host.resolve_namespace(repo), "project:github.com/example/repo"
        )
        subprocess.run(
            ["git", "-C", str(repo), "remote", "remove", "origin"],
            check=True, capture_output=True,
        )
        self.assertEqual(
            host.resolve_namespace(repo),
            f"project:{host._norm_abspath_key(repo)}",
        )
        self._break_git(repo)
        self.assertEqual(host.resolve_namespace(repo), "user:global")

    def test_drop_cached_namespace_forgets_entry(self):
        from storelib.namespace_cache import (
            drop_cached_namespace,
            get_cached_namespace,
            put_cached_namespace,
        )

        repo = Path(self._tmp.name) / "dropme"
        repo.mkdir()
        put_cached_namespace(
            self.data_dir, repo, "project:github.com/drop/repo", now=1.0
        )
        self.assertIsNotNone(
            get_cached_namespace(self.data_dir, repo, now=2.0, ttl_seconds=3600)
        )
        drop_cached_namespace(self.data_dir, repo)
        self.assertIsNone(
            get_cached_namespace(self.data_dir, repo, now=2.0, ttl_seconds=3600)
        )
        # Dropping again (nothing to remove) stays silent.
        drop_cached_namespace(self.data_dir, repo)

    def test_cache_key_normalizes_case_like_the_platform(self):
        """PR #199 review V7: the cache key must run through os.path.normcase,
        so case-insensitive filesystems collapse casing variants into one
        entry while case-sensitive filesystems keep them distinct."""
        from storelib.namespace_cache import _cache_path

        base = Path(self._tmp.name)
        a = base / "Repo"
        b = base / "repo"
        same_on_this_platform = os.path.normcase(str(a)) == os.path.normcase(str(b))
        self.assertEqual(
            _cache_path(self.data_dir, a) == _cache_path(self.data_dir, b),
            same_on_this_platform,
        )

    def test_nonfinite_written_never_serves(self):
        """PR #199 review V8: a NaN/Infinity `written` would never satisfy
        the TTL comparison and must be treated as corrupt (fail open)."""
        import json as _json

        from storelib.namespace_cache import (
            _cache_path,
            get_cached_namespace,
            put_cached_namespace,
        )

        repo = Path(self._tmp.name) / "nan"
        repo.mkdir()
        put_cached_namespace(
            self.data_dir, repo, "project:github.com/nan/repo", now=1.0
        )
        path = _cache_path(self.data_dir, repo)
        for bad in (float("nan"), float("inf")):
            path.write_text(
                _json.dumps({"namespace": "project:github.com/nan/repo",
                             "written": bad}),
                encoding="utf-8",
            )
            self.assertIsNone(
                get_cached_namespace(
                    self.data_dir, repo, now=2.0, ttl_seconds=3600
                )
            )

    def test_unique_tmp_per_write(self):
        """PR #199 review V9: concurrent writers for one checkout must never
        share an in-progress tmp file."""
        import os as _os

        from storelib.namespace_cache import _cache_path, put_cached_namespace

        repo = Path(self._tmp.name) / "tmpnames"
        repo.mkdir()
        seen = []
        real_replace = _os.replace

        def _spy_replace(src, dst, *a, **kw):
            seen.append(_os.path.basename(str(src)))
            return real_replace(src, dst, *a, **kw)

        with mock.patch("os.replace", side_effect=_spy_replace):
            put_cached_namespace(self.data_dir, repo, "project:one", now=1.0)
            put_cached_namespace(self.data_dir, repo, "project:two", now=2.0)
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], seen[1])
        self.assertTrue(
            all(name.endswith(".tmp") for name in seen), seen
        )
        self.assertFalse(_cache_path(self.data_dir, repo).with_name(
            _cache_path(self.data_dir, repo).name + ".tmp").exists())

    def test_resolve_data_dir_honors_zmem_store(self):
        """PR #199 review F1: an explicit ZMEM_STORE keeps sidecars beside
        that store, matching correction_queue's data-dir chain."""
        store_dir = Path(self._tmp.name) / "custom-store-home"
        store_dir.mkdir()
        with mock.patch.dict(
            os.environ,
            {"ZMEM_STORE": str(store_dir / "store.sqlite"),
             "ZMEM_DATA": ""},
            clear=False,
        ):
            os.environ.pop("ZMEM_DATA", None)
            self.assertEqual(host._resolve_data_dir(), store_dir)


if __name__ == "__main__":
    unittest.main()
