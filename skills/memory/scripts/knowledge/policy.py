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
- The config file is read BOUNDED (<= 64 KiB) and BOM-tolerant, exactly
  like ``storelib.config.load_config``'s contract.
- ``knowledge.enabled`` must be JSON ``true`` (strict) or nothing renders.
- TARGET IDS ONLY: the rendered policy lists target id keys that match the
  shared slug shape (1..24 chars, ``[a-z0-9-]``, no leading/trailing
  hyphen).  Target VALUES (roots, endpoints, credential locations) are
  never read, never rendered.  Display filtering DROPS unsafe keys; it is
  not an alternate validation authority - PR1's
  ``skills/memory/scripts/knowledge/config.py`` remains the validator a
  command invocation actually hits, so a config this filter mis-likes
  simply renders no policy while the real CLI keeps its own verdict.
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
which could silently consult an unrelated, possibly enabled config.

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

# How many target ids the one-line roster shows.  The remainder are counted
# ("(+N not shown)") so the model knows more exist without spending words.
MAX_TARGET_IDS_DISPLAY = 8

# The shared target-id slug shape (PR1 knowledge/config.py owns the
# validator; this display filter accepts exactly the same shape).
TARGET_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,22}[a-z0-9])?$")

_DIR_ENV_VARS = ("ZMEM_DATA", "CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")

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
    "= pending: inspect `knowledge status`; `flush` drains only queued\n"
    "entries, else re-run the same capture input (idempotent)."
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

_USAGE = (
    "usage: policy.py [--config PATH] [--no-capture] (--text | --json)\n"
    "\n"
    "Render the conditional knowledge-companion discovery policy from the\n"
    "store config.json (strict knowledge.enabled=true, target ids only).\n"
    "Read-only: no config writes, no network, no store access.\n"
    "  --config PATH   explicit config.json path (default: client-side\n"
    "                  store-dir resolution from ZMEM_STORE/ZMEM_DATA/...;\n"
    "                  an explicit empty path is refused, no fallback)\n"
    "  --no-capture    caller states publishing is disabled: render the\n"
    "                  read-only variant (reads still allowed)\n"
    "  --text          print the policy, or nothing when disabled (exit 0)\n"
    "  --json          print one status object (enabled/reason/targets/...)\n"
    "  -h, --help      this help\n"
    "Exit codes: 0 rendered-or-disabled, 2 usage."
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


def _read_config_object(path):
    """Read <= MAX_CONFIG_BYTES bytes and parse one JSON object.

    Fail-open to ``(obj, reason)`` like storelib.config.load_config, but
    silently (this is a display lane; diagnostics belong to the CLI/doctor
    surfaces): missing/zero-byte -> no knowledge object; oversized,
    malformed, or non-object -> the matching reason string.
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
        obj = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except Exception:
        return None, "malformed"
    if not isinstance(obj, dict):
        return None, "malformed"
    return obj, "ok"


def load_policy_state(config_path=None, environ=None):
    """Load the display-safe knowledge policy state from config.json.

    Returns ``{"config_path", "enabled", "reason", "targets",
    "targets_omitted"}``.  ``targets`` holds the SAFE target id keys in
    config order (display-capped by the caller, never the values);
    ``targets_omitted`` counts unsafe keys plus safe keys beyond the
    display cap.  Never raises, never writes, never reads target values.
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
    safe_ids = [key for key in raw_targets
                if isinstance(key, str) and TARGET_ID_RE.fullmatch(key)]
    state["enabled"] = True
    state["targets"] = safe_ids[:MAX_TARGET_IDS_DISPLAY]
    state["targets_omitted"] = len(safe_ids) - len(state["targets"]) + (
        len(raw_targets) - len(safe_ids))
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

    Empty for every non-enabled state (missing/malformed/oversized config,
    enabled not strictly true, zero display-safe target ids) and for any
    composed text that would reach the < 200 word cap.
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
            if index >= len(args):
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
