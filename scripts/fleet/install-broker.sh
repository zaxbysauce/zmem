#!/usr/bin/env bash
# install-broker.sh - point this Linux box at the fleet hub (issue #291).
#
# Contract (Workstream T PR 12): the broker gets a PER-BROKER credential -
# a singular ZMEM_MCP_TOKEN_FILE the CLIENT transport reads. The hub's
# principal map never leaves the hub. The installer:
#   * reads the token from stdin (never an argument), strips CR/LF;
#   * writes the token file mode 0600 owned by the invoking user (chown
#     under sudo) so the user-mode harness can read it;
#   * validates in a FRESH child with the reconstructed effective env and
#     asserts the transport resolves mcp BEFORE persisting anything;
#   * persists ZMEM_REMOTE_URL (full /mcp URL) + ZMEM_MCP_TOKEN_FILE +
#     ZMEM_DATA in an /etc/profile.d drop-in (session restart required);
#   * refuses conflicting effective config in the invoking user's env AND
#     the root env (sudo env_reset makes the root env a lie): check BOTH
#     scopes for ZMEM_HERMES_MODE != mcp, a differing ZMEM_MCP_URL, any
#     plural ZMEM_MCP_TOKENS_FILE, or any ZMEM_STORE;
#   * exits nonzero when `doctor --fleet` reports any fail check, printing
#     the doctor report so the failing checks are visible.
#
# Usage (token piped via stdin, never an argument):
#   cat token.txt | sudo ./install-broker.sh /path/to/zmem \
#       https://vsan-1.<tailnet>.ts.net/mcp [namespaces]

set -euo pipefail

REPO_ROOT="${1:?usage: install-broker.sh REPO_ROOT HUB_URL [NAMESPACES]}"
HUB_URL="${2:?HUB_URL required}"
NAMESPACES="${3:-}"

case "$HUB_URL" in
    https://*/mcp) ;;
    *) echo "install-broker: HUB_URL must be https:// and end in /mcp - the bearer token travels on every request (got: $HUB_URL)" >&2; exit 2 ;;
esac

# --- refuse conflicting effective config in BOTH env scopes -----------------
# Under sudo, env_reset drops the invoking user's ZMEM_* vars from THIS
# process, so the user-scope conflicts must be probed via SUDO_USER too.
check_conflicts() {
    local scope="$1" hermes mcpurl tokens store
    hermes="$(printenv ZMEM_HERMES_MODE || true)"
    mcpurl="$(printenv ZMEM_MCP_URL || true)"
    tokens="$(printenv ZMEM_MCP_TOKENS_FILE || true)"
    store="$(printenv ZMEM_STORE || true)"
    if [ -n "$hermes" ] && [ "$hermes" != "mcp" ]; then
        echo "install-broker: REFUSED ($scope): ZMEM_HERMES_MODE='$hermes' outranks both URL variables and would bypass the hub (only unset or 'mcp' is allowed)" >&2
        exit 2
    fi
    if [ -n "$mcpurl" ] && [ "$mcpurl" != "$HUB_URL" ]; then
        echo "install-broker: REFUSED ($scope): ZMEM_MCP_URL='$mcpurl' outranks ZMEM_REMOTE_URL and differs from HUB_URL" >&2
        exit 2
    fi
    if [ -n "$tokens" ]; then
        echo "install-broker: REFUSED ($scope): a ZMEM_MCP_TOKENS_FILE (the hub-side principal map) is set on a broker machine - unset it; the hub map never leaves the hub" >&2
        exit 2
    fi
    if [ -n "$store" ]; then
        echo "install-broker: REFUSED ($scope): an effective ZMEM_STORE is set - ZMEM_STORE together with ZMEM_REMOTE_URL is a configuration error (docs/FLEET.md)" >&2
        exit 2
    fi
}
check_conflicts "this process"
if [ "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    USER_ENV="$(sudo -u "$SUDO_USER" env 2>/dev/null || true)" || USER_ENV=""
    hermes_u="$(printf '%s\n' "$USER_ENV" | sed -n 's/^ZMEM_HERMES_MODE=//p' | tail -1)"
    mcpurl_u="$(printf '%s\n' "$USER_ENV" | sed -n 's/^ZMEM_MCP_URL=//p' | tail -1)"
    tokens_u="$(printf '%s\n' "$USER_ENV" | sed -n 's/^ZMEM_MCP_TOKENS_FILE=//p' | tail -1)"
    store_u="$(printf '%s\n' "$USER_ENV" | sed -n 's/^ZMEM_STORE=//p' | tail -1)"
    if [ -n "$hermes_u" ] && [ "$hermes_u" != "mcp" ]; then
        echo "install-broker: REFUSED (user $SUDO_USER): ZMEM_HERMES_MODE='$hermes_u' outranks both URL variables and would bypass the hub (only unset or 'mcp' is allowed)" >&2
        exit 2
    fi
    if [ -n "$mcpurl_u" ] && [ "$mcpurl_u" != "$HUB_URL" ]; then
        echo "install-broker: REFUSED (user $SUDO_USER): ZMEM_MCP_URL='$mcpurl_u' outranks ZMEM_REMOTE_URL and differs from HUB_URL" >&2
        exit 2
    fi
    if [ -n "$tokens_u" ]; then
        echo "install-broker: REFUSED (user $SUDO_USER): a ZMEM_MCP_TOKENS_FILE (the hub-side principal map) is set - unset it; the hub map never leaves the hub" >&2
        exit 2
    fi
    if [ -n "$store_u" ]; then
        echo "install-broker: REFUSED (user $SUDO_USER): an effective ZMEM_STORE is set - ZMEM_STORE together with ZMEM_REMOTE_URL is a configuration error (docs/FLEET.md)" >&2
        exit 2
    fi
fi

# --- read the token from stdin (strip CR so a Windows-authored token works) -
TOKEN="$(cat)"
TOKEN="${TOKEN%$'\r'}"
TOKEN="$(printf '%s' "$TOKEN" | tr -d '\r\n')"
[ -n "$TOKEN" ] || { echo "install-broker: no token piped on stdin" >&2; exit 2; }

# --- write the credential via python (proper JSON escaping, exact bytes) ----
# The token travels through a root-only temp file (never argv: /proc/<pid>/
# cmdline is world-readable; never the python heredoc's stdin, which carries
# the script itself).
install -d -m 755 /etc/zmem
TOKEN_FILE=/etc/zmem/broker-token.json
TOKEN_TMP="$(mktemp /tmp/zmem-token.XXXXXX)"
chmod 600 "$TOKEN_TMP"
printf '%s' "$TOKEN" > "$TOKEN_TMP"
python3 - "$TOKEN_FILE" "$NAMESPACES" "$TOKEN_TMP" <<'PYJSON'
import json, os, sys
path, namespaces, token_tmp = sys.argv[1], sys.argv[2], sys.argv[3]
with open(token_tmp, "r", encoding="utf-8") as fh:
    token = fh.read().strip()
os.unlink(token_tmp)
if not token:
    sys.stderr.write("install-broker: no token on stdin\n")
    sys.exit(2)
payload = {}
if namespaces.strip():
    ns = [n.strip() for n in namespaces.split(",") if n.strip()]
    if not ns:
        sys.stderr.write("install-broker: --namespaces parsed to an empty list (only separators?)\n")
        sys.exit(2)
    payload["namespaces"] = ns
payload["token"] = token
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, ensure_ascii=True)
os.chmod(path, 0o600)
PYJSON
rm -f "$TOKEN_TMP"
if [ "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    chown "$SUDO_USER" "$TOKEN_FILE"
fi
chmod 600 "$TOKEN_FILE"
ACTUAL_MODE="$(stat -c %a "$TOKEN_FILE")"
if [ "$ACTUAL_MODE" != "600" ]; then
    echo "install-broker: REFUSED: token file mode is $ACTUAL_MODE after hardening (want 600) - not proceeding" >&2
    exit 2
fi

# --- data dir: persist + own it like the token file --------------------------
DATA_DIR="${ZMEM_DATA:-/var/lib/zmem}"
if [ -e "$DATA_DIR" ] && [ ! -d "$DATA_DIR" ]; then
    echo "install-broker: REFUSED: effective ZMEM_DATA ('$DATA_DIR') is not a directory" >&2
    exit 2
fi
mkdir -p "$DATA_DIR"
if [ "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    chown "$SUDO_USER" "$DATA_DIR"
fi

# --- validate: FRESH child with the RECONSTRUCTED effective config -----------
# Validation runs BEFORE the drop-in is written: a failed install leaves no
# persisted configuration behind. The doctor report is printed so the
# failing checks are visible.
SCRIPTS="$REPO_ROOT/skills/memory/scripts"
DOCTOR="$SCRIPTS/doctor.py"
PROBE_OUT="$(mktemp)"
VALIDATION_OK=1
python3 - "$SCRIPTS" "$HUB_URL" "$TOKEN_FILE" "$DOCTOR" >"$PROBE_OUT" 2>&1 <<'PYEOF' || VALIDATION_OK=0
import os, subprocess, sys
scripts, url, token_file, doctor = sys.argv[1:5]
env = dict(os.environ)
env["ZMEM_REMOTE_URL"] = url
env["ZMEM_MCP_TOKEN_FILE"] = token_file
env["ZMEM_DATA"] = env.get("ZMEM_DATA", "/var/lib/zmem")
env.pop("ZMEM_MCP_TOKEN", None)
env.pop("ZMEM_MCP_TOKENS_FILE", None)
env.pop("ZMEM_MCP_URL", None)
sys.path.insert(0, scripts)
from fleet import transport
resolved = transport.resolve_transport_mode(env)
mode = resolved[0] if isinstance(resolved, tuple) else resolved
if mode is not transport.TransportMode.mcp:
    print("transport mode resolved to %r, not mcp" % (mode,), file=sys.stderr)
    sys.exit(10)
proc = subprocess.run([sys.executable, "-X", "utf8", doctor, "--fleet",
                       "--format", "json"], env=env, capture_output=True,
                      text=True)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
PYEOF
cat "$PROBE_OUT"
rm -f "$PROBE_OUT"
if [ "$VALIDATION_OK" -ne 1 ]; then
    echo "install-broker: broker validation FAILED (output above) - the token file at $TOKEN_FILE was written but NO configuration was persisted; fix the checks and re-run" >&2
    exit 1
fi

# --- persist the config (only after validation passes) -----------------------
PROFILE=/etc/profile.d/zmem-broker.sh
cat > "$PROFILE" <<EOF
# zmem broker configuration (issue #291; written by install-broker.sh)
export ZMEM_REMOTE_URL="$HUB_URL"
export ZMEM_MCP_TOKEN_FILE="$TOKEN_FILE"
export ZMEM_DATA="$DATA_DIR"
unset ZMEM_MCP_TOKEN ZMEM_MCP_URL ZMEM_MCP_TOKENS_FILE ZMEM_STORE
EOF
chmod 644 "$PROFILE"

echo
echo "Broker installed. The profile drop-in requires a new session before harnesses"
echo "inherit it. Residual: later user/process overrides can still shadow these"
echo "values (documented in docs/FLEET.md)."
