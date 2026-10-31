"""SUPPLEMENT to AC2 (#414-2): the PRODUCER side of the snapshot expiry
family (trace 414-416 supplement).

The frozen C2 drives the CONSUMER (``fleet/snapshot.py::
_build_from_payload``) with hand-built payloads; this supplement drives the
REAL producer — ``hermes-plugin/server/mcp_server.py::
_build_snapshot_payload`` — against a seeded storelib store:

- (a) payload shape: ``payload["expiry"]`` carries EXACTLY the two state
  rows' ``[memory_id, inject_until, expiry_class]`` triplets (a live one
  and an already-expired one) — NOT the fact row's record (facts cannot
  carry one) nor a hand-planted ORPHAN record for an unshipped id;
- (b) end-to-end: the ACTUAL payload dict (plus the env keys the broker
  needs) flows through ``_build_from_payload`` + ``_serve_offline_read`` —
  the expired state row withheld, the live one served;
- (c) cap boundary: a ``max_bytes`` fitting rows+revocations but NOT the
  expiry family is refused with ValueError; a fitting cap returns the
  payload with the family intact.

The producer module is loaded via importlib with its own directory on
sys.path (server startup is ``__main__``-guarded). DISCLOSED STUBBING: the
module's sibling ``auth.py`` imports ``mcp.server.auth.provider`` at module
scope; when the real ``mcp`` SDK is absent the loader installs a minimal
``sys.modules`` stub for exactly that chain (two class-shaped names) and
retries — the helper under test is stdlib-only and never touches it.

Isolation follows the frozen C2 exactly.  Standalone:
``python -X utf8 tests/test_snapshot_expiry_parity_supp.py``
"""

from __future__ import annotations
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
SERVER_DIR = REPO_ROOT / "hermes-plugin" / "server"

LIVE_MARKER = "api-supp-live.example.com"
DEAD_MARKER = "api-supp-dead.example.com"
PAST = "2026-01-01T00:00:00Z"
FUTURE = "2099-01-01T00:00:00Z"

# -- env pinned BEFORE any fleet/storelib/mcp_server import (and before the
# issue-#244 store isolation guard loads, which reads env at import time).
_TMP = tempfile.mkdtemp(prefix="zmem-c2s-prod-")
os.environ.update({
    "ZMEM_HOME": str(REPO_ROOT),
    "ZMEM_STORE": os.path.join(_TMP, "store.sqlite"),
    "ZMEM_DATA": _TMP,
    "ZMEM_MODELS_DIR": os.path.join(_TMP, "missing-models"),
    "ZMEM_MODEL_AUTODOWNLOAD": "0",
    "PYTHONIOENCODING": "utf-8",
    "ZMEM_NAMESPACE": "user:global",
    "ZMEM_MCP_TOKEN": "HUB_TOKEN",
})
for _k in ("ZMEM_BACKUP_DIR", "ZMEM_HOME_EXTRA", "CLAUDE_PLUGIN_DATA",
           "ZCODE_PLUGIN_DATA", "ZMEM_MCP_TOKENS_FILE"):
    os.environ.pop(_k, None)
import _store_guard  # noqa: E402,F401  (issue #244 isolation guard)

_SAVED_MODULES = {key: value for key, value in list(sys.modules.items())
                  if key in ("fleet", "storelib", "host")
                  or key.startswith(("fleet.", "storelib.", "host."))}
for _name in list(_SAVED_MODULES):
    del sys.modules[_name]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
    _PATH_INSERTED = True
else:
    _PATH_INSERTED = False
_PINNED_KEYS = ("ZMEM_HOME", "ZMEM_STORE", "ZMEM_DATA", "ZMEM_MODELS_DIR",
                "ZMEM_MODEL_AUTODOWNLOAD", "PYTHONIOENCODING",
                "ZMEM_NAMESPACE", "ZMEM_MCP_TOKEN", "ZMEM_BACKUP_DIR",
                "ZMEM_HOME_EXTRA", "CLAUDE_PLUGIN_DATA",
                "ZCODE_PLUGIN_DATA", "ZMEM_MCP_TOKENS_FILE")
_SAVED_ENV = {key: os.environ.get(key) for key in _PINNED_KEYS}

import storelib  # noqa: E402
from storelib import write as writelib  # noqa: E402
import fleet.snapshot as snap  # noqa: E402


def _load_producer():
    """importlib-load mcp_server.py (its dir on sys.path; stub mcp only if
    the real SDK is absent — see the module docstring's disclosure)."""
    import types
    saved_path = list(sys.path)
    stubs: list[str] = []
    try:
        sys.path.insert(0, str(SERVER_DIR))

        def _exec():
            spec = importlib.util.spec_from_file_location(
                "zmem_mcp_server_supp", SERVER_DIR / "mcp_server.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            return mod

        try:
            return _exec(), stubs
        except ImportError as first:
            if not (getattr(first, "name", "") or "").startswith("mcp"):
                raise
            # DISCLOSED STUB: only the module-level chain auth.py needs.
            root, server = types.ModuleType("mcp"), types.ModuleType(
                "mcp.server")
            auth_pkg = types.ModuleType("mcp.server.auth")
            provider = types.ModuleType("mcp.server.auth.provider")
            provider.AccessToken = object
            provider.TokenVerifier = object
            root.server, auth_pkg.provider = server, provider
            for name, module in (("mcp", root), ("mcp.server", server),
                                 ("mcp.server.auth", auth_pkg),
                                 ("mcp.server.auth.provider", provider)):
                sys.modules.setdefault(name, module)
                stubs.append(name)
            return _exec(), stubs
    finally:
        sys.path[:] = saved_path


MCP_SERVER, _MCP_STUBS = _load_producer()


def tearDownModule():  # noqa: N802 - unittest hook
    """Restore the ambient env / module cache (pytest co-run citizenship)."""
    for key in [key for key in list(sys.modules)
                if key in ("fleet", "storelib", "host",
                           "zmem_mcp_server_supp")
                or key.startswith(("fleet.", "storelib.", "host."))]:
        del sys.modules[key]
    sys.modules.update(_SAVED_MODULES)
    for name in (_MCP_STUBS or []):
        sys.modules.pop(name, None)
    if _PATH_INSERTED:
        try:
            sys.path.remove(str(SCRIPTS_DIR))
        except ValueError:  # pragma: no cover - defensive
            pass
    for key, value in _SAVED_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    shutil.rmtree(_TMP, ignore_errors=True)


def _quiet(fn, *args, **kwargs):
    buf_out, buf_err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        return fn(*args, **kwargs)


class ProducerExpiryFamilyTest(unittest.TestCase):
    """_build_snapshot_payload emits the additive expiry family, exactly."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="zmem-c2s-store-")
        os.environ["ZMEM_STORE"] = os.path.join(cls.tmp, "store.sqlite")
        os.environ["ZMEM_DATA"] = cls.tmp
        storelib._refresh_env_state()
        cls.conn = storelib.connect()
        storelib.init_db(cls.conn)
        storelib.migrate(cls.conn)
        cls.live_id = str(_quiet(
            writelib.add_memory, cls.conn, namespace="user:global",
            type_="state",
            content=f"supp live state: endpoint {LIVE_MARKER} "
                    f"rate limit is 42 rps",
            tags="c2s", source_ref="c2s", signal="test",
            expiry_class="endpoint", inject_until=FUTURE))
        cls.dead_id = str(_quiet(
            writelib.add_memory, cls.conn, namespace="user:global",
            type_="state",
            content=f"supp dead state: endpoint {DEAD_MARKER} "
                    f"rate limit is 43 rps",
            tags="c2s", source_ref="c2s", signal="test",
            expiry_class="setting", inject_until=PAST))
        cls.fact_id = str(_quiet(
            writelib.add_memory, cls.conn, namespace="user:global",
            type_="fact", content="supp plain fact row c2s",
            tags="c2s", source_ref="c2s", signal="test"))
        # A hand-planted ORPHAN side record for a memory id the payload will
        # never ship (FKs are inert by schema design) — the producer must
        # not emit it (never-orphans rule).
        cls.orphan_id = uuid.uuid4().hex
        cls.conn.execute(
            "INSERT INTO memory_expiry (memory_id, inject_until, "
            "expiry_class) VALUES (?, ?, ?)",
            (cls.orphan_id, FUTURE, "endpoint"))
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _build(self, **overrides):
        kwargs = dict(scoped=False, namespaces=None, principal_id="supp")
        kwargs.update(overrides)
        return MCP_SERVER._build_snapshot_payload(self.conn, **kwargs)

    def test_leg_a_expiry_family_exactly_the_two_state_triplets(self):
        payload = self._build()
        expiry = payload.get("expiry")
        self.assertIsInstance(expiry, list, "leg a: no expiry family")
        by_id = {trip[0]: trip for trip in expiry}
        self.assertEqual(
            sorted(by_id), sorted([self.live_id, self.dead_id]),
            f"leg a: the family must carry EXACTLY the two state-row "
            f"triplets (got ids {sorted(by_id)}; fact row "
            f"{self.fact_id[:8]}… and orphan {self.orphan_id[:8]}… must "
            f"be absent)")
        self.assertEqual(by_id[self.live_id],
                         [self.live_id, FUTURE, "endpoint"],
                         "leg a: live triplet must round-trip id/"
                         "inject_until/expiry_class verbatim")
        self.assertEqual(by_id[self.dead_id],
                         [self.dead_id, PAST, "setting"],
                         "leg a: the already-EXPIRED state row's triplet "
                         "must ship too (the consumer withholds it)")

    def _restore(self, payload):
        tmp = tempfile.mkdtemp(prefix="zmem-c2s-f5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ)
        env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
        env["ZMEM_DATA"] = tmp
        snap._build_from_payload(payload, env)

    def test_leg_c_restore_refuses_malformed_families(self):
        """F5 (second review): the broker restore REFUSES malformed
        families (ValueError -> the refresh's designed degrade) instead of
        silently arming the expiry gate on a non-state row. Control: the
        valid family restores; each mutation refuses."""
        payload = self._build()
        good_expiry = list(payload.get("expiry"))
        self.assertTrue(good_expiry, "leg c precondition: family non-empty")
        fact_triplet = [self.fact_id, FUTURE, "endpoint"]

        def with_expiry(value):
            mutated = dict(payload)
            mutated["expiry"] = value
            return mutated

        self._restore(payload)  # control: valid family restores

        cases = [
            ("length-4 triplet", with_expiry(
                good_expiry + [fact_triplet + ["extra"]])),
            ("non-list family", with_expiry("not-a-list")),
            ("unshipped id", with_expiry(
                good_expiry + [["0" * 32, FUTURE, "endpoint"]])),
            ("non-state parent", with_expiry(
                good_expiry + [list(fact_triplet)])),
            ("malformed inject_until", with_expiry(
                good_expiry + [[self.live_id, "not-a-timestamp",
                                "endpoint"]])),
            ("unknown expiry_class", with_expiry(
                good_expiry + [[self.live_id, FUTURE, "not-a-class"]])),
        ]
        for name, mutated in cases:
            with self.assertRaises(ValueError, msg=f"leg c ({name})"):
                self._restore(mutated)

    def test_leg_b_end_to_end_withhold_expired_serve_live(self):
        payload = self._build()
        tmp = tempfile.mkdtemp(prefix="zmem-c2s-leg2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        env = dict(os.environ)
        env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
        env["ZMEM_DATA"] = tmp
        snap._build_from_payload(payload, env)
        envelope, state = snap._serve_offline_read(
            env, query="rate limit", namespace="user:global",
            moment="user_prompt", session_id="c2s-supp", lane=None)
        self.assertEqual(state, "valid",
                         "leg b: the materialized snapshot must be valid")
        contents = [str(r.get("content", ""))
                    for r in (envelope or {}).get("results", [])]
        self.assertTrue(any(LIVE_MARKER in c for c in contents),
                        f"leg b: the LIVE state row must be served "
                        f"(results: {contents!r})")
        self.assertFalse(any(DEAD_MARKER in c for c in contents),
                         f"leg b: the EXPIRED state row (inject_until in "
                         f"the past, record shipped in payload['expiry']) "
                         f"leaked into the offline serve "
                         f"(results: {contents!r})")

    def test_leg_c_cap_refuses_when_only_the_family_overflows(self):
        full = self._build()
        expiry_bytes = sum(len(str(v)) for trip in full["expiry"]
                           for v in trip)
        self.assertGreater(
            expiry_bytes, 0,
            "leg c precondition: the producer must ship a non-empty "
            "expiry family for the cap boundary to mean anything (an "
            "always-empty family makes the expiry cap check dead code)")
        # rows+revocations fit, rows+revocations+expiry do not (the checks
        # are `> max_bytes`, so an exact-fit cap passes the first two).
        cap_without_family = full["payload_bytes"] - expiry_bytes
        refused = None
        try:
            self._build(max_bytes=cap_without_family)
        except ValueError as exc:
            refused = str(exc)
        self.assertIsNotNone(
            refused,
            "leg c: a cap that fits rows+revocations but NOT the expiry "
            "family was accepted — the byte accounting must cover the "
            "family")
        self.assertIn("cap", refused.lower(),
                      f"leg c: refusal must name the cap: {refused!r}")
        control = self._build(max_bytes=full["payload_bytes"])
        self.assertEqual(
            len(control["expiry"]), 2,
            "leg c control: a cap that fits everything must return the "
            "payload with the expiry family intact")


if __name__ == "__main__":
    unittest.main(verbosity=2)
