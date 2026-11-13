#!/usr/bin/env python
"""OpenCode store-side JSON bridge (issue #66 design step 3).

The ONLY OpenCode-side store process: reads ONE JSON request on stdin,
answers ONE JSON envelope on stdout. Never imports the host hook stack,
never throws into the caller — a bridge failure writes exactly one stderr
diagnostic and answers the EMPTY envelope (every required key present,
``excluded=[]``) so the OpenCode plugin can fail open.

Contract highlights (see the issue + trace plan):
- The bridge is the only OpenCode-side consumer of #158's
  ``select_and_budget_for_injection`` selector; OpenCode passes NO lane
  (``opencode`` is not a permitted injection lane), so its decision log
  omits ``lane=``.
- The selector is the sole budget owner: this bridge NEVER calls
  ``apply_token_budget`` a second time.
- Moments are restricted to the selector's five; anything else is refused
  with the empty envelope + a diagnostic.
- stdin is decoded and stdout is encoded EXPLICITLY as UTF-8 via the raw
  buffers (repo convention, cf. the ``--content -`` sentinel fix for issue
  #460): the process stdio codec must never decide how payload bytes are
  decoded on a cp1252 host.
"""
import io
import json
import os
import sys

_BRIDGE_PATH = os.path.dirname(os.path.abspath(__file__))

PERMITTED_MOMENTS = ("session_start", "user_prompt", "pretool", "subagent",
                     "precompact")

# Envelope keys the selector guarantees (storelib.inject
# INJECTION_ENVELOPE_REQUIRED); the empty envelope answers every one.
REQUIRED_KEYS = ("results", "count", "omitted", "reason", "excluded",
                 "candidate_ids", "tokens_used", "tokens_budget",
                 "budget_dropped", "budget_admission", "budget_truncated",
                 "budget_dropped_protected", "arms", "rendered")


def empty_envelope(reason: str, budget: int) -> dict:
    """Fail-open envelope: every required key, nothing else promised."""
    return {
        "results": [],
        "count": 0,
        "omitted": 0,
        "reason": reason,
        "excluded": [],
        "candidate_ids": [],
        "tokens_used": 0,
        "tokens_budget": int(budget),
        "budget_dropped": 0,
        "budget_admission": 0,
        "budget_truncated": 0,
        "budget_dropped_protected": 0,
        "arms": {},
        "rendered": "",
    }


def _resolve_namespace(request: dict) -> str:
    """Request namespace wins; else ZMEM_NAMESPACE; else the canonical
    git-remote resolver over the probed worktree (the same key the launcher
    derives); else user:global."""
    requested = str(request.get("namespace") or "").strip()
    if requested:
        return requested
    explicit = os.environ.get("ZMEM_NAMESPACE", "").strip()
    if explicit:
        return explicit
    worktree = str(request.get("worktree") or "").strip()
    if worktree:
        try:
            sys.path.insert(0, _BRIDGE_PATH)
            import host as _host  # noqa: PLC0415 - deliberate local import
            resolved = _host.resolve_namespace(worktree)
            if resolved:
                return resolved
        except Exception:  # noqa: BLE001 - fall through to the default
            pass
    return "user:global"


def run_request(request: dict) -> dict:
    """Run one injection request through the #158 selector. Fail-open."""
    budget = int(request.get("budget_tokens") or 1500)
    try:
        moment = str(request.get("moment") or "")
        if moment not in PERMITTED_MOMENTS:
            raise ValueError("moment not permitted for opencode: %r"
                             % moment)
        session_id = str(request.get("session_id") or "").strip()
        if not session_id:
            raise ValueError("session_id is required (issue #66: an "
                             "unsessioned drive must not inject under a "
                             "shared identity)")
        query = str(request.get("query") or "")
        namespace = _resolve_namespace(request)

        sys.path.insert(0, _BRIDGE_PATH)
        import storelib  # noqa: PLC0415 - env must be pinned by the caller

        conn = storelib.connect()
        try:
            envelope = storelib.select_and_budget_for_injection(
                conn,
                query=query,
                namespace=namespace,
                moment=moment,
                session_id=session_id,
                lane=None,  # opencode is not a permitted injection lane
                limit=int(request.get("limit") or 5),
                global_limit=int(request.get("global_limit") or 3),
                budget_tokens=budget,
                no_telemetry=True,
            )
        finally:
            conn.close()
        # Optional selector keys ride along (injection_risk,
        # candidate_lanes, budget_note); `excluded` is [] when empty by the
        # selector's own contract. Normalize the one optional that may be
        # absent so consumers never KeyError on it.
        envelope.setdefault("excluded", [])
        return envelope
    except Exception as exc:  # noqa: BLE001 - fail open, never throw
        try:
            sys.stderr.write("[zmem-opencode] bridge failure: %s\n" % (exc,))
        except Exception:  # noqa: BLE001 - diagnostic is best-effort
            pass
        return empty_envelope("bridge-error", budget)


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("[zmem-opencode] bridge stdin failure: %s\n" % (exc,))
        out = empty_envelope("bridge-error", 1500)
    else:
        try:
            request = json.loads(raw.decode("utf-8", errors="replace"))
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write("[zmem-opencode] bridge decode failure: %s\n"
                             % (exc,))
            out = empty_envelope("bridge-error", 1500)
        else:
            out = run_request(request)
    payload = json.dumps(out, ensure_ascii=False) + "\n"
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  newline="\n")
    sys.stdout.write(payload)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
