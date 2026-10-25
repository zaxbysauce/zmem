#!/usr/bin/env bash
# install-broker.sh — point this Linux box at the fleet hub (issue #291).
#
# Contract (Workstream T PR 12): the broker machine gets a PER-BROKER
# credential — a singular ZMEM_MCP_TOKEN_FILE the CLIENT transport reads.
# The hub's principal MAP never leaves the hub. The installer:
#   * writes the token file mode 0600;
#   * persists ZMEM_REMOTE_URL (full /mcp URL) + ZMEM_MCP_TOKEN_FILE in an
#     /etc/profile.d drop-in (a session restart is required);
#   * refuses conflicting effective config: ZMEM_HERMES_MODE values other
#     than unset/mcp, a differing ZMEM_MCP_URL, any plural
#     ZMEM_MCP_TOKENS_FILE, or any ZMEM_STORE;
#   * validates with a FRESH child whose env is RECONSTRUCTED from the
#     persisted values and asserts the transport resolves MCP;
#   * exits nonzero when `doctor --fleet` reports any fail check.
#
# Usage (token is piped via stdin, never an argument):
#   cat token.txt | sudo ./install-broker.sh /path/to/zmem https://vsan-1.<tailnet>.ts.net/mcp [namespaces]

set -euo pipefail

REPO_ROOT="${1:?usage: install-broker.sh REPO_ROOT HUB_URL [NAMESPACES]}"
HUB_URL="${2:?HUB_URL required}"
NAMESPACES="${3:-}"

case "$HUB_URL" in
    */mcp) ;;
    *) echo "install-broker: HUB_URL must be the full MCP endpoint ending in /mcp (got: $HUB_URL)" >&2; exit 2 ;;
esac

# --- refuse conflicting effective config ------------------------------------
if [ -n "${ZMEM_HERMES_MODE:-}" ] && [ "$ZMEM_HERMES_MODE" != "mcp" ]; then
    echo "install-broker: REFUSED: effective ZMEM_HERMES_MODE='$ZMEM_HERMES_MODE' outranks both URL variables and would bypass the hub (only unset or 'mcp' is allowed)" >&2
    exit 2
fi
if [ -n "${ZMEM_MCP_URL:-}" ] && [ "$ZMEM_MCP_URL" != "$HUB_URL" ]; then
    echo "install-broker: REFUSED: effective ZMEM_MCP_URL='$ZMEM_MCP_URL' outranks ZMEM_REMOTE_URL and differs from HUB_URL" >&2
    exit 2
fi
if [ -n "${ZMEM_MCP_TOKENS_FILE:-}" ]; then
    echo "install-broker: REFUSED: a ZMEM_MCP_TOKENS_FILE (the hub-side principal map) is set on a broker machine — unset it; the hub map never leaves the hub" >&2
    exit 2
fi
if [ -n "${ZMEM_STORE:-}" ]; then
    echo "install-broker: REFUSED: an effective ZMEM_STORE is set — ZMEM_STORE together with ZMEM_REMOTE_URL is a configuration error (docs/FLEET.md)" >&2
    exit 2
fi

# --- token file: 0600 --------------------------------------------------------
TOKEN="$(cat)"
[ -n "$TOKEN" ] || { echo "install-broker: no token piped on stdin" >&2; exit 2; }
install -d -m 755 /etc/zmem
TOKEN_FILE=/etc/zmem/broker-token.json
(umask 077; printf '{' > "$TOKEN_FILE"
 if [ -n "$NAMESPACES" ]; then
     first=1
     printf '"namespaces":[' >> "$TOKEN_FILE"
     IFS=','; for ns in $NAMESPACES; do
         [ $first -eq 1 ] || printf ',' >> "$TOKEN_FILE"
         printf '"%s"' "$ns" >> "$TOKEN_FILE"
         first=0
     done
     unset IFS
     printf ',' >> "$TOKEN_FILE"
 fi
 printf '"token":"%s"}' "$TOKEN" >> "$TOKEN_FILE")
chmod 600 "$TOKEN_FILE"

# --- persist the config (session restart required) ---------------------------
PROFILE=/etc/profile.d/zmem-broker.sh
cat > "$PROFILE" <<EOF
# zmem broker configuration (issue #291; written by install-broker.sh)
export ZMEM_REMOTE_URL="$HUB_URL"
export ZMEM_MCP_TOKEN_FILE="$TOKEN_FILE"
unset ZMEM_MCP_TOKEN
EOF
chmod 644 "$PROFILE"

# --- ZMEM_DATA must be a writable directory -----------------------------------
DATA_DIR="${ZMEM_DATA:-/var/lib/zmem}"
if [ -e "$DATA_DIR" ] && [ ! -d "$DATA_DIR" ]; then
    echo "install-broker: REFUSED: effective ZMEM_DATA ('$DATA_DIR') is not a directory" >&2
    exit 2
fi
mkdir -p "$DATA_DIR"

# --- validate: FRESH child with the RECONSTRUCTED effective config -----------
SCRIPTS="$REPO_ROOT/skills/memory/scripts"
DOCTOR="$SCRIPTS/doctor.py"
python3 - "$SCRIPTS" "$HUB_URL" "$TOKEN_FILE" "$DOCTOR" <<'PYEOF'
import os, subprocess, sys
scripts, url, token_file, doctor = sys.argv[1:5]
env = dict(os.environ)
env["ZMEM_REMOTE_URL"] = url
env["ZMEM_MCP_TOKEN_FILE"] = token_file
env.pop("ZMEM_MCP_TOKEN", None)
env.pop("ZMEM_MCP_TOKENS_FILE", None)
sys.path.insert(0, scripts)
from fleet import transport
mode = transport.resolve_transport_mode(env)
if mode is not transport.TransportMode.MCP:
    print("transport mode resolved to %s, not MCP" % mode, file=sys.stderr)
    sys.exit(10)
rc = subprocess.run([sys.executable, "-X", "utf8", doctor, "--fleet",
                     "--format", "json"], env=env, capture_output=True,
                    text=True).returncode
sys.exit(rc)
PYEOF
rc=$?
if [ $rc -ne 0 ]; then
    echo "install-broker: broker validation failed (exit $rc) — fix the reported checks before harnesses rely on this box" >&2
    exit $rc
fi

echo
echo "Broker installed. The profile drop-in requires a new session before harnesses"
echo "inherit it. Residual: later user/process overrides can still shadow these"
echo "values (documented in docs/FLEET.md)."
