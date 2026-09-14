#!/usr/bin/env bash
# zmem-postcompact.sh — Claude Code PostCompact compatibility hook.
#
# Issue #158 retired the compact snapshot/summary sidecar. PreCompact now
# clears the delivery ledger through the store CLI and the following
# SessionStart uses the ordinary session_start selector. Claude still sends
# PostCompact, and the hook remains registered for host compatibility, but
# there is no context channel or persistence work to perform here.
#
# Keep this adapter deliberately fail-open and output a valid empty hook
# result. In particular, do not resolve store.py or import storelib: doing so
# would recreate the old PostCompact-owned sidecar path.

set -uo pipefail

printf '{}\n'
exit 0
