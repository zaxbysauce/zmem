"""Operation-token derivation for the passive inject query (issue #88 / #85 direction 2).

Field finding from #85: at decision points the UserPromptSubmit prompt is
prose with ZERO lexical overlap with the operation-adjacent lessons that
matter ("use the swarm-pr-review skill on the PR" vs "git stash pop",
"reset --soft origin/main", "citation shifts"). 216/239 hook decisions in
that session retrieved an EMPTY pool while explicit 2–3 operation-keyword
queries returned the missed lessons at rank 1–2. The retrieval signal at
decision points lives in the tool commands the session is executing.

This module is the SINGLE source for turning recent tool activity into
recall-query tokens — the UserPromptSubmit hook body and the gold-set eval
composer both import it, so the eval measures exactly what the hook does.

LLM-free by design: pure string functions, no network, no writes.

FTS5 operator safety: every token emitted by ``derive_ops_tokens`` matches
the allowlist regex ``[A-Za-z0-9._/+-]+`` (alphanumerics, dot, underscore,
slash, plus, hyphen). FTS operators (AND/OR/NOT/NEAR, parentheses, quotes)
can never survive as syntax through this path. Defense-in-depth: the store's
own FTS lane escapes ``"`` → ``""`` and wraps every whitespace term as a
quoted prefix phrase before it reaches the MATCH expression, so even a
hypothetical unsafe token could not become FTS syntax there.

Runs standalone (no storelib siblings required at import time).
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import List

# A token must be entirely allowlisted characters to be emitted.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")

# Command verbs whose SUBCOMMANDS carry the retrieval signal (a bare "git" or
# "bun" token matches nothing useful; "git stash pop" is the lesson-shaped
# phrase). Ordered longest-first is irrelevant here — we walk the command's
# tokens and, when the head matches a known runner, keep a bounded window of
# following tokens.
_RUNNER_HEADS = frozenset({
    "git", "gh", "svn",
    "npm", "pnpm", "yarn", "bun", "deno",
    "pip", "pip3", "uv", "poetry", "pipx",
    "cargo", "go", "mvn", "gradle", "make", "cmake",
    "docker", "kubectl", "helm", "terraform",
    "python", "python3", "pytest", "py.test", "tox", "nox",
    "node", "npx", "tsc", "eslint", "biome", "prettier", "ruff", "mypy",
    "robocopy", "rsync", "curl",
})

# Hazardous git/gh subcommands — always kept when following a runner head
# (push, reset, stash, rebase, merge...). These are the #85 incident verbs.
_HAZARDOUS_SUBS = frozenset({
    "push", "pull", "fetch", "reset", "stash", "rebase", "merge", "revert",
    "checkout", "restore", "clean", "cherry-pick", "filter-branch",
    "submodule", "worktree", "amend", "force-push", "apply", "pop",
})

# Hard caps: the ops tail must stay small relative to the prose query so it
# augments (not replaces) the prompt. Per the #85 maintainer spec (direction
# 2/B), the ops tokens occupy a FIXED RESERVED SLICE INSIDE the existing
# 500-char query cap — never appended after prompt[:500] (a long swarm
# prompt would keep the prose and throw away "stash pop").
_MAX_TOKENS = 12
_MAX_TAIL_CHARS = 240
_PROMPT_KEEP_CHARS = 500
_RESERVED_TAIL_CHARS = 150

# Ring bounds (write side mirrors these in the hook; both live here so the
# reader and any future writer cannot drift).
_RING_TRIM_TO_LINES = 64
_RING_MAX_BYTES = 65536


def _clean_token(tok: str) -> str:
    """Lowercase, strip surrounding punctuation noise, and allowlist-check
    one candidate token. Returns "" when the token must not be emitted."""
    tok = tok.strip().strip("\"'`;,(){}[]<>|&$").lower()
    if not tok:
        return ""
    if not _TOKEN_RE.match(tok):
        return ""
    return tok


def derive_ops_tokens(*events: str) -> List[str]:
    """Derive bounded, deduped, lowercased operation tokens from tool-event
    strings (shell commands, file paths, test names).

    Pure and LLM-free. Every emitted token passes the character allowlist,
    so the result is safe to splice into the FTS query by construction.
    Order: most recent event's tokens first (callers pass newest-last; we
    walk reversed so fresh operations win the cap).
    """
    out: List[str] = []
    seen = set()

    def _push(tok: str) -> None:
        if len(out) >= _MAX_TOKENS:
            return
        if tok and tok not in seen:
            seen.add(tok)
            out.append(tok)

    for event in reversed(list(events)):  # newest first
        if not event:
            continue
        if len(out) >= _MAX_TOKENS:
            break
        words = event.split()
        if not words:
            continue
        head = words[0].strip().strip("\"'`").lower()
        head = head.rsplit("/", 1)[-1] or head  # /usr/bin/git → git
        if head in _RUNNER_HEADS:
            # Keep the runner plus a bounded subcommand window — but only
            # tokens that are STRUCTURAL, not argument values: the primary
            # subcommand (first word after the runner), known hazardous
            # subcommands (the #85 incident verbs), and file-shaped tokens
            # (a dot/dash/underscore/slash — paths, test files, refs like
            # origin/main). A bare word argument ('hunter2', 'main') is an
            # argument VALUE and never reaches the ring (spec B: allowlisted
            # argv tokens only, no secrets).
            _push(head)
            for i, w in enumerate(words[1:5]):
                t = _clean_token(w)
                if not t or t.startswith("-"):
                    continue
                if i == 0 or t in _HAZARDOUS_SUBS or any(
                        c in t for c in "./_-"):
                    _push(t)
        else:
            # Non-runner event: file paths / test names — keep the basename
            # (the part that actually matches lesson corpora like
            # "atomic-write-ratchet.test.ts").
            last = words[-1] if words else ""
            base = last.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            t = _clean_token(base)
            if t and ("." in t or "-" in t or "_" in t):
                _push(t)

    # Bound the total tail size.
    tail: List[str] = []
    used = 0
    for tok in out:
        if used + len(tok) + 1 > _MAX_TAIL_CHARS:
            break
        tail.append(tok)
        used += len(tok) + 1
    return tail


def compose_inject_query(prompt: str, ops: str) -> str:
    """Compose the passive inject query: prose + derived ops tokens, with
    the ops tail occupying a fixed reserved slice INSIDE the 500-char cap
    (never appended after it — #85 spec B: concatenating then capping keeps
    the prose and throws away the operation verbs on long prompts).

    BYTE-EXACT IDENTITY CONTRACT: when ``ops`` is empty (or yields no
    tokens) the result is exactly ``prompt.strip()[:500]`` — this is what
    keeps every legacy gold item's query, and therefore the committed eval
    scores, identical when no ops context exists. Pinned by unit test.
    """
    base = (prompt or "").strip()[:_PROMPT_KEEP_CHARS]
    tokens = derive_ops_tokens(ops) if ops else []
    if not tokens:
        return base
    tail = " ".join(tokens)
    if len(tail) > _RESERVED_TAIL_CHARS:
        # Cut at the last complete token so a verb is never severed mid-word.
        tail = tail[:_RESERVED_TAIL_CHARS].rsplit(" ", 1)[0]
    prose_budget = max(0, _PROMPT_KEEP_CHARS - _RESERVED_TAIL_CHARS)
    prose = base[:prose_budget].rstrip()
    return (prose + " " + tail).strip()[:_PROMPT_KEEP_CHARS]


# Kill switch (the #85 maintainer spec names this exact env var): any value
# other than "0" leaves the lane on; "0" disables it everywhere the helper
# is consulted (hook body, Hermes prefetch, any future surface).
_QUERY_CONTEXT_ENV = "ZMEM_QUERY_CONTEXT"


def query_context_enabled() -> bool:
    """True unless ZMEM_QUERY_CONTEXT=0 (kill switch, #85 spec B)."""
    return os.environ.get(_QUERY_CONTEXT_ENV, "1").strip() != "0"


def _ring_path(data_dir: str, session_id: str) -> str:    # Session ids are host-generated (UUID-ish), but sanitize anyway — the
    # ring filename must never escape the ops dir.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id)[:128] or "session"
    return os.path.join(data_dir, "ops", safe + ".log")


def read_ops_ring(data_dir: str, session_id: str, max_events: int = 8) -> List[str]:
    """Read the newest ``max_events`` operation descriptors from the
    per-session ring at ``<data_dir>/ops/<session>.log``.

    Fail-open: missing dir/file, torn lines (concurrent append), or any IO
    error degrade to []. Returns oldest-first within the returned window so
    callers (and tests) see the same order the events happened in.
    """
    if not data_dir or not session_id:
        return []
    path = _ring_path(data_dir, session_id)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    events: List[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue  # torn concurrent append — skip
        if not isinstance(obj, dict):
            continue
        desc = obj.get("ops") or ""
        if isinstance(desc, str) and desc:
            events.append(desc)
    if max_events <= 0:
        return []
    return events[-max_events:]


def append_ops_ring(data_dir: str, session_id: str, tool: str, op: str) -> bool:
    """Append one operation event to the per-session ring (write side,
    called from the PostToolUse / post_tool_call hooks).

    Spec B (#85): the sidecar stores ONLY the tool name plus the
    ALLOWLISTED operation tokens (git subcommand chains, test-runner verbs,
    edited-path basenames) — never a raw command dump, stdout, secrets, or
    a free-text transcript tail. Derivation happens here, at the single
    write chokepoint, so no raw command ever lands on disk. An event that
    derives to nothing is not written at all.

    Ring-capped: when the file exceeds ``_RING_MAX_BYTES`` it is trimmed to
    the newest ``_RING_TRIM_TO_LINES`` lines before the append. Best-effort:
    returns False on any failure — callers never let ring health affect
    hook behavior.
    """
    if not data_dir or not session_id or not op:
        return False
    desc = " ".join(derive_ops_tokens(op))
    if not desc:
        return False
    path = _ring_path(data_dir, session_id)
    line = json.dumps({
        "ts": int(time.time()),
        "tool": (tool or "")[:32],
        "ops": desc,
    })
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) > _RING_MAX_BYTES:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-_RING_TRIM_TO_LINES:]
                with open(path, "w", encoding="utf-8") as f:
                    f.writelines(tail)
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except OSError:
        return False
