#!/usr/bin/env bash
set -u

# The request hook is deliberately fail-open. Its only stdout contract is the
# unchanged hook acknowledgement; malformed input must never block a prompt.
PAYLOAD="$(cat 2>/dev/null || true)"
export ZMEM_CLOSEOUT_PAYLOAD="$PAYLOAD"
HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"

python - "$HOOK_DIR" <<'PYEOF' 2>/dev/null || true
import json
import os
import sys
from pathlib import Path

store = os.environ.get("ZMEM_STORE_PY")
root = os.environ.get("ZMEM_ROOT")
if store:
    # An explicitly supplied store script identifies the installed revision
    # whose helper must own the sidecars, ahead of an ambient legacy root.
    sys.path.insert(0, str(Path(store).resolve().parent))
elif root:
    sys.path.insert(0, str(Path(root) / "skills" / "memory" / "scripts"))
elif len(sys.argv) > 1:
    # Pass this as argv, rather than an environment path, so MSYS converts it
    # to the native path Python expects on Windows.
    sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent / "skills" / "memory" / "scripts"))

try:
    from closeout_request import arm_request
    payload = json.loads(os.environ.get("ZMEM_CLOSEOUT_PAYLOAD", "{}"))
    session_id = os.environ.get("ZMEM_SESSION") or str(payload.get("session_id") or "")
    prompt = str(payload.get("prompt") or "")
    if session_id:
        arm_request(session_id, prompt)
except Exception:
    pass
PYEOF

printf '{}\n'
exit 0
