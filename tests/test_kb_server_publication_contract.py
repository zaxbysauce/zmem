"""Arm's-length acceptance checks: the kb-http PUBLICATION SERVER
contract, pinned in-process (issue trace 480-481-482, Fix 7, rebuild r2).

Published selectors:
  - CreateOnlyTest.test_existing_locator_answers_409_bytes_untouched
  - LocatorSafetyTest.test_traversal_segment_refused_before_fs_effect

REFERENCE EVIDENCE (retained, not closure): issue #482's deployed-server
ACs (AC4-AC6) are SPLIT OUT of this PR and stay open on the issue — this
suite is the zmem-side conformance reference (real client + reference
contract server).  The deployed fleet KB server does NOT conform today
(accepts non-hex locators); that work is tracked on the issue.

The reference server (the _MockKB idiom from
tests/test_knowledge_documents.py:519-616, extended to serve a REAL
tempdir vault via http.server on 127.0.0.1:0) implements the full wire
contract both zmem's KBClient and a conforming fleet KB server share:

  POST {endpoint}/{locator} {"mode":"create","content":...}
    - grammar gate FIRST (the exact documents.py locator grammar — see
      LocatorSafetyTest) -> 4xx BEFORE any filesystem effect
    - valid NEW locator     -> 201, bytes land at <vault>/<locator>
    - existing locator      -> 409 create-only, bytes UNTOUCHED (even
                               identical content; the CLIENT recovers via
                               GET-compare); under CONCURRENT creates of
                               different content exactly one 201/one 409
                               and the stored bytes match the winner
    - auth: write-scope token required (read-scope -> 403, missing -> 401)
  GET  -> 200 {"content": ...} | 404 | 4xx grammar refusal (read scope OK)

ALSO drives the REAL KBClient end-to-end: create -> 201 -> GET readback
digest match; existing identical -> existed_match; differing bytes at
the same locator -> ConflictError.

Run: python -X utf8 tests/test_kb_server_publication_contract.py
"""

from __future__ import annotations
import os
import sys
import tempfile

# Scratch env pinned BEFORE the isolation guard and any knowledge import.
_TMP = tempfile.mkdtemp(prefix="zmem-kbpub-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
import _store_guard  # noqa: F401,E402  (issue #244 isolation guard)

import http.server
import json
import shutil
import subprocess
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from knowledge.config import KBHTTPTarget  # noqa: E402
from knowledge.documents import (  # noqa: E402
    ConflictError,
    KBClient,
    parse_locator,
    prepare_document,
)

WRITE_TOKEN = "kb-write-scope-token-0123456789abcdef"
READ_TOKEN = "kb-read-scope-token-fedcba9876543210"
_PREFIX = "/api/notes/"
# Grammar-valid digest segment: exactly four 16-hex groups joined by '-'.
_SEG = "aaaa1111aaaa2222-bbbb3333bbbb4444-cccc5555cccc6666-dddd7777dddd8888"


class _VaultKBHandler(http.server.BaseHTTPRequestHandler):
    """The conforming reference publication server: create-only,
    grammar-gated, scoped-bearer-auth'd, backed by a real tempdir vault.
    State lives on the BASE class (the _MockKB idiom)."""

    vault: Path | None = None
    # locator -> threading.Barrier(2) for the concurrent-create probe.
    barrier_locators: dict[str, threading.Barrier] = {}

    def log_message(self, *args):  # silence
        pass

    @classmethod
    def reset(cls, vault: Path) -> None:
        _VaultKBHandler.vault = vault
        _VaultKBHandler.barrier_locators = {}

    # -- helpers ---------------------------------------------------------

    def _scope(self) -> str | None:
        """The bearer token's scope: "write", "read", or None."""
        header = self.headers.get("Authorization") or ""
        if header == "Bearer " + WRITE_TOKEN:
            return "write"
        if header == "Bearer " + READ_TOKEN:
            return "read"
        if header.startswith("Bearer "):
            return "unknown"
        return None

    def _locator(self) -> str:
        return urllib.parse.unquote(self.path[len(_PREFIX):])

    def _vault_path(self, locator: str) -> Path:
        return _VaultKBHandler.vault / locator

    def _reply(self, status: int, obj: dict | None = None) -> None:
        payload = b"" if obj is None else json.dumps(obj).encode("utf-8")
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)

    def _grammar_gate(self) -> str | None:
        """Refuse ANY locator outside <logical-key>/<4x16hex>.md BEFORE
        any filesystem effect, and refuse publication THROUGH a
        symlink/junction inside the vault (containment: the resolved
        final path must stay under the resolved vault root, and no path
        component may be a link)."""
        locator = self._locator()
        try:
            parse_locator(locator)
        except ValueError:
            self._reply(400)
            return None
        final = self._vault_path(locator)
        vault_real = os.path.realpath(str(_VaultKBHandler.vault))
        probe = _VaultKBHandler.vault
        for part in Path(locator).parts:
            probe = probe / part
            if probe.is_symlink():
                self._reply(400)
                return None
        if os.path.realpath(str(final)) != vault_real and \
                not os.path.realpath(str(final)).startswith(
                    vault_real.rstrip("\\/") + os.sep):
            self._reply(400)
            return None
        return locator

    # -- routes ----------------------------------------------------------

    def do_POST(self):  # noqa: N802 (http.server naming)
        scope = self._scope()
        if scope is None:
            self._reply(401)
            return
        if scope != "write":
            self._reply(403)   # read scope (or unknown token) may not write
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._reply(400)
            return
        if (not isinstance(body, dict) or body.get("mode") != "create"
                or not isinstance(body.get("content"), str)):
            self._reply(400)
            return
        locator = self._grammar_gate()
        if locator is None:
            return  # refused before any fs effect
        barrier = _VaultKBHandler.barrier_locators.get(locator)
        if barrier is not None:
            try:                # concurrent-create probe: arrive together
                barrier.wait(timeout=10)
            except threading.BrokenBarrierError:
                pass
        final = self._vault_path(locator)
        # ATOMIC create-only: O_CREAT|O_EXCL — under concurrency exactly
        # one writer wins; a loser sees FileExistsError -> 409, and the
        # winner's bytes are never partially visible.
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(final), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            self._reply(409)    # create-only: never overwrite
            return
        with os.fdopen(fd, "wb") as handle:
            handle.write(body["content"].encode("utf-8"))
        self._reply(201, {"locator": locator})

    def do_GET(self):  # noqa: N802
        scope = self._scope()
        if scope is None:
            self._reply(401)
            return
        locator = self._grammar_gate()
        if locator is None:
            return
        final = self._vault_path(locator)
        if not final.exists():
            self._reply(404)
            return
        self._reply(200, {"content": final.read_bytes().decode("utf-8")})


class _VaultKBServer:
    def __init__(self, vault: Path) -> None:
        _VaultKBHandler.reset(vault)
        self.httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _VaultKBHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        return "http://127.0.0.1:%d/api/notes" % self.httpd.server_address[1]

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _client(server: _VaultKBServer) -> KBClient:
    return KBClient(KBHTTPTarget(
        id="fleet", endpoint=server.endpoint, allow_http=True,
        credential_env="ZMEM_KB_TEST_TOKEN", timeout_s=10.0,
        max_response_bytes=65536),
        env={"ZMEM_KB_TEST_TOKEN": WRITE_TOKEN})


def _raw(server: _VaultKBServer, method: str, locator: str,
         body: dict | None = None, token: str = WRITE_TOKEN):
    url = server.endpoint + "/" + urllib.parse.quote(locator, safe="/")
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header("Authorization", "Bearer " + token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _files_under(root: Path) -> list[str]:
    """Snapshot EVERY filesystem entry — files, DIRECTORIES and LINKS —
    so an empty-directory or symlink effect can never pass a
    before-any-effect snapshot."""
    entries = []
    for p in root.rglob("*"):
        rel = str(p.relative_to(root)).replace("\\", "/")
        if p.is_symlink():
            entries.append(rel + " ->link")
        elif p.is_dir():
            entries.append(rel + "/")
        else:
            entries.append(rel)
    return sorted(entries)


class _ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="zmem-kbpubcase-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.parent), True)
        self.vault = self.parent / "vault"
        self.vault.mkdir()
        self.server = _VaultKBServer(self.vault)
        self.addCleanup(self.server.close)


class CreateOnlyTest(_ServerTestCase):
    """REFERENCE evidence for issue #482 (deployed-server ACs stay open)."""

    def test_existing_locator_answers_409_bytes_untouched(self):
        client = _client(self.server)

        # End-to-end create through the REAL client: 201 + GET readback
        # digest match (verified inside the client).
        doc = prepare_document("fleet", "guides/deploy", "first body\n")
        created = client.create(doc)
        self.assertFalse(created.existed_match)
        final = self.vault / doc.locator
        self.assertTrue(final.is_file())
        self.assertEqual(final.read_bytes(), doc.content)

        # Existing IDENTICAL locator: 409 (create-only) -> the client
        # recovers via GET-compare -> existed_match; bytes never rewritten.
        stat_before = final.stat()
        again = client.create(doc)
        self.assertTrue(again.existed_match)
        self.assertEqual(final.read_bytes(), doc.content)
        self.assertEqual(final.stat().st_mtime_ns, stat_before.st_mtime_ns)

        # Raw POST with DIFFERENT content: 409, stored bytes untouched.
        status, _ = _raw(self.server, "POST", doc.locator,
                         {"mode": "create", "content": "tampered\n"})
        self.assertEqual(status, 409)
        self.assertEqual(final.read_bytes(), doc.content)
        self.assertEqual(final.stat().st_mtime_ns, stat_before.st_mtime_ns)

        # Raw POST with IDENTICAL content: STILL 409 (create-only server).
        status, _ = _raw(self.server, "POST", doc.locator,
                         {"mode": "create", "content": "first body\n"})
        self.assertEqual(status, 409)
        self.assertEqual(final.read_bytes(), doc.content)

        # Differing bytes at a document's own (content-derived) locator:
        # the client raises ConflictError; the foreign bytes survive.
        honest = prepare_document("fleet", "guides/occupied",
                                  "honest body\n")
        foreign_bytes = b"foreign tampered bytes\n"
        status, _ = _raw(self.server, "POST", honest.locator,
                         {"mode": "create",
                          "content": foreign_bytes.decode("utf-8")})
        self.assertEqual(status, 201)
        with self.assertRaises(ConflictError):
            client.create(honest)
        self.assertEqual((self.vault / honest.locator).read_bytes(),
                         foreign_bytes)

        # Scoped auth: read token -> 403 on POST; no token -> 401; the
        # write token (already proven above) succeeds.
        status, _ = _raw(self.server, "POST", honest.locator,
                         {"mode": "create", "content": "scoped\n"},
                         token=READ_TOKEN)
        self.assertEqual(status, 403, "a read-scope token may not create")
        status, _ = _raw(self.server, "POST", honest.locator,
                         {"mode": "create", "content": "anon\n"},
                         token=None)
        self.assertEqual(status, 401, "a missing token is unauthorized")
        # read scope CAN read.
        status, payload = _raw(self.server, "GET", doc.locator,
                               token=READ_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload.decode("utf-8"))["content"],
                         "first body\n")

        # Concurrent creates of DIFFERENT content to one locator: exactly
        # one 201 and one 409; the stored bytes match the 201 winner; no
        # partial file.
        race = "guides/race/%s.md" % _SEG
        _VaultKBHandler.barrier_locators[race] = threading.Barrier(2)
        outcomes = []

        def actor(content: str):
            status, _ = _raw(self.server, "POST", race,
                             {"mode": "create", "content": content})
            outcomes.append((status, content))

        t1 = threading.Thread(target=actor, args=("race body one\n",))
        t2 = threading.Thread(target=actor, args=("race body two\n",))
        t1.start(); t2.start(); t1.join(20); t2.join(20)
        self.assertFalse(t1.is_alive() and t2.is_alive())
        statuses = sorted(s for s, _ in outcomes)
        self.assertEqual(statuses, [201, 409],
                         "exactly one 201 and one 409: %r" % (outcomes,))
        winner = next(c for s, c in outcomes if s == 201)
        stored = (self.vault / race).read_bytes()
        self.assertEqual(stored, winner.encode("utf-8"),
                         "the stored bytes match the 201 winner")
        self.assertIn(stored.decode("utf-8"),
                      ("race body one\n", "race body two\n"))
        self.assertEqual(
            len([e for e in _files_under(self.vault)
                 if not e.endswith("/")]),
            3, "no partial or extra files from the race (deploy, occupied, "
               "race winner)")


class LocatorSafetyTest(_ServerTestCase):
    """REFERENCE evidence for issue #482 (deployed-server ACs stay open).

    Pins the FULL shared locator grammar (documents.py:199-297): the key
    is 1..8 '/'-separated segments, each a 1..31 lowercase slug
    [a-z0-9-] or a dashed lowercase uuid; total key length <= 128; no
    empty/'.'/'..' segments, no backslash, no leading/trailing slash; the
    digest stem is EXACTLY four 16-hex groups joined by '-'; the suffix
    is exactly '.md'."""

    def test_traversal_segment_refused_before_fs_effect(self):
        bad_locators = [
            # traversal segments
            "guides/../escape/%s.md" % _SEG,
            "../escape/%s.md" % _SEG,
            "guides/..%%2Fescape%%2F%s.md" % _SEG,   # percent-encoded ..
            # uppercase / invalid key segments
            "Guides/deploy/%s.md" % _SEG,
            "guides/Deploy/%s.md" % _SEG,
            "guides/deploy!/%s.md" % _SEG,
            "guides//deploy/%s.md" % _SEG,          # empty segment
            "/guides/deploy/%s.md" % _SEG,          # leading slash
            "guides/deploy/%s.md/" % _SEG,          # trailing slash
            "guides\\deploy\\%s.md" % _SEG,         # backslash
            "guides/deploy/%%5C%s.md" % _SEG,        # percent-encoded backslash
            "guides/deploy/%%2E%%2E/%s.md" % _SEG,  # encoded '.' segment
            "guides%%2Fdeploy/%s.md" % _SEG,        # encoded slash: the raw
                                                    # helper re-encodes '%', so
                                                    # the segment keeps a
                                                    # literal '%' -> invalid
            # digest stem grammar (exactly 4x16 lowercase hex, '-')
            "guides/deploy/not-hex-at-all.md",
            "guides/deploy/AAAA1111AAAA2222-BBBB3333BBBB4444-CCCC5555CCCC6666-DDDD7777DDDD8888.md",
            "guides/deploy/%s-extra.md" % _SEG,     # 5 groups
            "guides/deploy/aaaa1111aaaa2222-bbbb3333bbbb4444.md",  # 2 groups
            "guides/deploy/aaaa1111aaaa2222bbbb3333bbbb4444cccc5555cccc6666dddd7777dddd8888.md",
            # wrong suffix
            "guides/deploy/%s.txt" % _SEG,
            # double-encoded slash becomes a literal '%2F' slug char
            "guides%%2Fdeploy/%s.md" % _SEG,
        ]
        parent_before = _files_under(self.parent)
        for locator in bad_locators:
            status, _ = _raw(self.server, "POST", locator,
                             {"mode": "create", "content": "escape\n"})
            self.assertTrue(400 <= status < 500,
                            "locator %r must be refused 4xx, got %d"
                            % (locator, status))
            self.assertEqual(_files_under(self.parent), parent_before,
                             "locator %r refused BEFORE any filesystem "
                             "effect (snapshot covers OUTSIDE the vault "
                             "too)" % (locator,))
            status, _ = _raw(self.server, "GET", locator)
            self.assertTrue(400 <= status < 500,
                            "GET of invalid locator %r must be 4xx, got %d"
                            % (locator, status))
            self.assertEqual(_files_under(self.parent), parent_before)

        # The grammar's real ACCEPTS (pin the positive side so the gate is
        # exactly the shared grammar, not a blanket refusal):
        #  - deep key AT the cap (8 segments) and a dashed-uuid segment
        deep = "/".join("seg%d" % i for i in range(8)) + "/%s.md" % _SEG
        status, _ = _raw(self.server, "POST", deep,
                         {"mode": "create", "content": "deep ok\n"})
        self.assertEqual(status, 201,
                         "an 8-segment key is AT the grammar cap (valid)")
        uuid_seg = "7e57d004-2b97-0e7a-b45f-5387369e2a91/%s.md" % _SEG
        status, _ = _raw(self.server, "POST", uuid_seg,
                         {"mode": "create", "content": "uuid ok\n"})
        self.assertEqual(status, 201)
        #  - 9 segments is over the grammar's depth cap -> refused, with
        #    a FULL parent-tree after-snapshot (no fs effect anywhere).
        depth_baseline = _files_under(self.parent)
        too_deep = "/".join("seg%d" % i for i in range(9)) + "/%s.md" % _SEG
        status, _ = _raw(self.server, "POST", too_deep,
                         {"mode": "create", "content": "too deep\n"})
        self.assertTrue(400 <= status < 500,
                        "a 9-segment key exceeds MAX_KEY_SEGMENTS=8")
        self.assertEqual(_files_under(self.parent), depth_baseline,
                         "FULL parent-tree equality after the over-depth "
                         "refusal (no file/dir/link effect anywhere)")
        #  - a key segment over the 31-char slug cap -> refused; a
        #    31-char segment (AT the cap) is ACCEPTED.
        length_baseline = _files_under(self.parent)
        long_seg = "s" * 32 + "/%s.md" % _SEG
        status, _ = _raw(self.server, "POST", long_seg,
                         {"mode": "create", "content": "long seg\n"})
        self.assertTrue(400 <= status < 500,
                        "a 32-char key segment exceeds the 1..31 slug cap")
        self.assertEqual(_files_under(self.parent), length_baseline,
                         "FULL parent-tree equality after the over-cap "
                         "segment refusal (no file/dir/link effect)")
        at_cap_seg = "s" * 31 + "/%s.md" % _SEG
        status, _ = _raw(self.server, "POST", at_cap_seg,
                         {"mode": "create", "content": "at cap seg\n"})
        self.assertEqual(status, 201,
                         "a 31-char key segment is AT the grammar cap "
                         "(accepted)")
        #  - total key length: 128 (AT the cap) accepted, 129 refused.
        key128 = "/".join(["k" * 24] * 4 + ["k" * 28])   # 124 + 4 slashes
        self.assertEqual(len(key128), 128)
        status, _ = _raw(self.server, "POST", key128 + "/%s.md" % _SEG,
                         {"mode": "create", "content": "key at cap\n"})
        self.assertEqual(status, 201,
                         "a 128-char total key is AT the grammar cap "
                         "(accepted)")
        baseline2 = _files_under(self.parent)
        key129 = "/".join(["k" * 24] * 4 + ["k" * 29])
        self.assertEqual(len(key129), 129)
        status, _ = _raw(self.server, "POST", key129 + "/%s.md" % _SEG,
                         {"mode": "create", "content": "key over cap\n"})
        self.assertTrue(400 <= status < 500,
                        "a 129-char total key exceeds MAX_KEY_LENGTH=128")
        self.assertEqual(_files_under(self.parent), baseline2,
                         "FULL parent-tree equality after the over-cap "
                         "total-key refusal")

        # Malformed JSON body / wrong mode value -> 4xx before any fs
        # effect (raw socket-level bodies the JSON helper cannot build).
        import socket as _socket
        baseline_now = _files_under(self.parent)
        for body_bytes in (
            b'{"mode": "overwrite", "content": "wrong mode\n"}',
            b'{"mode": "create", "content": null}',
            b'{"no_mode": true}',
            b'{not json at all',
        ):
            with _socket.create_connection(
                    ("127.0.0.1", self.server.httpd.server_address[1]),
                    timeout=10) as sock:
                head = ("POST /api/notes/guides/bad/%s.md HTTP/1.1\r\n"
                        "Host: 127.0.0.1\r\n"
                        "Authorization: Bearer %s\r\n"
                        "Content-Type: application/json\r\n"
                        "Content-Length: %d\r\n"
                        "Connection: close\r\n\r\n"
                        % (_SEG, WRITE_TOKEN, len(body_bytes)))
                sock.sendall(head.encode("utf-8") + body_bytes)
                reply = sock.recv(4096).decode("utf-8", "replace")
            status_line = reply.splitlines()[0] if reply else ""
            code = int(status_line.split()[1]) if status_line and \
                len(status_line.split()) > 1 else 0
            self.assertTrue(
                400 <= code < 500,
                "malformed body %r must be refused 4xx, got %r"
                % (body_bytes[:40], status_line))
            self.assertEqual(_files_under(self.parent), baseline_now,
                             "no filesystem effect from the malformed body")

        # Partial-publication: the client disconnects MID-POST (a short
        # read against Content-Length) -> no partial file, nothing
        # recorded anywhere in the snapshot.
        with _socket.create_connection(
                ("127.0.0.1", self.server.httpd.server_address[1]),
                timeout=10) as sock:
            body = b'{"mode": "create", "content": "partial attempt\\n"}'
            head = ("POST /api/notes/guides/partial/%s.md HTTP/1.1\r\n"
                    "Host: 127.0.0.1\r\n"
                    "Authorization: Bearer %s\r\n"
                    "Content-Type: application/json\r\n"
                    "Content-Length: %d\r\n"
                    "Connection: close\r\n\r\n"
                    % (_SEG, WRITE_TOKEN, len(body)))
            # Send HALF the JSON, claim the full length, disconnect: the
            # server's read comes up short and the parse must fail.
            sock.sendall(head.encode("utf-8") + body[:len(body) // 2])
            sock.close()                                 # disconnect
        import time as _time
        _time.sleep(0.3)  # let the server observe the short read
        snapshot_now = _files_under(self.parent)
        self.assertFalse(
            any("partial" in e for e in snapshot_now),
            "a mid-POST disconnect must leave NO partial file or "
            "directory: %r" % (snapshot_now,))
        # FULL before/after parent-tree equality (no fs effect anywhere)
        # AND a readback of the partial locator 404s — nothing landed.
        self.assertEqual(snapshot_now, baseline_now,
                         "FULL parent-tree equality after the mid-POST "
                         "disconnect (no file/dir/link effect anywhere)")
        status, _ = _raw(self.server, "GET", "guides/partial/%s.md" % _SEG)
        self.assertEqual(status, 404,
                         "a GET for the partial locator answers 404 "
                         "(nothing landed); got %d" % (status,))

        # Readback boundaries: a GET for a DIFFERENT (valid but absent)
        # locator answers 404; a traversal GET is 4xx; and an UNRELATED
        # note's bytes+mtime are untouched by refused publications.
        absent = "guides/absent/%s.md" % _SEG
        status, _ = _raw(self.server, "GET", absent)
        self.assertEqual(status, 404,
                         "a valid-but-absent locator answers 404")
        status, _ = _raw(self.server, "GET",
                         "guides/../escape/%s.md" % _SEG)
        self.assertTrue(400 <= status < 500,
                        "a traversal GET is refused 4xx")
        unrelated = self.vault / deep
        stat_before = unrelated.stat()
        bytes_before = unrelated.read_bytes()
        status, _ = _raw(self.server, "POST", long_seg,
                         {"mode": "create", "content": "refusal probe\n"})
        self.assertTrue(400 <= status < 500)
        self.assertEqual(unrelated.read_bytes(), bytes_before,
                         "a refused publication never touches an "
                         "unrelated note's bytes")
        self.assertEqual(unrelated.stat().st_mtime_ns,
                         stat_before.st_mtime_ns,
                         "a refused publication never touches an "
                         "unrelated note's mtime")

        # Symlink/junction containment: a LINK inside the vault pointing
        # OUTSIDE — a grammar-valid locator under the linked directory
        # must never write through it.  (On Windows a directory junction
        # needs no privileges; a true symlink needs developer mode — try
        # the junction first and report what was created.)
        outside = self.parent / "outside"
        outside.mkdir()
        linked = self.vault / "linked"
        link_kind = "none"
        try:
            os.symlink(str(outside), str(linked), target_is_directory=True)
            link_kind = "symlink"
        except (OSError, NotImplementedError):
            try:
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J",
                     str(linked), str(outside)],
                    capture_output=True, timeout=15, check=True)
                link_kind = "junction"
            except (OSError, subprocess.SubprocessError):
                link_kind = "none"
        if link_kind != "none":
            through = "linked/deploy/%s.md" % _SEG
            status, _ = _raw(self.server, "POST", through,
                             {"mode": "create", "content": "escape\n"})
            self.assertTrue(
                400 <= status < 500,
                "publication through an in-vault %s must be refused 4xx, "
                "got %d" % (link_kind, status))
            self.assertEqual(
                [e for e in _files_under(outside) if not e.endswith("/")],
                [], "NOTHING may be written through the link (outside the "
                    "vault)")
            # The containment snapshot also covers the link itself.
            self.assertTrue(any("linked" in e for e in
                                _files_under(self.vault)))
            import subprocess as _sp  # noqa: PLC0415 - junction cleanup
            if link_kind == "junction":
                _sp.run(["cmd", "/c", "rmdir", str(linked)],
                        capture_output=True, timeout=15)
        else:
            # Recorded skip: neither symlink nor junction could be created
            # (report notes this host's result).
            self.skipTest(
                "could not create a symlink or junction on this host; "
                "containment leg not executed")


if __name__ == "__main__":
    unittest.main()
