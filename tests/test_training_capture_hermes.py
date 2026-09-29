"""Hermes lifecycle callbacks feed only the bounded partial-capture adapter."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def _plugin_context():
    plugins = types.ModuleType("plugins")
    memory = types.ModuleType("plugins.memory")
    memory._get_active_memory_provider = lambda: "zmem"
    plugins.memory = memory
    agent = types.ModuleType("agent")
    provider_api = types.ModuleType("agent.memory_provider")
    provider_api.MemoryProvider = type("MemoryProvider", (), {})
    agent.memory_provider = provider_api
    modules = {
        "plugins": plugins,
        "plugins.memory": memory,
        "agent": agent,
        "agent.memory_provider": provider_api,
    }
    with mock.patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            f"training_capture_hermes_{uuid.uuid4().hex}",
            ROOT / "hermes-plugin" / "__init__.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


class HermesTrainingCaptureTests(unittest.TestCase):
    def test_sync_turn_starts_governed_partial_without_asserting_outcome(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            calls = []

            def capture(action, payload):
                calls.append((action, payload))
                return {"capture_id": "capture-hermes", "state": "partial"}

            with mock.patch.object(plugin, "_run_training_capture", side_effect=capture), \
                 mock.patch.object(plugin, "_background_training_capture") as snapshot:
                # No host identity means the first-epoch callback can only
                # produce an uncorrelated standalone partial.
                provider.on_turn_start(17, "prompt text")
                self.assertIsNone(provider.sync_turn(
                    "prompt text", "assistant text",
                ))

            self.assertEqual([action for action, _ in calls], ["start_standalone"])
            self.assertNotIn("acknowledge", [action for action, _ in calls])
            self.assertNotIn("complete", [action for action, _ in calls])
            action, payload = calls[0]
            self.assertEqual(action, "start_standalone")
            self.assertEqual(payload["host"], "hermes")
            self.assertEqual(payload["session_id"], "session-hermes")
            self.assertTrue(payload["capture_key"])
            self.assertNotEqual(payload["capture_key"], "17")
            self.assertEqual(payload["prompt"], "prompt text")
            self.assertEqual(payload["assistant_response"], "assistant text")
            self.assertNotIn("host_task_id", payload)
            snapshot.assert_not_called()

    def test_post_tool_callback_is_observation_only_and_preserves_empty_response(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            calls = []
            with mock.patch.object(plugin, "_background_training_capture",
                                   side_effect=lambda action, payload: calls.append((action, payload))), \
                 mock.patch.object(plugin, "_enqueue_native_evidence"):
                self.assertEqual(provider.post_tool_call(
                    session_id="session-hermes", task_id="task-hermes",
                    turn_id="turn-hermes",
                    tool_name="read_file", result="ok",
                ), {})

            self.assertEqual(len(calls), 1)
            action, payload = calls[0]
            self.assertEqual(action, "observe")
            self.assertEqual(payload["observation_kind"], "post_tool_call")
            self.assertEqual(payload["host_task_id"], "task-hermes")
            self.assertEqual(payload["capture_key"], "turn-hermes")
            self.assertEqual(payload["observation"]["result"], "ok")

    def test_prefetch_waits_for_sync_turn_before_snapshot(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            calls = []
            envelope = {
                "rendered": "<<<ZMEM_UNTRUSTED_FENCE>>>context<<<END>>>",
                "effective_ops": ["zmem_search"],
                "transform_version": "v2",
            }
            provider._transport = mock.Mock()
            provider._transport.prefetch.return_value = envelope
            with mock.patch.dict(os.environ, {"ZMEM_QUERY_CONTEXT": "0", "ZMEM_INJECT": "1"}, clear=False), \
                 mock.patch.object(plugin, "_background_training_capture",
                                   side_effect=lambda action, payload: calls.append((action, payload)) or {}):
                provider.on_turn_start(1, "prompt", session_id="session-hermes")
                self.assertEqual(provider.prefetch("prompt", session_id="session-hermes"),
                                 envelope["rendered"])
                self.assertEqual(calls, [])
                with mock.patch.object(
                    plugin, "_run_training_capture",
                    return_value={"capture_id": "capture-hermes", "state": "partial"},
                ) as start:
                    provider.sync_turn("prompt", "response", session_id="session-hermes")

            self.assertEqual(len(calls), 1)
            action, payload = calls[0]
            self.assertEqual(action, "snapshot")
            self.assertEqual(payload["rendered"], envelope["rendered"])
            self.assertEqual(payload["effective_ops"], envelope["effective_ops"])
            self.assertEqual(payload["transform_version"], "v2")
            self.assertEqual(payload["capture_key"], start.call_args.args[1]["capture_key"])

    def test_official_lifecycle_rejects_ambiguous_repeated_prompt(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            provider._transport = mock.Mock()
            provider._transport.prefetch.return_value = {
                "rendered": "context", "effective_ops": [], "transform_version": "v1",
            }
            calls = []
            with mock.patch.object(plugin, "_run_training_capture",
                                   return_value={"capture_id": "capture-hermes"}) as start, \
                 mock.patch.object(plugin, "_background_training_capture",
                                   side_effect=lambda action, payload: calls.append((action, payload))):
                provider.on_turn_start(1, "repeat", session_id="session-hermes")
                provider.on_turn_start(2, "repeat", session_id="session-hermes")
                provider.prefetch("repeat", session_id="session-hermes")
                provider.sync_turn("repeat", "response", session_id="session-hermes")

            self.assertEqual(calls, [])
            self.assertEqual(start.call_count, 1)
            self.assertEqual(start.call_args.args[0], "start_standalone")
            self.assertTrue(start.call_args.args[1]["capture_key"])
            self.assertEqual(start.call_args.args[1]["session_id"], "session-hermes")

    def test_out_of_order_distinct_turns_keep_their_opaque_keys(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            provider._transport = mock.Mock()
            provider._transport.prefetch.side_effect = [
                {"rendered": "context-a", "effective_ops": [], "transform_version": "v1"},
                {"rendered": "context-b", "effective_ops": [], "transform_version": "v1"},
            ]
            calls = []
            with mock.patch.object(plugin, "_run_training_capture",
                                   return_value={"capture_id": "capture-hermes"}) as start, \
                 mock.patch.object(plugin, "_background_training_capture",
                                   side_effect=lambda action, payload: calls.append((action, payload))):
                provider.on_turn_start(1, "first", session_id="session-hermes")
                provider.on_turn_start(2, "second", session_id="session-hermes")
                provider.prefetch("first", session_id="session-hermes")
                provider.prefetch("second", session_id="session-hermes")
                provider.sync_turn("second", "response-2", session_id="session-hermes")
                provider.sync_turn("first", "response-1", session_id="session-hermes")

            self.assertEqual([action for action, _ in calls], ["snapshot", "snapshot"])
            self.assertNotEqual(calls[0][1]["capture_key"], calls[1][1]["capture_key"])
            self.assertEqual([payload["rendered"] for _, payload in calls],
                             ["context-b", "context-a"])

    def test_mismatch_expiry_and_session_rotation_fail_closed(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            provider._transport = mock.Mock()
            provider._transport.prefetch.return_value = {
                "rendered": "context", "effective_ops": [], "transform_version": "v1",
            }
            calls = []
            with mock.patch.object(plugin, "_run_training_capture",
                                   return_value={"capture_id": "capture-hermes"}) as start, \
                 mock.patch.object(plugin, "_background_training_capture",
                                   side_effect=lambda action, payload: calls.append((action, payload))):
                provider.on_turn_start(1, "right prompt", session_id="session-hermes")
                provider.prefetch("wrong prompt", session_id="session-hermes")
                provider.sync_turn("right prompt", "response", session_id="session-hermes")
                provider.on_turn_start(2, "expired prompt", session_id="session-hermes")
                provider._turn_tickets[0]["expires_at"] = 0
                provider.sync_turn("expired prompt", "response", session_id="session-hermes")
                provider.on_turn_start(3, "rotated prompt", session_id="session-hermes")
                provider.on_session_switch("new-session")
                provider.sync_turn("rotated prompt", "response", session_id="new-session")

            self.assertEqual(calls, [])
            self.assertEqual(
                [call.args[0] for call in start.call_args_list],
                ["start", "start_standalone", "start_standalone"],
            )
            self.assertTrue(start.call_args_list[0].args[1]["capture_key"])
            self.assertEqual(
                start.call_args_list[1].args[1]["session_id"], "session-hermes",
            )
            self.assertEqual(
                start.call_args_list[2].args[1]["session_id"], "new-session",
            )

    def test_explicit_stale_session_is_rejected_after_atomic_rotation(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider.initialize("session-old")
            old_epoch = provider._turn_epoch
            provider.on_session_switch("session-new")

            provider.on_turn_start(
                1, "stale callback", session_id="session-old",
            )
            self.assertEqual(provider._turn_tickets, [])

            provider.on_turn_start(
                2, "current callback", session_id="session-new",
            )
            self.assertEqual(len(provider._turn_tickets), 1)
            self.assertEqual(provider._turn_tickets[0]["epoch"], old_epoch + 1)
            self.assertEqual(provider._turn_tickets[0]["session_id"], "session-new")

    def test_delayed_prefetch_cannot_attach_after_same_session_rotation(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider.initialize("session-same")
            provider._transport = mock.Mock()
            entered = threading.Event()
            release = threading.Event()

            def delayed_prefetch(*_args, **_kwargs):
                entered.set()
                self.assertTrue(release.wait(2.0))
                return {
                    "rendered": "delayed context",
                    "effective_ops": [],
                    "transform_version": "v1",
                }

            provider._transport.prefetch.side_effect = delayed_prefetch
            provider.on_turn_start(1, "same prompt", session_id="session-same")
            result: dict[str, str] = {}
            worker = threading.Thread(
                target=lambda: result.setdefault(
                    "rendered",
                    provider.prefetch("same prompt", session_id="session-same"),
                ),
            )
            worker.start()
            self.assertTrue(entered.wait(2.0))
            provider.on_session_switch("session-same")
            release.set()
            worker.join(2.0)

            self.assertFalse(worker.is_alive())
            self.assertEqual(result["rendered"], "delayed context")
            self.assertEqual(provider._turn_tickets, [])

    def test_live_duplicate_tombstone_blocks_delayed_and_later_callbacks(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider.initialize("session-duplicate")
            provider._transport = mock.Mock()
            entered = threading.Event()
            release = threading.Event()
            provider._transport.prefetch.side_effect = lambda *_args, **_kwargs: (
                entered.set(),
                release.wait(2.0),
                {
                    "rendered": "late duplicate context",
                    "effective_ops": [],
                    "transform_version": "v1",
                },
            )[-1]
            starts = []
            snapshots = []
            with mock.patch.object(
                plugin, "_run_training_capture",
                side_effect=lambda action, payload: starts.append((action, payload))
                or {"capture_id": "capture-duplicate", "state": "partial"},
            ), mock.patch.object(
                plugin, "_background_training_capture",
                side_effect=lambda action, payload: snapshots.append((action, payload)),
            ):
                provider.on_turn_start(
                    1, "repeat prompt", session_id="session-duplicate",
                )
                worker = threading.Thread(
                    target=provider.prefetch,
                    args=("repeat prompt",),
                    kwargs={"session_id": "session-duplicate"},
                )
                worker.start()
                self.assertTrue(entered.wait(2.0))

                provider.on_turn_start(
                    2, "repeat prompt", session_id="session-duplicate",
                )
                provider.sync_turn(
                    "repeat prompt", "response", session_id="session-duplicate",
                )
                release.set()
                worker.join(2.0)
                provider.on_turn_start(
                    3, "repeat prompt", session_id="session-duplicate",
                )
                provider.sync_turn(
                    "repeat prompt", "later response", session_id="session-duplicate",
                )

            self.assertFalse(worker.is_alive())
            self.assertEqual(
                [action for action, _ in starts],
                ["start_standalone", "start_standalone"],
            )
            self.assertEqual(snapshots, [])
            self.assertEqual(len(provider._turn_tombstones), 1)

    def test_omitted_session_after_rotation_is_blocked_for_provider_lifetime(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider.initialize("session-old")
            provider.on_session_switch("session-new")
            starts = []
            provider._transport = mock.Mock()
            provider._transport.prefetch.return_value = {
                "rendered": "must not fetch",
                "effective_ops": [],
                "transform_version": "v1",
            }
            with mock.patch.object(
                plugin, "_run_training_capture",
                side_effect=lambda action, payload: starts.append((action, payload))
                or {"capture_id": "capture-standalone", "state": "partial"},
            ), mock.patch.object(
                plugin, "_background_training_capture",
            ) as snapshot:
                provider.on_turn_start(1, "prompt")
                self.assertEqual(provider._turn_tickets, [])
                self.assertEqual(provider.prefetch("prompt"), "")
                provider.sync_turn("prompt", "response")

            self.assertEqual(starts, [])
            provider._transport.prefetch.assert_not_called()
            snapshot.assert_not_called()

            provider.initialize("session-new")
            with mock.patch.object(plugin, "_run_training_capture") as start:
                provider.sync_turn("prompt", "response")
            start.assert_not_called()

    def test_first_epoch_omitted_sync_cannot_claim_new_explicit_ticket(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider.initialize("session-first")
            provider._transport = mock.Mock()
            provider._transport.prefetch.return_value = {
                "rendered": "first epoch context",
                "effective_ops": [],
                "transform_version": "v1",
            }
            starts = []
            snapshots = []
            with mock.patch.object(
                plugin, "_run_training_capture",
                side_effect=lambda action, payload: starts.append((action, payload))
                or {"capture_id": "capture-explicit", "state": "partial"},
            ), mock.patch.object(
                plugin, "_background_training_capture",
                side_effect=lambda action, payload: snapshots.append((action, payload)),
            ):
                provider.on_turn_start(1, "prompt", session_id="session-first")
                provider.sync_turn("prompt", "omitted response")
                self.assertEqual(
                    [action for action, _ in starts], ["start_standalone"],
                )
                self.assertEqual(len(provider._turn_tickets), 1)
                provider.prefetch("prompt", session_id="session-first")
                provider.sync_turn(
                    "prompt", "explicit response", session_id="session-first",
                )

            self.assertEqual([action for action, _ in starts], [
                "start_standalone", "start",
            ])
            self.assertEqual([action for action, _ in snapshots], ["snapshot"])
            self.assertEqual(len(provider._turn_tickets), 0)

    def test_omitted_same_prompt_after_rotation_cannot_consume_explicit_ticket(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider.initialize("session-old")
            provider.on_session_switch("session-new")
            provider._transport = mock.Mock()
            provider._transport.prefetch.return_value = {
                "rendered": "current context",
                "effective_ops": [],
                "transform_version": "v1",
            }
            starts = []
            snapshots = []
            with mock.patch.object(
                plugin, "_run_training_capture",
                side_effect=lambda action, payload: starts.append((action, payload))
                or {"capture_id": "capture-current", "state": "partial"},
            ), mock.patch.object(
                plugin, "_background_training_capture",
                side_effect=lambda action, payload: snapshots.append((action, payload)),
            ):
                provider.on_turn_start(
                    1, "same prompt", session_id="session-new",
                )
                provider.sync_turn("same prompt", "delayed old response")
                self.assertEqual(starts, [])
                self.assertEqual(len(provider._turn_tickets), 1)

                self.assertEqual(provider.prefetch("same prompt"), "")
                provider.prefetch("same prompt", session_id="session-new")
                provider.sync_turn(
                    "same prompt", "current response", session_id="session-new",
                )

            self.assertEqual([action for action, _ in starts], ["start"])
            self.assertEqual([action for action, _ in snapshots], ["snapshot"])
            provider._transport.prefetch.assert_called_once()

    def test_explicit_no_ticket_partial_and_stale_explicit_rejection_after_rotation(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider.initialize("session-old")
            provider.on_session_switch("session-new")
            starts = []
            with mock.patch.object(
                plugin, "_run_training_capture",
                side_effect=lambda action, payload: starts.append((action, payload))
                or {"capture_id": "capture-explicit", "state": "partial"},
            ):
                provider.sync_turn(
                    "no ticket", "current response", session_id="session-new",
                )
                provider.sync_turn(
                    "stale prompt", "stale response", session_id="session-old",
                )

            self.assertEqual([action for action, _ in starts], ["start_standalone"])
            self.assertEqual(starts[0][1]["session_id"], "session-new")

    def test_fresh_provider_instance_resets_omitted_callback_block(self):
        with _plugin_context() as plugin:
            blocked = plugin.ZmemMemoryProvider()
            blocked.initialize("session-old")
            blocked.on_session_switch("session-new")
            self.assertTrue(blocked._omitted_turn_callbacks_blocked)

            fresh = plugin.ZmemMemoryProvider()
            fresh.initialize("session-fresh")
            starts = []
            with mock.patch.object(
                plugin, "_run_training_capture",
                side_effect=lambda action, payload: starts.append((action, payload))
                or {"capture_id": "capture-fresh", "state": "partial"},
            ):
                fresh.sync_turn("fresh prompt", "fresh response")

            self.assertFalse(fresh._omitted_turn_callbacks_blocked)
            self.assertEqual([action for action, _ in starts], ["start_standalone"])

    def test_standalone_without_active_session_fails_closed(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            with mock.patch.object(plugin, "_run_training_capture") as start:
                provider.sync_turn("prompt", "response")
                provider.sync_turn(
                    "prompt", "response", session_id="stale-without-active",
                )
            start.assert_not_called()

    def test_sequential_duplicate_after_claim_gets_a_distinct_capture_key(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider.initialize("session-sequential")
            starts = []
            with mock.patch.object(
                plugin, "_run_training_capture",
                side_effect=lambda action, payload: starts.append((action, payload))
                or {"capture_id": f"capture-{len(starts)}", "state": "partial"},
            ):
                provider.on_turn_start(
                    1, "sequential prompt", session_id="session-sequential",
                )
                provider.sync_turn(
                    "sequential prompt", "first response",
                    session_id="session-sequential",
                )
                provider.on_turn_start(
                    2, "sequential prompt", session_id="session-sequential",
                )
                provider.sync_turn(
                    "sequential prompt", "second response",
                    session_id="session-sequential",
                )

            self.assertEqual([action for action, _ in starts], ["start", "start"])
            self.assertNotEqual(starts[0][1]["capture_key"], starts[1][1]["capture_key"])
            self.assertEqual(provider._turn_tombstones, set())

    def test_tombstone_capacity_blocks_epoch_and_rotation_resets_it(self):
        with _plugin_context() as plugin, mock.patch.object(
            plugin, "_HERMES_TURN_TOMBSTONE_MAX", 2,
        ):
            provider = plugin.ZmemMemoryProvider()
            provider.initialize("session-cap")
            for number, prompt in enumerate(("one", "two", "three"), start=1):
                provider.on_turn_start(
                    number, prompt, session_id="session-cap",
                )
                provider.on_turn_start(
                    number + 10, prompt, session_id="session-cap",
                )

            self.assertTrue(provider._turn_correlation_blocked)
            self.assertEqual(len(provider._turn_tombstones), 2)
            self.assertEqual(provider._turn_tickets, [])
            provider.on_turn_start(99, "after overflow", session_id="session-cap")
            self.assertEqual(provider._turn_tickets, [])

            old_epoch = provider._turn_epoch
            provider.on_session_switch("session-cap")
            self.assertEqual(provider._turn_epoch, old_epoch + 1)
            self.assertFalse(provider._turn_correlation_blocked)
            self.assertEqual(provider._turn_tombstones, set())
            provider.on_turn_start(100, "after overflow", session_id="session-cap")
            self.assertEqual(len(provider._turn_tickets), 1)

    def test_official_lifecycle_links_snapshot_after_31_second_turn(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes-long"
            provider._namespace = "project:hermes-hook"
            provider._transport = mock.Mock()
            provider._transport.prefetch.return_value = {
                "rendered": "long-turn context",
                "effective_ops": ["zmem_search"],
                "transform_version": "v1",
            }
            clock = [100.0]
            calls = []
            with mock.patch.object(plugin.time, "monotonic", side_effect=lambda: clock[0]), \
                 mock.patch.object(
                     plugin, "_run_training_capture",
                     return_value={"capture_id": "capture-hermes"},
                 ), mock.patch.object(
                     plugin, "_background_training_capture",
                     side_effect=lambda action, payload: calls.append((action, payload)),
                 ):
                provider.on_turn_start(
                    1, "long-turn prompt", session_id="session-hermes-long",
                )
                provider.prefetch("long-turn prompt", session_id="session-hermes-long")
                clock[0] += 31.0
                provider.sync_turn(
                    "long-turn prompt", "long-turn response",
                    session_id="session-hermes-long",
                )

            self.assertEqual([action for action, _ in calls], ["snapshot"])
            self.assertEqual(calls[0][1]["rendered"], "long-turn context")

    def test_turn_ticket_expires_after_bounded_ttl(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes-expiry"
            provider._namespace = "project:hermes-hook"
            clock = [100.0]
            with mock.patch.object(plugin.time, "monotonic", side_effect=lambda: clock[0]):
                provider.on_turn_start(
                    1, "expired after ttl", session_id="session-hermes-expiry",
                )
                clock[0] += plugin._HERMES_TURN_TICKET_TTL_S + 1.0
                self.assertIsNone(
                    provider._claim_turn_ticket(
                        "session-hermes-expiry", "expired after ttl",
                    )
                )
            self.assertEqual(provider._turn_tickets, [])

    def test_ticket_retains_only_redacted_bounded_envelope(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            provider._namespace = "project:hermes-hook"
            provider._transport = mock.Mock()
            raw = "Bearer sk-test-12345678901234567890 user@example.com"
            provider._transport.prefetch.return_value = {
                "rendered": raw, "effective_ops": [raw], "transform_version": "v1",
            }
            calls = []
            with mock.patch.object(plugin, "_run_training_capture",
                                   return_value={"capture_id": "capture-hermes"}), \
                 mock.patch.object(plugin, "_background_training_capture",
                                   side_effect=lambda action, payload: calls.append((action, payload))):
                prompt = "private prompt should not be retained"
                provider.on_turn_start(1, prompt, session_id="session-hermes")
                self.assertNotIn(prompt, repr(provider._turn_tickets))
                provider.prefetch(prompt, session_id="session-hermes")
                retained = provider._turn_tickets[0]["envelope"]
                provider.sync_turn(prompt, "response", session_id="session-hermes")

            self.assertNotIn("sk-test-12345678901234567890", str(retained))
            self.assertNotIn("user@example.com", str(retained))
            self.assertEqual(calls[0][1]["rendered"], "[REDACTED_SECRET] [REDACTED_EMAIL]")
            self.assertEqual(calls[0][1]["effective_ops"],
                             ["[REDACTED_SECRET] [REDACTED_EMAIL]"])

    def test_redactor_uses_resolved_home_without_sys_path_pollution(self):
        with _plugin_context() as plugin, tempfile.TemporaryDirectory(
            prefix="zmem-hermes-redactor-"
        ) as temporary:
            scripts = Path(temporary) / "skills" / "memory" / "scripts"
            scripts.mkdir(parents=True)
            (scripts / "redaction.py").write_text(
                "def redact_training_text(value):\n"
                "    return '[HOME_REDACTED]', 1\n",
                encoding="utf-8",
            )
            before_path = list(sys.path)
            before_training_capture = sys.modules.get("storelib.training_capture")
            with mock.patch.dict(os.environ, {"ZMEM_HOME": temporary}, clear=False):
                redact = plugin._hermes_capture_redactor()
                self.assertIsNotNone(redact)
                self.assertEqual(redact("caller value")[0], "[HOME_REDACTED]")
            self.assertEqual(sys.path, before_path)
            self.assertIs(
                sys.modules.get("storelib.training_capture"),
                before_training_capture,
            )

    def test_hermes_redactor_matches_canonical_training_helper(self):
        redaction_spec = importlib.util.spec_from_file_location(
            f"training_redaction_test_{uuid.uuid4().hex}",
            ROOT / "skills" / "memory" / "scripts" / "redaction.py",
        )
        assert redaction_spec is not None and redaction_spec.loader is not None
        canonical = importlib.util.module_from_spec(redaction_spec)
        redaction_spec.loader.exec_module(canonical)
        samples = (
            "Bearer sk-test-12345678901234567890 user@example.com",
            r"path C:\workspace\private.txt and /tmp/private.txt",
            "plain training context",
        )
        with _plugin_context() as plugin:
            redactor = plugin._hermes_capture_redactor()
            self.assertIsNotNone(redactor)
            self.assertEqual(
                [redactor(value) for value in samples],
                [canonical.redact_training_text(value) for value in samples],
            )

    def test_oversized_prompt_cannot_collide_with_a_truncated_digest(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            oversized = "x" * (plugin._MAX_PROMPT_CHARS + 1)
            provider.on_turn_start(1, oversized)
            self.assertEqual(provider._turn_tickets, [])

    def test_oversized_snapshot_fields_fail_closed_without_snapshot(self):
        with _plugin_context() as plugin:
            def oversized(limit: int) -> str:
                return "x " * (limit // 2 + 1)

            oversized_envelopes = (
                {
                    "rendered": oversized(plugin._HERMES_TICKET_RENDERED_MAX_BYTES),
                    "effective_ops": [],
                    "transform_version": "v1",
                },
                {
                    "rendered": "context",
                    "effective_ops": [
                        oversized(plugin._HERMES_TICKET_VERSION_MAX_BYTES)
                    ],
                    "transform_version": "v1",
                },
                {
                    "rendered": "context",
                    "effective_ops": [],
                    "transform_version": oversized(plugin._HERMES_TICKET_VERSION_MAX_BYTES),
                },
            )
            for index, envelope in enumerate(oversized_envelopes):
                provider = plugin.ZmemMemoryProvider()
                provider._session_id = f"session-hermes-{index}"
                provider._namespace = "project:hermes-hook"
                provider._transport = mock.Mock()
                provider._transport.prefetch.return_value = envelope
                calls = []
                with mock.patch.object(
                    plugin, "_run_training_capture",
                    return_value={"capture_id": "capture-hermes"},
                ), mock.patch.object(
                    plugin, "_background_training_capture",
                    side_effect=lambda action, payload: calls.append((action, payload)),
                ):
                    provider.on_turn_start(
                        1, "prompt", session_id=provider._session_id,
                    )
                    provider.prefetch("prompt", session_id=provider._session_id)
                    provider.sync_turn("prompt", "response", session_id=provider._session_id)
                self.assertEqual(calls, [], f"field case {index} unexpectedly snapshotted")

    def test_reinitialize_same_session_clears_stale_turn_ticket(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "same-session"
            provider._mode = None
            provider.on_turn_start(1, "stale prompt", session_id="same-session")
            self.assertEqual(len(provider._turn_tickets), 1)
            provider.initialize("same-session")
            self.assertEqual(provider._turn_tickets, [])

    def test_official_lifecycle_links_one_snapshot_through_real_adapter(self):
        with _plugin_context() as plugin, tempfile.TemporaryDirectory(
            prefix="zmem-hermes-ticket-"
        ) as temporary:
            root = Path(temporary)
            store = root / "store.sqlite"
            env = {
                "ZMEM_HOME": str(ROOT),
                "ZMEM_STORE": str(store),
                "ZMEM_DATA": str(root / "data"),
                "ZMEM_CAPTURE": "1",
                "ZMEM_EMBED_PROFILE": "fake",
                "ZMEM_MODEL_AUTODOWNLOAD": "0",
                "ZMEM_MODELS_DIR": str(root / "models"),
                "PYTHONUTF8": "1",
                "ZMEM_CAPTURE_CONSENT_SCOPE": "local-training",
                "ZMEM_CAPTURE_CONTENT_LICENSE": "CC-BY-4.0",
                "ZMEM_CAPTURE_REDACTION_POLICY_VERSION": "policy-v1",
            }
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes-real"
            provider._namespace = "project:hermes-real"
            provider._transport = mock.Mock()
            provider._transport.prefetch.return_value = {
                "rendered": "real adapter context",
                "effective_ops": ["zmem_search"],
                "transform_version": "v1",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                provider.on_turn_start(
                    1, "real adapter prompt", session_id="session-hermes-real",
                )
                provider.prefetch("real adapter prompt", session_id="session-hermes-real")
                provider.sync_turn(
                    "real adapter prompt", "real adapter response",
                    session_id="session-hermes-real",
                )
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    if store.exists():
                        conn = sqlite3.connect(store)
                        try:
                            try:
                                count = conn.execute(
                                    "SELECT count(*) FROM training_delivery_snapshot"
                                ).fetchone()[0]
                            except sqlite3.OperationalError:
                                count = 0
                        finally:
                            conn.close()
                        if count:
                            break
                    time.sleep(0.05)
                time.sleep(0.2)

            conn = sqlite3.connect(store)
            try:
                rows = conn.execute(
                    "SELECT c.capture_id, s.rendered "
                    "FROM training_capture AS c "
                    "JOIN training_delivery_snapshot AS s "
                    "ON s.capture_id = c.capture_id"
                ).fetchall()
            finally:
                conn.close()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], "real adapter context")

    def test_capture_marker_is_scoped_to_the_store_child(self):
        with _plugin_context() as plugin:
            completed = types.SimpleNamespace(returncode=0, stdout="{}", stderr="")
            with mock.patch.object(plugin, "_resolve_store_py", return_value=Path("store.py")), \
                 mock.patch.object(plugin, "_python_bin", return_value="python"), \
                 mock.patch.object(plugin.subprocess, "run", return_value=completed) as run:
                plugin._run_passive_store(["recent"], capture=True)
                child_env = run.call_args.kwargs["env"]
                self.assertEqual(child_env["ZMEM_CAPTURE"], "1")
                self.assertNotIn("ZMEM_CAPTURE", os.environ)

    def test_sync_and_background_capture_fail_open_without_worker_traceback(self):
        with _plugin_context() as plugin:
            provider = plugin.ZmemMemoryProvider()
            provider._session_id = "session-hermes"
            with mock.patch.object(plugin, "_run_training_capture",
                                   side_effect=RuntimeError("capture exploded")):
                self.assertIsNone(provider.sync_turn("prompt", "response"))
                class InlineThread:
                    def __init__(self, *, target, **_kwargs):
                        self.target = target

                    def start(self):
                        self.target()

                with mock.patch.object(plugin.threading, "Thread", InlineThread):
                    plugin._background_training_capture("observe", {"session_id": "session-hermes"})


if __name__ == "__main__":
    unittest.main()
