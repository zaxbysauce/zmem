"""Regression contract for the closeout follow-up (#529).

The tests deliberately exercise the public hook and store entry points in
isolated temporary directories.  They are expected to be red on the base
commit until the follow-up implementation lands.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
STORE_PY = REPO_ROOT / "skills" / "memory" / "scripts" / "store.py"
REQUEST_SH = REPO_ROOT / "hooks" / "zmem-closeout-request.sh"
GATE_SH = REPO_ROOT / "hooks" / "zmem-closeout-gate.sh"
SESSION_START_SH = REPO_ROOT / "hooks" / "zmem-session-start.sh"
BASH = shutil.which("bash") or r"C:\Program Files\Git\bin\bash.exe"


def _env(tmp: str, sid: str, *, store: str | None = None, data: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    for key in (
        "ZMEM_STORE", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE", "ZMEM_SESSION",
        "ZMEM_REMOTE_URL", "ZMEM_TRANSCRIPT", "CLAUDE_CODE_SESSION_ID",
    ):
        env.pop(key, None)
    env.update({
        "ZMEM_SESSION": sid,
        "ZMEM_NAMESPACE": "project:closeout-followup",
        "ZMEM_DATA": data or tmp,
        "ZMEM_ROOT": str(REPO_ROOT),
        "ZMEM_STORE_PY": str(STORE_PY),
        "ZMEM_CORE_MD": str(Path(tmp) / "fixture-core.md"),
        "ZMEM_SESSION_CADENCE": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
    })
    if store is not None:
        env["ZMEM_STORE"] = store
    return env


def _store(tmp: str, sid: str, *args: str, store: str | None = None,
           data: str | None = None, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(STORE_PY), *args],
        input=input_text,
        text=True,
        capture_output=True,
        env=_env(tmp, sid, store=store, data=data),
        timeout=60,
    )


def _marker(tmp: str, sid: str, suffix: str) -> Path:
    stem = hashlib.sha256(sid.encode("utf8")).hexdigest()[:32]
    return Path(tmp) / "ops" / f"{stem}{suffix}"


def _request(tmp: str, sid: str, prompt: str, *, store: str | None = None,
             data: str | None = None) -> subprocess.CompletedProcess[str]:
    if store is None:
        wrote = _marker(tmp, sid, ".wrote")
        wrote.parent.mkdir(exist_ok=True)
        if not wrote.exists():
            wrote.write_text("1\n", encoding="utf8")
    payload = json.dumps({"session_id": sid, "prompt": prompt})
    return subprocess.run(
        [BASH, str(REQUEST_SH)],
        input=payload,
        text=True,
        capture_output=True,
        env=_env(tmp, sid, store=store, data=data),
        timeout=60,
    )


def _gate(tmp: str, sid: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, str(GATE_SH)], input=json.dumps({"session_id": sid}), text=True,
        capture_output=True, env=_env(tmp, sid), timeout=60,
    )


def _memory_id(tmp: str, sid: str) -> str:
    status = _store(tmp, sid, "closeout-status", "--session-id", sid)
    report = json.loads(status.stdout)
    rows = report.get("unreviewed_rows", [])
    if not rows:
        raise AssertionError(status.stdout)
    return rows[-1]["id"]


class ArmingIntentTest(unittest.TestCase):
    def test_negation_question_and_quoted_trigger_do_not_arm(self) -> None:
        for prompt in (
            "don't closeout yet",
            "what is the closeout skill?",
            'Please review this quoted text: "closeout"',
        ):
            with self.subTest(prompt=prompt), tempfile.TemporaryDirectory() as tmp:
                sid = "intent-sid"
                self.assertEqual(_request(tmp, sid, prompt).returncode, 0)
                self.assertFalse(_marker(tmp, sid, ".closeout-requested").exists())

    def test_real_request_arms_and_repeated_prompt_does_not_replace_arm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sid = "real-sid"
            self.assertEqual(_request(tmp, sid, "Please run the session closeout now").returncode, 0)
            marker = _marker(tmp, sid, ".closeout-requested")
            self.assertTrue(marker.exists())
            marker.write_text("armed\nblocks:1\n", encoding="utf8")
            original = marker.read_bytes()
            self.assertEqual(_request(tmp, sid, "Please run the session closeout now").returncode, 0)
            self.assertEqual(marker.read_bytes(), original)


class NotRequestedReleaseTest(unittest.TestCase):
    def test_release_is_a_terminal_not_requested_disposition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sid = "release-sid"
            self.assertEqual(_request(tmp, sid, "Please closeout this session").returncode, 0)
            self.assertEqual(_gate(tmp, sid).returncode, 2)
            released = _store(tmp, sid, "closeout-release", "--session-id", sid)
            self.assertEqual(released.returncode, 0, released.stderr)
            body = json.loads(released.stdout)
            self.assertEqual(body.get("disposition"), "not_requested")
            status = _store(tmp, sid, "closeout-status", "--session-id", sid)
            report = json.loads(status.stdout)
            self.assertEqual(report.get("request_disposition"), "not_requested")
            self.assertFalse(report.get("ack_valid"))
            self.assertFalse(_marker(tmp, sid, ".closeout").exists())
            self.assertEqual(_gate(tmp, sid).returncode, 0)
            self.assertEqual(_request(tmp, sid, "Please closeout this session").returncode, 0)
            self.assertTrue(_marker(tmp, sid, ".closeout-requested").exists())
            self.assertEqual(_gate(tmp, sid).returncode, 2)

    def test_release_refuses_when_unarmed_or_wrong_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sid = "release-owner"
            self.assertEqual(_store(tmp, sid, "closeout-release", "--session-id", sid).returncode, 2)
            self.assertEqual(_request(tmp, sid, "Please closeout this session").returncode, 0)
            self.assertEqual(_store(tmp, "other-session", "closeout-release", "--session-id", sid).returncode, 2)


class SkipReasonTest(unittest.TestCase):
    def test_short_or_placeholder_reason_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sid = "skip-sid"
            for reason in (".", "n/a", "later"):
                with self.subTest(reason=reason):
                    result = _store(tmp, sid, "closeout-skip", "--session-id", sid,
                                    "--step", "review-queue", "--reason", reason)
                    self.assertEqual(result.returncode, 2)


class ExactSessionMatchTest(unittest.TestCase):
    def test_session_prefixes_do_not_cross_review_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for sid in ("s1", "s1:agent", "s1x", "s1_under%wild"):
                result = _store(tmp, sid, "add", "--namespace", "project:closeout-followup",
                                "--type", "lesson", "--content", f"row-{sid}", "--no-dedup",
                                "--source-ref", f"session:{sid}")
                self.assertEqual(result.returncode, 0, result.stderr)
            status = _store(tmp, "s1", "closeout-status", "--session-id", "s1")
            self.assertEqual(status.returncode, 0, status.stderr)
            report = json.loads(status.stdout)
            self.assertEqual(len(report.get("unreviewed_rows", [])), 2)

    def test_review_accepts_colon_suffix_but_rejects_wildcard_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sid = "s_%"
            for name, source, expected in (
                ("owner", sid, 0),
                ("suffix", f"{sid}:agent", 0),
                ("foreign", "sAx", 2),
            ):
                added = _store(tmp, sid, "add", "--namespace", "project:closeout-followup",
                               "--type", "lesson", "--content", f"row-{name}", "--no-dedup",
                               "--source-ref", f"session:{source}", "--json")
                self.assertEqual(added.returncode, 0, added.stderr)
                memory_id = json.loads(added.stdout)["id"]
                reviewed = _store(tmp, sid, "closeout-review", "--session-id", sid,
                                  "--memory-id", memory_id, "--verdict", "keep")
                self.assertEqual(reviewed.returncode, expected, reviewed.stdout + reviewed.stderr)


class VerdictActsTest(unittest.TestCase):
    def test_invalidate_and_resignal_require_a_real_operation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sid = "verdict-sid"
            added = _store(tmp, sid, "add", "--namespace", "project:closeout-followup",
                           "--type", "lesson", "--content", "live row",
                           "--source-ref", f"session:{sid}")
            memory_id = _memory_id(tmp, sid)
            for verdict in ("invalidate", "resignal"):
                reviewed = _store(tmp, sid, "closeout-review", "--session-id", sid,
                                  "--memory-id", memory_id, "--verdict", verdict)
                self.assertEqual(reviewed.returncode, 2)
                self.assertRegex(reviewed.stdout.lower() + reviewed.stderr.lower(),
                                 r"(invalidate|update)")
            recalled = _store(tmp, sid, "recall", "--namespace", "project:closeout-followup",
                              "--query", "live row", "--min-confidence", "0", "--no-bump", "--json")
            self.assertIn(memory_id, recalled.stdout)


class SessionIdentityTest(unittest.TestCase):
    def test_session_start_surfaces_confirmed_session_and_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sid = "identity-sid"
            payload = json.dumps({"session_id": sid})
            result = subprocess.run(
                [BASH, str(SESSION_START_SH)], input=payload, text=True,
                capture_output=True, env=_env(tmp, sid), timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(f"session_id={sid}", result.stdout)
            self.assertIn("namespace=project:closeout-followup", result.stdout)

    def test_anonymous_fallback_does_not_claim_a_confirmed_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(
                [BASH, str(SESSION_START_SH)], input="{}", text=True,
                capture_output=True, env=_env(tmp, ""), timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("session_id=anonymous-", result.stdout)


class SharedDataDirTest(unittest.TestCase):
    def test_external_store_and_hooks_share_the_store_ops_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as data:
            sid = "external-store-sid"
            store_path = str(Path(tmp) / "nested" / "memory.sqlite")
            Path(store_path).parent.mkdir()
            result = _store(tmp, sid, "add", "--namespace", "project:closeout-followup",
                            "--type", "lesson", "--content", "external marker",
                            "--source-ref", f"session:{sid}", store=store_path, data=data)
            self.assertEqual(result.returncode, 0, result.stderr)
            store_dir = str(Path(store_path).parent)
            self.assertTrue(_marker(store_dir, sid, ".wrote").exists())
            requested = _request(tmp, sid, "Please closeout this session", store=store_path, data=data)
            self.assertEqual(requested.returncode, 0, requested.stderr)
            self.assertTrue(_marker(store_dir, sid, ".closeout-requested").exists())
            self.assertFalse(_marker(data, sid, ".closeout-requested").exists())


class QueueListRedactionTest(unittest.TestCase):
    def test_secret_warning_is_redacted_in_json_output_but_stored_row_survives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sid = "queue-redaction-sid"
            sys.path.insert(0, str(REPO_ROOT / "skills" / "memory" / "scripts"))
            import correction_queue

            queue_dir = Path(tmp) / "queue"
            original = "password=fixture-secret-123"
            item = correction_queue.make_item(
                message=original,
                type_="explicit",
                patterns="password",
                confidence=0.9,
                sentiment="neutral",
                decay_days=30,
                session=sid,
                namespace="project:closeout-followup",
                host="test",
                capture_mode="manual",
            )
            item["secret_warning"] = True
            self.assertTrue(correction_queue.append_queue(
                "project:closeout-followup", item, queue_dir=queue_dir))
            result = _store(tmp, sid, "queue-list", "--namespace", "project:closeout-followup", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            flagged = [entry for entry in payload.get("items", []) if entry.get("secret_warning")]
            self.assertEqual(len(flagged), 1)
            self.assertNotIn("password=fixture-secret-123", flagged[0].get("message", ""))
            self.assertIn("[REDACTED_SECRET]", flagged[0].get("message", ""))
            text = _store(tmp, sid, "queue-list", "--namespace", "project:closeout-followup")
            self.assertEqual(text.returncode, 0, text.stderr)
            self.assertNotIn(original, text.stdout)
            self.assertIn("[REDACTED_SECRET]", text.stdout)
            stored = correction_queue.load_queue("project:closeout-followup", queue_dir=queue_dir)
            self.assertEqual(stored[0]["message"], original)


class GlobalReadbackGuidanceTest(unittest.TestCase):
    """Discriminating and preserving checks for issue #543's skill change."""

    SKILL = REPO_ROOT / "skills" / "closeout" / "SKILL.md"

    def test_global_rows_are_read_back_in_their_own_namespace(self) -> None:
        text = self.SKILL.read_text(encoding="utf8")
        self.assertIn("--namespace user:global", text)
        self.assertIn("own namespace", text.lower())
        self.assertRegex(text.lower(), r"project[- ]first|global[- ]limit")

    def test_project_readback_command_and_merge_explanation_are_preserved(self) -> None:
        text = self.SKILL.read_text(encoding="utf8")
        self.assertIn("recall --query", text)
        self.assertIn("--include-global", text)


if __name__ == "__main__":
    unittest.main()
