"""Tests for the knowledge companion config reader (PR1).

``knowledge.config.load_knowledge_config`` reads the optional ``knowledge``
object of the store-side config.json THROUGH ``storelib.config.load_config``
and strictly validates it.  These tests pin the contract:

  - default off: no file / no ``knowledge`` key -> enabled=False, no targets
  - ``enabled`` must be a strict boolean
  - target ids: 1..24 lowercase slugs, everything else refused
  - markdown targets: absolute root only, no ``..``
  - kb-http targets: https default; plain http ONLY with explicit
    ``allow_http: true``; exact endpoint (no query/fragment/userinfo/
    trailing slash); exactly one of credential_env / token_file; bounded
    timeout_s and max_response_bytes
  - unknown keys fail loudly; malformed files fail OPEN to disabled (the
    storelib reader's contract), while a PRESENT-but-invalid knowledge
    object fails CLOSED with KnowledgeConfigError
  - the env mapping is captured, never consulted for token values here

Run: python tests/test_knowledge_config.py
"""

from __future__ import annotations
import _store_guard  # noqa: F401  (issue #244 isolation guard)

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "skills" / "memory" / "scripts"

# Keep any storelib import away from a real store, per repo convention.
_TMP = tempfile.mkdtemp(prefix="zmem-knfcfg-")
os.environ["ZMEM_STORE"] = os.path.join(_TMP, "store.sqlite")
os.environ.setdefault("ZMEM_MODEL_AUTODOWNLOAD", "0")
sys.path.insert(0, str(SCRIPTS_DIR))

from knowledge.config import (  # noqa: E402
    KBHTTPTarget,
    KnowledgeConfigError,
    MarkdownTarget,
    validate_target_id,
    load_knowledge_config,
)

GOOD_KB = {
    "kind": "kb-http",
    "endpoint": "https://hub.lan:8443/api/notes",
    "credential_env": "ZMEM_KB_TOKEN",
}


def write_config(tmpdir: Path, obj: object, name: str = "config.json",
                 bom: bool = False) -> Path:
    path = Path(tmpdir) / name
    text = json.dumps(obj)
    path.write_bytes(("﻿" if bom else "") .encode("utf-8") + text.encode("utf-8"))
    return path


def knowledge_cfg(tmpdir: Path, knowledge: object, base: dict | None = None,
                  bom: bool = False) -> Path:
    obj = dict(base or {})
    if knowledge is not None:
        obj["knowledge"] = knowledge
    return write_config(tmpdir, obj, bom=bom)


class DefaultOffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-knfcfg-"))

    def test_missing_file_is_default_off(self):
        cfg = load_knowledge_config(self.tmp / "absent.json")
        self.assertFalse(cfg.enabled)
        self.assertEqual(dict(cfg.targets), {})

    def test_no_knowledge_key_is_default_off(self):
        cfg = load_knowledge_config(knowledge_cfg(self.tmp, None,
                                                  {"auto_retain": 0.5}))
        self.assertFalse(cfg.enabled)
        self.assertEqual(dict(cfg.targets), {})

    def test_malformed_file_fails_open_to_disabled(self):
        path = self.tmp / "config.json"
        path.write_text("{not json", encoding="utf-8")
        cfg = load_knowledge_config(path)
        self.assertFalse(cfg.enabled)

    def test_zero_byte_file_is_default_off(self):
        path = self.tmp / "config.json"
        path.write_bytes(b"")
        cfg = load_knowledge_config(path)
        self.assertFalse(cfg.enabled)

    def test_bom_prefixed_valid_file_loads(self):
        path = knowledge_cfg(self.tmp, {"enabled": True, "targets": {}}, bom=True)
        cfg = load_knowledge_config(path)
        self.assertTrue(cfg.enabled)
        self.assertEqual(dict(cfg.targets), {})

    def test_disabled_with_targets_parsed(self):
        path = knowledge_cfg(self.tmp, {
            "enabled": False,
            "targets": {"vault": {"kind": "markdown",
                                  "root": str(self.tmp / "vault")}},
        })
        cfg = load_knowledge_config(path)
        self.assertFalse(cfg.enabled)
        self.assertIn("vault", cfg.targets)


class StrictnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-knfcfg-"))

    def assert_bad(self, knowledge: object) -> None:
        path = knowledge_cfg(self.tmp, knowledge)
        with self.assertRaises(KnowledgeConfigError):
            load_knowledge_config(path)

    def test_knowledge_not_object(self):
        self.assert_bad(["enabled"])

    def test_enabled_must_be_boolean(self):
        self.assert_bad({"enabled": "true", "targets": {}})
        self.assert_bad({"enabled": 1, "targets": {}})
        self.assert_bad({"enabled": None, "targets": {}})

    def test_enabled_required(self):
        self.assert_bad({"targets": {}})

    def test_unknown_top_key(self):
        self.assert_bad({"enabled": True, "targets": {},
                         "extra": 1})

    def test_targets_not_object(self):
        self.assert_bad({"enabled": True, "targets": []})

    def test_target_not_object(self):
        self.assert_bad({"enabled": True,
                         "targets": {"a": "markdown"}})

    def test_unknown_target_kind(self):
        self.assert_bad({"enabled": True, "targets": {
            "a": {"kind": "obsidian"}}})
        self.assert_bad({"enabled": True, "targets": {"a": {}}})

    def test_unknown_target_key(self):
        self.assert_bad({"enabled": True, "targets": {
            "a": {"kind": "markdown", "root": "D:/v", "model": "x"}}})
        self.assert_bad({"enabled": True, "targets": {
            "b": dict(GOOD_KB, retries=2)}})

    def test_bad_target_ids(self):
        for bad in ("", "A", "Notes", "-lead", "trail-",
                    "x" * 25, "has space", "has_underscore"):
            with self.assertRaises(KnowledgeConfigError, msg=bad):
                validate_target_id(bad)

    def test_good_target_ids(self):
        for good in ("a", "notes", "fleet-kb-2", "x" * 24):
            self.assertEqual(validate_target_id(good), good)


class MarkdownTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-knfcfg-"))

    def load(self, target: object) -> object:
        path = knowledge_cfg(self.tmp, {"enabled": True,
                                        "targets": {"v": target}})
        return load_knowledge_config(path).targets["v"]

    def test_valid(self):
        root = self.tmp / "vault" / "notes"
        target = self.load({"kind": "markdown", "root": str(root)})
        self.assertIsInstance(target, MarkdownTarget)
        self.assertEqual(target.id, "v")
        self.assertEqual(target.root, root)

    def test_relative_root_refused(self):
        with self.assertRaises(KnowledgeConfigError):
            self.load({"kind": "markdown", "root": "vault/notes"})

    def test_dotdot_root_refused(self):
        with self.assertRaisesRegex(KnowledgeConfigError,
                                    "must not contain.*components"):
            self.load({"kind": "markdown",
                       "root": str(self.tmp / "vault" / ".." / "etc")})

    def test_root_not_string(self):
        with self.assertRaises(KnowledgeConfigError):
            self.load({"kind": "markdown", "root": 3})

    def test_root_missing(self):
        with self.assertRaises(KnowledgeConfigError):
            self.load({"kind": "markdown"})


class KBHTTPTargetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-knfcfg-"))

    def load(self, target: object):
        path = knowledge_cfg(self.tmp, {"enabled": True,
                                        "targets": {"kb": target}})
        return load_knowledge_config(path).targets["kb"]

    def test_valid_https_defaults(self):
        target = self.load(dict(GOOD_KB))
        self.assertIsInstance(target, KBHTTPTarget)
        self.assertEqual(target.endpoint, "https://hub.lan:8443/api/notes")
        self.assertFalse(target.allow_http)
        self.assertEqual(target.credential_env, "ZMEM_KB_TOKEN")
        self.assertIsNone(target.token_file)
        self.assertGreater(target.timeout_s, 0)
        self.assertGreater(target.max_response_bytes, 0)

    def test_http_requires_explicit_allow_http(self):
        with self.assertRaises(KnowledgeConfigError):
            self.load(dict(GOOD_KB, endpoint="http://hub.lan/api/notes"))
        target = self.load(dict(GOOD_KB, endpoint="http://hub.lan/api/notes",
                                allow_http=True))
        self.assertTrue(target.allow_http)

    def test_allow_http_must_be_boolean(self):
        with self.assertRaises(KnowledgeConfigError):
            self.load(dict(GOOD_KB, endpoint="http://hub.lan/api/notes",
                           allow_http="true"))

    def test_endpoint_shapes_refused(self):
        for bad in (
            "ftp://hub.lan/api/notes",
            "https://hub.lan/api/notes?x=1",
            "https://hub.lan/api/notes#frag",
            "https://user:pw@hub.lan/api/notes",
            "https://hub.lan/api/notes/",
            "https://hub.lan",
            "not a url",
        ):
            with self.assertRaises(KnowledgeConfigError, msg=bad):
                self.load(dict(GOOD_KB, endpoint=bad))

    def test_credential_exactly_one(self):
        with self.assertRaisesRegex(KnowledgeConfigError, "exactly one"):
            self.load(dict(GOOD_KB, token_file=str(self.tmp / "tok")))
        stripped = {k: v for k, v in GOOD_KB.items()
                    if k != "credential_env"}
        with self.assertRaisesRegex(KnowledgeConfigError, "exactly one"):
            self.load(stripped)

    def test_bad_credential_env_name(self):
        with self.assertRaises(KnowledgeConfigError):
            self.load(dict(GOOD_KB, credential_env="bad-name"))

    def test_token_file_must_be_absolute(self):
        stripped = {k: v for k, v in GOOD_KB.items()
                    if k != "credential_env"}
        with self.assertRaises(KnowledgeConfigError):
            self.load(dict(stripped, token_file="secrets/tok"))
        token_file = self.tmp / "tok.txt"
        target = self.load(dict(stripped, token_file=str(token_file)))
        self.assertEqual(target.token_file, token_file)

    def test_timeout_bounds(self):
        for bad in (0, -1, 121, "10", True):
            with self.assertRaises(KnowledgeConfigError, msg=repr(bad)):
                self.load(dict(GOOD_KB, timeout_s=bad))
        self.load(dict(GOOD_KB, timeout_s=0.5))
        self.load(dict(GOOD_KB, timeout_s=120))

    def test_max_response_bytes_bounds(self):
        for bad in (0, -1, 17 * 1024 * 1024, "big", 1.5, False):
            with self.assertRaises(KnowledgeConfigError, msg=repr(bad)):
                self.load(dict(GOOD_KB, max_response_bytes=bad))
        self.load(dict(GOOD_KB, max_response_bytes=256))
        self.load(dict(GOOD_KB, max_response_bytes=16 * 1024 * 1024))


class EnvCaptureTests(unittest.TestCase):
    """The env mapping is captured for later token lookups; loading never
    reads credential values (present or absent, the parse is identical)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="zmem-knfcfg-"))

    def test_env_recorded_not_consulted(self):
        path = knowledge_cfg(self.tmp, {"enabled": True,
                                        "targets": {"kb": dict(GOOD_KB)}})
        caller_env = {"ZMEM_KB_TOKEN": "x"}
        with_env = load_knowledge_config(path, env=caller_env)
        without_env = load_knowledge_config(path, env={})
        # The config captures the ACTUAL caller's mapping object (identity,
        # not a copy) so later credential lookups see the caller's view of
        # the environment; loading itself never reads the token value.
        self.assertIs(with_env.env, caller_env)
        self.assertEqual(with_env.targets["kb"].credential_env,
                         "ZMEM_KB_TOKEN")
        self.assertEqual(with_env.enabled, without_env.enabled)

    def test_source_path_recorded(self):
        path = knowledge_cfg(self.tmp, {"enabled": True, "targets": {}})
        cfg = load_knowledge_config(path)
        self.assertEqual(cfg.source_path, path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
