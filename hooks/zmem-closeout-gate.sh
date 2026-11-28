#!/usr/bin/env bash
set -u

# Keep pass-through bytes ahead of all best-effort gate work. A missing helper,
# store or sidecar I/O error is fail-open by design.
PAYLOAD="$(cat 2>/dev/null || true)"
export ZMEM_CLOSEOUT_PAYLOAD="$PAYLOAD"
HOOK_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
printf '{}\n'

python - "$HOOK_DIR" <<'PYEOF'
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

store_env = os.environ.get("ZMEM_STORE_PY")
root = os.environ.get("ZMEM_ROOT")
if store_env:
    scripts = Path(store_env).resolve().parent
elif root:
    scripts = Path(root) / "skills" / "memory" / "scripts"
elif len(sys.argv) > 1:
    scripts = Path(sys.argv[1]).resolve().parent / "skills" / "memory" / "scripts"
else:
    scripts = None
default_store = scripts / "store.py" if scripts else None
if scripts:
    sys.path.insert(0, str(scripts))

try:
    from closeout_request import clear_if_matches, increment_blocks, read_request
    payload = json.loads(os.environ.get("ZMEM_CLOSEOUT_PAYLOAD", "{}"))
    session_id = os.environ.get("ZMEM_SESSION") or str(payload.get("session_id") or "")
    state = read_request(session_id) if session_id else None
    store = Path(os.environ.get("ZMEM_STORE_PY") or default_store)
    if state is None:
        raise SystemExit(0)
    if not store.is_file():
        print("zmem closeout is armed but closeout-status is unavailable; allowing Stop", file=sys.stderr)
        raise SystemExit(0)
    if state.blocks >= 5:
        print("[zmem] closeout gate: five consecutive blocks; degrading to advisory", file=sys.stderr)
        raise SystemExit(0)
    with tempfile.TemporaryFile(mode="w+b") as output:
        result = subprocess.run(
            [sys.executable, str(store), "closeout-status", "--session-id", session_id],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        output.seek(0, 2)
        size = output.tell()
        output.seek(0)
        raw = output.read(64 * 1024 + 1) if size <= 64 * 1024 else b""
    try:
        report = json.loads(raw.decode("utf-8"))
    except (TypeError, ValueError):
        report = {}
    if result.returncode == 0 and report.get("ack_valid") is True:
        clear_if_matches(session_id, state.nonce)
        raise SystemExit(0)
    blocks = increment_blocks(session_id, state.nonce)
    if blocks is not None and blocks <= 5:
        fields = []
        for field in ("missing", "stale", "unreviewed"):
            value = report.get(field)
            if isinstance(value, list) and value:
                fields.append(f"{field}={','.join(str(item)[:80] for item in value[:3])}")
            elif isinstance(value, str) and value:
                fields.append(f"{field}={value[:160]}")
        reason = report.get("reason") if isinstance(report.get("reason"), str) else "; ".join(fields)
        if not reason:
            reason = "acknowledgement is missing, stale, or unreviewed"
        status_command = f"{sys.executable} {store} closeout-status --session-id {session_id}"
        print(f"zmem closeout is armed ({reason}); inspect with: {status_command}", file=sys.stderr)
        raise SystemExit(2)
except SystemExit:
    raise
except Exception:
    raise SystemExit(0)
PYEOF
status=$?
if [ "$status" -eq 2 ]; then
  exit 2
fi
exit 0
