"""Tests for knowledge document admission and immutable targets (PR1).

Covers, per the approved plan's validation promises:
  - canonical_content: secret scan FIRST and REJECTING (api_key=, Bearer,
    sk-, AWS/AIza/JWT-ish, PEM, 32+ hex, 40+ base64ish, ssh-style
    commands, the redaction marker itself), LF normalization with exactly
    one terminal newline, size bound
  - digest segmentation (4x16 hex) and the deterministic locator; helper
    ids (ref/locator/segmented digest) must NOT trip the secret scanner
  - logical key / ref validation negatives (path escape, backslash,
    uppercase, overlong, too many segments)
  - MarkdownStore: exclusive create, byte-identical recreate success,
    tampered same-locator file -> ConflictError with NO overwrite, missing
    version, missing root, reparse-point ancestors refused
  - KBClient against a LOCAL mock HTTP server on 127.0.0.1 only:
    create+readback, 409-equal recovery, 409-mismatch conflict, timeout
    recovery (GET equal -> success; GET absent -> unknown outcome), auth
    refusal without a token oracle and with the token never in any
    message, redirect refusal, oversized response refusal, malformed
    bodies, and the wire shape {"mode":"create","content":...} with no
    PUT/PATCH ever issued

No live network: the mock server binds 127.0.0.1 with an ephemeral port
and is shut down in tearDown.  Filesystem work happens in temp dirs.

Run: python tests/test_knowledge_documents.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import hashlib
import http.server
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

_TMP = tempfile.mkdtemp(prefix="zmem-knfdoc-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
sys.path.insert(0, str(SCRIPTS_DIR))

from redaction import redact_secret_like_text  # noqa: E402

from knowledge.config import (  # noqa: E402
    KBHTTPTarget,
    MarkdownTarget,
)
from knowledge.documents import (  # noqa: E402
    ConflictError,
    CreateResult,
    Document,
    DocumentRejected,
    IntegrityError,
    KBClient,
    MarkdownStore,
    TargetError,
    TransportError,
    canonical_content,
    content_digest,
    logical_ref,
    open_target,
    parse_locator,
    parse_segmented_digest,
    prepare_document,
    segment_digest,
    validate_logical_key,
    version_locator,
)

TOKEN = "unit-test-bearer-token-0123456789abcdef"
UUID_KEY = "7e57d004-2b97-0e7a-b45f-5387369e2a91"


def make_doc(target: str, key: str, text: str) -> Document:
    return prepare_document(target, key, text)


class CanonicalContentTests(unittest.TestCase):
    def test_normalizes_lf_and_single_terminal_newline(self):
        self.assertEqual(canonical_content("a\r\nb\rc\n\n\n"), b"a\nb\nc\n")
        self.assertEqual(canonical_content("no newline"), b"no newline\n")

    def test_digest_is_over_canonical_bytes(self):
        a = canonical_content("x\r\ny")
        b = canonical_content("x\ny\n")
        self.assertEqual(a, b)
        self.assertEqual(content_digest(a), hashlib.sha256(a).hexdigest())

    def test_rejects_empty_and_whitespace(self):
        with self.assertRaises(DocumentRejected):
            canonical_content("")
        with self.assertRaises(DocumentRejected):
            canonical_content(" \n\t\n")

    def test_rejects_secret_shapes(self):
        samples = (
            "api_key=abcdEFGH1234",
            "TOKEN: supersecretvalue1",
            "password=hunter2hunter2",
            "Bearer abcdefghijklmnopqrstuvwxyz123456",
            "sk-ant-api03-1234567890abcdefghij",
            "sk-proj-1234567890abcdefghijKLMN",
            "AKIAIOSFODNN7EXAMPLE",
            "AIza" + "a" * 35,
            "eyJ" + "A" * 40 + "." + "B" * 12,
            "ghp_" + "a" * 36,
            "xoxb-1234567890abcdefghij",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",        # 40 hex
            "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaA",  # base64ish run
            "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
            "sshpass -p secretpw123 ssh host",
            "sudo -S dnf install x",
            "db_password=correcthorsebatterystaple",
        )
        for sample in samples:
            with self.assertRaises(DocumentRejected, msg=sample[:30]):
                canonical_content(sample)

    def test_redaction_marker_itself_rejects(self):
        # Fail-safe: an already-redacted credential SHAPE still counts as a
        # detection and refuses publication rather than passing as
        # pre-cleaned (marker text in bare prose, with no credential
        # keyword, is not a hit — that is the scanner's own semantics).
        with self.assertRaises(DocumentRejected):
            canonical_content("password: [REDACTED_SECRET] please")

    def test_accepts_clean_prose(self):
        text = "# Runbook\n\n1. rotate certs\n2. restart the service\n"
        self.assertEqual(canonical_content(text), text.encode("utf-8"))

    def test_size_bound(self):
        huge = "x" * (4 * 1024 * 1024 + 2)
        with self.assertRaises(DocumentRejected):
            canonical_content(huge)

    def test_rejection_message_carries_no_content(self):
        try:
            canonical_content("api_key=abcdEFGH1234")
        except DocumentRejected as exc:
            self.assertNotIn("abcdEFGH1234", str(exc))


class IdentityTests(unittest.TestCase):
    def test_logical_key_positives(self):
        self.assertEqual(validate_logical_key("notes"), "notes")
        self.assertEqual(validate_logical_key("run/deep-2/leaf"),
                         "run/deep-2/leaf")
        self.assertEqual(validate_logical_key(UUID_KEY), UUID_KEY)
        self.assertEqual(
            validate_logical_key("run/" + UUID_KEY + "/leaf"),
            "run/" + UUID_KEY + "/leaf")

    def test_logical_key_negatives(self):
        bad = (
            "", "/", "/lead", "trail/", "a//b", "a/./b", "../escape",
            "a/../b", "back\\slash", "UPPER", "a/B", "-lead", "trail-",
            "a/" + "x" * 32,             # 32-char slug segment
            "x" * 32,                    # 32-char top segment
            "/".join("s%d" % i for i in range(9)),   # >8 segments
            "x" * 129,                   # overlong
            "7e57d0042b970e7ab45f5387369e2a91",      # undashed 32-hex uuid
        )
        for sample in bad:
            with self.assertRaises(DocumentRejected, msg=sample[:24]):
                validate_logical_key(sample)

    def test_logical_ref_shape(self):
        ref = logical_ref("notes", "runbook/deploy")
        self.assertEqual(ref, "knowledge:notes:runbook/deploy")
        with self.assertRaises(DocumentRejected):
            logical_ref("Notes", "x")
        with self.assertRaises(DocumentRejected):
            logical_ref("notes", "../escape")

    def test_credential_shaped_refs_rejected_at_admission(self):
        """A target id like `dev-token` assembles a ref whose
        `token:<key>` substring matches the capture credential patterns —
        the exact parity break (manual local writes succeeded while every
        auto-mode sync/import/rescan refused the same ref). Admission
        rejects the ASSEMBLED ref with a count-only message, before any
        document create; the scanner-safe chunk grammar still passes."""
        for bad_target in ("dev-token", "api-key", "service-secret",
                           "db-password", "passwd"):
            with self.assertRaises(DocumentRejected, msg=bad_target):
                logical_ref(bad_target, "runbooks/deploy")
        # delta-4: a SHORT key passed the bare-ref scan (the credential
        # value-span needs 8+ non-whitespace chars) while the compact
        # JSON payload — which glues `","locator":…` straight onto the
        # ref — still tripped the auto detector and ingest redacted the
        # payload mid-JSON. Payload-context screening closes the bypass
        # for EVERY key length.
        for short_key in ("x", "a/b", "run"):
            with self.assertRaises(DocumentRejected, msg=short_key):
                logical_ref("dev-token", short_key)
            with self.assertRaises(DocumentRejected, msg=short_key):
                logical_ref("svc-secret", short_key)
        try:
            logical_ref("dev-token", "runbooks/deploy")
        except DocumentRejected as exc:
            msg = str(exc)
            self.assertIn("hit(s)", msg)          # count-only
            self.assertNotIn("runbooks/deploy", msg)  # no matched text
            self.assertNotIn("dev-token", msg)       # no ref quoted
        # clean chunk grammar still admitted (docs/tags unaffected)
        self.assertTrue(logical_ref("fleet-kb", UUID_KEY)
                        .startswith("knowledge:"))
        self.assertTrue(logical_ref("notes", "run/" + UUID_KEY + "/leaf")
                        .startswith("knowledge:"))
        clean = logical_ref("fleet-kb", "runbooks/deploy-window")
        _redacted, hits = redact_secret_like_text(clean + '","locator"')
        self.assertEqual(hits, 0)

    def test_segmented_digest_roundtrip(self):
        digest = content_digest(b"payload\n")
        segmented = segment_digest(digest)
        self.assertEqual(len(segmented), 64 + 3)
        self.assertEqual(parse_segmented_digest(segmented), digest)
        with self.assertRaises(ValueError):
            segment_digest("short")
        with self.assertRaises(ValueError):
            parse_segmented_digest(digest)          # raw 64hex is not the form
        with self.assertRaises(ValueError):
            parse_segmented_digest("z" * 16 + "-a" * 3)

    def test_locator_deterministic_and_parseable(self):
        doc = make_doc("notes", "run/leaf", "stable body\n")
        again = make_doc("notes", "run/leaf", "stable body\n")
        self.assertEqual(doc.locator, again.locator)
        key, digest = parse_locator(doc.locator)
        self.assertEqual(key, "run/leaf")
        self.assertEqual(digest, doc.digest)
        self.assertTrue(doc.locator.endswith(".md"))

    def test_locator_negatives(self):
        for bad in ("run/leaf.md", "run/leaf/note.md",
                    "run/" + "z" * 16 + "-" + "a" * 16 + "-" + "b" * 16
                    + "-" + "c" * 16 + ".txt",
                    "run/../" + "a" * 16 + "-" + "b" * 16 + "-" + "c" * 16
                    + "-" + "d" * 16 + ".md",
                    "a\\b.md"):
            with self.assertRaises(ValueError, msg=bad[:20]):
                parse_locator(bad)

    def test_helper_ids_avoid_the_scanner(self):
        """The ref, locator, and segmented digest of an admitted document
        must be invisible to the secret scanner (a raw 64-hex digest would
        not be)."""
        doc = make_doc("notes", "runbook/" + UUID_KEY + "/deploy", "body\n")
        for text in (doc.ref, doc.locator, doc.segmented_digest):
            _redacted, hits = redact_secret_like_text(text)
            self.assertEqual(hits, 0, text)


class MarkdownStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="zmem-knfvault-"))
        self.store = MarkdownStore(MarkdownTarget(id="notes", root=self.root))

    def test_create_read_roundtrip(self):
        doc = make_doc("notes", "run/deploy", "one\ntwo\n")
        result = self.store.create(doc)
        self.assertIsInstance(result, CreateResult)
        self.assertFalse(result.existed_match)
        self.assertTrue((self.root / doc.locator).is_file())
        self.assertEqual(self.store.read(doc.locator), b"one\ntwo\n")

    def test_recreate_identical_is_success(self):
        doc = make_doc("notes", "run/deploy", "same\n")
        first = self.store.create(doc)
        second = self.store.create(doc)
        self.assertFalse(first.existed_match)
        self.assertTrue(second.existed_match)

    def test_tampered_locator_file_is_conflict_without_overwrite(self):
        doc = make_doc("notes", "run/deploy", "authored body\n")
        target = self.root / doc.locator
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"tampered bytes\n")
        with self.assertRaises(ConflictError):
            self.store.create(doc)
        self.assertEqual(target.read_bytes(), b"tampered bytes\n")

    def test_two_versions_same_key_coexist(self):
        v1 = make_doc("notes", "run/deploy", "v1 body\n")
        v2 = make_doc("notes", "run/deploy", "v2 body\n")
        self.store.create(v1)
        self.store.create(v2)
        self.assertNotEqual(v1.locator, v2.locator)
        self.assertEqual(self.store.read(v1.locator), b"v1 body\n")
        self.assertEqual(self.store.read(v2.locator), b"v2 body\n")

    def test_read_missing_version(self):
        doc = make_doc("notes", "run/absent", "x\n")
        with self.assertRaises(TargetError):
            self.store.read(doc.locator)

    def test_missing_root_refused(self):
        store = MarkdownStore(MarkdownTarget(
            id="notes", root=self.root / "not-created"))
        doc = make_doc("notes", "run/deploy", "x\n")
        with self.assertRaises(TargetError):
            store.create(doc)

    def test_wrong_target_id_refused(self):
        doc = make_doc("other", "run/deploy", "x\n")
        with self.assertRaises(ValueError):
            self.store.create(doc)

    def test_escaped_locator_refused_on_read(self):
        for bad in ("../" + "a" * 16 + ".md",
                    "a/..%2Fb.md", "a\\b.md", "a/b.md"):
            with self.assertRaises((ValueError, TargetError), msg=bad):
                self.store.read(bad)

    def _dirlink_or_skip(self, link: Path, dest: Path) -> None:
        """Create a directory symlink; on Windows fall back to a junction
        (also a reparse point, creatable without admin)."""
        try:
            os.symlink(str(dest), str(link))
            return
        except (OSError, NotImplementedError):
            pass
        if os.name == "nt":
            import _winapi
            try:
                _winapi.CreateJunction(str(dest), str(link))
                return
            except (OSError, AttributeError):
                pass
        self.skipTest("no reparse-point capability on this host")

    def _symlink_or_skip(self, link: Path, dest: Path) -> None:
        try:
            os.symlink(str(dest), str(link))
        except (OSError, NotImplementedError):
            self.skipTest("os.symlink unavailable on this host")

    def test_symlinked_directory_refused(self):
        real_dir = self.root / "real"
        real_dir.mkdir()
        link_dir = self.root / "linked"
        self._dirlink_or_skip(link_dir, real_dir)
        store = MarkdownStore(MarkdownTarget(id="notes", root=self.root))
        doc = make_doc("notes", "linked/deeper", "x\n")
        with self.assertRaises(TargetError):
            store.create(doc)

    def test_symlinked_root_refused(self):
        real_root = Path(tempfile.mkdtemp(prefix="zmem-knfreal-"))
        link_root = Path(tempfile.mkdtemp(prefix="zmem-knflink-")) / "vault"
        self._dirlink_or_skip(link_root, real_root)
        store = MarkdownStore(MarkdownTarget(id="notes", root=link_root))
        doc = make_doc("notes", "run/deploy", "x\n")
        with self.assertRaises(TargetError):
            store.create(doc)

    def test_reparse_ancestor_above_vault_root_refused(self):
        """A junction ABOVE the vault root: the root itself is a real
        directory (so _require_root passes) and realpath containment holds
        (root and note resolve through the junction together), so only the
        full-ancestor walk past the vault root — the docstring's contract,
        matching storelib.source.is_safe_regular_path — catches it.
        Pre-fix the chain check stopped at the vault root and the create
        wrote straight through the junctioned ancestor."""
        base = Path(tempfile.mkdtemp(prefix="zmem-knfabove-"))
        holder = base / "holder"
        real = base / "real"
        holder.mkdir()
        (real / "vault").mkdir(parents=True)
        self._dirlink_or_skip(holder / "link", real)  # junction above root
        root = holder / "link" / "vault"              # real dir, via link
        self.assertTrue(root.is_dir())
        store = MarkdownStore(MarkdownTarget(id="notes", root=root))
        doc = make_doc("notes", "run/deploy", "through the junction\n")
        with self.assertRaises(TargetError):
            store.create(doc)
        # nothing was written through the unsafe ancestor
        self.assertFalse((real / "vault" / doc.locator).exists())
        # and the read path refuses the same chain (missing="none")
        with self.assertRaises(TargetError):
            store.read(doc.locator)

    def test_symlinked_note_file_refused(self):
        doc = make_doc("notes", "run/deploy", "genuine body\n")
        target = self.root / doc.locator
        target.parent.mkdir(parents=True, exist_ok=True)
        decoy = self.root / "decoy.md"
        decoy.write_bytes(b"decoy\n")
        self._symlink_or_skip(target, decoy)
        with self.assertRaises((TargetError, ConflictError)):
            self.store.create(doc)
        # and reading through the link is refused too
        with self.assertRaises(TargetError):
            self.store.read(doc.locator)


class _MockKB(http.server.BaseHTTPRequestHandler):
    """Minimal KB: POST create (409 on existing), GET, strict bearer auth.

    Records every request for wire-shape assertions.  State lives on the
    BASE class dicts (mutated in place by reset) so per-test subclasses
    can never shadow it with their own class attributes.
    """
    notes: dict[str, str] = {}
    token: str = TOKEN
    requests: list[tuple[str, str, dict]] = []

    def log_message(self, *args):  # silence
        pass

    def _authorized(self) -> bool:
        return self.headers.get("Authorization") == "Bearer " + _MockKB.token

    @classmethod
    def reset(cls) -> None:
        _MockKB.notes.clear()
        _MockKB.requests.clear()

    def _record(self, method: str) -> None:
        _MockKB.requests.append(
            (method, self.path, dict(self.headers.items())))

    def _rel(self) -> str:
        prefix = "/api/notes/"
        return urllib.parse.unquote(self.path[len(prefix):])

    def do_POST(self):  # noqa: N802 (http.server naming)
        self._record("POST")
        if not self._authorized():
            self._reply(401)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if body.get("mode") != "create" or not isinstance(
                body.get("content"), str):
            self._reply(400)
            return
        rel = self._rel()
        if rel in _MockKB.notes:
            self._reply(409)
            return
        _MockKB.notes[rel] = body["content"]
        self._reply(201, {"locator": rel})

    def do_GET(self):  # noqa: N802
        self._record("GET")
        if not self._authorized():
            self._reply(401)
            return
        rel = self._rel()
        content = _MockKB.notes.get(rel)
        if content is None:
            self._reply(404)
            return
        self._reply(200, {"content": content})

    def _reply(self, status: int, obj: dict | None = None) -> None:
        payload = b"" if obj is None else json.dumps(obj).encode("utf-8")
        self.send_response(status)
        if payload:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if payload:
            self.wfile.write(payload)


class MockKBServer:
    def __init__(self, handler=_MockKB) -> None:
        handler.reset()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                     handler)
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


def kb_client(endpoint: str, *, token: str = TOKEN, env: dict | None = None,
              timeout_s: float = 5.0, max_bytes: int = 65536) -> KBClient:
    return KBClient(
        KBHTTPTarget(id="fleet", endpoint=endpoint, allow_http=True,
                     credential_env="ZMEM_KB_TEST_TOKEN", timeout_s=timeout_s,
                     max_response_bytes=max_bytes),
        env={"ZMEM_KB_TEST_TOKEN": token} if env is None else env)


class KBClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = MockKBServer()

    def tearDown(self) -> None:
        self.server.close()

    def test_create_wire_shape_and_readback(self):
        client = kb_client(self.server.endpoint)
        doc = make_doc("fleet", "guides/deploy", "canonical body\n")
        result = client.create(doc)
        self.assertFalse(result.existed_match)
        self.assertEqual(result.locator, doc.locator)
        # The stored note is exactly the canonical full content.
        self.assertEqual(_MockKB.notes[doc.locator], "canonical body\n")
        self.assertEqual(client.read(doc.locator), b"canonical body\n")
        posts = [r for r in _MockKB.requests if r[0] == "POST"]
        self.assertEqual(len(posts), 1)
        self.assertTrue(posts[0][1].endswith(
            urllib.parse.quote(doc.locator, safe="/")))

    def test_recreate_409_equal_is_success(self):
        client = kb_client(self.server.endpoint)
        doc = make_doc("fleet", "guides/deploy", "twice\n")
        first = client.create(doc)
        second = client.create(doc)
        self.assertFalse(first.existed_match)
        self.assertTrue(second.existed_match)
        methods = [r[0] for r in _MockKB.requests]
        self.assertIn("GET", methods)  # recovery read happened

    def test_409_mismatch_is_conflict_and_never_put(self):
        client = kb_client(self.server.endpoint)
        doc = make_doc("fleet", "guides/tamper", "honest body\n")
        _MockKB.notes[doc.locator] = "different body\n"
        with self.assertRaises(ConflictError):
            client.create(doc)
        methods = {r[0] for r in _MockKB.requests}
        self.assertNotIn("PUT", methods)
        self.assertNotIn("PATCH", methods)
        self.assertNotIn("DELETE", methods)

    def test_post_success_then_readback_mismatch(self):
        class Liar(_MockKB):
            def do_POST(self):  # noqa: N802
                self._record("POST")
                if not self._authorized():
                    self._reply(401)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                json.loads(self.rfile.read(length))
                # Accept the create but answer every GET with other bytes:
                # the client must trust only its own digest comparison.
                _MockKB.notes[self._rel()] = "claimed body\n"
                self._reply(201)

            def do_GET(self):  # noqa: N802
                self._record("GET")
                if not self._authorized():
                    self._reply(401)
                    return
                self._reply(200, {"content": "actual different\n"})

        server = MockKBServer(Liar)
        try:
            client = kb_client(server.endpoint)
            doc = make_doc("fleet", "guides/liar", "claimed body\n")
            with self.assertRaises(IntegrityError):
                client.create(doc)
        finally:
            server.close()

    def test_timeout_recovery_equal_content(self):
        class Slow(_MockKB):
            def do_POST(self):  # noqa: N802
                self._record("POST")
                if not self._authorized():
                    self._reply(401)
                    return
                import time
                # The server COMMITS the note, then the response is lost to
                # a slow write: recovery GET must find the identical bytes.
                _MockKB.notes[self._rel()] = "late but right\n"
                time.sleep(2.0)
                self._reply(201)

        server = MockKBServer(Slow)
        try:
            client = kb_client(server.endpoint, timeout_s=0.4)
            doc = make_doc("fleet", "guides/slow", "late but right\n")
            result = client.create(doc)
            self.assertTrue(result.existed_match)
        finally:
            server.close()

    def test_timeout_recovery_absent_is_unknown_outcome(self):
        class DropPost(_MockKB):
            def do_POST(self):  # noqa: N802
                self._record("POST")
                import time
                time.sleep(2.0)
                self._reply(500)

        server = MockKBServer(DropPost)
        try:
            client = kb_client(server.endpoint, timeout_s=0.4)
            doc = make_doc("fleet", "guides/lost", "lost body\n")
            with self.assertRaises(TransportError):
                client.create(doc)
        finally:
            server.close()

    def test_missing_token_refused_before_network(self):
        client = kb_client(self.server.endpoint, env={})
        doc = make_doc("fleet", "guides/noauth", "x\n")
        with self.assertRaises(TargetError):
            client.create(doc)
        self.assertEqual(_MockKB.requests, [])   # no request left the process

    def test_wrong_token_is_auth_refusal_without_token_leak(self):
        client = kb_client(self.server.endpoint, token="totally-wrong-tok")
        doc = make_doc("fleet", "guides/badauth", "x\n")
        with self.assertRaises(TargetError) as ctx:
            client.create(doc)
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn("totally-wrong-tok", str(ctx.exception))

    def test_redirect_refused(self):
        class Redirect(_MockKB):
            def do_GET(self):  # noqa: N802
                self._record("GET")
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/evil")
                self.end_headers()

        server = MockKBServer(Redirect)
        try:
            client = kb_client(server.endpoint)
            doc = make_doc("fleet", "guides/redir", "x\n")
            with self.assertRaises(TransportError):
                client.create(doc)   # POST succeeds, readback GET redirects
        finally:
            server.close()

    def test_oversized_response_refused(self):
        class Big(_MockKB):
            def do_GET(self):  # noqa: N802
                self._record("GET")
                payload = b"x" * 70000
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = MockKBServer(Big)
        try:
            client = kb_client(server.endpoint, max_bytes=65536)
            doc = make_doc("fleet", "guides/big", "x\n")
            _MockKB.notes[doc.locator] = "x\n"
            with self.assertRaises(TransportError):
                client.read(doc.locator)
        finally:
            server.close()

    def test_malformed_get_body_refused(self):
        class Garbage(_MockKB):
            def do_GET(self):  # noqa: N802
                self._record("GET")
                payload = b"<html>not json</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        server = MockKBServer(Garbage)
        try:
            client = kb_client(server.endpoint)
            doc = make_doc("fleet", "guides/garbage", "x\n")
            with self.assertRaises(TransportError):
                client.read(doc.locator)
        finally:
            server.close()

    def test_read_missing_version(self):
        client = kb_client(self.server.endpoint)
        doc = make_doc("fleet", "guides/absent", "x\n")
        with self.assertRaises(TargetError):
            client.read(doc.locator)

    def test_wrong_target_id_refused(self):
        client = kb_client(self.server.endpoint)
        doc = make_doc("other", "guides/x", "x\n")
        with self.assertRaises(ValueError):
            client.create(doc)

    def test_token_file_credential(self):
        token_file = Path(tempfile.mkdtemp(prefix="zmem-knftok-")) / "tok"
        token_file.write_text(TOKEN + "\n", encoding="utf-8")
        client = KBClient(
            KBHTTPTarget(id="fleet", endpoint=self.server.endpoint,
                         allow_http=True, token_file=token_file,
                         timeout_s=5.0, max_response_bytes=65536))
        doc = make_doc("fleet", "guides/filetok", "file token body\n")
        result = client.create(doc)
        self.assertFalse(result.existed_match)

    def test_token_file_oversized_refused(self):
        token_file = Path(tempfile.mkdtemp(prefix="zmem-knftok-")) / "tok"
        token_file.write_bytes(b"x" * 5000)
        client = KBClient(
            KBHTTPTarget(id="fleet", endpoint=self.server.endpoint,
                         allow_http=True, token_file=token_file,
                         timeout_s=5.0, max_response_bytes=65536))
        with self.assertRaises(TargetError):
            client.read("a/" + "1" * 16 + "-" + "2" * 16 + "-" + "3" * 16
                        + "-" + "4" * 16 + ".md")


class OpenTargetTests(unittest.TestCase):
    def test_dispatch(self):
        root = Path(tempfile.mkdtemp(prefix="zmem-knfopen-"))
        markdown = open_target(MarkdownTarget(id="notes", root=root))
        self.assertIsInstance(markdown, MarkdownStore)
        kb = open_target(
            KBHTTPTarget(id="fleet", endpoint="https://hub.invalid/api/notes",
                         credential_env="T"),
            env={"T": "x"})
        self.assertIsInstance(kb, KBClient)
        with self.assertRaises(TypeError):
            open_target(object())


if __name__ == "__main__":
    unittest.main(verbosity=2)
