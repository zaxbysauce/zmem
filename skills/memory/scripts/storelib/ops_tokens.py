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

import hashlib
import json
import os
import re
import shlex
import sys
import time
from typing import List, Optional

# A token must be entirely allowlisted characters to be emitted.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")

# Secret-SHAPED tokens (review PRR-91-002): common credential prefixes are
# dropped before the file-shaped gate so a literal token pasted into a
# command never lands on disk or in a query. Shape check, not a redaction
# guarantee — operators must still not paste secrets into commands.
# KNOWN FALSE-POSITIVE COST (#93 B4): any token starting one of these
# prefixes is dropped EVEN WHEN IT IS A LEGITIMATE FILENAME or config var
# (a file literally named `sk-something.ts`, `npm_config_registry`, …) —
# fail-safe by design (recall-signal loss, never a leak); rename if a real
# path collides.
_SECRET_SHAPE_RE = re.compile(
    r"^(?:gh[pousr]_|github_pat_|sk-ant|sk-proj|sk-|xox[baprs]-|AKIA|"
    r"glpat-|shpat_|dop_v1_|npm_|pypi-)",
    re.IGNORECASE,
)

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
    "py",  # Windows launcher; canonicalized to "python" at normalization
    "node", "npx", "tsc", "eslint", "biome", "prettier", "ruff", "mypy",
    "robocopy", "rsync", "curl",
})

# Issue #176: the public, DETERMINISTIC view of the runner-head vocabulary
# for the entity classifier's `command` kind (membership tests only). The
# frozenset above stays the single source — this alias is sorted so the
# tuple is stable across processes despite frozenset iteration order;
# token extraction is unchanged.
RUNNER_HEADS: tuple[str, ...] = tuple(sorted(_RUNNER_HEADS))

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

# Checkpoint phrases are deliberately immutable and ordered.  Several command
# shapes overlap (a forced push is also a push), so table order is the
# precedence contract for the matcher below.  Keep this data in the storelib
# boundary: passive hooks transport input but never duplicate this policy.
CHECKPOINT_PHRASES = (
    ("stash-consume", "foreign-stash conflict verify stash list"),
    ("reset", "stale tree fetch main rebase verify diff"),
    ("force-push", "stale tree fetched base force-with-lease"),
    ("branch-publication", "stale tree fetched base force-with-lease"),
    ("base-rewrite", "base drift citation re-pin"),
    ("path-test", "basename ratchet citation re-pin local battery"),
)

_PRETOOL_FIELDS = (
    "command", "cmd", "file_path", "notebook_path", "path", "description",
)
_CHECKPOINT_INPUT_CHARS = 150


def checkpoint_query_expansion(tool_input: object) -> str:
    """Return the one bounded checkpoint phrase selected from ``tool_input``.

    Matching is over compact, sorted-key JSON and only the first 150 Unicode
    characters.  The full object is canonicalized so the slice is independent
    of insertion order; callers still use only ``_PRETOOL_FIELDS`` when
    deriving operation tokens.  Invalid/non-object input and unknown command
    shapes fail closed.
    """
    if not isinstance(tool_input, dict):
        return ""
    try:
        canonical = json.dumps(
            tool_input, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return ""
    text = canonical[:_CHECKPOINT_INPUT_CHARS].lower()

    # Ordered predicates are intentionally explicit.  In particular, ``list``
    # is not a consuming stash operation, and ordinary push is checked only
    # after its force-specific form.
    if re.search(r"git stash\s+(?:pop|apply|drop)(?:\b|\W)", text):
        return CHECKPOINT_PHRASES[0][1]
    if re.search(r"git reset\s+--(?:soft|hard)(?:\b|\W)", text):
        return CHECKPOINT_PHRASES[1][1]
    if re.search(
            r'git push\s+[^"}]*?(?:--force-with-lease|--force|-f)(?:\b|\W)',
            text):
        return CHECKPOINT_PHRASES[2][1]
    if re.search(r"git push(?:\b|\W)", text):
        return CHECKPOINT_PHRASES[3][1]
    if re.search(r"git merge\s+--squash(?:\b|\W)", text):
        return CHECKPOINT_PHRASES[4][1]

    if re.search(
            r'"(?:description|file_path|notebook_path|path)":"[^"}]*'
            r'(?:test|citation)', text):
        return CHECKPOINT_PHRASES[5][1]
    return ""


def compose_pretool_query(tool_input: object) -> tuple[List[str], str]:
    """Derive current pre-tool operation tokens and one checkpoint phrase."""
    if not query_context_enabled() or not isinstance(tool_input, dict):
        return [], ""
    events = [
        value.strip() for key in _PRETOOL_FIELDS
        if isinstance(value := tool_input.get(key), str) and value.strip()
    ]
    # Flatten exactly as the existing pretool path does, keeping the matcher
    # and token derivation separate so checkpoint-only events remain useful.
    tokens = derive_ops_tokens(" ".join(events)) if events else []
    return tokens, checkpoint_query_expansion(tool_input)


def _clean_token(tok: str) -> str:
    """Lowercase, strip surrounding punctuation noise, and allowlist-check
    one candidate token. Returns "" when the token must not be emitted."""
    tok = tok.strip().strip("\"'`;,(){}[]<>|&$").lower()
    if not tok:
        return ""
    # Keep file-shaped context useful without persisting absolute or nested
    # paths from host commands.  The basename is sufficient for recall and
    # prevents usernames/project roots leaking into the ops ring.
    if ("/" in tok or "\\" in tok) and not tok.startswith(("origin/", "refs/")):
        tok = tok.replace("\\", "/").rsplit("/", 1)[-1]
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
        # Windows-shaped heads (issue #314 AC2): normalize BOTH path
        # separators and a trailing .exe/.bat/.cmd extension before the
        # membership test, then canonicalize the Windows Python launcher
        # `py` (and its extension forms) to `python` so its token shape
        # matches the POSIX alias exactly.
        head = head.rsplit("\\", 1)[-1]
        head = head.rsplit("/", 1)[-1] or head  # /usr/bin/git → git
        for _ext in (".exe", ".bat", ".cmd"):
            if head.endswith(_ext):
                head = head[: -len(_ext)]
                break
        if head == "py":
            head = "python"
        if head in _RUNNER_HEADS:
            # Keep the runner plus a bounded subcommand window — but only
            # tokens that are STRUCTURAL, not argument values: the primary
            # subcommand (first word after the runner), known hazardous
            # subcommands (the #85 incident verbs), and file-shaped tokens
            # (a dot/dash/underscore/slash — paths, test files, refs like
            # origin/main). A bare word argument ('hunter2', 'main') is an
            # argument VALUE and never reaches the ring (spec B: allowlisted
            # argv tokens only, no secrets). Secret-SHAPED tokens (common
            # credential prefixes — review PRR-91-002) are dropped even when
            # file-shaped: a literal `ghp_…` pasted into a command must not
            # land on disk or in a query.
            #
            # Interpreter-invocation asymmetry is EMERGENT token-shape
            # filtering, not a two-shape collapse policy (issue #251 AC4):
            # there is NO -c/-m special case. After a runner, a payload
            # token survives iff it passes the allowlist AND is the i==0
            # primary subcommand, a known hazardous subcommand, or
            # file-shaped (the rule above). So `python script.py` keeps the
            # script basename (i==0 — it is the operation-adjacent recall
            # signal), while `python -c "print(1)"` reduces to the bare
            # runner (-c takes the i==0 slot and hits the dash guard;
            # print(1) fails the allowlist) and `python -m pytest tests/`
            # reduces too (-m dashes out, pytest is a bare word at i>0,
            # tests/ collapses to an empty basename in _clean_token) — yet
            # `python -m pkg.mod` keeps `pkg.mod` because it is file-shaped.
            # The DECISION recorded here: do not "simplify" the first shape
            # into the second — collapsing `script.py` to a bare `python`
            # would lose that recall signal and re-admit broadly; that is a
            # behaviour change needing its own decision record and pin, not
            # a cleanup.
            _push(head)
            for i, w in enumerate(words[1:5]):
                # Preserve the shape of the raw argv token before
                # ``_clean_token`` strips a path to its basename.  A path
                # such as ``../check`` is structural context even though the
                # sanitized token is only ``check``; dropping that distinction
                # loses useful worktree/checkout recall.  Keep the secret
                # check below after sanitization so a path-shaped credential
                # is still rejected.
                raw_structural = any(c in w for c in "./_-\\")
                t = _clean_token(w)
                if not t or t.startswith("-"):
                    continue
                if _SECRET_SHAPE_RE.match(t):
                    continue
                if i == 0 or t in _HAZARDOUS_SUBS or any(
                        c in t for c in "./_-") or raw_structural:
                    _push(t)
        else:
            # Non-runner event: file paths / test names — keep the basename
            # (the part that actually matches lesson corpora like
            # "atomic-write-ratchet.test.ts").
            last = words[-1] if words else ""
            base = last.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            t = _clean_token(base)
            # Issue #163 (PRR-022): a dash-prefixed token becomes an argv
            # OPTION downstream (--ops-token / spliced FTS terms) and is
            # refused or misparsed — drop it at derivation, same rule the
            # runner branch applies.
            if t and not t.startswith("-") \
                    and not _SECRET_SHAPE_RE.match(t):
                if "." in t or "-" in t or "_" in t:
                    _push(t)

    # Bound the total tail size. A single token longer than the reserved
    # slice can never fit whole (review PRR-91-014) — drop it rather than
    # sever it mid-word downstream.
    tail: List[str] = []
    used = 0
    for tok in out:
        if len(tok) > _RESERVED_TAIL_CHARS:
            continue
        if used + len(tok) + 1 > _MAX_TAIL_CHARS:
            break
        tail.append(tok)
        used += len(tok) + 1
    return tail


def _sanitize_derived_tokens(ops: str) -> List[str]:
    """Safety-only filter for an ALREADY-DERIVED token string (issue #314).

    ``ops`` here is the whitespace-joined output of ``derive_ops_tokens``
    (the only production caller composes exactly that). Every token already
    passed the derivation chokepoint's guards, so NO shape or vocabulary
    filtering is re-applied — that re-derivation was the #314 defect (a
    non-runner-headed join reduced to ``[]`` and a runner-headed join lost
    tokens past the ``words[1:5]`` window). Only the per-token SAFETY
    guards are enforced again, cheaply, because this string boundary should
    not become a bypass: the character allowlist (via ``_clean_token``),
    the secret-shape rejection, the leading-dash guard, dedup, and the
    existing caps.
    """
    out: List[str] = []
    seen = set()
    for tok in ops.split():
        t = _clean_token(tok)
        if not t or t in seen:
            continue
        if t.startswith("-"):
            continue
        if _SECRET_SHAPE_RE.match(t):
            continue
        if len(out) >= _MAX_TOKENS:
            break
        seen.add(t)
        out.append(t)
    return out


def compose_inject_query(prompt: str, ops: str, checkpoint: str = "") -> str:
    """Compose the passive inject query: prose + derived ops tokens, with
    the ops tail occupying a fixed reserved slice INSIDE the 500-char cap
    (never appended after it — #85 spec B: concatenating then capping keeps
    the prose and throws away the operation verbs on long prompts).

    The separator shares the reserved budget (prose ≤ 349 + " " + tail ≤ 150
    = 500 exactly, review PRR-91-003), and derive_ops_tokens never returns a
    token longer than the slice — so no token is ever severed mid-word.

    BYTE-EXACT IDENTITY CONTRACT: when ``ops`` is empty (or yields no
    tokens) the result is exactly ``prompt.strip()[:500]`` — this is what
    keeps every legacy gold item's query, and therefore the committed eval
    scores, identical when no ops context exists. Pinned by unit test.
    """
    base = (prompt or "").strip()[:_PROMPT_KEEP_CHARS]
    valid_checkpoints = {phrase for _, phrase in CHECKPOINT_PHRASES}
    checkpoint = checkpoint if checkpoint in valid_checkpoints else ""
    tokens = _sanitize_derived_tokens(ops) if ops else []
    if not tokens:
        if not checkpoint:
            return base
        tail = checkpoint.strip()
        prose_budget = max(0, _PROMPT_KEEP_CHARS - len(tail) - 1)
        return (base[:prose_budget].rstrip() + " " + tail).strip()[:
            _PROMPT_KEEP_CHARS]
    tail = " ".join(tokens)
    if len(tail) > _RESERVED_TAIL_CHARS:
        # Cut at the last complete token (tokens are each ≤ the slice, so a
        # space boundary always exists within the cut for multi-token tails).
        tail = tail[:_RESERVED_TAIL_CHARS].rsplit(" ", 1)[0]
    if checkpoint:
        # The operation tail keeps its historical 150-character reservation;
        # the phrase is additive and consumes only the prose share.  Both are
        # joined once so neither can be split at the final 500-character cap.
        tail = (tail + " " + checkpoint.strip()).strip()
        prose_budget = max(0, _PROMPT_KEEP_CHARS - len(tail) - 1)
    else:
        prose_budget = max(0, _PROMPT_KEEP_CHARS - _RESERVED_TAIL_CHARS - 1)
    prose = base[:prose_budget].rstrip()
    return (prose + " " + tail).strip()[:_PROMPT_KEEP_CHARS]


# Kill switch (the #85 maintainer spec names this exact env var): any value
# other than "0" leaves the lane on; "0" disables it everywhere the helper
# is consulted (hook body, Hermes prefetch, any future surface).
_QUERY_CONTEXT_ENV = "ZMEM_QUERY_CONTEXT"


def query_context_enabled() -> bool:
    """True unless ZMEM_QUERY_CONTEXT=0 (kill switch, #85 spec B)."""
    return os.environ.get(_QUERY_CONTEXT_ENV, "1").strip() != "0"


# ---------------------------------------------------------------------------
# Pre-tool query sanitation.  The pretool lane exists to surface hazard
# lessons before an OPERATION; navigation, read-only inspection and inline
# scripts name no operation, and their raw text (cd/ls/wc, path fragments,
# heredoc Python source) only feeds unrelated lexical/vector/entity matches.
# Pure, stdlib-only, LLM-free; never raises.
# ---------------------------------------------------------------------------

_PRETOOL_SANITIZE_ENV = "ZMEM_PRETOOL_SANITIZE"
_PRETOOL_QUERY_CAP = 500

# Closed vocabulary: navigation / read-only inspection utilities.  Search
# tools (grep, rg) are deliberately NOT here (they are operations a lesson may
# target), nor git or any runner, nor anything with a common write mode
# (sort -o, date -s, tee).  The rule everywhere below is FAIL TOWARD KEEPING:
# a segment is dropped only when it is positively recognised as read-only.
_INSPECTION_HEADS = frozenset({
    "cd", "pushd", "popd", "ls", "dir", "pwd", "cat", "head", "tail", "wc",
    "echo", "printf", "true", "which", "type", "stat", "file",
    "cut", "tr", "less", "more", "whoami",
})
# Interpreters whose argument can be inline SOURCE CODE (not a shell
# command): interpreter -> the flags that say so.
_INLINE_CODE_FLAGS = {
    "python": ("-", "-c"),
    "python3": ("-", "-c"),
    "node": ("-", "-e", "--eval", "-p", "--print"),
    "bun": ("-e", "--eval"),
    "ruby": ("-", "-e"),
    "perl": ("-", "-e"),
}
_INPLACE_INTERPRETERS = frozenset({"ruby", "perl"})
# Shells: a ``-c`` string (or a heredoc body) is itself a shell command and
# is sanitized recursively, never dropped, so ``bash -c "git push --force"``
# still names its operation.
_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})
_POWERSHELLS = frozenset({"pwsh", "powershell"})
_SHELL_C_MAX_DEPTH = 2
_FIND_MUTATORS = frozenset({
    "-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0",
    "-fprintf", "-fls",
})
# The ONLY sed script accepted as read-only: an optional numeric/``$``
# address (or range) followed by ``p`` or ``=``.  Anything else (``w file``,
# GNU ``e`` execute, ``s///e``, ``/re/p``) is kept rather than analysed.
_SED_PRINT_ONLY_RE = re.compile(r"^[0-9,$~+!]*[p=]$")
_NULL_SINKS = frozenset({"/dev/null", "nul", "$null"})
_PUNCT_CHARS = frozenset("();<>|&")
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_ABS_PATH_RE = re.compile(r"^(?:[/~\\]|[A-Za-z]:[\\/])")
_SHORT_FLAGS_RE = re.compile(r"^-[A-Za-z]+$")
_POSIX_C_FLAG_RE = re.compile(r"^-[A-Za-z]*c$")
_HEREDOC_RE = re.compile(
    r"<<(?!<)-?[ \t]*\\?(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
# Commands that EXECUTE what they read on stdin: a heredoc fed to one of
# these is a script (shell or SQL), so its body is sanitized and kept.
_BODY_EXECUTORS = frozenset({
    "ssh", "sudo", "doas", "psql", "mysql", "sqlite3", "docker", "podman",
    "kubectl",
})


def pretool_sanitize_enabled() -> bool:
    """True unless ZMEM_PRETOOL_SANITIZE=0 (kill switch)."""
    return os.environ.get(_PRETOOL_SANITIZE_ENV, "1").strip() != "0"


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _abs_basename(tok: str) -> str:
    """Absolute path -> basename ("" when empty); anything else unchanged."""
    if _ABS_PATH_RE.match(tok):
        # A bare root (``/``, ``~``, ``C:\``) has no basename: keep it, so
        # ``rm -rf /`` does not read like ``rm -rf``.
        return tok.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] or tok
    return tok


def _is_punct(tok: str) -> bool:
    return bool(tok) and all(c in _PUNCT_CHARS for c in tok)


def _strip_heredocs(command: str) -> tuple:
    """Remove heredoc BODIES; return ``(text, bodies)``.

    The line that opens a heredoc stays (operator and delimiter word
    included) and so does every line after the terminator, so a command
    that follows a heredoc is still seen.  A ``<<WORD`` with no terminator
    line is not a heredoc (``a << b`` arithmetic, C++ streams): nothing is
    removed for it.
    """
    lines = command.split("\n")
    out: list = []
    bodies: list = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for match in _HEREDOC_RE.finditer(line):
            delim = match.group(2)
            end = i
            while end < len(lines) and lines[end].strip() != delim:
                end += 1
            if end >= len(lines):
                continue
            bodies.append("\n".join(lines[i:end]))
            i = end + 1
    return "\n".join(out), bodies


def _split_segments(command: str):
    """Return ``[{"t": tokens, "b": heredoc bodies, "w": writes}, ...]`` or
    ``None`` when the text cannot be tokenized (unbalanced quotes)."""
    text, bodies = _strip_heredocs(command.replace("\\\n", " "))
    # A newline separates commands exactly like ``;`` (shlex would fold it
    # into whitespace and glue two commands into one segment).
    text = text.replace("\n", " ; ")
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.escape = ""
        toks = list(lex)
    except ValueError:
        return None
    # The heredoc pre-pass is regex-only and blind to quoting.  Trust it
    # only when the tokenizer sees exactly as many heredoc operators as
    # bodies were removed; any disagreement (a ``<<`` inside a quoted
    # string, an unusual delimiter form) means real commands may have been
    # stripped as a "body", so the caller keeps the raw text instead.
    heredoc_ops = sum(
        1 for tok in toks
        if _is_punct(tok) and "<<" in tok and not tok.startswith("<<<"))
    if heredoc_ops != len(bodies):
        return None
    body_iter = iter(bodies)
    segments: list = []
    cur = {"t": [], "b": [], "w": False, "i": False, "p": False}
    pending = ""
    for tok in toks:
        if _is_punct(tok):
            pending = ""
            if "(" in tok and ("<" in tok or ">" in tok):
                # ``<(cmd)`` / ``>(cmd)`` process substitution feeds one
                # command's output to another as a FILE; the link between
                # the two is not tracked here, so the command is not judged.
                return None
            if "(" in tok:
                # Subshell / ``$(``: what follows is a COMMAND.  Start a
                # new segment so it is judged on its own.  A pipe INTO a
                # subshell feeds every command inside it, a link Pass 2
                # cannot follow: do not judge the command.
                if "|" in tok or (not cur["t"] and not cur["b"]
                                  and segments and segments[-1]["p"]):
                    return None
                if cur["t"] or cur["b"]:
                    segments.append(cur)
                cur = {"t": [], "b": [], "w": False, "i": False, "p": False}
            elif tok.startswith("<<<"):
                pending = "herestring"
            elif "<<" in tok:
                pending = "heredoc"
            elif "<" in tok or ">" in tok:
                if cur["t"] and cur["t"][-1].isdigit():
                    cur["t"].pop()
                # ``>``/``>>``/``&>`` write a file; ``>&`` duplicates an fd.
                if ">" in tok and not tok.startswith(">&"):
                    pending = "write"
                else:
                    pending = "skip"
                    if "<" in tok:
                        cur["i"] = True  # stdin read from a file
            else:
                # ``|`` / ``|&`` (not ``||``): this segment's output is the
                # next segment's input.
                cur["p"] = tok.startswith("|") and not tok.startswith("||")
                if cur["t"] or cur["b"]:
                    segments.append(cur)
                cur = {"t": [], "b": [], "w": False, "i": False, "p": False}
            continue
        if pending == "heredoc":
            cur["b"].append(next(body_iter, ""))
        elif pending == "herestring":
            cur["b"].append(tok)
        elif pending == "write":
            if tok.lower() not in _NULL_SINKS:
                # Output redirected to a real file is a WRITE: the segment
                # is kept whatever its head, and the target names the file.
                cur["w"] = True
                cur["t"].append(tok)
        elif pending != "skip":
            cur["t"].append(tok)
        pending = ""
    if cur["t"] or cur["b"]:
        segments.append(cur)
    return segments


def _normalise_head(tok: str) -> str:
    head = tok.strip().strip("\"'`").lower()
    head = head.rsplit("\\", 1)[-1]
    head = head.rsplit("/", 1)[-1] or head
    for ext in (".exe", ".bat", ".cmd"):
        if head.endswith(ext):
            head = head[: -len(ext)]
            break
    return "python" if head == "py" else head


def _segment_dropped(head: str, args: list, inline: bool) -> bool:
    """True only for a segment positively recognised as naming no operation."""
    if head in _INSPECTION_HEADS:
        return True
    if head == "sed":
        in_place = any(
            a.startswith("-i") or a.startswith("--in-place")
            or (_SHORT_FLAGS_RE.match(a) and "i" in a)
            for a in args)
        has_n = any(_SHORT_FLAGS_RE.match(a) and "n" in a for a in args)
        # ``sed -n 'w out.txt'`` writes a file through the script itself.
        # Read-only only in its one plain form: ``sed -n <print-script>
        # [files]``.  Any other flag (-e, -f, --expression: more scripts
        # that would each need checking) keeps the command.
        flags = [a for a in args if a.startswith("-")]
        operands = [a for a in args if not a.startswith("-")]
        print_only = bool(operands) and bool(
            _SED_PRINT_ONLY_RE.match(operands[0]))
        return (has_n and not in_place and flags == ["-n"] and print_only)
    if head == "find":
        return not any(a in _FIND_MUTATORS for a in args)
    return False


def _runs_opaque_code(head: str, args: list, inline: bool,
                      has_inner: bool) -> bool:
    """True when the segment executes code this module cannot read as a
    shell command: interpreter inline source (``python -c``, ``python -
    <<EOF``, ``node -e``, ``perl -e``) or a shell fed its script on stdin
    (``... | bash``, ``bash < deploy.sh``).  Such code can do anything —
    delete a tree, rewrite a store, force-push through subprocess — so the
    sanitizer ABSTAINS for the whole command rather than judge it."""
    flags = _INLINE_CODE_FLAGS.get(head)
    if flags is not None:
        if inline or any(a in flags for a in args):
            return True
        return any(_SHORT_FLAGS_RE.match(a) and "e" in a for a in args) \
            and head in _INPLACE_INTERPRETERS
    if head in _POSIX_SHELLS and not has_inner:
        # No -c string, no heredoc body, and no script operand: it reads
        # its commands from stdin.
        return not any(not a.startswith("-") for a in args)
    return False


class _Abstain(Exception):
    """Internal: the command runs code the sanitizer cannot classify."""


def _shell_inner_source(head: str, args: list, bodies: list):
    """The nested shell command a shell segment runs, or ``None``."""
    if head in _POSIX_SHELLS:
        for i, a in enumerate(args):
            if _POSIX_C_FLAG_RE.match(a):
                return args[i + 1] if i + 1 < len(args) else None
        return "\n".join(bodies) if bodies else None
    if head in _POWERSHELLS:
        for i, a in enumerate(args):
            if a.lower() in ("-c", "-command"):
                return " ".join(args[i + 1:]) or None
        return None
    if head == "cmd":
        for i, a in enumerate(args):
            if a.lower() in ("/c", "/k"):
                return " ".join(args[i + 1:]) or None
    return None


def sanitize_pretool_command(command: str, _depth: int = 0) -> str:
    """Reduce a shell command to the text that names an OPERATION.

    Navigation and read-only inspection segments are dropped, redirection
    plumbing is removed, and absolute paths collapse to their basename
    (relative paths are kept as written).  Everything not positively
    recognised as read-only is KEPT: a segment that writes a file through
    redirection, carries a command substitution, or runs a nested shell
    command (``bash -c``, ``pwsh -Command``, ``cmd /c``, a shell heredoc —
    sanitized recursively) survives.  A command that runs code this module
    cannot read (interpreter inline source, a shell fed its script on
    stdin) or that cannot be tokenized is not judged at all: it comes back
    whitespace-collapsed and otherwise untouched.  A
    command with no quotes, absolute paths, shell operators or dropped
    segments comes back whitespace-collapsed only.  Never raises.
    """
    if not isinstance(command, str):
        return ""
    try:
        segments = _split_segments(command)
        if segments is None:
            return _collapse(command)[:_PRETOOL_QUERY_CAP]
        def _words(tokens: list, collapse: bool = True) -> list:
            out = []
            for tok in tokens:
                if collapse:
                    tok = _abs_basename(tok)
                # Punctuation-only tokens are plumbing, except a bare path
                # root, which is a real (and dangerous) operand.
                if tok and (any(c.isalnum() for c in tok)
                            or _ABS_PATH_RE.match(tok)):
                    out.append(tok)
            return out

        # Pass 1: decide every segment.  ``None`` = dropped (read-only);
        # a list (possibly empty) = kept.
        decided: list = []
        for seg in segments:
            tokens = seg["t"]
            i = 0
            # Leading NAME=value assignments are plumbing, unless the value
            # runs a command (``X=`kubectl delete ns prod```): keep those.
            while (i < len(tokens) and _ENV_ASSIGN_RE.match(tokens[i])
                   and "`" not in tokens[i] and "$(" not in tokens[i]):
                i += 1
            # The assignments are skipped only to FIND the command; they
            # stay in a kept segment's text (``RAILS_ENV=production rake
            # db:drop`` names its target).
            assigns = tokens[:i]
            tokens = tokens[i:]
            if not tokens:
                decided.append(None)
                continue
            head = _normalise_head(tokens[0])
            args = tokens[1:]
            if head in _BODY_EXECUTORS and seg["b"] and not seg["i"]:
                # A heredoc fed to ssh/sudo/psql/... is a script it runs,
                # in a language this module does not assume (shell, SQL,
                # Python under sudo): the body is kept as written.
                out = _words(assigns + tokens)
                inner = _collapse("\n".join(seg["b"]))
                if inner:
                    out.append(inner)
                decided.append(out)
                continue
            inner_src = _shell_inner_source(head, args, seg["b"])
            if (inner_src is not None and not seg["i"]
                    and _depth < _SHELL_C_MAX_DEPTH):
                inner = sanitize_pretool_command(inner_src, _depth + 1)
                decided.append([inner] if inner else [])
                continue
            if _runs_opaque_code(head, args, bool(seg["b"]),
                                 inner_src is not None):
                raise _Abstain()
            substitution = any("$(" in t or "`" in t for t in tokens)
            if (inner_src is None and not seg["w"] and not substitution
                    and _segment_dropped(head, args, bool(seg["b"]))):
                decided.append(None)
                continue
            # A KEPT command that reads a heredoc, here-string or file on
            # stdin consumes (and may execute) text this module did not
            # read: do not judge the command at all.  That includes
            # ``cat > wipe.sh <<EOF`` -- the file it writes may be run by
            # the very next command.
            if seg["b"] or seg["i"]:
                raise _Abstain()
            # A sed/awk script such as ``/re/p`` looks like an absolute
            # path: leave those commands' operands exactly as written.
            decided.append(_words(assigns + tokens,
                                  collapse=head not in ("sed", "awk")))
        # Pass 2: a dropped segment whose output is piped into a kept one
        # is that command's INPUT (``echo "rm -rf x" | sudo bash``,
        # ``cat patch | git apply``): dropping it would drop the operation.
        for n in range(len(segments) - 1):
            if (decided[n] is None and segments[n]["p"]
                    and decided[n + 1] is not None):
                raise _Abstain()
        kept = [word for out in decided if out for word in out]
        return " ".join(kept)[:_PRETOOL_QUERY_CAP].strip()
    except Exception:
        # Includes _Abstain: the whole command is returned as it was.
        return _collapse(command)[:_PRETOOL_QUERY_CAP]


def pretool_query_text(tool_input: object) -> Optional[str]:
    """Sanitised pretool query for ``tool_input``.

    ``None``: not applicable (non-dict, no usable ``_PRETOOL_FIELDS`` string,
    or kill switch off) -- the caller keeps its legacy query.  ``""``: the
    call names no operation.  Never raises.
    """
    try:
        if not pretool_sanitize_enabled() or not isinstance(tool_input, dict):
            return None
        parts: list = []
        description = ""
        any_field = False
        has_command = False
        command_kept = False
        for key in _PRETOOL_FIELDS:
            val = tool_input.get(key)
            if not isinstance(val, str) or not val.strip():
                continue
            any_field = True
            if key in ("command", "cmd"):
                has_command = True
                s = sanitize_pretool_command(val)
                if s:
                    command_kept = True
                    parts.append(s)
            elif key == "description":
                description = _collapse(val)
            else:
                s = _abs_basename(_collapse(val))
                if s:
                    parts.append(s)
        if not any_field:
            return None
        if description and (not has_command or command_kept):
            parts.append(description)
        return " ".join(parts)[:_PRETOOL_QUERY_CAP].strip()
    except Exception:
        return None


def _sidecar_stem(session_id: str) -> str:
    """Sidecar names hash the COMPLETE session id (issue #122): two ids whose
    sanitized forms share a 128-character prefix therefore land on distinct
    files instead of silently sharing one ring/cursor."""
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]


def _ring_path(data_dir: str, session_id: str) -> str:
    """Ops ring sidecar path — sha256-truncated stem (issue #122); the ring
    filename can never escape the ops dir and never collides across long ids.
    """
    return os.path.join(data_dir, "ops", _sidecar_stem(session_id) + ".log")


def ring_cursor(data_dir: str, session_id: str) -> tuple:
    """Delivery cursor for the session's ring: ``(last_ts, event_count)``
    where event_count counts PARSED event lines. Consumed by the Hermes
    pre_llm_call reflect hook's at-most-once delivery marker (issue #90).

    The count matters because ring timestamps are second-granularity
    (``int(time.time())``): a second event appended in the SAME second after
    a delivery has an equal ts, and a ts-only comparison would suppress it
    forever (final-critic finding). Cursor comparison is lexicographic on
    ``(ts, count)`` compared against the stored delivered cursor:
    ``(300, 1) > (200, 5)`` delivers (a strictly newer ts always wins, even
    with a lower count after a trim); ``(200, 2) > (200, 1)`` delivers
    (same-second growth via the count tiebreak); ``(200, 1) <= (200, 2)``
    stays suppressed (same-second event whose post-trim count did not grow
    past the delivered cursor — accepted corner). Returns (0.0, 0) when the
    ring is absent or unreadable.
    """
    if not data_dir or not session_id:
        return (0.0, 0)
    path = _ring_path(data_dir, session_id)
    last = 0.0
    count = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    try:
                        ts = float(obj.get("ts", 0) or 0)
                    except (TypeError, ValueError):
                        continue
                    count += 1
                    if ts > last:
                        last = ts
    except OSError:
        return (0.0, 0)
    return (last, count)


def _marker_path(data_dir: str, session_id: str, suffix: str) -> str:
    return os.path.join(data_dir, "ops", _sidecar_stem(session_id) + suffix)


def write_delivered_cursor(data_dir: str, session_id: str, cursor: tuple) -> None:
    """Persist a delivery cursor marker (best-effort; never raises). Issue
    #122 pins the file bytes as ``"<ts> <count>\\n"`` (final LF, no CRLF
    translation) — the hermes_compat fixtures compare them byte-for-byte."""
    try:
        os.makedirs(os.path.dirname(
            _marker_path(data_dir, session_id, ".delivered")), exist_ok=True)
        with open(_marker_path(data_dir, session_id, ".delivered"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write("{0} {1}\n".format(float(cursor[0]), int(cursor[1])))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Prefetch retry budget (issue #122): a cursor gets two total prefetch
# attempts — one initial and one retry — persisted as
# ``ops/<sha256[:32]>.attempts`` holding ``"<cursor-ts> <attempts>\n"``
# (exact two-field format; pinned by the hermes_compat fixtures). Later
# invocations skip a cursor whose attempts are exhausted until the ring
# cursor changes. The hook writes the same file/format itself (it must not
# import storelib), so the format is a pinned cross-process contract.
# ---------------------------------------------------------------------------

_ATTEMPTS_SUFFIX = ".attempts"


def clear_retry_state(data_dir: str, session_id: str) -> None:
    """Drop the attempt marker (a successful non-empty render commits once)."""
    try:
        os.remove(_marker_path(data_dir, session_id, _ATTEMPTS_SUFFIX))
    except OSError:
        pass


def read_ops_ring(
    data_dir: str, session_id: str, max_events: int = 8, *,
    strict_errors: bool = False,
) -> List[str]:
    """Read the newest ``max_events`` operation descriptors from the
    per-session ring at ``<data_dir>/ops/<session>.log``.

    Fail-open legacy behavior remains the default: missing dir/file, torn lines
    (concurrent append), or any IO error degrade to [].  Query-rewrite opts into
    ``strict_errors`` so a permission/IO failure cannot be mistaken for a
    normal empty ring; a missing ring is still normal in strict mode. Returns
    oldest-first within the returned window so callers see the same order the
    events happened in.
    """
    if not data_dir or not session_id:
        return []
    path = _ring_path(data_dir, session_id)
    try:
        # The writer trims to _RING_MAX_BYTES, but a concurrent append or a
        # hand-created legacy sidecar must not turn a passive read into an
        # unbounded allocation.  Read a bounded tail and discard its first
        # partial physical line; malformed/torn entries already fail open
        # below.
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _RING_MAX_BYTES))
            raw = f.read(_RING_MAX_BYTES)
        text = raw.decode("utf-8", "replace")
        if size > _RING_MAX_BYTES:
            text = text.split("\n", 1)[1] if "\n" in text else ""
        lines = text.splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        if strict_errors:
            raise
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
    called from the post_tool_call hooks).

    Spec B (#85): the sidecar stores ONLY the tool name plus the
    ALLOWLISTED operation tokens (git subcommand chains, test-runner verbs,
    edited-path basenames) — never a raw command dump, stdout, secrets, or
    a free-text transcript tail. Derivation happens here, at the single
    write chokepoint, so no raw command ever lands on disk. An event that
    derives to nothing is not written at all.

    Ring-capped (#163): enforced AFTER the append — complete JSONL lines
    are dropped oldest-first until the ring holds at most
    ``_RING_TRIM_TO_LINES`` lines AND at most ``_RING_MAX_BYTES`` UTF-8
    bytes, then the newest complete lines are rewritten atomically. A torn
    trailing line (no newline) is never retained. Best-effort: returns
    False on any failure — callers never let ring health affect hook
    behavior.
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
        # PRR-007: repair a torn trailing line BEFORE appending — an append
        # in "a" mode would otherwise concatenate this event onto the torn
        # bytes, fusing both into one unparseable line.  Truncate to the
        # last complete newline so the torn partial bytes are dropped.
        with open(path, "a+b") as f:
            size = f.seek(0, os.SEEK_END)
            if size:
                f.seek(size - 1)
                if f.read(1) != b"\n":
                    f.seek(0)
                    raw = f.read()
                    last_nl = raw.rfind(b"\n")
                    f.seek(last_nl + 1 if last_nl >= 0 else 0)
                    f.truncate()
            f.write(line.encode("utf-8") + b"\n")
        # Issue #163: enforce BOTH caps after the append, so 65 appends of
        # any size can never leave 65 live events.  Keep the newest
        # complete lines that fit both bounds and rotate atomically
        # (#122 pattern: same-directory temp, flush + fsync, replace) so
        # an interrupted trim never leaves partial JSONL.
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
        parts = raw.split("\n")
        if parts and parts[-1] == "":
            parts.pop()          # trailing newline's empty tail
        elif parts:
            parts.pop()          # torn tail without a newline: never kept
        kept = []
        total = 0
        for ln in reversed(parts):
            size = len((ln + "\n").encode("utf-8"))
            if len(kept) >= _RING_TRIM_TO_LINES or total + size > _RING_MAX_BYTES:
                break
            kept.append(ln)
            total += size
        kept.reverse()
        if len(kept) != len(parts):
            tmp = path + ".tmp"
            try:
                with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                    for kept_line in kept:
                        f.write(kept_line + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, path)
            except OSError as exc:
                # PRR-008: a swallowed replace failure used to leave the
                # caps unenforced with {"ok":true} and no signal (Windows
                # raises WinError 5 when a reader holds the ring open).
                # Surface it on stderr — stdout stays machine-parseable and
                # ring health still never fails the caller.
                try:
                    sys.stderr.write(
                        "[zmem] ops ring trim failed; caps deferred "
                        "(%s)\n" % (exc,))
                except Exception:
                    pass
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return True
    except OSError:
        return False
