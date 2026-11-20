"""One-shot registration of the closeout-request (UserPromptSubmit) and
closeout-gate (Stop) hooks in the three host hook manifests. Idempotent:
re-running adds nothing. Run from the repo root."""

import json

SPEC = {
    "hooks/hooks.zcode.json": {
        "root_var": "${ZCODE_PLUGIN_ROOT}",
        "structured": True,
    },
    "hooks/hooks.claude.json": {
        "root_var": "${CLAUDE_PLUGIN_ROOT}",
        "structured": True,
    },
    "hooks/hooks.codex.json": {
        "root_var": "${PLUGIN_ROOT}",
        "structured": False,  # codex manifest: command string + commandWindows
    },
}


def request_entry(root_var: str, structured: bool) -> dict:
    if structured:
        return {"hooks": [{
            "type": "process",
            "command": "node",
            "args": [f"{root_var}/hooks/zmem-launch.js", "closeout-request"],
            "timeout": 15,
        }]}
    cmd = f'node "{root_var}/hooks/zmem-launch.js" closeout-request'
    return {"hooks": [{
        "type": "command",
        "command": cmd,
        "commandWindows": cmd,
        "timeout": 15,
    }]}


def gate_entry(root_var: str, structured: bool) -> dict:
    if structured:
        return {"hooks": [{
            "type": "process",
            "command": "node",
            "args": [f"{root_var}/hooks/zmem-launch.js", "closeout-gate"],
            "timeout": 15,
        }]}
    cmd = f'node "{root_var}/hooks/zmem-launch.js" closeout-gate'
    return {"hooks": [{
        "type": "command",
        "command": cmd,
        "commandWindows": cmd,
        "timeout": 15,
    }]}


for path, spec in SPEC.items():
    with open(path, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    hooks = d["hooks"]
    changed = []
    if "closeout-request" not in json.dumps(hooks.get("UserPromptSubmit", [])):
        hooks.setdefault("UserPromptSubmit", []).append(
            request_entry(spec["root_var"], spec["structured"]))
        changed.append("UserPromptSubmit+=closeout-request")
    if "closeout-gate" not in json.dumps(hooks.get("Stop", [])):
        hooks.setdefault("Stop", []).append(
            gate_entry(spec["root_var"], spec["structured"]))
        changed.append("Stop+=closeout-gate")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(d, indent=2) + "\n")
    print(path, "->", changed or "already registered")
