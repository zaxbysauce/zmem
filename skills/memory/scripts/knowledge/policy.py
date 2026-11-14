#!/usr/bin/env python
"""Standalone stdlib display policy for the knowledge companion (PR2).

PR1 ships the OFF-BY-DEFAULT knowledge companion: nothing runs, captures,
publishes, or injects unless an operator adds a strict ``knowledge`` object
to the store's ``config.json`` AND a knowledge command is explicitly
invoked (by the operator or an authorized agent workflow) - never
autonomously.  This module is PR2's single shared loader for the
*conditional discovery policy* - the short instruction that tells a
session the feature exists, which target IDS are configured, and where the
workflow lives.

Hard boundaries (approved plan, 2026-11-13):
- STDLIB ONLY, closed import set (``glob``, ``json``, ``os``, ``re``,
  ``sys``): never imports ``storelib``/``host``, never opens SQLite, never
  touches the network, never stats target roots, never writes anywhere.
- The config file is read BOUNDED (<= 64 KiB) and BOM-tolerant; decoding
  is STRICT UTF-8 - undecodable bytes refuse the whole render (display
  fail-closed; storelib.config's replace-decode is a store-lane behavior,
  not a display one).
- ``knowledge.enabled`` must be JSON ``true`` (strict) or nothing renders.
- TARGET IDS ONLY: the rendered policy lists target id keys that match the
  shared slug shape (1..24 chars, ``[a-z0-9-]``, no leading/trailing
  hyphen).  Target VALUES (roots, endpoints, credential locations) are
  never read, never rendered.
- DISPLAY IS FAIL-CLOSED AS A WHOLE: a config file that exists but is
  undecodable, carries a duplicate JSON key, an unknown knowledge/target
  key, or any invalid target kind/spec or field SHAPE suppresses the
  ENTIRE render (treated as disabled) - a partially-rendered roster would
  advertise a config that PR1's real validator
  (``skills/memory/scripts/knowledge/config.py``, the authority a command
  invocation actually hits) refuses to load.  The shape mirror is
  structure-only (key sets, kinds, JSON field types); value judgments
  (absolute roots, URL grammar, numeric ranges) stay with the validator,
  so a host-local path shape still renders on every host.  Refusals are
  silent-to-display and content-free: no target ids, no values, no file
  content in any reason string.
- The rendered text is < 200 words and deterministic for
  (targets, allow_capture).
- The ``ZMEM_INJECT`` / ``ZMEM_CAPTURE`` kill switches are CALLER policy:
  callers gate injection themselves and pass ``allow_capture`` (the CLI
  flag is ``--no-capture``).  This module never reads those variables.

Config path resolution is CLIENT-SIDE and local: the same store-dir chain
the store uses (``ZMEM_STORE`` > ``ZMEM_DATA`` > ``CLAUDE_PLUGIN_DATA`` >
``ZCODE_PLUGIN_DATA`` > ``~/.zmem`` > legacy probes), never a hub fetch -
in remote mode (``ZMEM_REMOTE_URL``) the operator's local config.json
still decides.  A directory variable naming an existing regular file is
REFUSED (issue #291 parity): resolution returns ``""`` rather than falling
through to an unrelated config location.

CLI (read-only, no config writes, no stderr noise):

    policy.py [--config PATH] [--no-capture] (--text | --json)

An EXPLICIT empty ``--config`` value is honored as a refusal (disabled,
reason ``no-config-path``) - it never falls back to default resolution,
which could silently consult an unrelated, possibly enabled config.  A
MISSING ``--config`` value - end of arguments, or the next flag where a
path was expected (``--config --json`` used to swallow ``--json`` as the
path) - is a usage error: exit 2, empty stdout, a stderr usage line, and
no config resolution at all.

``--text`` prints the policy or nothing (exit 0 either way - disabled is a
state, not an error).  ``--json`` always prints one status object with
``enabled``, ``reason``, ``targets``, ``targets_omitted``, ``capture``,
``policy_version``, ``word_count``, and ``text``.  Exit 2 is usage only.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys

__all__ = [
    "POLICY_VERSION",
    "MAX_CONFIG_BYTES",
    "MAX_POLICY_WORDS",
    "MAX_TARGET_IDS_DISPLAY",
    "resolve_config_path",
    "load_policy_state",
    "render_policy",
    "policy_status",
    "main",
]

POLICY_VERSION = 1

# Mirror storelib.config.MAX_CONFIG_BYTES: the store config.json itself is
# bounded, so a larger file is a broken state, not a policy input.
MAX_CONFIG_BYTES = 65536

# Hard cap on the rendered instruction (words).  The composed templates
# stay well below this; the guard exists so a future edit cannot ship an
# oversized policy silently - it renders nothing instead.
MAX_POLICY_WORDS = 200

# How many target ids the one-line roster shows.  The remainder are VALID
# ids beyond the cap, counted ("(+N not shown)") so the model knows more
# exist without spending words; invalid keys are never counted here - one
# invalid key refuses the whole render (fail-closed as a whole), it is not
# silently dropped from the roster.
MAX_TARGET_IDS_DISPLAY = 8

# The shared target-id slug shape (PR1 knowledge/config.py owns the
# validator; this display filter accepts exactly the same shape).
TARGET_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")

_DIR_ENV_VARS = ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")

# Fail-closed display parity with the PR1 validator (knowledge/config.py):
# the tables below mirror its STRUCTURE contract - the knowledge object's
# key set and each kind's target-spec key set.  What the validator checks
# about VALUES (absolute roots, URL grammar, numeric ranges, env-var name
# grammar) stays validator-only: those are host-local judgments this
# display lane never makes, so a value this filter cannot judge still
# renders exactly as the real CLI would accept it.
_KNOWLEDGE_KEYS = frozenset(("enabled", "targets"))
_TARGET_KEYS = {
    "markdown": frozenset(("kind", "root")),
    "kb-http": frozenset(("kind", "endpoint", "allow_http", "credential_env",
                          "token_file", "timeout_s", "max_response_bytes")),
}

_HEADING = "# ZMem knowledge companion (operator-enabled external notes)"

_SHARED_PARA = (
    "Targets hold durable reference notes you explicitly author, curate,\n"
    "and attach evidence to - never raw transcripts, never secrets. Keep\n"
    "routine session lessons in zmem memory (compact, private); targets\n"
    "store long-lived notes outside zmem (Markdown vault or KB endpoint;\n"
    "access follows the target's own permissions; Obsidian is optional,\n"
    "never required)."
)

_CAPTURE_PARA = (
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
    "input (idempotent)."
)

_READONLY_PARA = (
    "This session is read-only for targets (publishing disabled): do not\n"
    "write, create, or capture notes into targets. Read via the configured\n"
    "Python interpreter: `knowledge read --target <id> --key <key>\n"
    "--namespace <ns>`, verify with `knowledge check`; `knowledge status`\n"
    "shows pointers. Derive the namespace per the memory skill and always\n"
    "pass `--namespace`."
)

_FOOTER = ('Full workflow and limits: the "knowledge" skill (memory skill '
           "knowledge section).")

# Args that are always flags, never a --config VALUE: a missing value
# (end of argv, or the next flag where a path was expected) is a usage
# error, never a silent fallback to default config resolution (FB-030:
# `--config --json` used to swallow --json as the path and render rc 0).
_CONFIG_FLAGS = frozenset(("-h", "--help", "--config", "--no-capture",
                           "--text", "--json"))

_USAGE = (
    "usage: policy.py [--config PATH] [--no-capture] (--text | --json)\n"
    "\n"
    "Render the conditional knowledge-companion discovery policy from the\n"
    "store config.json (strict knowledge.enabled=true, target ids only).\n"
    "Read-only: no config writes, no network, no store access.\n"
    "  --config PATH   explicit config.json path (default: client-side\n"
    "                  store-dir resolution from ZMEM_STORE/ZMEM_DATA/...;\n"
    "                  an explicit empty path is refused, no fallback; a\n"
    "                  MISSING value - end of arguments or the next flag\n"
    "                  - is a usage error, never a silent fallback)\n"
    "  --no-capture    caller states publishing is disabled: render the\n"
    "                  read-only variant (reads still allowed)\n"
    "  --text          print the policy, or nothing when disabled (exit 0)\n"
    "  --json          print one status object (enabled/reason/targets/...)\n"
    "  -h, --help      this help\n"
    "Exit codes: 0 rendered-or-disabled, 2 usage (unknown flag,\n"
    "conflicting modes, or a missing --config value)."
)


def _clean(value):
    """Env values are strings; whitespace-only counts as unset."""
    if value is None:
        return ""
    return str(value).strip()


def _store_dir(store_path):
    """Directory holding the store file (symlinks resolved like
    storelib.config's ``Path(STORE_PATH).resolve().parent``)."""
    try:
        real = os.path.realpath(store_path)
    except OSError:
        real = store_path
    return os.path.dirname(real)


def _home_dir(environ, home):
    """Home for the legacy/default limbs (test isolation via ``home=``)."""
    if home:
        return os.path.expanduser(str(home))
    if environ is not None:
        for key in ("USERPROFILE", "HOME"):
            value = _clean(environ.get(key))
            if value:
                return os.path.expanduser(value)
    return os.path.expanduser("~")


def _legacy_plugin_store(home_dir_path):
    """Newest ``~/.zcode/cli/plugins/data/*zmem*/store.sqlite`` or None.

    Mirrors host._legacy_plugin_store's glob (case behavior included) as a
    last resort, for parity with what the store itself would resolve on an
    un-migrated box.  Never raises.
    """
    root = os.path.join(home_dir_path, ".zcode", "cli", "plugins", "data")
    try:
        matches = glob.glob(os.path.join(root, "*zmem*", "store.sqlite"))
        matches = [p for p in matches if os.path.isfile(p)]
    except Exception:
        return None
    if not matches:
        return None
    try:
        return max(matches, key=os.path.getmtime)
    except OSError:
        return matches[0]


def resolve_config_path(environ=None, *, home=None):
    """Resolve the client-side store ``config.json`` path, or ``""``.

    ``home`` is KEYWORD-ONLY (test isolation; callers pass ``environ``
    positionally at most).

    Precedence mirrors ``host.resolve_store_path`` (the config lives beside
    the resolved store): ``ZMEM_STORE`` > ``ZMEM_DATA`` >
    ``CLAUDE_PLUGIN_DATA`` > ``ZCODE_PLUGIN_DATA`` > ``~/.zmem`` (if its
    store exists) > legacy manual ``~/.zcode/memory`` (if it exists) >
    newest legacy plugin store > ``~/.zmem`` default.  A directory-shaped
    variable naming an existing regular FILE is refused (issue #291):
    ``""`` is returned, never a fallback location.
    """
    env = os.environ if environ is None else environ
    explicit = _clean(env.get("ZMEM_STORE"))
    if explicit:
        return os.path.join(_store_dir(os.path.expanduser(explicit)),
                            "config.json")
    for var in _DIR_ENV_VARS:
        value = _clean(env.get(var))
        if not value:
            continue
        directory = os.path.expanduser(value)
        if os.path.isfile(directory):
            return ""  # refused: wrong path KIND, do not fall through
        return os.path.join(
            _store_dir(os.path.join(directory, "store.sqlite")),
            "config.json")
    house = _home_dir(env, home)
    default_store = os.path.join(house, ".zmem", "store.sqlite")
    if os.path.isfile(default_store):
        return os.path.join(_store_dir(default_store), "config.json")
    legacy_manual = os.path.join(house, ".zcode", "memory", "store.sqlite")
    if os.path.isfile(legacy_manual):
        return os.path.join(_store_dir(legacy_manual), "config.json")
    legacy_plugin = _legacy_plugin_store(house)
    if legacy_plugin:
        return os.path.join(_store_dir(legacy_plugin), "config.json")
    return os.path.join(_store_dir(default_store), "config.json")


def _no_duplicate_keys(pairs):
    """``object_pairs_hook``: a JSON object with a duplicate key is
    malformed.  Stdlib ``json.loads`` silently keeps the LAST duplicate;
    the display lane refuses the whole file instead (FB-023: an operator
    edit that duplicated a key is exactly the state the real validator
    rejects, so nothing may render).  The raised error stays content-free.
    """
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate key")
        obj[key] = value
    return obj


def _read_config_object(path):
    """Read <= MAX_CONFIG_BYTES bytes and parse one JSON object.

    Fail-closed to ``(None, reason)``, silently (this is a display lane;
    diagnostics belong to the CLI/doctor surfaces): missing/zero-byte ->
    no knowledge object; oversized, undecodable (STRICT UTF-8 after BOM
    handling - no replace-decode), duplicate JSON keys (anywhere in the
    file), malformed, or non-object -> the matching reason string.  No
    reason ever carries file content.
    """
    try:
        if not os.path.isfile(path):
            return None, "missing"
        with open(path, "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        return None, "unreadable"
    if len(raw) > MAX_CONFIG_BYTES:
        return None, "oversized"
    if not raw:
        return None, "no-knowledge"
    try:
        obj = json.loads(raw.decode("utf-8-sig"),
                         object_pairs_hook=_no_duplicate_keys)
    except Exception:
        return None, "malformed"
    if not isinstance(obj, dict):
        return None, "malformed"
    return obj, "ok"


def _nonempty_str(value):
    """A string with non-whitespace content (shape-only: the value itself
    is never read further, never rendered)."""
    return isinstance(value, str) and bool(value.strip())


def _target_spec_ok(spec):
    """Shape-only acceptance of one target spec (fail-closed display).

    Mirrors the validator's STRUCTURE rules: an object spec, a known kind,
    known keys only, and JSON field types - including the exactly-one
    credential-location rule for kb-http.  Values are never read beyond
    type/emptiness, so no path, endpoint, or credential location can leak
    through this check, and any config the real validator would reject on
    shape never renders here either.
    """
    if not isinstance(spec, dict):
        return False
    kind = spec.get("kind")
    if not isinstance(kind, str):
        return False
    keys = _TARGET_KEYS.get(kind)
    if keys is None or set(spec) - keys:
        return False
    if kind == "markdown":
        return _nonempty_str(spec.get("root"))
    if not _nonempty_str(spec.get("endpoint")):
        return False
    if not isinstance(spec.get("allow_http", False), bool):
        return False
    credential_env = spec.get("credential_env")
    token_file = spec.get("token_file")
    if credential_env is not None and not _nonempty_str(credential_env):
        return False
    if token_file is not None and not _nonempty_str(token_file):
        return False
    if (credential_env is None) == (token_file is None):
        return False  # exactly one credential location (validator parity)
    timeout = spec.get("timeout_s")
    if timeout is not None and (isinstance(timeout, bool)
                                or not isinstance(timeout, (int, float))):
        return False
    max_bytes = spec.get("max_response_bytes")
    if max_bytes is not None and (isinstance(max_bytes, bool)
                                  or not isinstance(max_bytes, int)):
        return False
    return True


def load_policy_state(config_path=None, environ=None):
    """Load the display-safe knowledge policy state from config.json.

    Returns ``{"config_path", "enabled", "reason", "targets",
    "targets_omitted"}``.  ``targets`` holds the target id keys in config
    order (display-capped by the caller, never the values);
    ``targets_omitted`` counts valid ids beyond the display cap - an
    invalid key or spec shape refuses the WHOLE config (fail-closed as a
    whole, FB-023) instead of being dropped or counted.  Never raises,
    never writes, never reads target values; every refusal reason is
    content-free.
    """
    if config_path is None:
        config_path = resolve_config_path(environ)
    state = {
        "config_path": config_path or None,
        "enabled": False,
        "reason": "no-config-path",
        "targets": [],
        "targets_omitted": 0,
    }
    if not config_path:
        return state
    obj, reason = _read_config_object(config_path)
    if obj is None:
        state["reason"] = reason
        return state
    knowledge = obj.get("knowledge")
    if knowledge is None:
        state["reason"] = "no-knowledge"
        return state
    if not isinstance(knowledge, dict):
        state["reason"] = "malformed"
        return state
    if set(knowledge) - _KNOWLEDGE_KEYS:
        state["reason"] = "invalid-config"
        return state
    enabled = knowledge.get("enabled")
    if enabled is not True:  # strict: exactly JSON true, missing included
        state["reason"] = "disabled" if enabled is False else "invalid-enabled"
        return state
    raw_targets = knowledge.get("targets")
    if raw_targets is None:
        raw_targets = {}
    if not isinstance(raw_targets, dict):
        state["reason"] = "invalid-targets"
        return state
    # Fail-closed AS A WHOLE: one invalid id shape or spec shape
    # suppresses the ENTIRE render - no partial roster, no counting.  A
    # partially-rendered policy would advertise a config the real CLI
    # (knowledge/config.py) refuses to load.
    for tid, spec in raw_targets.items():
        if (not isinstance(tid, str) or not TARGET_ID_RE.fullmatch(tid)
                or not _target_spec_ok(spec)):
            state["reason"] = "invalid-config"
            return state
    safe_ids = list(raw_targets)
    state["enabled"] = True
    state["targets"] = safe_ids[:MAX_TARGET_IDS_DISPLAY]
    state["targets_omitted"] = len(safe_ids) - len(state["targets"])
    state["reason"] = "enabled" if state["targets"] else "no-targets"
    return state


def _compose(target_ids, targets_omitted, allow_capture):
    roster = "Configured target ids: %s." % ", ".join(target_ids)
    if targets_omitted > 0:
        roster += " (+%d not shown)" % targets_omitted
    body = (_READONLY_PARA if not allow_capture else _CAPTURE_PARA)
    return "\n\n".join([_HEADING, roster, _SHARED_PARA, body, _FOOTER])


def render_policy(config_path=None, environ=None, *, allow_capture=True):
    """Render the discovery policy, or ``""`` when it must not appear.

    Empty for every non-enabled state (missing/malformed/oversized/
    undecodable config, enabled not strictly true, zero valid target ids)
    and for any composed text that would reach the < 200 word cap.  Any
    invalid shape in an existing config refuses the whole render
    (fail-closed as a whole) - valid-looking siblings never partially
    render.
    """
    state = load_policy_state(config_path, environ)
    if not state["enabled"] or not state["targets"]:
        return ""
    text = _compose(state["targets"], state["targets_omitted"],
                    allow_capture)
    if len(text.split()) >= MAX_POLICY_WORDS:
        return ""
    return text


def policy_status(config_path=None, environ=None, *, allow_capture=True):
    """One JSON-able status object for the ``--json`` CLI and callers."""
    state = load_policy_state(config_path, environ)
    text = ""
    if state["enabled"] and state["targets"]:
        candidate = _compose(state["targets"], state["targets_omitted"],
                             allow_capture)
        if len(candidate.split()) < MAX_POLICY_WORDS:
            text = candidate
    status = dict(state)
    status["capture"] = bool(allow_capture)
    status["policy_version"] = POLICY_VERSION
    status["word_count"] = len(text.split())
    status["text"] = text
    return status


def main(argv=None):
    """CLI entry: [--config PATH] [--no-capture] (--text | --json)."""
    # Force LF output so the policy bytes are platform-invariant (the
    # Windows text layer would otherwise translate to CRLF).
    try:
        sys.stdout.reconfigure(newline="\n")
    except (AttributeError, ValueError, OSError):
        pass
    args = list(sys.argv[1:] if argv is None else argv)
    config = None  # None = absent (default resolution); "" = explicit refusal
    mode = None
    allow_capture = True
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in ("-h", "--help"):
            sys.stdout.write(_USAGE + "\n")
            return 0
        if arg == "--config":
            index += 1
            if index >= len(args) or args[index] in _CONFIG_FLAGS:
                sys.stderr.write("policy.py: --config requires a path\n")
                return 2
            config = args[index]
        elif arg == "--text":
            if mode is not None:
                sys.stderr.write("policy.py: choose one of --text/--json\n")
                return 2
            mode = "text"
        elif arg == "--json":
            if mode is not None:
                sys.stderr.write("policy.py: choose one of --text/--json\n")
                return 2
            mode = "json"
        elif arg == "--no-capture":
            allow_capture = False
        else:
            sys.stderr.write("policy.py: unknown argument %r\n" % (arg,))
            return 2
        index += 1
    mode = mode or "text"
    # An explicit --config (even the empty string) is handed through
    # verbatim: load_policy_state treats "" as the refused state instead
    # of silently consulting default resolution (reviewer nit).
    if mode == "json":
        sys.stdout.write(json.dumps(
            policy_status(config, None, allow_capture=allow_capture),
            ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    text = render_policy(config, None, allow_capture=allow_capture)
    if text:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
