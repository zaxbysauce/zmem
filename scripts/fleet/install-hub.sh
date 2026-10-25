#!/usr/bin/env bash
# install-hub.sh — register the zmem hub on this Linux box (issue #291).
#
# Contract (Workstream T PR 12, AC3):
#   * the hub runs as a systemd service bound to 127.0.0.1 ONLY (never a
#     wildcard bind; the server itself refuses 0.0.0.0 without
#     ZMEM_MCP_ALLOW_INSECURE_BIND, and this installer never sets it);
#   * the tailnet exposes it via `tailscale serve` HTTPS;
#   * a principal map whose mode is not 0600 is REFUSED before anything is
#     configured;
#   * idempotent: re-running rewrites the unit and restarts it.
#
# Usage:
#   sudo ./install-hub.sh /path/to/zmem /srv/zmem-data /srv/zmem-data/principal-map.json [port]

set -euo pipefail

REPO_ROOT="${1:?usage: install-hub.sh REPO_ROOT DATA_DIR TOKENS_FILE [PORT]}"
DATA_DIR="${2:?DATA_DIR required}"
TOKENS_FILE="${3:?TOKENS_FILE required}"
PORT="${4:-8765}"

# --- refuse a world/group-readable principal map ---------------------------
if [ ! -f "$TOKENS_FILE" ]; then
    echo "install-hub: principal map not found: $TOKENS_FILE (create it with issue-principal.py first)" >&2
    exit 2
fi
MODE="$(stat -c %a "$TOKENS_FILE")"
if [ "$MODE" != "600" ]; then
    echo "install-hub: REFUSED: $TOKENS_FILE mode is $MODE (want 600). Fix it first: chmod 600 '$TOKENS_FILE'" >&2
    exit 2
fi

mkdir -p "$DATA_DIR"
SERVER_PY="$REPO_ROOT/hermes-plugin/server/mcp_server.py"
[ -f "$SERVER_PY" ] || { echo "install-hub: mcp_server.py not found under $REPO_ROOT" >&2; exit 2; }

# --- systemd unit bound to 127.0.0.1 ONLY -----------------------------------
ENV_FILE="$DATA_DIR/hub.env"
cat > "$ENV_FILE" <<EOF
ZMEM_MCP_TOKENS_FILE=$TOKENS_FILE
ZMEM_DATA=$DATA_DIR
EOF
chmod 600 "$ENV_FILE"

UNIT=/etc/systemd/system/zmem-hub.service
cat > "$UNIT" <<EOF
[Unit]
Description=zmem fleet hub (issue #291)
After=network-online.target

[Service]
EnvironmentFile=$ENV_FILE
ExecStart=/usr/bin/env python3 -X utf8 "$SERVER_PY" --host 127.0.0.1 --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now zmem-hub.service
sleep 2
systemctl --no-pager --lines=5 status zmem-hub.service || true

# --- tailscale serve: tailnet HTTPS -> localhost ONLY -----------------------
if command -v tailscale >/dev/null 2>&1; then
    tailscale serve --bg --https=443 "http://127.0.0.1:$PORT"
    tailscale serve status
else
    echo "install-hub: tailscale not found — install it, then run: tailscale serve --bg --https=443 http://127.0.0.1:$PORT" >&2
fi

echo
echo "Hub installed. Issue one principal per broker with python3 scripts/fleet/issue-principal.py,"
echo "then run scripts/fleet/install-broker.sh on each broker machine."
