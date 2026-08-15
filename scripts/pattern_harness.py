#!/usr/bin/env python3
"""Pattern-tuning harness for zmem's correction-pattern library (issue #46).

Adapted from claude-reflect (https://github.com/BayramAnnakov/claude-reflect),
MIT-licensed, `scripts/compare_detection.py` — minus the semantic/LLM side.
zmem's scripts must be stdlib-only and multi-host: this harness NEVER shells out
to `claude -p` or any model. It only runs the regex pattern functions.

Purpose: tune the ported patterns in skills/memory/scripts/corrections.py against
your real transcripts before/after changing them. Not wired into any hook — it
is a developer/offline tool.

Input: one or more transcript JSONL paths, or `--project <dir>` to resolve the
encoded `~/.claude/projects/` folder (handling the underscore/hyphen encoding
ambiguity like the original). Transcripts are Claude-Code-format for PR 1 (zmem
mining covers CC transcripts first; other hosts are out of scope this PR).

Output: per-pattern hit counts, all matched messages grouped by pattern with
confidence, and a `--show-nonmatches N` sample so a reviewer can eyeball
false negatives. Use `--json` for programmatic consumption.

Python 3.8+, stdlib only, ASCII-safe output.

Usage:
  python pattern_harness.py <transcript.jsonl> [more.jsonl ...]
  python pattern_harness.py --project <dir> --limit 100 [--show-nonmatches 5] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

# Resolve corrections.py from the repo layout:
# <repo>/scripts/pattern_harness.py -> <repo>/skills/memory/scripts/corrections.py
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "skills" / "memory" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from corrections import (  # noqa: E402
    detect_patterns,
    extract_user_messages,
)


def _ascii(value: object) -> str:
    """Coerce to str and strip anything outside ASCII (legacy Windows consoles
    can otherwise raise UnicodeEncodeError mid-report)."""
    return str(value).encode("ascii", "replace").decode("ascii")


def _print(msg: str, *, err: bool = False) -> None:
    print(_ascii(msg), file=sys.stderr if err else sys.stdout)


def find_project_sessions(project_path: str):
    """Resolve the encoded `~/.claude/projects/` folder for a project and return
    its *.jsonl session files. Handles the underscore/hyphen ambiguity the way
    claude-reflect's original does (try the verbatim name, then hyphenated)."""
    project_path = os.path.abspath(project_path)
    project_name = os.path.basename(project_path)
    claude_projects = Path.home() / ".claude" / "projects"
    if not claude_projects.is_dir():
        return []
    for folder in claude_projects.iterdir():
        if folder.is_dir() and project_name.lower() in folder.name.lower():
            return sorted(folder.glob("*.jsonl"))
    project_name_hyphen = project_name.replace("_", "-")
    for folder in claude_projects.iterdir():
        if folder.is_dir() and project_name_hyphen.lower() in folder.name.lower():
            return sorted(folder.glob("*.jsonl"))
    return []


def collect_messages(paths, limit):
    """Run extract_user_messages over each path, flatten, and cap at limit.

    ``limit`` is validated (must be a positive int or None) by main() before this
    is called, so a negative/zero value can never silently eat the tail via a
    ``[:-1]``-style slice.
    """
    messages = []
    for p in paths:
        messages.extend(extract_user_messages(p))
        if limit and len(messages) >= limit:
            break
    return messages[:limit] if limit else messages


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Tune zmem's correction patterns against real transcripts "
                    "(offline, stdlib-only, no model calls).")
    parser.add_argument("transcripts", nargs="*", help="one or more CC transcript JSONL paths")
    parser.add_argument("--project", default=None,
                        help="resolve the encoded ~/.claude/projects/<dir> folder for a "
                             "project and scan its *.jsonl session files")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the number of user messages analyzed")
    parser.add_argument("--show-nonmatches", type=int, default=0,
                        help="print up to N matched/false-(non)match samples for eyeballing")
    parser.add_argument("--json", action="store_true",
                        help="emit a JSON report instead of human-readable text")
    args = parser.parse_args()

    # --limit and --show-nonmatches must be non-negative; a negative value is
    # almost certainly a user typo and, if passed through, would silently
    # truncate via a [:-1]-style slice (limit truncates the tail; show-nonmatches
    # truncates the sample).
    if args.limit is not None and args.limit <= 0:
        parser.error("--limit must be a positive integer (or omit it)")
    if args.show_nonmatches < 0:
        parser.error("--show-nonmatches must be a non-negative integer")

    paths = list(args.transcripts)
    if args.project:
        proj_sessions = find_project_sessions(args.project)
        if not proj_sessions:
            _print(f"[pattern-harness] no sessions found for project {args.project!r}",
                   err=True)
            return 1
        paths = list(paths) + proj_sessions

    if not paths:
        parser.error("provide at least one transcript path (or --project)")

    messages = collect_messages(paths, args.limit)

    # Per-pattern hit counts (across all messages).
    pattern_hits = Counter()
    # Matched messages grouped by pattern name -> list of (message, confidence).
    matched_by_pattern = defaultdict(list)
    nonmatched = []

    for text in messages:
        item_type, patterns, confidence, sentiment, decay = detect_patterns(text)
        if not item_type:
            nonmatched.append(text)
            continue
        for name in patterns.split():
            pattern_hits[name] += 1
            matched_by_pattern[name].append((text, confidence))

    if args.json:
        report = {
            "messages_analyzed": len(messages),
            "matched": len(messages) - len(nonmatched),
            "nonmatched": len(nonmatched),
            "pattern_hits": dict(pattern_hits.most_common()),
            "matched_by_pattern": {
                name: [{"message": m, "confidence": c} for m, c in rows]
                for name, rows in matched_by_pattern.items()
            },
            "nonmatched_sample": nonmatched[:args.show_nonmatches] if args.show_nonmatches else [],
        }
        print(json.dumps(report, ensure_ascii=True))
        # ensure_ascii=True keeps the JSON output ASCII-safe, honoring the
        # module's documented "ASCII-safe output" contract (a legacy Windows
        # console can otherwise raise UnicodeEncodeError mid-report).
        return 0

    _print(f"[pattern-harness] messages analyzed: {len(messages)}")
    _print(f"[pattern-harness] matched: {len(messages) - len(nonmatched)}  "
           f"nonmatched: {len(nonmatched)}")
    if pattern_hits:
        _print("\nPer-pattern hit counts:")
        for name, n in pattern_hits.most_common():
            _print(f"  {name:20s} {n}")
    if matched_by_pattern:
        _print("\nMatched messages grouped by pattern:")
        for name, rows in matched_by_pattern.items():
            _print(f"\n  == {name} ==")
            for text, conf in rows[:5]:
                _print(f"    [{conf:.2f}] {_ascii(text)[:120]}")
    if args.show_nonmatches and nonmatched:
        _print(f"\nNon-match sample (first {args.show_nonmatches}):")
        for text in nonmatched[:args.show_nonmatches]:
            _print(f"  - {_ascii(text)[:120]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
