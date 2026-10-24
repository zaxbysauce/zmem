"""Published host evidence must not retain raw recall memory contents."""
import json
from pathlib import Path
import tempfile
import unittest

REDACTED = "[REDACTED: raw recall output removed because it contained live credentials]"


def unsafe_recall_paths(root):
    """Return filenames only; never include raw evidence in assertion output."""
    unsafe = []
    for path in sorted(root.rglob("*-launcher-recall.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            safe = isinstance(payload, dict) and payload.get("stdout_raw") == REDACTED
        except (ValueError, OSError):
            safe = False
        if not safe:
            unsafe.append(path.relative_to(root).as_posix())
    return unsafe


class HostEvidenceRedactionTests(unittest.TestCase):
    def test_published_recall_outputs_are_redacted(self):
        root = Path(__file__).resolve().parents[1] / "docs" / "host-gate-evidence"
        self.assertTrue(root.is_dir())
        for harness in ("claude", "codex", "zcode"):
            self.assertFalse((root / "287-ac7" / f"{harness}-launcher-recall.json").exists())
        self.assertEqual(unsafe_recall_paths(root), [])

    def test_new_nested_raw_or_malformed_recall_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "future-host-gate"
            nested.mkdir()
            path = nested / "new-launcher-recall.json"
            for content in (json.dumps({"stdout_raw": "synthetic private recall"}), "{", "{}"):
                path.write_text(content, encoding="utf-8")
                self.assertEqual(unsafe_recall_paths(root), ["future-host-gate/new-launcher-recall.json"])
            path.write_text(json.dumps({"stdout_raw": REDACTED}), encoding="utf-8")
            self.assertEqual(unsafe_recall_paths(root), [])


if __name__ == "__main__":
    unittest.main()
