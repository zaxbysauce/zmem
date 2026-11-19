#!/usr/bin/env bash
# zmem-closeout-gate.sh — Stop hook (shared, both hosts).
#
# Blocks turn-end (exit 2; stderr is fed back as the blocking reason)
# while BOTH hold:
#   1. the user requested a session closeout this session
#      (`.closeout-requested` armed by zmem-closeout-request.sh), and
#   2. the fail-closed closeout gate has not passed
#      (`store.py closeout-status --session-id <sid>` reports
#       ack_valid: false — missing/stale step receipts, unreviewed
#       session rows, or no ack recorded).
#
# This is the mechanical tripwire for the observed failure class
# (zaxbygraph PR #81 feedback session, 2026-11-19: the agent captured
# ad-hoc rows, wrote "closeout DONE", never ran the closeout skill —
# the user flagged the class as recurring). The escape hatches are
# deliberate: run the closeout skill to completion (the fail-closed
# closeout-ack then passes), or the operator interrupts.
#
# LOOP BUDGET: consecutive gate blocks are counted in the request
# marker; after 5 the gate degrades to a pass-through with a loud
# advisory (a closeout that cannot complete in 5 forced continuations
# is blocked on something the operator must look at, and an infinite
# block loop helps nobody). Deliberate tampering with the counter file
# is visible and out of band.
#
# FAIL-OPEN on infrastructure: if store.py / the status command cannot
# run at all, the gate exits 0 rather than wedging every Stop — the
# fail-closed property lives in closeout-ack itself, not here.
#
# Payload: stdin JSON with session_id (ZMEM_SESSION canonical env is the
# primary source; the payload is the fallback).

set -u

PAYLOAD="$(cat 2>/dev/null || true)"
export ZMEM_CLOSEOUT_PAYLOAD="$PAYLOAD"

python - <<'PYEOF'
import hashlib, json, os, subprocess, sys

try:
    payload = json.loads(os.environ.get("ZMEM_CLOSEOUT_PAYLOAD") or "{}")
except Exception:
    payload = {}
# Canonical env first (the launcher's ZMEM_SESSION is the same sid the
# write path stamps); the payload session_id is the fallback.
sid = os.environ.get("ZMEM_SESSION", "") or str(payload.get("session_id") or "")
if not sid:
    sys.exit(0)

data_dir = os.environ.get("ZMEM_DATA") or os.environ.get("ZCODE_PLUGIN_DATA") \
    or os.path.expanduser("~/.zmem")
stem = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:32]
ops = os.path.join(data_dir, "ops")
requested = os.path.join(ops, stem + ".closeout-requested")

if not os.path.isfile(requested):
    sys.exit(0)  # no closeout requested this session: the gate is inert

# Loop budget: count consecutive blocks inside the request marker.
try:
    with open(requested, "r", encoding="utf-8") as fh:
        content = fh.read().strip()
    count = 0
    for line in content.splitlines():
        if line.startswith("blocks:"):
            count = int(line.split(":", 1)[1].strip() or 0)
except (OSError, ValueError):
    count = 0
if count >= 5:
    sys.stderr.write(
        "[zmem] closeout gate: 5 consecutive blocks without a valid "
        "closeout — degrading to advisory. The closeout for session "
        + sid + " is still incomplete (`store.py closeout-status "
        "--session-id " + sid + "`); inspect why the steps cannot "
        "complete before ending the session.\n")
    sys.exit(0)

status_py = os.environ.get("ZMEM_STORE_PY", "")
if not status_py:
    root = os.environ.get("ZMEM_ROOT", "")
    candidate = os.path.join(
        root, "skills", "memory", "scripts", "store.py") if root else ""
    if candidate and os.path.isfile(candidate):
        status_py = candidate
if not status_py:
    sys.exit(0)  # fail-open: no store.py resolvable in this host context

p = subprocess.run(
    [sys.executable, status_py, "closeout-status", "--session-id", sid],
    capture_output=True, text=True, timeout=60,
)
try:
    report = json.loads(p.stdout or "{}")
except Exception:
    report = {}

if report.get("ack_valid"):
    # Gate passed: disarm the request (the counter rides inside it).
    try:
        os.remove(requested)
    except OSError:
        pass
    sys.exit(0)

# Gate unmet: increment the block counter inside the request marker
# (same file the count is read from), then BLOCK (exit 2).
try:
    with open(requested, "w", encoding="utf-8") as fh:
        fh.write(f"armed\nblocks:{count + 1}\n")
except OSError:
    pass

missing = ", ".join(report.get("missing_steps", [])) or "none"
stale = ", ".join(report.get("stale_steps", [])) or "none"
unreviewed = len(report.get("unreviewed_rows", []))
sys.stderr.write(
    "[zmem] closeout gate: the user requested a session closeout and it "
    "is not complete. Missing step receipts: " + missing + ". Stale "
    "receipts (a store write happened after them): " + stale + ". "
    "Unreviewed session rows: " + str(unreviewed) + ". Run the closeout "
    "skill workflow now: execute the remaining steps, record them with "
    "`store.py closeout-step --session-id " + sid + " --step <step>`, "
    "discharge rows with `store.py closeout-review`, then re-run "
    "`store.py closeout-status` until ack_valid is true, and record the "
    "closeout with `store.py closeout-ack --session-id " + sid + "`.\n")
sys.exit(2)
PYEOF
rc=$?
exit "$rc"
