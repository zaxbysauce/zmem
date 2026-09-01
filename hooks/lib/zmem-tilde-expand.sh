#!/usr/bin/env bash
# Shared tilde expansion for the hook data-dir resolvers — ONE
# implementation sourced by zmem-convention-capture.sh and
# zmem-session-start.sh (PRR-101 review round 2: every bash resolver of the
# lane must apply the same expansion the python readers apply, or a
# tilde-valued var splits writer from reader).
#
# Usage: zmem_tilde_expand   (mutates the global DATA_DIR in place)
# - no tilde            -> DATA_DIR and DATA_DIR_IS_NATIVE untouched
# - tilde + PYTHON_BIN  -> DATA_DIR becomes the expanduser result and
#                          DATA_DIR_IS_NATIVE=1 (python output is native to
#                          the interpreter that consumes it, so callers
#                          skip to_py_path)
# - tilde, no python    -> DATA_DIR left verbatim (fail-open, identical to
#                          the pre-expansion behavior)
zmem_tilde_expand() {
  case "$DATA_DIR" in
    "~"*)
      [ -n "$PYTHON_BIN" ] || return 0
      _ZTE_EXPANDED="$("$PYTHON_BIN" -c 'import os, sys; sys.stdout.write(os.path.expanduser(sys.argv[1]))' "$DATA_DIR" 2>/dev/null)"
      if [ -n "$_ZTE_EXPANDED" ]; then
        DATA_DIR="$_ZTE_EXPANDED"
        DATA_DIR_IS_NATIVE=1
      fi
      ;;
  esac
}
