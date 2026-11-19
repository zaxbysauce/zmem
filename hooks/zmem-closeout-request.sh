#!/usr/bin/env bash
# zmem-closeout-request.sh — UserPromptSubmit hook (shared, both hosts).
#
# Arms the per-session closeout-requested marker when the user's prompt
# asks for a session closeout ("closeout", "wrap up", "end of session",
# "capture what we learned"). The matching Stop hook
# (zmem-closeout-gate.sh) blocks turn-end while the marker is armed and
# the fail-closed closeout gate has not passed, so a requested closeout
# cannot end in a false "done" claim (observed on zaxbygraph PR #81's
# feedback session, 2026-11-19: the agent captured ad-hoc rows, wrote
# "closeout DONE", and never ran the closeout skill).
#
# FAIL-OPEN: any error exits 0 quietly — a marker failure can never block
# a prompt.
#
# Payload: stdin JSON with session_id + prompt. ZMEM_DATA (canonical env
# from zmem-launch.js; ~/.zmem fallback) locates the ops dir.

set -u

PROMPT_TEXT="$(cat 2>/dev/null || true)"
export ZMEM_CLOSEOUT_PAYLOAD="$PROMPT_TEXT"

python - <<'PYEOF' 2>/dev/null
import hashlib, json, os, re, sys

try:
    payload = json.loads(os.environ.get("ZMEM_CLOSEOUT_PAYLOAD") or "{}")
except Exception:
    payload = {}
# Canonical env first (the launcher's ZMEM_SESSION is the same sid the
# write path stamps); the payload session_id is the fallback.
sid = os.environ.get("ZMEM_SESSION", "") or str(payload.get("session_id") or "")
prompt = str(payload.get("prompt") or "").lower()

if not sid:
    sys.exit(0)

data_dir = os.environ.get("ZMEM_DATA") or os.environ.get("ZCODE_PLUGIN_DATA") \
    or os.path.expanduser("~/.zmem")
stem = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
ops = os.path.join(data_dir, "ops")
os.makedirs(ops, exist_ok=True)

# Disarm first: a fresh prompt clears any stale request so the gate only
# fires for the turn that actually carries a closeout request.
try:
    os.remove(os.path.join(ops, stem + ".closeout-requested"))
except OSError:
    pass

if not re.search(r"\b(closeout|wrap\s*up|wrap-up|end\s+of\s+session|"
                 r"session\s+closeout|capture\s+what\s+we\s+(learned|did))\b",
                 prompt):
    sys.exit(0)

# Arm only when this session has store writes to close out (a session
# with no `.wrote` marker has nothing to close — the gate would be noise).
if not os.path.isfile(os.path.join(ops, stem + ".wrote")):
    sys.exit(0)

with open(os.path.join(ops, stem + ".closeout-requested"), "w",
          encoding="utf-8") as fh:
    fh.write("armed\n")
PYEOF
exit 0
