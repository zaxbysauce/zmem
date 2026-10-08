"""Feedback-round pins for issue #162 review findings (PR #298 round 5).

Unfrozen companion to tests/test_hermes_prefetch.py — pins the behaviors
added by the swarm-pr-feedback round: already-delivered envelopes are never
cached (PRR-001), pending-key ownership tokens survive a switch-back
(PRR-003), the cache entry cap evicts (PRR-007), oversized rendered text is
refused (PRR-007), the closed key policy logs unknown-key rejections
(PRR-005), and the SDK-import branch of RecallStatus (TI-01).
"""

import contextlib
import copy
import importlib.util
import io
import json
import logging
import os
import sys
import tempfile
import types
import unittest
import atexit
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_SCRATCH = Path(tempfile.mkdtemp(prefix="zmem-162-fb-"))
atexit.register(shutil.rmtree, str(_SCRATCH), ignore_errors=True)
os.environ["ZMEM_STORE"] = str(_SCRATCH / "store.sqlite")
os.environ["ZMEM_DATA"] = str(_SCRATCH)
os.environ["ZMEM_MODELS_DIR"] = str(_SCRATCH / "missing-models")
os.environ["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
os.environ["ZMEM_NAMESPACE"] = "project:fixture"

_FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "hermes"


def _load_fake_executor():
    spec = importlib.util.spec_from_file_location(
        "zmem_fake_executor_162fb", REPO_ROOT / "tests" / "support" /
        "fake_executor.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["zmem_fake_executor_162fb"] = module
    spec.loader.exec_module(module)
    return module


def _load_provider(name, sdk_recall_status=None):
    """Import hermes-plugin/__init__.py with the Hermes ABC stubbed.

    When ``sdk_recall_status`` is given, the stub ALSO exposes RecallStatus
    so the plugin takes the SDK-import branch (review TI-01 — previously
    that branch had zero coverage).  The stub module is REUSED across loads
    (the first registration wins in sys.modules), so sdk_recall_status is
    installed onto the live stub before the plugin executes.
    """
    agent = sys.modules.get("agent")
    mp = sys.modules.get("agent.memory_provider")
    if agent is None or mp is None:
        agent = types.ModuleType("agent")
        mp = types.ModuleType("agent.memory_provider")

        class MemoryProvider:
            pass

        mp.MemoryProvider = MemoryProvider
        agent.memory_provider = mp
        sys.modules["agent"] = agent
        sys.modules["agent.memory_provider"] = mp
    if sdk_recall_status is not None:
        mp.RecallStatus = sdk_recall_status
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "hermes-plugin" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _fixture_envelopes():
    cases = json.loads(
        (_FIXTURE_DIR / "prefetch_cases.json").read_text(encoding="utf-8"))
    return {case["query"]: case["envelope"] for case in cases}


class FakeTransport:
    def __init__(self, envelopes=None, ledger=None):
        self.envelopes = envelopes if envelopes is not None \
            else _fixture_envelopes()
        self.ledger = ledger if ledger is not None else []
        self.calls = []

    def prefetch(self, query, *, namespace, session_id, moment, ops_tokens,
                 lane):
        import copy as _copy
        import hashlib as _hashlib
        self.calls.append({"query": query, "session_id": session_id})
        normalized = " ".join(str(query).split())[:500]
        payload = (b"zmem-prefetch-v1\0" + normalized.encode("utf-8")
                   + b"\0" + namespace.encode("utf-8")
                   + b"\0user_prompt\0hermes-provider")
        fingerprint = _hashlib.sha256(payload).hexdigest()
        row = (session_id, fingerprint)
        if row in self.ledger:
            envelope = self.envelopes["project beta"]
        else:
            self.ledger.append(row)
            envelope = self.envelopes.get(query, self.envelopes["no match"])
        return _copy.deepcopy(envelope)


def _fingerprint(query):
    import hashlib
    normalized = " ".join(query.split())[:500]
    payload = (b"zmem-prefetch-v1\0" + normalized.encode("utf-8")
               + b"\0project:fixture\0user_prompt\0hermes-provider")
    return hashlib.sha256(payload).hexdigest()


def _provider(mod, scheduler, deadline, transport):
    provider = mod.ZmemMemoryProvider(scheduler=scheduler, deadline=deadline,
                                      clock=scheduler.now)
    provider._session_id = "sid-a"
    provider._namespace = "project:fixture"
    provider._transport = transport
    return provider


class AlreadyDeliveredNotCachedTest(unittest.TestCase):
    """PRR-001: an already-delivered envelope (the ledger-race loser) is
    passed through but never cached — the winner's full envelope stays
    served within the TTL, and with no winner the next turn re-queries the
    store, which still enforces the ledger."""

    def test_already_delivered_never_cached(self):
        fake = _load_fake_executor()
        mod = _load_provider("zmem_162_fb_ad")
        scheduler = fake.FakeExecutor()
        deadline = fake.FakeExecutor()
        transport = FakeTransport()
        provider = _provider(mod, scheduler, deadline, transport)
        key = ("sid-a", _fingerprint("project alpha"))
        submitted = []
        original_submit = scheduler.submit

        def recording_submit(fn, delay_s=None):
            submitted.append(fn)
            return original_submit(fn, delay_s=delay_s)

        scheduler.submit = recording_submit

        # Warm wins the ledger and caches the FULL envelope.
        provider.queue_prefetch("project alpha", session_id="sid-a")
        submitted[0]()
        cached = provider._prefetch_cache.get(key)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["count"], 3)

        # Live call for the same key after the entry goes stale: the
        # transport's ledger already holds the row, so it returns the
        # already-delivered envelope.  The passthrough is "" AND the loser
        # is not re-cached over the winner.
        scheduler.advance(30.01)
        rendered = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(rendered, "")
        self.assertIsNone(provider._prefetch_cache.get(key),
                          "the already-delivered loser must not be re-cached")

    def test_already_delivered_with_no_winner_not_cached(self):
        fake = _load_fake_executor()
        mod = _load_provider("zmem_162_fb_ad2")
        scheduler = fake.FakeExecutor()
        deadline = fake.FakeExecutor()
        transport = FakeTransport()
        provider = _provider(mod, scheduler, deadline, transport)
        transport.ledger.append(("sid-a", _fingerprint("project alpha")))
        key = ("sid-a", _fingerprint("project alpha"))

        rendered = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(rendered, "")
        self.assertIsNone(provider._prefetch_cache.get(key),
                          "the already-delivered loser must not be cached")


class OwnershipTokenTest(unittest.TestCase):
    """PRR-003: a switch-back re-registration survives the stale job's
    finally — pending markers carry ownership tokens."""

    def test_stale_job_finally_spares_successor_marker(self):
        fake = _load_fake_executor()
        mod = _load_provider("zmem_162_fb_own")
        scheduler = fake.FakeExecutor()
        deadline = fake.FakeExecutor()
        transport = FakeTransport()
        provider = _provider(mod, scheduler, deadline, transport)

        submitted = []
        original_submit = scheduler.submit

        def recording_submit(fn, delay_s=None):
            submitted.append(fn)
            return original_submit(fn, delay_s=delay_s)

        scheduler.submit = recording_submit
        provider.queue_prefetch("project alpha", session_id="sid-a")
        job1 = submitted[0]
        # Switch away and back: the switch clears sid-a's marker, the
        # re-queue re-adds it with a NEW token, and only then does job1
        # finish.  Job1's finally must remove only ITS token.
        provider.on_session_switch("sid-b")
        provider.queue_prefetch("project alpha", session_id="sid-b")
        provider.on_session_switch("sid-a")
        provider.queue_prefetch("project alpha", session_id="sid-a")
        key = ("sid-a", _fingerprint("project alpha"))
        marker_before = provider._pending_keys.get(key)
        self.assertIsNotNone(marker_before)
        job1()  # stale job unwinds
        self.assertEqual(provider._pending_keys.get(key), marker_before,
                         "the successor's marker must survive job1's finally")


class CacheCapTest(unittest.TestCase):
    """PRR-007: the cache evicts past the entry cap and refuses oversized
    rendered text."""

    def test_entry_cap_evicts_oldest(self):
        mod = _load_provider("zmem_162_fb_cap")
        cache = mod._PrefetchCache(ttl_s=30.0, clock=lambda: 0.0)
        for index in range(mod._PREFETCH_CACHE_MAX_ENTRIES + 4):
            cache.put((f"sid-{index}", "f"), {"rendered": str(index)})
        self.assertEqual(len(cache._entries),
                         mod._PREFETCH_CACHE_MAX_ENTRIES)
        # The four oldest entries were evicted.
        self.assertIsNone(cache.get(("sid-0", "f")))
        self.assertIsNotNone(
            cache.get((f"sid-{mod._PREFETCH_CACHE_MAX_ENTRIES + 3}", "f")))

    def test_mcp_context_alias_stripped_and_accepted(self):
        # The repo's own MCP server adds an additive `context` alias
        # duplicating `rendered`; the closed-key check must not reject it.
        fake = _load_fake_executor()
        mod = _load_provider("zmem_162_fb_alias")
        scheduler = fake.FakeExecutor()
        deadline = fake.FakeExecutor()
        transport = FakeTransport()
        provider = _provider(mod, scheduler, deadline, transport)
        aliased = dict(_fixture_envelopes()["project alpha"])
        aliased["context"] = aliased["rendered"]
        transport.envelopes["project alpha"] = aliased
        rendered = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(
            rendered, _fixture_envelopes()["project alpha"]["rendered"])
        cached = provider._prefetch_cache.get(
            ("sid-a", _fingerprint("project alpha")))
        self.assertIsNotNone(cached)
        self.assertNotIn("context", cached)

    def test_oversized_rendered_refused(self):
        fake = _load_fake_executor()
        mod = _load_provider("zmem_162_fb_big")
        scheduler = fake.FakeExecutor()
        deadline = fake.FakeExecutor()
        transport = FakeTransport()
        provider = _provider(mod, scheduler, deadline, transport)
        big = _fixture_envelopes()["project alpha"]
        big["rendered"] = "x" * (mod._PREFETCH_RENDERED_MAX_CHARS + 1)
        transport.envelopes["project alpha"] = big
        rendered = provider.prefetch("project alpha", session_id="sid-a")
        self.assertEqual(rendered, "")  # refused, not cached, not injected
        self.assertIsNone(provider._prefetch_cache.get(
            ("sid-a", _fingerprint("project alpha"))))


class SdkRecallStatusBranchTest(unittest.TestCase):
    """TI-01: exercise the SDK-import branch with a host-shaped stub."""

    def test_sdk_recall_status_branch_used_when_present(self):
        class HostRecallStatus:
            def __init__(self, provider_label="zmem", count=0, glyph="*"):
                self.provider_label = provider_label
                self.count = count
                self.glyph = glyph

        fake = _load_fake_executor()
        mod = _load_provider("zmem_162_fb_sdk",
                             sdk_recall_status=HostRecallStatus)
        self.assertIs(mod.RecallStatus, HostRecallStatus)
        transport = FakeTransport()
        scheduler = fake.FakeExecutor()
        deadline = fake.FakeExecutor()
        provider = _provider(mod, scheduler, deadline, transport)
        status = provider.recall_status()
        self.assertIsInstance(status, HostRecallStatus)
        rendered = provider.prefetch("project alpha", session_id="sid-a")
        self.assertNotEqual(rendered, "")
        self.assertEqual(provider.recall_status().count, 3)


class KeyPolicyDegradationLogTest(unittest.TestCase):
    """PRR-005: the constants-fallback and unknown-key rejection are
    diagnosable (warn-once / debug) instead of silent."""

    def test_unknown_key_logged_and_rejected(self):
        fake = _load_fake_executor()
        provider_mod = _load_provider("zmem_162_fb_keylog")
        envelope = dict(_fixture_envelopes()["project alpha"])
        envelope["mystery_future_key"] = 1
        scheduler = fake.FakeExecutor()
        deadline = fake.FakeExecutor()
        provider = _provider(provider_mod, scheduler, deadline,
                             FakeTransport())
        handler = logging.NullHandler()
        old_level = provider_mod.logger.level
        provider_mod.logger.addHandler(handler)
        provider_mod.logger.setLevel(logging.DEBUG)
        try:
            # assertLogs REPLACES the logger's handlers, so capture through
            # its own output attribute rather than the stream handler.
            with self.assertLogs(provider_mod.logger, "DEBUG") as captured:
                valid = provider._envelope_is_valid(envelope)
        finally:
            provider_mod.logger.removeHandler(handler)
            provider_mod.logger.setLevel(old_level)
        self.assertFalse(valid)
        self.assertIn("mystery_future_key", "".join(captured.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
