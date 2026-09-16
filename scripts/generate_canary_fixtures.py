#!/usr/bin/env python3
"""Deterministic generator for tests/fixtures/canary/expected-lanes.json.

Reads the three committed hook manifests and the committed fake-executable
bytes, then writes the nine-lane contract record: lane names, host/mode
mapping, manifest-derived hook ids, fake version strings, and calculated
lowercase SHA-256 values. Output is UTF-8 with one final LF.

This file is PRODUCED, never rewritten by a test:
    python scripts/generate_canary_fixtures.py --write

The live canary/<canary_lane>.json artifacts are checked structurally and
never compared to this fake-executable record (it is not an oracle for live
measurements).
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "canary"
EXPECTED = FIXTURE_DIR / "expected-lanes.json"

LANE_HOSTS = {
    "hermes-gateway": "hermes",
    "hermes-provider-mode": "hermes",
    "hermes-compat-mode": "hermes",
    "claude-compact": "claude",
    "codex-trust": "codex",
    "zcode-duplicate": "zcode",
    "exec-form-claude": "claude",
    "exec-form-codex": "codex",
    "exec-form-zcode": "zcode",
}
HERMES_LANE_MODE = {
    "hermes-gateway": "gateway",
    "hermes-provider-mode": "provider",
    "hermes-compat-mode": "compatibility",
}
INTERPRETER_STEMS = {"node", "bash", "sh", "python", "python3",
                     "pwsh", "powershell"}


def _shell_tokens(command):
    out = []
    for part in command.split():
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
            part = part[1:-1]
        if part:
            out.append(part)
    return out


def _command_basename(command):
    parts = _shell_tokens(command)
    if not parts:
        return None
    stem = parts[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = stem.rsplit(".", 1)[0].lower()
    if stem in INTERPRETER_STEMS and len(parts) > 1:
        nxt = parts[1].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return nxt
    return parts[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def _manifest_ids(host):
    """Every <event>:<command-basename> id, walking the two-level manifest
    shape (hooks.<Event> -> matcher entries -> hooks[] command objects)."""
    manifest = REPO_ROOT / "hooks" / ("hooks.%s.json" % host)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    ids = []
    for event, entries in (data.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            commands = entry.get("hooks")
            if not isinstance(commands, list):
                commands = [entry] if "command" in entry else []
            for command in commands:
                if not isinstance(command, dict):
                    continue
                raw = command.get("command")
                if isinstance(raw, list):
                    base = str(raw[0]).rsplit("/", 1)[-1] if raw else None
                else:
                    base = _command_basename(str(raw))
                if base:
                    hid = "%s:%s" % (event, base)
                    if hid not in ids:
                        ids.append(hid)
    return sorted(ids)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="generate_canary_fixtures.py",
        description="write tests/fixtures/canary/expected-lanes.json "
                    "deterministically",
    )
    parser.add_argument("--write", action="store_true", default=True,
                        help="write the fixture (default; explicit for the "
                             "documented command line)")
    args = parser.parse_args(argv)

    fakes = json.loads(
        (FIXTURE_DIR / "fake-executables.json").read_text(encoding="utf-8"))
    fake_sha = {host: hashlib.sha256(spec["image"].encode("utf-8")).hexdigest()
                for host, spec in fakes.items()}

    hermes = json.loads(
        (FIXTURE_DIR / "hermes" / "cdf4c76.json").read_text(encoding="utf-8"))
    duplicate = json.loads(
        (FIXTURE_DIR / "zcode" / "duplicate-install.json")
        .read_text(encoding="utf-8"))

    lanes = {}
    for lane, host in LANE_HOSTS.items():
        record = {
            "host": host,
            "mode": HERMES_LANE_MODE.get(lane),
            "manifest_hook_ids": (_manifest_ids(host)
                                  if host != "hermes" else []),
            "fake_sha256": (fake_sha[host] if host in fakes else None),
            "fake_version": (fakes[host]["version_stdout"].strip()
                             if host in fakes else None),
        }
        lanes[lane] = record
    lanes["hermes-gateway"]["pinned_sha"] = hermes["sha"]
    lanes["hermes-gateway"]["pinned_version"] = hermes["version"]
    lanes["hermes-gateway"]["pinned_commands"] = hermes["commands"]
    lanes["hermes-gateway"]["callback_evidence"] = hermes["callback_evidence"]
    lanes["zcode-duplicate"]["duplicate_roots"] = duplicate["roots"]
    lanes["zcode-duplicate"]["one_copy_root"] = duplicate["one_copy_root"]
    lanes["zcode-duplicate"]["expected_two_copy_reason"] = \
        duplicate["expected_two_copy_reason"]
    lanes["zcode-duplicate"]["expected_one_copy_reason"] = \
        duplicate["expected_one_copy_reason"]

    payload = {
        "schema": "zmem-canary-expected-lanes",
        "namespace": "project:fixture-96",
        "session_id": "00000000-0000-4000-8000-000000000096",
        "timestamp": "2026-09-10T00:00:00Z",
        "lanes": lanes,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False,
                      sort_keys=True) + "\n"
    EXPECTED.write_text(text, encoding="utf-8", newline="\n")
    print("wrote %s (%d lanes)" % (EXPECTED.relative_to(REPO_ROOT),
                                   len(lanes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
