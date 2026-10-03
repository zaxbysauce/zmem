# Hermes C6 offline fixture binding

`tests/fixtures/source/repro/checks.py C6` requires an already prepared
provider lane. The test never installs a package and must run without network
access.

1. Materialize Hermes source revision `00373b537616c96e0ca604b831890a113df07ac0`
   in a disposable CI workspace.
2. Use the test interpreter's isolated environment with
   `ruamel.yaml==0.18.16` already installed.
3. Set `ZMEM_TEST_HERMES_PROVIDER_ROOT` to that source root and run exactly:

   ```text
   python tests/fixtures/source/repro/checks.py C6 --repo-root .
   ```

   This is a required dedicated CI job, called `test-source-hermes` in the
   implementation plan. It uses Python 3.14.5 and must install only
   `ruamel.yaml==0.18.16` during job setup, before the command. The command
   itself never downloads or installs a provider or package. It verifies
   `hermes_state.py` SHA-256 is
   `3f0995ca2bc122a16454d2ae59455806289695636b55b6ac4b64acf1cd577469`.
   It also verifies the 198-module SessionDB runtime closure and `pyproject`
   hashes in `hermes-provider-manifest.json`, including the holders, message,
   session, and WAL modules. A changed helper cannot silently replace the
   pinned provider.

## CI routing contract

The generic `test` job in `.github/workflows/ci.yml` runs every
`tests/test_*.py` file on Python 3.11. `tests/test_source_acceptance.py` is
therefore intentionally C1 through C5 only. It has no C6 skip: C6 is executed
by a separate required `test-source-hermes` job.

That job must perform these steps in order:

1. Check out this repository and separately check out
   `NousResearch/hermes-agent` at
   `00373b537616c96e0ca604b831890a113df07ac0` into an owned CI path.
2. Set up Python `3.14.5`; install `ruamel.yaml==0.18.16` in that job's
   isolated environment.
3. Set `ZMEM_TEST_HERMES_PROVIDER_ROOT` to the separately checked-out source
   and run the exact C6 command above with that same interpreter.

The job must not use `continue-on-error`, a conditional skip, or a fallback to
JSONL. The C6 command validates the provider revision, the 198-module runtime
closure, `pyproject.toml`, and the installed `ruamel.yaml` version before it
creates its disposable native fixture. The provider checkout and dependency
setup are CI setup, rather than runtime behavior of the test or resolver.

The test builds a temporary canonical `state.db` through the real
`hermes_state.SessionDB` API, then keeps a SQLite WAL writer open during the
resolver call. It supplies a distinct JSONL export as bait. A passing resolver
returns the native message anchor and null byte offsets, never the bait. It
also refuses an import failure and a WAL database with missing sidecars before
those cases can create `-wal` or `-shm` files. A separately held `BEGIN
EXCLUSIVE` lock produces an actual provider open failure; that path also
refuses without using the export bait.

The selected logical read-only contract permits a changed, already existing
`state.db-shm` reader-coordination hash. `state.db`, `state.db-wal`, and the
source file set remain byte-identical. This fixture does not claim that a
preflight stat makes concurrent host replacement race-free; a live host may
change source state after preflight, so resolver failures in that interval must
remain visible refusals.
