#!/usr/bin/env bash
# install-hub.sh - register the zmem hub on this Linux box (issue #291).
#
# Contract (Workstream T PR 12, AC3):
#   * the hub runs as a systemd service bound to 127.0.0.1 ONLY (never a
#     wildcard bind; the server itself refuses 0.0.0.0 without
#     ZMEM_MCP_ALLOW_INSECURE_BIND, and this installer never sets it),
#     as the INVOKING USER (never root) with NoNewPrivileges hardening;
#   * the tailnet exposes it via `tailscale serve` HTTPS;
#   * a principal map whose mode is not 0600, whose owner is neither the
#     service user nor root, is REFUSED before anything is configured;
#   * idempotent: re-running rewrites the unit and RESTARTS it, so the
#     running hub converges onto the new configuration;
#   * the installer verifies the service actually came up before printing
#     success.
#
# Usage:
#   sudo ./install-hub.sh /path/to/zmem /srv/zmem-data /srv/zmem-data/principal-map.json [port]

set -euo pipefail

REPO_ROOT="${1:?usage: install-hub.sh REPO_ROOT DATA_DIR TOKENS_FILE [PORT [HTTPS_PORT]]}"
DATA_DIR="${2:?DATA_DIR required}"
TOKENS_FILE="${3:?TOKENS_FILE required}"
PORT="${4:-8765}"
HTTPS_PORT="${5:-443}"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "install-hub: systemd (systemctl) is required - this installer targets Linux hosts only" >&2
    exit 2
fi
case "$PORT" in
    ''|*[!0-9]*) echo "install-hub: PORT must be numeric (got: $PORT)" >&2; exit 2 ;;
    *) [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || { echo "install-hub: PORT must be 1-65535 (got: $PORT)" >&2; exit 2; } ;;
esac
# Issue #423: the serve HTTPS port is a parameter (default 443).
case "$HTTPS_PORT" in
    ''|*[!0-9]*) echo "install-hub: HTTPS_PORT must be numeric (got: $HTTPS_PORT)" >&2; exit 2 ;;
    *) [ "$HTTPS_PORT" -ge 1 ] && [ "$HTTPS_PORT" -le 65535 ] || { echo "install-hub: HTTPS_PORT must be 1-65535 (got: $HTTPS_PORT)" >&2; exit 2; } ;;
esac
case "$DATA_DIR$TOKENS_FILE$REPO_ROOT" in
    *[\\\#\$\'\"]*)
        echo "install-hub: REFUSED: paths containing whitespace-quoting/metacharacters (\\ # \$ ' \") are not supported by the generated unit (got one in DATA_DIR/TOKENS_FILE/REPO_ROOT)" >&2
        exit 2 ;;
esac
SERVICE_USER="${SUDO_USER:-$(id -un)}"
if [ "$SERVICE_USER" = "root" ]; then
    echo "install-hub: REFUSED: run via sudo from an unprivileged account (the hub must not run as root; SUDO_USER names the service user)" >&2
    exit 2
fi

# --- refuse a world/group-readable or foreign-owned principal map -----------
if [ ! -f "$TOKENS_FILE" ]; then
    echo "install-hub: principal map not found: $TOKENS_FILE (create it with issue-principal.py first)" >&2
    exit 2
fi
MODE="$(stat -c %a "$TOKENS_FILE")"
if [ "$MODE" != "600" ]; then
    echo "install-hub: REFUSED: $TOKENS_FILE mode is $MODE (want 600). Fix it first: chmod 600 '$TOKENS_FILE'" >&2
    exit 2
fi
MAP_OWNER="$(stat -c %U "$TOKENS_FILE")"
if [ "$MAP_OWNER" != "$SERVICE_USER" ] && [ "$MAP_OWNER" != "root" ]; then
    echo "install-hub: REFUSED: $TOKENS_FILE is owned by '$MAP_OWNER', not the service user ($SERVICE_USER) or root - a non-owner could append principals to a map the root-adjacent hub trusts" >&2
    exit 2
fi

mkdir -p "$DATA_DIR"
chown "$SERVICE_USER" "$DATA_DIR"
SERVER_PY="$REPO_ROOT/hermes-plugin/server/mcp_server.py"
[ -f "$SERVER_PY" ] || { echo "install-hub: mcp_server.py not found under $REPO_ROOT" >&2; exit 2; }
PYTHON_BIN="$(command -v python3)"
[ -n "$PYTHON_BIN" ] || { echo "install-hub: python3 not found on PATH" >&2; exit 2; }

# --- systemd unit bound to 127.0.0.1 ONLY, as the service user --------------
ENV_FILE="$DATA_DIR/hub.env"
cat > "$ENV_FILE" <<EOF
ZMEM_MCP_TOKENS_FILE=$TOKENS_FILE
ZMEM_DATA=$DATA_DIR
EOF
chown "$SERVICE_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

UNIT=/etc/systemd/system/zmem-hub.service
cat > "$UNIT" <<EOF
[Unit]
Description=zmem fleet hub (issue #291)
After=network-online.target

[Service]
User=$SERVICE_USER
UMask=0077
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=$DATA_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PYTHON_BIN -X utf8 "$SERVER_PY" --host 127.0.0.1 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl restart zmem-hub.service
sleep 2
if ! systemctl --no-pager --lines=5 status zmem-hub.service; then
    echo "install-hub: REFUSED: the hub service did not come up (status above) - not configuring tailscale serve against a dead port" >&2
    exit 1
fi

# --- tailscale serve: tailnet HTTPS -> localhost ONLY -----------------------
# Issue #423: refuse an occupied :443 before re-pointing serve (a live
# non-zmem handler there would be silently replaced); -HttpsPort/HTTPS_PORT
# publishes the hub on another port instead.
if command -v tailscale >/dev/null 2>&1; then
    SERVE_STATUS="$(tailscale serve status 2>/dev/null || true)"
    # Review round 1: line-scoped (herestring, not a pipe - no SIGPIPE under
    # pipefail) and PORT-aware detection. An occupier is a web handler with
    # no explicit port or :443 (tailscale suppresses the default) or a
    # TLS-terminated TCP forward on 443; a handler on another port is NOT a
    # :443 occupier, and a web handler already proxying OUR localhost port
    # is the idempotent re-run.
    OCCUPIER=0; OURS=0
    while IFS= read -r line; do
        case "$line" in
            *"|"*"-- tcp://"*":443 "*) OCCUPIER=1 ;;
        esac
        printf '%s' "$line" | grep -Eq '^https?://[^/: ]+(:443)?([[:space:]]|$)' && OCCUPIER=1
        printf '%s' "$line" | grep -Eq "proxy http://127\.0\.0\.1:$PORT([^0-9]|\$)" && OURS=1
    done <<EOF_STATUS
$SERVE_STATUS
EOF_STATUS
    if [ "$HTTPS_PORT" = "443" ] && [ "$OCCUPIER" = 1 ] && [ "$OURS" = 0 ]; then
        echo "install-hub: REFUSED: tailscale serve already has a handler on :443" >&2
        echo "(an operator service is live there). Re-run with an explicit" >&2
        echo "HTTPS_PORT=<n> (5th arg) to publish the hub on another port" >&2
        echo "instead of replacing it. Current serve status:" >&2
        printf '%s
' "$SERVE_STATUS" >&2
        exit 2
    fi
    tailscale serve --bg --https="$HTTPS_PORT" "http://127.0.0.1:$PORT"
    tailscale serve status
else
    echo "install-hub: tailscale not found - install it, then run: tailscale serve --bg --https=$HTTPS_PORT http://127.0.0.1:$PORT" >&2
fi

echo
echo "Hub installed (service zmem-hub as user $SERVICE_USER)."
echo "Issue one principal per broker with python3 scripts/fleet/issue-principal.py,"
echo "then run scripts/fleet/install-broker.sh on each broker machine."
