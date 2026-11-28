"""Guard request identity across interrupted closeout releases."""

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills" / "memory" / "scripts"))
import closeout_request as request


class RequestIdentityTest(unittest.TestCase):
    session_id = "closeout-request-identity"

    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        env = patch.dict(os.environ, {
            "ZMEM_STORE": str(Path(scratch.name) / "store.sqlite"),
            "ZMEM_SESSION": self.session_id,
        })
        env.start()
        self.addCleanup(env.stop)
        self.paths = request.ops_paths(self.session_id)
        self.paths["ops"].mkdir()
        self.paths["wrote"].write_text("1\n", encoding="utf-8")

    def test_interrupted_release_cannot_block_or_clear_a_later_arm(self):
        self.assertTrue(request.arm_request(self.session_id, "Please run the closeout"))
        previous = request.read_request(self.session_id)
        self.assertIsNotNone(previous)
        original_unlink = Path.unlink

        def interrupt_release(path, *args, **kwargs):
            if path == self.paths["requested"]:
                raise OSError("interrupted after recording the disposition")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", interrupt_release):
            with self.assertRaisesRegex(OSError, "interrupted"):
                request.release(self.session_id)

        self.assertTrue(self.paths["requested"].exists())
        self.assertIsNone(request.read_request(self.session_id))
        self.assertEqual(request.disposition(self.session_id), "not_requested")
        self.assertTrue(request.arm_request(self.session_id, "Please run the closeout"))
        current = request.read_request(self.session_id)
        self.assertIsNotNone(current)
        self.assertNotEqual(current.nonce, previous.nonce)
        self.assertIsNone(request.increment_blocks(self.session_id, previous.nonce))
        self.assertFalse(request.clear_if_matches(self.session_id, previous.nonce))
        self.assertEqual(request.read_request(self.session_id), current)
        self.assertIsNone(request.disposition(self.session_id))

    def test_ordinary_prompts_keep_the_gate_inert_and_explicit_requests_arm(self):
        for prompt in (
            "What's the closeout skill?",
            "Don't run a closeout yet",
            "> closeout",
            "```text\ncloseout\n```",
            "Closeout status explains the remaining steps",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(request.arm_request(self.session_id, prompt))
                self.assertIsNone(request.read_request(self.session_id))

        for prompt in ("closeout", "Could you run a closeout?", "wrap-up", "end of session"):
            with self.subTest(prompt=prompt):
                self.assertTrue(request.arm_request(self.session_id, prompt))
                active = request.read_request(self.session_id)
                self.assertIsNotNone(active)
                self.assertTrue(request.clear_if_matches(self.session_id, active.nonce))


if __name__ == "__main__":
    unittest.main()
