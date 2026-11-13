"""Issue #460: `--content -` stdin sentinel must decode UTF-8 explicitly.

Drives the REAL store.py CLI via subprocess with PYTHONIOENCODING pinned to
cp1252 (a default Windows ANSI codepage) so the defect is reproducible on any
host: a bare sys.stdin.read() decodes the piped UTF-8 bytes with the stdio
codec and silently mojibakes non-ASCII content. The `get` output is
json.dumps() with default ensure_ascii=True, so the round-trip assertion is
independent of the child's stdout codec.
"""
from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_PY = os.path.join(REPO_ROOT, "skills", "memory", "scripts", "store.py")
PYTHON = sys.executable
NS = "user:global"
PAYLOAD = "café — naïve"
PAYLOAD_BYTES = PAYLOAD.encode("utf-8")
PAYLOAD_CRLF_BYTES = PAYLOAD.replace(" ", "\r\n", 1).encode("utf-8")


# Ambient env that would reroute or re-scope the child (an operator shell or
# a live host session): ZMEM_REMOTE_URL + ZMEM_STORE is a config error (rc 2),
# and the rest change which store/namespace/session the child resolves.
_AMBIENT_STRIP = (
    "ZMEM_REMOTE_URL", "ZMEM_DATA", "ZMEM_HOME", "ZMEM_NAMESPACE",
    "ZMEM_SESSION", "ZMEM_SESSION_ID", "ZMEM_MCP_URL", "ZMEM_MCP_TOKEN",
    "ZMEM_MCP_TOKENS_FILE", "ZMEM_INJECT",
)


def _pinned_env(tmp: str) -> dict:
    """Store isolation + stdio codec pinned to cp1252 (the defect host)."""
    env = {**os.environ}
    for key in _AMBIENT_STRIP:
        env.pop(key, None)
    env["ZMEM_STORE"] = os.path.join(tmp, "store.sqlite")
    env["ZMEM_MODELS_DIR"] = os.path.join(tmp, "no-such-models")
    env["ZMEM_MODEL_AUTODOWNLOAD"] = "0"
    env["PYTHONIOENCODING"] = "cp1252"
    return env


def _run_bytes(env: dict, *args: str, stdin: bytes | None = None):
    return subprocess.run([PYTHON, STORE_PY, *args], env=env,
                          input=stdin, capture_output=True, timeout=120)


def _added_id(out: bytes) -> str:
    """Prefer the structured `add --json` envelope ({"ok", "id", ...}) over
    scraping the human "added memory <id>" receipt line."""
    text = out.decode("utf-8", "replace")
    brace = text.find("{")
    if brace != -1:
        try:
            envelope = json.loads(text[brace:])
            if envelope.get("id"):
                return str(envelope["id"])
        except ValueError:
            pass  # human-receipt output; fall through to the line scrape
    for line in text.splitlines():
        if "added memory" in line:
            return line.split("memory ")[1].split(" ")[0]
    raise AssertionError(f"no add result: {text[-400:]}")


def _stored_content(env: dict, mid: str) -> str:
    r = _run_bytes(env, "get", "--id", mid)
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")[-400:]
    raw = r.stdout.decode("utf-8", "replace")
    return json.loads(raw[raw.find("{"):])["content"]


class StdinEncodingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zmem-enc-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = _pinned_env(self.tmp)

    def _add_argv(self, content: str) -> str:
        r = _run_bytes(self.env, "add", "--json", "--no-dedup", "--namespace", NS,
                       "--type", "lesson", "--confidence", "0.5",
                       "--source-ref", "probe:encoding",
                       "--content", content)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        return _added_id(r.stdout)

    def test_add_stdin_roundtrip_is_utf8_on_cp1252_host(self):
        # Issue #460 AC1: the `--content -` sentinel must store the exact
        # UTF-8 codepoints regardless of the stdio codec.
        r = _run_bytes(self.env, "add", "--json", "--no-dedup", "--namespace", NS,
                       "--type", "lesson", "--confidence", "0.5",
                       "--source-ref", "probe:encoding",
                       "--content", "-", stdin=PAYLOAD_BYTES)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        mid = _added_id(r.stdout)
        self.assertEqual(_stored_content(self.env, mid), PAYLOAD)

    def test_update_stdin_roundtrip_is_utf8_on_cp1252_host(self):
        # update is append-only (issue #59, 4.2): the new content lands on a
        # NEW live row, so the round-trip reads the id from the --json result.
        mid = self._add_argv("placeholder row")
        r = _run_bytes(self.env, "update", "--id", mid, "--content", "-",
                       "--json", stdin=PAYLOAD_BYTES)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        raw = r.stdout.decode("utf-8", "replace")
        new_mid = json.loads(raw[raw.find("{"):])["id"]
        self.assertEqual(_stored_content(self.env, new_mid), PAYLOAD)

    def test_add_argv_roundtrip_floor_on_cp1252_host(self):
        # Issue #460 AC2: the argv path must keep storing exact codepoints
        # (regression floor for the fix).
        mid = self._add_argv(PAYLOAD)
        self.assertEqual(_stored_content(self.env, mid), PAYLOAD)

    def test_add_stdin_normalizes_crlf_to_lf(self):
        # PR #461 review F-002: the TextIOWrapper read (default newline=None)
        # applies universal newlines — the \r\n a text-mode parent
        # (Hermes/MCP on Windows) emits is translated back to \n (the LF
        # text the parent sent), so the stored length equals the logical
        # length. The argv path stores its argument verbatim.
        r = _run_bytes(self.env, "add", "--json", "--no-dedup", "--namespace", NS,
                       "--type", "lesson", "--confidence", "0.5",
                       "--source-ref", "probe:encoding",
                       "--content", "-", stdin=PAYLOAD_CRLF_BYTES)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        mid = _added_id(r.stdout)
        self.assertEqual(
            _stored_content(self.env, mid),
            PAYLOAD.replace(" ", "\n", 1),
        )

    def test_update_stdin_normalizes_crlf_to_lf(self):
        # The update path shares the sentinel read; pin its newline
        # translation too so a regression on update cannot slip through.
        mid = self._add_argv("placeholder row")
        r = _run_bytes(self.env, "update", "--id", mid, "--content", "-",
                       "--json", stdin=PAYLOAD_CRLF_BYTES)
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        raw = r.stdout.decode("utf-8", "replace")
        new_mid = json.loads(raw[raw.find("{"):])["id"]
        self.assertEqual(
            _stored_content(self.env, new_mid),
            PAYLOAD.replace(" ", "\n", 1),
        )

    def test_add_stdin_near_cap_multiline_from_text_mode_parent(self):
        # PR #461 review F-002: Hermes/MCP spawn store.py with text=True
        # and pipe LF payloads; on Windows the text-mode pipe layer turns
        # every \n into \r\n, so a 63,999-char payload with 1,999 breaks
        # arrives as 65,998 chars — over the 65,536 cap unless the child
        # translates back before the cap check. The add must succeed
        # (rc 0) and store the logical LF text.
        payload = "\n".join(["a" * 31] * 2000)
        self.assertEqual(len(payload), 63999)
        self.assertEqual(payload.count("\n"), 1999)
        r = subprocess.run(
            [PYTHON, STORE_PY, "add", "--json", "--no-dedup", "--namespace", NS,
             "--type", "lesson", "--confidence", "0.5",
             "--source-ref", "probe:encoding", "--content", "-"],
            env=self.env, input=payload, capture_output=True,
            text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])
        mid = _added_id(r.stdout.encode("utf-8"))
        self.assertEqual(_stored_content(self.env, mid), payload)

    def test_add_stdin_invalid_utf8_bytes_stored_as_replacement_chars(self):
        # PR #461 review F-004: pin the disclosed errors="replace" policy —
        # invalid UTF-8 bytes store as U+FFFD with rc 0 (a strict-decode
        # mutant must fail this leg).
        r = _run_bytes(self.env, "add", "--json", "--no-dedup", "--namespace", NS,
                       "--type", "lesson", "--confidence", "0.5",
                       "--source-ref", "probe:encoding",
                       "--content", "-", stdin=b"ab\xffcd")
        self.assertEqual(r.returncode, 0, r.stderr.decode("utf-8", "replace"))
        mid = _added_id(r.stdout)
        self.assertEqual(_stored_content(self.env, mid), "ab\ufffdcd")


if __name__ == "__main__":
    unittest.main()
