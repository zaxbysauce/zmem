"""Knowledge companion display policy helper (PR2) - unit + CLI tests.

Drives skills/memory/scripts/knowledge/policy.py as a STANDALONE module:
loaded by file path (no package, no storelib, no PR1 files required - the
directory holds only this helper in the PR2 worktree).  Pins:
- client-side config path resolution precedence (ZMEM_STORE > ZMEM_DATA >
  plugin vars > home probes) and the issue-#291 file-shaped refusal;
- strict default-off semantics: missing / malformed / oversized / BOM /
  non-boolean enabled / no display-safe ids all render nothing;
- fail-closed AS A WHOLE (FB-023): one invalid shape anywhere in an
  existing config - bad kind, duplicate JSON key, unknown key, malformed
  UTF-8, non-boolean enabled, non-object target - suppresses the ENTIRE
  render, never a partial roster, with no id or value fragments in any
  output;
- ids-only rendering (target values never read or shown), display cap;
- < 200 word deterministic text in both capture modes;
- read-only CLI (--text/--json/--config/--no-capture, no config writes);
- hygiene: closed stdlib import set, no write-mode file opens, no
  sqlite/network/storelib imports at runtime.
"""
import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY = (REPO_ROOT / "skills" / "memory" / "scripts" / "knowledge"
          / "policy.py")

# The PR2 worktree ships policy.py WITHOUT the PR1 package files; loading
# by path keeps that independence explicit.
_SPEC = importlib.util.spec_from_file_location("zmem_knowledge_policy_test",
                                               POLICY)
policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(policy)


def _write_config(directory, payload, raw=None):
    path = Path(directory) / "config.json"
    if raw is not None:
        path.write_bytes(raw)
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _knowledge_config(enabled=True, targets=None):
    return {"knowledge": {"enabled": enabled, "targets": targets or {}}}


def _markdown_targets(*ids):
    # Portable absolute roots (tempdir-derived, valid-looking on Windows
    # AND Linux).  The display loader never validates root VALUES (shape
    # only), so the roots need not exist - but portable fixtures keep the
    # configs plausible wherever the suite runs (CI-002 fixture family).
    base = Path(tempfile.gettempdir()).resolve()
    return {tid: {"kind": "markdown",
                  "root": str(base / ("vault-%s" % tid))}
            for tid in ids}


def _kbhttp_target(**overrides):
    target = {"kind": "kb-http",
              "endpoint": "https://kb.internal.example:8443/api/notes",
              "credential_env": "ZMEM_KB_FLEET_TOKEN"}
    target.update(overrides)
    return target


class ScratchDirTest(unittest.TestCase):
    def _scratch(self):
        base = Path(tempfile.mkdtemp(prefix="zmem-kp-"))
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        return base


class ResolveConfigPathTest(ScratchDirTest):
    """Client-side resolution mirrors the store's own chain."""

    def test_zmem_store_wins(self):
        base = self._scratch()
        data = base / "data"
        data.mkdir()
        store = base / "elsewhere" / "store.sqlite"
        resolved = policy.resolve_config_path(
            {"ZMEM_STORE": str(store), "ZMEM_DATA": str(data)})
        expected = os.path.join(
            os.path.dirname(os.path.realpath(str(store))), "config.json")
        self.assertEqual(resolved, expected)

    def test_dir_vars_in_precedence_order(self):
        base = self._scratch()
        a, b, c = (base / name for name in ("a", "b", "c"))
        for d in (a, b, c):
            d.mkdir()

        def cfg(d):
            # The config sits beside <dir>/store.sqlite with symlinks in
            # the dir prefix resolved (storelib parity).
            return os.path.join(os.path.realpath(str(d)), "config.json")
        self.assertEqual(policy.resolve_config_path({"ZMEM_DATA": str(a)}),
                         cfg(a))
        self.assertEqual(
            policy.resolve_config_path({"CLAUDE_PLUGIN_DATA": str(b)}),
            cfg(b))
        self.assertEqual(
            policy.resolve_config_path({"ZCODE_PLUGIN_DATA": str(c)}),
            cfg(c))
        # ZMEM_DATA outranks the plugin vars when several are set.
        self.assertEqual(
            policy.resolve_config_path({"ZMEM_DATA": str(a),
                                        "ZCODE_PLUGIN_DATA": str(c)}),
            cfg(a))

    def test_file_shaped_dir_var_refused_without_fallback(self):
        # Issue #291 parity: ZMEM_DATA naming an existing FILE is refused -
        # "" and never a silent fall-through to the next limb.
        base = self._scratch()
        victim = base / "not-a-dir"
        victim.write_text("x", encoding="utf-8")
        good = base / "good"
        good.mkdir()
        self.assertEqual(
            policy.resolve_config_path({"ZMEM_DATA": str(victim),
                                        "CLAUDE_PLUGIN_DATA": str(good)}),
            "")

    def test_home_default_and_legacy_manual(self):
        base = self._scratch()
        home = base / "home"
        home.mkdir()
        real_home = os.path.realpath(str(home))
        # No store anywhere: the box-wide default is ~/.zmem.
        self.assertEqual(
            policy.resolve_config_path({}, home=str(home)),
            os.path.join(real_home, ".zmem", "config.json"))
        # A legacy manual store outranks the (absent) box-wide default.
        legacy = home / ".zcode" / "memory"
        legacy.mkdir(parents=True)
        (legacy / "store.sqlite").write_bytes(b"")
        self.assertEqual(
            policy.resolve_config_path({}, home=str(home)),
            os.path.join(os.path.realpath(str(legacy)), "config.json"))

    def test_legacy_plugin_probe_newest_wins(self):
        base = self._scratch()
        home = base / "home"
        plugins = home / ".zcode" / "cli" / "plugins" / "data"
        old = plugins / "zmem-old"
        new = plugins / "zmem-new"
        for d in (old, new):
            d.mkdir(parents=True)
            (d / "store.sqlite").write_bytes(b"")
        os.utime(str(old / "store.sqlite"), (1, 1))
        os.utime(str(new / "store.sqlite"), (2_000_000_000, 2_000_000_000))
        self.assertEqual(
            policy.resolve_config_path({}, home=str(home)),
            os.path.join(os.path.realpath(str(new)), "config.json"))


class LoadPolicyStateTest(ScratchDirTest):
    def _state(self, directory, raw=None, payload=None):
        path = _write_config(directory, payload if payload is not None
                             else _knowledge_config(True,
                                                    _markdown_targets("notes")),
                             raw=raw)
        return policy.load_policy_state(path)

    def test_missing_file_is_default_off(self):
        base = self._scratch()
        state = policy.load_policy_state(str(base / "config.json"))
        self.assertFalse(state["enabled"])
        self.assertEqual(state["reason"], "missing")
        self.assertEqual(state["targets"], [])

    def test_directory_path_is_missing(self):
        base = self._scratch()
        state = policy.load_policy_state(str(base))
        self.assertFalse(state["enabled"])
        self.assertEqual(state["reason"], "missing")

    def test_malformed_and_non_object_fail_closed(self):
        base = self._scratch()
        self.assertEqual(self._state(base, raw=b"{ not json")["reason"],
                         "malformed")
        self.assertEqual(self._state(base, raw=b"[1, 2]")["reason"],
                         "malformed")
        self.assertEqual(
            self._state(base, payload={"knowledge": ["nope"]})["reason"],
            "malformed")

    def test_oversized_config_rejected(self):
        base = self._scratch()
        big = dict(_knowledge_config(True, _markdown_targets("notes")))
        big["padding"] = "x" * (policy.MAX_CONFIG_BYTES + 1024)
        state = self._state(base, payload=big)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["reason"], "oversized")

    def test_bom_prefixed_valid_config_enables(self):
        base = self._scratch()
        raw = b"\xef\xbb\xbf" + json.dumps(
            _knowledge_config(True, _markdown_targets("notes"))
        ).encode("utf-8")
        state = self._state(base, raw=raw)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["targets"], ["notes"])

    def test_no_knowledge_key_is_default_off(self):
        base = self._scratch()
        state = self._state(base, payload={"auto_retain": True})
        self.assertFalse(state["enabled"])
        self.assertEqual(state["reason"], "no-knowledge")

    def test_enabled_must_be_strict_json_true(self):
        base = self._scratch()
        self.assertEqual(
            self._state(base, payload=_knowledge_config(False))["reason"],
            "disabled")
        for bad in ("true", 1, None, [True]):
            payload = {"knowledge": {"targets": _markdown_targets("notes")}}
            if bad is not None:
                payload["knowledge"]["enabled"] = bad
            state = self._state(base, payload=payload)
            self.assertFalse(state["enabled"], "enabled=%r must not pass"
                             % (bad,))
            self.assertEqual(state["reason"], "invalid-enabled")

    def test_targets_must_be_an_object(self):
        base = self._scratch()
        state = self._state(base, payload={
            "knowledge": {"enabled": True, "targets": ["notes"]}})
        self.assertFalse(state["enabled"])
        self.assertEqual(state["reason"], "invalid-targets")

    def test_zero_targets_enabled_but_not_renderable(self):
        base = self._scratch()
        state = self._state(base, payload=_knowledge_config(True, {}))
        self.assertTrue(state["enabled"])
        self.assertEqual(state["targets"], [])
        self.assertEqual(state["reason"], "no-targets")
        self.assertEqual(
            policy.render_policy(str(base / "config.json")), "")

    def test_unsafe_ids_refuse_whole_render_not_dropped(self):
        # FB-023 fail-closed-as-a-whole: a non-slug id used to be silently
        # DROPPED from the roster (and counted in targets_omitted); now ONE
        # invalid id suppresses the ENTIRE render - the real validator
        # (knowledge/config.py) refuses the same config, so no partial
        # roster may advertise it.
        base = self._scratch()
        raw_targets = dict(_markdown_targets("notes", "fleet-2"))
        raw_targets["Bad Key"] = {"kind": "markdown", "root": "x"}
        path = _write_config(base, _knowledge_config(True, raw_targets))
        state = policy.load_policy_state(path)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["reason"], "invalid-config")
        self.assertEqual(state["targets"], [])
        self.assertEqual(state["targets_omitted"], 0)
        self.assertEqual(policy.render_policy(path), "")
        status = json.dumps(policy.policy_status(path))
        self.assertNotIn("notes", status)
        self.assertNotIn("fleet-2", status)

    def test_display_cap_counts_omitted(self):
        # All-VALID ids: the "(+N not shown)" note counts only ids beyond
        # the display cap (invalid keys never reach this count - they
        # refuse the whole render instead).
        base = self._scratch()
        ids = ["t-%02d" % i for i in range(12)]
        path = _write_config(base, _knowledge_config(
            True, _markdown_targets(*ids)))
        state = policy.load_policy_state(path)
        self.assertEqual(state["targets"], ids[:policy.MAX_TARGET_IDS_DISPLAY])
        self.assertEqual(state["targets_omitted"], 4)
        text = policy.render_policy(path)
        self.assertIn("(+4 not shown)", text)
        for tid in ids[:policy.MAX_TARGET_IDS_DISPLAY]:
            self.assertIn(tid, text)
        self.assertNotIn("t-08", text)

    def test_refused_resolution_yields_no_policy(self):
        base = self._scratch()
        victim = base / "file"
        victim.write_text("x", encoding="utf-8")
        state = policy.load_policy_state(
            None, {"ZMEM_DATA": str(victim)})
        self.assertFalse(state["enabled"])
        self.assertEqual(state["reason"], "no-config-path")

    def test_explicit_empty_path_is_refused_not_defaulted(self):
        # "" means "explicitly given, unusable" - never a silent fallback
        # to default resolution, which could consult an unrelated enabled
        # config (reviewer nit).
        self.assertEqual(policy.render_policy(""), "")
        status = policy.policy_status("")
        self.assertFalse(status["enabled"])
        self.assertEqual(status["reason"], "no-config-path")
        self.assertEqual(status["targets"], [])
        self.assertEqual(status["text"], "")


class FailClosedWholeConfigTest(ScratchDirTest):
    """FB-023: display fails closed AS A WHOLE.

    One invalid shape anywhere in an EXISTING config suppresses the
    ENTIRE render (bounded disabled-style output) - never a partial
    roster of the valid-looking siblings, and never a target id or value
    fragment in any output (ids-only per the existing contract; refusals
    are content-free).
    """

    def _refused(self, payload=None, raw=None, fragments=()):
        """Write the config, render it both ways, assert whole refusal."""
        base = self._scratch()
        # Portable scratch-derived root so the VALID sibling target is
        # plausible on every OS (the loader never validates values).
        valid = {"kind": "markdown", "root": str(base / "vault-notes")}
        if payload is not None:
            targets = payload.setdefault(
                "knowledge", {}).setdefault("targets", {})
            if "fleet" in targets and targets["fleet"] == "VALID":
                targets["fleet"] = valid
            if "notes" in targets and targets["notes"] == "VALID":
                targets["notes"] = valid
        path = _write_config(base, payload or {}, raw=raw)
        for allow_capture in (True, False):
            self.assertEqual(
                policy.render_policy(path, allow_capture=allow_capture), "",
                "an invalid config must render nothing in either mode")
        status = policy.policy_status(path)
        self.assertFalse(status["enabled"])
        self.assertEqual(status["targets"], [])
        self.assertEqual(status["targets_omitted"], 0)
        self.assertEqual(status["text"], "")
        self.assertIn(status["reason"],
                      ("malformed", "invalid-config", "invalid-enabled",
                       "invalid-targets"))
        # No id or value fragments anywhere in the bounded output.
        status_json = json.dumps(status)
        for fragment in fragments:
            self.assertNotIn(fragment, status_json,
                             "refusal leaked %r" % (fragment,))
        return status

    def test_bad_kind_suppresses_whole_render(self):
        status = self._refused(payload={"knowledge": {"enabled": True, "targets": {
            "notes": {"kind": "bogus", "root": "/vault"},
            "fleet": "VALID"}}},
            fragments=("notes", "fleet", "bogus", "vault"))
        self.assertEqual(status["reason"], "invalid-config")

    def test_unknown_target_key_suppresses_whole_render(self):
        self._refused(payload={"knowledge": {"enabled": True, "targets": {
            "notes": {"kind": "markdown", "root": "/vault", "extra": 1},
            "fleet": "VALID"}}},
            fragments=("notes", "fleet"))

    def test_unknown_knowledge_key_suppresses_whole_render(self):
        self._refused(payload={"knowledge": {"enabled": True,
                                             "typo": True,
                                             "targets": {"notes": "VALID"}}},
                      fragments=("notes", "typo"))

    def test_non_object_target_suppresses_whole_render(self):
        self._refused(payload={"knowledge": {"enabled": True, "targets": {
            "notes": "markdown", "fleet": "VALID"}}},
            fragments=("notes", "fleet"))

    def test_missing_kind_suppresses_whole_render(self):
        self._refused(payload={"knowledge": {"enabled": True, "targets": {
            "notes": {"root": "/vault"}, "fleet": "VALID"}}},
            fragments=("notes", "fleet"))

    def test_non_string_root_suppresses_whole_render(self):
        self._refused(payload={"knowledge": {"enabled": True, "targets": {
            "notes": {"kind": "markdown", "root": 7},
            "fleet": "VALID"}}},
            fragments=("notes", "fleet"))

    def test_kbhttp_field_shapes_suppress_whole_render(self):
        bad_specs = (
            {"kind": "kb-http"},  # no endpoint, no credential location
            {"kind": "kb-http", "endpoint": 5, "credential_env": "T_OK"},
            {"kind": "kb-http", "endpoint": "  ",
             "credential_env": "T_OK"},
            {"kind": "kb-http", "endpoint": "https://kb.example.invalid/n",
             "allow_http": "yes", "credential_env": "T_OK"},
            {"kind": "kb-http", "endpoint": "https://kb.example.invalid/n",
             "credential_env": "T_OK", "token_file": "/both/creds"},
            {"kind": "kb-http", "endpoint": "https://kb.example.invalid/n",
             "credential_env": "T_OK", "timeout_s": "fast"},
            {"kind": "kb-http", "endpoint": "https://kb.example.invalid/n",
             "credential_env": "T_OK", "max_response_bytes": 1.5},
            {"kind": "kb-http", "endpoint": "https://kb.example.invalid/n",
             "credential_env": 42},
        )
        for spec in bad_specs:
            status = self._refused(payload={"knowledge": {
                "enabled": True,
                "targets": {"fleet": spec, "notes": "VALID"}}},
                fragments=("fleet", "notes", "kb.example.invalid"))
            self.assertEqual(status["reason"], "invalid-config",
                             "spec=%r" % (spec,))

    def test_valid_kbhttp_still_renders(self):
        # Counter-mutant: a VALID kb-http spec (exactly one credential
        # location, optional knobs well-typed) must keep rendering - the
        # shape mirror is validator-subset, not validator-superset.
        base = self._scratch()
        payload = {"knowledge": {"enabled": True, "targets": {
            "fleet": _kbhttp_target(timeout_s=10, max_response_bytes=4096),
            "notes": {"kind": "markdown", "root": str(base / "vault")}}}}
        path = _write_config(base, payload)
        state = policy.load_policy_state(path)
        self.assertEqual(state["reason"], "enabled")
        self.assertEqual(state["targets"], ["fleet", "notes"])
        self.assertIn("fleet", policy.render_policy(path))

    def test_malformed_utf8_suppresses_whole_render(self):
        # Strict decode (no replace-decode): undecodable bytes anywhere
        # refuse the whole file.
        raw = (b'{"knowledge": {"enabled": true, "targets": {"notes": '
               b'{"kind": "markdown", "root": "D:\xff\xfe"}}}}')
        status = self._refused(raw=raw, fragments=("notes",))
        self.assertEqual(status["reason"], "malformed")

    def test_duplicate_keys_suppress_whole_render(self):
        # Stdlib json keeps the LAST duplicate silently; the display lane
        # refuses the whole file (object_pairs_hook detection) wherever
        # the duplicate sits - top level, knowledge object, or a target
        # spec.
        raws = (
            b'{"knowledge": {"enabled": true, "enabled": false, '
            b'"targets": {"notes": {"kind": "markdown", "root": "x"}}}}',
            b'{"auto_retain": false, "auto_retain": true, "knowledge": '
            b'{"enabled": true, "targets": {}}}',
            b'{"knowledge": {"enabled": true, "targets": {"notes": '
            b'{"kind": "markdown", "kind": "kb-http", "root": "x"}}}}',
        )
        for raw in raws:
            status = self._refused(raw=raw, fragments=("notes",))
            self.assertEqual(status["reason"], "malformed", "raw=%r" % raw)


# Byte pins for the VALID-config render (regression pin for the
# fail-closed work: valid configs must keep rendering byte-for-byte;
# only a deliberate wording fix - like the FB-030 whole-queue flush
# truth - ever changes these, never incidentally).
_GOLDEN_CAPTURE = (
    "# ZMem knowledge companion (operator-enabled external notes)\n"
    "\n"
    "Configured target ids: notes, fleet.\n"
    "\n"
    "Targets hold durable reference notes you explicitly author, curate,\n"
    "and attach evidence to - never raw transcripts, never secrets. Keep\n"
    "routine session lessons in zmem memory (compact, private); targets\n"
    "store long-lived notes outside zmem (Markdown vault or KB endpoint;\n"
    "access follows the target's own permissions; Obsidian is optional,\n"
    "never required).\n"
    "\n"
    "Invoke via the configured Python interpreter and memory store CLI.\n"
    "Read: `knowledge read --target <id> --key <key> --namespace <ns>`,\n"
    "verify with `knowledge check`; `knowledge status` shows pointers.\n"
    "Derive the namespace per the memory skill; always pass `--namespace`\n"
    "(the default puts project pointers in user:global).\n"
    "\n"
    "Publish explicitly, never automatically: screen for secrets (capture\n"
    "rejects them), then `knowledge capture --target <id> --key <key>\n"
    "--namespace <ns> --content <text|->`; at a closeout the user\n"
    "requested in this session, store relevant explicit notes\n"
    "(agent/reminder-initiated closeouts draft only, no capture). Exit 3\n"
    "= pending: inspect `knowledge status`; `knowledge flush` drains the\n"
    "whole pending queue (no session filter), else re-run the same capture\n"
    "input (idempotent).\n"
    "\n"
    "Full workflow and limits: the \"knowledge\" skill (memory skill "
    "knowledge section)."
)

_GOLDEN_READONLY = (
    "# ZMem knowledge companion (operator-enabled external notes)\n"
    "\n"
    "Configured target ids: notes, fleet.\n"
    "\n"
    "Targets hold durable reference notes you explicitly author, curate,\n"
    "and attach evidence to - never raw transcripts, never secrets. Keep\n"
    "routine session lessons in zmem memory (compact, private); targets\n"
    "store long-lived notes outside zmem (Markdown vault or KB endpoint;\n"
    "access follows the target's own permissions; Obsidian is optional,\n"
    "never required).\n"
    "\n"
    "This session is read-only for targets (publishing disabled): do not\n"
    "write, create, or capture notes into targets. Read via the configured\n"
    "Python interpreter: `knowledge read --target <id> --key <key>\n"
    "--namespace <ns>`, verify with `knowledge check`; `knowledge status`\n"
    "shows pointers. Derive the namespace per the memory skill and always\n"
    "pass `--namespace`.\n"
    "\n"
    "Full workflow and limits: the \"knowledge\" skill (memory skill "
    "knowledge section)."
)


class RenderPolicyTest(ScratchDirTest):
    def _config(self, targets=None):
        base = self._scratch()
        return str(_write_config(base, _knowledge_config(
            True, targets if targets is not None
            else _markdown_targets("notes", "fleet"))))

    def test_deterministic_and_mode_dependent(self):
        path = self._config()
        capture = policy.render_policy(path, allow_capture=True)
        readonly = policy.render_policy(path, allow_capture=False)
        self.assertEqual(capture, policy.render_policy(path,
                                                       allow_capture=True))
        self.assertNotEqual(capture, readonly)

    def test_valid_config_render_regression_pin(self):
        # (f) regression pin: a VALID config renders byte-for-byte the
        # golden text in both modes - the fail-closed work must never
        # perturb valid rendering (deliberate wording fixes update the
        # golden, e.g. the FB-030 whole-queue flush truth).
        path = self._config()
        self.assertEqual(policy.render_policy(path, allow_capture=True),
                         _GOLDEN_CAPTURE)
        self.assertEqual(policy.render_policy(path, allow_capture=False),
                         _GOLDEN_READONLY)

    def test_word_cap_both_modes_at_display_max(self):
        ids = ["t-%02d" % i for i in range(16)]  # 8 shown + 8 omitted
        path = self._config(_markdown_targets(*ids))
        for allow_capture in (True, False):
            text = policy.render_policy(path, allow_capture=allow_capture)
            words = len(text.split())
            self.assertGreater(words, 60,
                               "policy must be substantive, not empty")
            self.assertLess(words, policy.MAX_POLICY_WORDS,
                            "policy must stay under the %d-word cap"
                            % policy.MAX_POLICY_WORDS)

    def test_capture_mode_content_pins(self):
        # Whitespace-normalized so a template line-wrap can never break a
        # semantic pin (parent-run log call_B4UABqaksnJRfbDdXIqVUR7G).
        flat = " ".join(policy.render_policy(self._config(),
                                             allow_capture=True).split())
        for needle in ("knowledge read --target", "knowledge check",
                       "knowledge status", "knowledge capture",
                       "--namespace",
                       "configured Python interpreter",
                       "Obsidian is optional",
                       "author", "curate", "evidence", "secrets",
                       "notes", "fleet", "idempotent"):
            self.assertIn(needle, flat)
        # zmem compact memory vs external targets distinction is taught.
        self.assertIn("zmem memory", flat)
        # Exit-3 recovery guidance names the ACTUAL subcommand — a bare
        # `flush` was ambiguous (feedback FB-005): the CLI is `knowledge
        # flush`, and it drains the WHOLE pending queue (no session
        # filter exists — FB-030 truth pass).
        self.assertIn("knowledge flush` drains the whole pending queue",
                      flat)
        # The workflow pointer names the portable skill.
        self.assertIn('"knowledge" skill', flat)
        # Closeout capture is scoped to a USER-requested closeout in this
        # session; agent/reminder-initiated closeouts draft only (final
        # critic a36b: the existing hooks auto-remind, so an unqualified
        # closeout line would imply autonomous capture authority).
        self.assertIn("at a closeout the user requested in this session",
                      flat)
        self.assertIn("draft only, no capture", flat)

    def test_readonly_mode_teaches_reads_without_write_invitation(self):
        text = policy.render_policy(self._config(), allow_capture=False)
        flat = " ".join(text.split())
        for needle in ("knowledge read --target", "knowledge check",
                       "knowledge status", "--namespace",
                       "publishing disabled",
                       "configured Python interpreter"):
            self.assertIn(needle, flat)
        self.assertNotIn("knowledge capture", text,
                         "no write invitation when capture is disabled")
        self.assertNotIn("capture --target", text)
        self.assertNotIn("flush", text)
        # The read-only variant carries no closeout capture qualifier at
        # all: it teaches reads only, nothing about publishing moments.
        self.assertNotIn("closeout", text.lower())

    def test_target_values_never_rendered(self):
        base = self._scratch()
        targets = {
            "notes": {"kind": "markdown",
                      "root": str(base / "super-secret-vault")},
            "fleet": {"kind": "kb-http",
                      "endpoint": "https://kb.internal.example:8443/api/notes",
                      "credential_env": "ZMEM_KB_FLEET_TOKEN"},
        }
        path = str(_write_config(base, _knowledge_config(True, targets)))
        for allow_capture in (True, False):
            text = policy.render_policy(path, allow_capture=allow_capture)
            self.assertNotIn("super-secret-vault", text)
            self.assertNotIn("kb.internal.example", text)
            self.assertNotIn("8443", text)
            self.assertNotIn("ZMEM_KB_FLEET_TOKEN", text)
            status = json.dumps(policy.policy_status(
                path, allow_capture=allow_capture))
            self.assertNotIn("super-secret-vault", status)
            self.assertNotIn("kb.internal.example", status)
            self.assertNotIn("ZMEM_KB_FLEET_TOKEN", status)

    def test_disabled_states_render_nothing(self):
        base = self._scratch()
        cases = [
            {"knowledge": {"enabled": False,
                           "targets": _markdown_targets("notes")}},
            {"knowledge": {"enabled": "true",
                           "targets": _markdown_targets("notes")}},
            {"knowledge": {"enabled": True, "targets": {}}},
            {"knowledge": {"enabled": True, "targets": {
                "not a slug": {"kind": "markdown",
                               "root": str(base / "vault")}}}},
            {"something_else": True},
        ]
        for payload in cases:
            path = _write_config(base, payload)
            self.assertEqual(policy.render_policy(path), "",
                             "payload %r must render nothing" % (payload,))
        # Oversized / malformed / missing likewise.
        path = base / "big.json"
        path.write_bytes(b'{"knowledge":{"enabled":true},"pad":"'
                         + b"x" * (policy.MAX_CONFIG_BYTES + 10) + b'"}')
        self.assertEqual(policy.render_policy(str(path)), "")
        (base / "broken.json").write_bytes(b"{ nope")
        self.assertEqual(policy.render_policy(str(base / "broken.json")), "")

    def test_status_shape(self):
        path = self._config()
        status = policy.policy_status(path, allow_capture=True)
        self.assertEqual(
            set(status), {"config_path", "enabled", "reason", "targets",
                          "targets_omitted", "capture", "policy_version",
                          "word_count", "text"})
        self.assertTrue(status["enabled"])
        self.assertEqual(status["reason"], "enabled")
        self.assertEqual(status["targets"], ["notes", "fleet"])
        self.assertEqual(status["capture"], True)
        self.assertEqual(status["policy_version"], policy.POLICY_VERSION)
        self.assertEqual(status["word_count"],
                         len(status["text"].split()))
        self.assertEqual(status["text"],
                         policy.render_policy(path, allow_capture=True))


def _cli_env(**extra):
    """Minimal, hermetic child env: no inherited ZMEM_* switches."""
    keep = ("PATH", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "COMSPEC",
            "TEMP", "TMP")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra)
    return env


class CliTest(ScratchDirTest):
    def _run(self, *args, env=None, cwd=None):
        return subprocess.run(
            [sys.executable, str(POLICY), *args],
            capture_output=True, env=env, cwd=cwd or str(REPO_ROOT),
            timeout=120)

    def _enabled_dir(self):
        base = self._scratch()
        _write_config(base, _knowledge_config(
            True, _markdown_targets("notes", "fleet")))
        return base

    def test_text_prints_policy_and_nothing_on_stderr(self):
        base = self._enabled_dir()
        proc = self._run("--config", str(base / "config.json"), "--text",
                         env=_cli_env())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        expected = policy.render_policy(str(base / "config.json")) + "\n"
        self.assertEqual(proc.stdout.decode("utf-8"), expected)

    def test_text_disabled_prints_nothing_exit_zero(self):
        base = self._scratch()
        _write_config(base, _knowledge_config(False))
        proc = self._run("--config", str(base / "config.json"), "--text",
                         env=_cli_env())
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")
        self.assertEqual(proc.stderr, b"")

    def test_json_status_shape_and_capture_flag(self):
        base = self._enabled_dir()
        for extra, capture in (({}, True), ({"ZMEM_CAPTURE": "0"}, True)):
            env = _cli_env(**extra)
            env["ZMEM_DATA"] = str(base)
            proc = self._run("--json", env=env)
            self.assertEqual(proc.returncode, 0)
            status = json.loads(proc.stdout.decode("utf-8"))
            self.assertEqual(status["enabled"], True)
            self.assertEqual(status["targets"], ["notes", "fleet"])
            self.assertEqual(status["capture"], capture)  # env never read
        proc = self._run("--config", str(base / "config.json"),
                         "--json", "--no-capture", env=_cli_env())
        status = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(status["capture"], False)
        self.assertNotIn("knowledge capture", status["text"])

    def test_explicit_config_path_needs_no_env(self):
        # No ZMEM_* in the child env at all: the explicit path wins.
        base = self._enabled_dir()
        proc = self._run("--config", str(base / "config.json"), "--text",
                         env=_cli_env())
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"knowledge companion", proc.stdout)

    def test_env_resolution_when_no_config_flag(self):
        base = self._enabled_dir()
        env = _cli_env(ZMEM_DATA=str(base))
        proc = self._run("--text", env=env)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"knowledge companion", proc.stdout)

    def test_no_config_writes(self):
        base = self._enabled_dir()
        (base / "ops-marker.txt").write_text("untouched", encoding="utf-8")

        def snapshot():
            return {str(p.relative_to(base)): p.read_bytes()
                    for p in sorted(base.rglob("*")) if p.is_file()}
        before = snapshot()
        for args in (["--config", str(base / "config.json"), "--text"],
                     ["--config", str(base / "config.json"), "--json"],
                     ["--text"]):
            self._run(*args, env=_cli_env(ZMEM_DATA=str(base)))
        self.assertEqual(snapshot(), before,
                         "the CLI must never write inside the config dir")

    def test_explicit_empty_config_refused_without_fallback(self):
        # `--config ""` must disable with reason no-config-path; the old
        # CLI collapsed it to default resolution (reviewer nit).
        for mode in ("--text", "--json"):
            proc = self._run("--config", "", mode, env=_cli_env())
            self.assertEqual(proc.returncode, 0, "args=%r" % (mode,))
            if mode == "--text":
                self.assertEqual(proc.stdout, b"")
            else:
                status = json.loads(proc.stdout.decode("utf-8"))
                self.assertFalse(status["enabled"])
                self.assertEqual(status["reason"], "no-config-path")
                self.assertEqual(status["targets"], [])
                self.assertEqual(status["text"], "")

    def test_missing_config_value_is_usage_error_no_fallback(self):
        # FB-030: a missing --config value - end of argv, or the next
        # flag swallowed as the path (`--config --json` used to consume
        # --json and render rc 0) - exits 2 with empty stdout and NEVER
        # falls back to default resolution.  ZMEM_DATA points at an
        # ENABLED config, so a silent fallback would print a policy.
        base = self._enabled_dir()
        env = _cli_env(ZMEM_DATA=str(base))
        for args in (["--config"], ["--text", "--config"],
                     ["--config", "--json"], ["--config", "--text"],
                     ["--config", "--no-capture"],
                     ["--config", "--config", str(base / "config.json")]):
            proc = self._run(*args, env=env)
            self.assertEqual(proc.returncode, 2, "args=%r" % (args,))
            self.assertEqual(proc.stdout, b"", "args=%r" % (args,))
            self.assertNotEqual(proc.stderr, b"", "args=%r" % (args,))

    def test_usage_errors_exit_two(self):
        base = self._enabled_dir()
        env = _cli_env(ZMEM_DATA=str(base))
        for args in (["--bogus"], ["--text", "--json"],
                     ["--config"], ["--config", str(base / "config.json"),
                                    "--text", "extra"]):
            proc = self._run(*args, env=env)
            self.assertEqual(proc.returncode, 2, "args=%r" % (args,))
            self.assertEqual(proc.stdout, b"")
            self.assertNotEqual(proc.stderr, b"")

    def test_help_exit_zero(self):
        proc = self._run("--help", env=_cli_env())
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"--no-capture", proc.stdout)


class HygieneTest(unittest.TestCase):
    """The helper's import and I/O surface stays closed and read-only."""

    FORBIDDEN_MODULES = ("sqlite3", "socket", "ssl", "urllib.request",
                         "http.client", "subprocess", "ctypes", "argparse",
                         "asyncio", "storelib", "host", "requests")

    def test_closed_import_set(self):
        tree = ast.parse(POLICY.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0]
                                for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported.add(node.module.split(".")[0])
        self.assertEqual(imported, {"__future__", "glob", "json", "os",
                                    "re", "sys"},
                         "policy.py must keep its stdlib-only closed set")

    def test_file_opens_are_read_only(self):
        tree = ast.parse(POLICY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                is_open = node.func.id == "open"
            elif isinstance(node.func, ast.Attribute):
                # CI-003-R1: an attribute open — something.open(...),
                # a.b.open(...) — is a file open too; the read-only gate
                # must not be satisfiable by switching a bare open() to a
                # bound/attribute call shape.
                is_open = node.func.attr == "open"
            else:
                continue
            if not is_open:
                continue
            mode = node.args[1].value if len(node.args) > 1 else None
            self.assertEqual(mode, "rb",
                             "policy.py opens files read-only ('rb')")

    def test_runtime_imports_nothing_heavy(self):
        # Import + render from an unrelated cwd with an empty PYTHONPATH:
        # proves PR1-independence (no storelib) and no sqlite/network pulls.
        with tempfile.TemporaryDirectory(prefix="zmem-kp-hygiene-") as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps(
                _knowledge_config(True, _markdown_targets("notes"))),
                encoding="utf-8")
            code = (
                "import importlib.util, os, sys\n"
                "spec = importlib.util.spec_from_file_location("
                "'kp_under_test', os.environ['KP_POLICY'])\n"
                "mod = importlib.util.module_from_spec(spec)\n"
                "spec.loader.exec_module(mod)\n"
                "assert mod.render_policy(os.environ['KP_CONFIG'])\n"
                "bad = [n for n in %r if n in sys.modules]\n"
                "print(','.join(bad))\n" % (self.FORBIDDEN_MODULES,))
            env = _cli_env(KP_POLICY=str(POLICY), KP_CONFIG=str(config))
            proc = subprocess.run(
                [sys.executable, "-c", code], capture_output=True,
                env=env, cwd=tmp, timeout=120)
            self.assertEqual(proc.returncode, 0,
                             proc.stderr.decode("utf-8", "replace"))
            self.assertEqual(proc.stdout.decode("utf-8").strip(), "",
                             "forbidden modules must never load")


if __name__ == "__main__":
    unittest.main()
