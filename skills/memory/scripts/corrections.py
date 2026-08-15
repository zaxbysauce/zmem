#!/usr/bin/env python3
"""Host-agnostic correction-pattern engine for zmem transcript mining.

Ported from claude-reflect (https://github.com/BayramAnnakov/claude-reflect),
MIT-licensed, `scripts/lib/reflect_utils.py` (c) BayramAnnakov. Attribution
retained per the MIT license.

Scope: the pattern functions (`detect_patterns`, `should_include_message`) are
HOST-AGNOSTIC pure text analysis — they know nothing about a Claude Code
transcript. PR 2/3 of the claude-reflect port (live capture + historical
mining) reuse them directly, which is why they cannot assume any host. Only
`extract_user_messages` is Claude-Code-format-specific (it walks the CC JSONL
shape); that is stated in its docstring.

Stdlib-only, Python 3.8+ compatible, cross-platform (Windows CI). Never opens
the zmem store, never writes to disk, never shells out.

Only `extract_user_messages` reads a transcript; every other symbol here is a
pure function over strings.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional, Tuple

# =============================================================================
# Pattern definitions (ported verbatim from claude-reflect reflect_utils.py)
# =============================================================================

# Explicit marker patterns (highest confidence)
EXPLICIT_PATTERNS = [
    (r"remember:", "remember:", 0.90, 120),  # pattern, name, confidence, decay_days
]

# Positive feedback patterns
POSITIVE_PATTERNS = [
    (r"perfect!|exactly right|that's exactly", "perfect", 0.70, 90),
    (r"that's what I wanted|great approach", "great-approach", 0.70, 90),
    (r"keep doing this|love it|excellent|nailed it", "keep-doing", 0.70, 90),
]

# Correction patterns (conservative set to minimize false positives)
# Format: (regex_pattern, pattern_name, is_strong)
#
# DESIGN NOTES:
# - These patterns are English-centric as a FAST first-pass filter
# - Non-English corrections are caught by the CJK tier below
# - We use STRUCTURAL signals (length, questions, task requests) for
#   language-agnostic filtering
# - Users can use explicit markers like "remember:" in any language
CORRECTION_PATTERNS = [
    (r"^no[,. ]+", "no,", True),  # Starts with "no," - common correction opener
    (r"^don't\b|^do not\b", "don't", True),  # Starts with don't/do not
    (r"^stop\b|^never\b", "stop/never", True),  # Starts with stop/never
    (r"that's (wrong|incorrect)|that is (wrong|incorrect)", "that's-wrong", True),
    (r"^actually[,. ]", "actually", False),  # Starts with "actually"
    (r"^I meant\b|^I said\b", "I-meant/said", True),  # Clarification
    (r"^I told you\b|^I already told\b", "I-told-you", True),  # Higher confidence
    (r"use .{1,30} not\b", "use-X-not-Y", True),  # "use X not Y" - limited gap
]

# Guardrail patterns - "don't do X unless" constraints (highest confidence for
# corrections); detect user frustrations about unwanted changes.
# Format: (regex_pattern, pattern_name, confidence, decay_days)
GUARDRAIL_PATTERNS = [
    (r"don't (?:add|include|create) .{1,40} unless", "dont-unless-asked", 0.90, 120),
    (r"only (?:change|modify|edit|touch) what I (?:asked|requested|said)", "only-what-asked", 0.90, 120),
    (r"stop (?:refactoring|changing|modifying|editing) (?:unrelated|other|surrounding)", "stop-unrelated", 0.90, 120),
    (r"don't (?:over-engineer|add extra|be too|make unnecessary)", "dont-over-engineer", 0.85, 90),
    (r"don't (?:refactor|reorganize|restructure) (?:unless|without)", "dont-refactor-unless", 0.85, 90),
    (r"leave .{1,30} (?:alone|unchanged|as is)", "leave-alone", 0.85, 90),
    (r"don't (?:add|include) (?:comments|docstrings|type hints|annotations) (?:unless|to code)", "dont-add-annotations", 0.85, 90),
    (r"(?:minimal|minimum|only necessary) changes", "minimal-changes", 0.80, 90),
]

# Structural patterns indicating FALSE POSITIVES (language-agnostic);
# focus on MESSAGE STRUCTURE rather than specific words.
FALSE_POSITIVE_PATTERNS = [
    r"[?\uff1f]$",  # Ends with question mark (ASCII ? or full-width ？)
    r"[\u55ce\u5417\u5462\u304b\uae4c]$",  # Ends with CJK question particle
    r"^(please|can you|could you|would you|help me)\b",  # Task request openers
    r"(help|fix|check|review|figure out|set up)\s+(this|that|it|the)\b",  # Task verbs
    r"(error|failed|could not|cannot|can't|unable to)\s+\w+",  # Error descriptions
    r"(is|was|are|were)\s+(not|broken|failing)",  # Bug reports
    r"^I (need|want|would like)\b",  # Task requests
    r"^(ok|okay|alright)[,.]?\s+(so|now|let)",  # Task continuations
]

# English phrases that look like correction openers but are NOT corrections.
NON_CORRECTION_PHRASES = [
    r"^no\s+problem",        # "No problem" - agreement
    r"^no\s+worries",        # "No worries" - agreement
    r"^no\s+need\b",         # "No need" - acknowledgment
    r"^no\s+way\b",          # "No way!" - surprise/exclamation
    r"^don't\s+worry",       # "Don't worry" - reassurance
    r"^don't\s+mind",        # "Don't mind" - agreement
    r"^don't\s+bother",      # "Don't bother" - polite decline
    r"^never\s+mind",        # "Never mind" - dismissal
    r"^stop\s+worrying",     # "Stop worrying" - reassurance
]

# CJK correction patterns (parallel to English CORRECTION_PATTERNS).
# Format: (regex_pattern, pattern_name, is_strong)
CJK_CORRECTION_PATTERNS = [
    # Japanese
    (r"^いや[、,.\s]|^いや違", "iya", True),       # いや、〜 / いや違う - "no, ..."
    (r"^違う[、，,.\s！!。]|^ちがう[、,.\s]", "chigau", True),  # 違う、〜 - "wrong, ..."
    (r"そうじゃなく[てけ]|そっちじゃなく[てけ]", "souja-nakute", True),  # "not that"
    (r"間違[いえっ]て", "machigatte", True),       # 間違ってる - "it's wrong"
    (r"じゃなくて.{0,30}にして", "janakute-nishite", True),  # 〜じゃなくて〜にして
    (r"^やめて[。！!]?\s*$", "yamete", True),      # やめて - "stop"
    (r"^そうじゃない", "souja-nai", True),          # そうじゃない - "that's not right"
    (r"って言った[のよでじゃ]", "tte-itta", True),   # って言ったのに - "I told you"
    # Chinese
    (r"^不是[，,. ]", "bushi", True),              # 不是、〜 - "no, ..."
    (r"^错了|^錯了", "cuole", True),               # 错了 - "wrong"
    (r"不要.{0,20}要", "buyao-yao", True),         # 不要X要Y - "don't X, use Y"
    # Korean
    (r"^아니[,. ]", "ani", True),                  # 아니, - "no, ..."
    (r"틀렸", "teullyeoss", True),                 # 틀렸 - "wrong"
]

# Maximum prompt length for live capture (UserPromptSubmit hook). Prompts longer
# than this are almost certainly system content, not user corrections. Explicit
# "remember:" markers are always processed regardless of length.
MAX_CAPTURE_PROMPT_LENGTH = 500

# Maximum message length for weak patterns (structural heuristic). Long messages
# are more likely to be context/tasks than corrections.
MAX_WEAK_PATTERN_LENGTH = 150

# Very short messages without question marks are more likely corrections.
MIN_SHORT_CORRECTION_LENGTH = 80

# zmem's own injection markers that must never be detected as user corrections.
# These are the launcher-extraction sentinels and hook headers zmem injects into
# context (never a genuine user message), so we extend the upstream skip list
# with them (issue #46). Trade-off documented: a user who literally types a
# reserved sentinel as a correction is skipped — acceptable, they are reserved
# tokens.
ZMEM_INJECTION_MARKERS = [
    r"<<<ZMEM_JSON>>>",        # launcher extraction sentinel (zmem-reflect.sh)
    r"<<<END>>>",              # launcher extraction sentinel (zmem-reflect.sh)
    r"# Relevant memories \(zmem recall",  # recall hook injected header
    r"# Loaded from memory \(Tier",        # SessionStart core-memory header
]


def detect_patterns(text: str) -> Tuple[Optional[str], str, float, str, int]:
    """Detect correction/positive/explicit patterns in text.

    Returns a (type, matched_patterns, confidence, sentiment, decay_days) tuple.
    ``type`` is one of "explicit", "positive", "auto", "guardrail", or None.
    Ported from claude-reflect reflect_utils.py ``detect_patterns``.
    """
    # Too short to be actionable (e.g. "OK", "好", "yes"). CJK characters carry
    # more meaning per char, so use a lower threshold.
    stripped = text.strip()
    has_cjk = bool(re.search(r"[\u3000-\u9fff\uf900-\ufaff\uac00-\ud7af]", stripped))
    short_threshold = 2 if has_cjk else 4
    if len(stripped) <= short_threshold:
        return (None, "", 0.0, "correction", 90)

    # Explicit "remember:" - always highest priority.
    for pattern, name, confidence, decay in EXPLICIT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return ("explicit", name, confidence, "correction", decay)

    # Guardrail patterns - "don't do X unless" constraints.
    for pattern, name, confidence, decay in GUARDRAIL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return ("guardrail", name, confidence, "correction", decay)

    # FALSE POSITIVE patterns - skip these messages.
    for fp_pattern in FALSE_POSITIVE_PATTERNS:
        if re.search(fp_pattern, text, re.IGNORECASE):
            return (None, "", 0.0, "correction", 90)

    # Non-correction English phrases (before correction patterns). Prevents
    # "No problem", "Don't worry" etc. from being caught as corrections.
    for nc_pattern in NON_CORRECTION_PHRASES:
        if re.search(nc_pattern, text, re.IGNORECASE):
            return (None, "", 0.0, "correction", 90)

    # Positive patterns.
    matched_positive = []
    for pattern, name, confidence, decay in POSITIVE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            matched_positive.append(name)
    if matched_positive:
        return ("positive", " ".join(matched_positive), 0.70, "positive", 90)

    # Skip long messages for weak patterns (likely task requests).
    text_length = len(text)

    # CJK correction patterns (language-specific). Use stripped text for anchor
    # patterns (^/$) to handle leading/trailing whitespace.
    matched_cjk = []
    cjk_strong = False
    for pattern, name, is_strong in CJK_CORRECTION_PATTERNS:
        if re.search(pattern, stripped):
            matched_cjk.append(name)
            if is_strong:
                cjk_strong = True
    if matched_cjk:
        confidence = 0.75 if cjk_strong else 0.60
        decay_days = 90 if cjk_strong else 60
        if text_length < MIN_SHORT_CORRECTION_LENGTH:
            confidence = min(0.90, confidence + 0.10)
        elif text_length > 300:
            confidence = max(0.50, confidence - 0.15)
        return ("auto", " ".join(matched_cjk), confidence, "correction", decay_days)

    # English correction patterns.
    matched_corrections = []
    pattern_count = 0
    has_strong_pattern = False
    has_i_told_you = False
    for pattern, name, is_strong in CORRECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            # Skip weak patterns in long messages.
            if not is_strong and text_length > MAX_WEAK_PATTERN_LENGTH:
                continue
            matched_corrections.append(name)
            pattern_count += 1
            if is_strong:
                has_strong_pattern = True
            if name == "I-told-you":
                has_i_told_you = True

    if matched_corrections:
        # Confidence from pattern count, type, and length.
        if has_i_told_you:
            confidence = 0.85
            decay_days = 120
        elif pattern_count >= 3:
            confidence = 0.85
            decay_days = 120
        elif pattern_count >= 2:
            confidence = 0.75
            decay_days = 90
        elif has_strong_pattern:
            confidence = 0.70
            decay_days = 60
        else:
            confidence = 0.55  # Reduced for weak single patterns
            decay_days = 45

        # Adjust confidence based on message length (structural signal).
        if text_length < MIN_SHORT_CORRECTION_LENGTH:
            confidence = min(0.90, confidence + 0.10)  # Boost for short messages
        elif text_length > 300:
            confidence = max(0.50, confidence - 0.15)  # Reduce for long messages
        elif text_length > 150:
            confidence = max(0.55, confidence - 0.10)

        return ("auto", " ".join(matched_corrections), confidence, "correction", decay_days)

    return (None, "", 0.0, "correction", 90)


def should_include_message(text: str) -> bool:
    """Check if a message should be included in correction detection.

    Filters out system content like XML tags, JSON, tool results, and session
    continuations that should never be treated as user corrections. Used by the
    transcript extractor (and later by the live-capture hook). Host-agnostic;
    the skip list is extended with zmem's own injection markers so zmem's hook
    output is never detected as a correction (issue #46).
    """
    if not text.strip():
        return False

    skip_patterns = [
        r"^<",              # XML tags (<task-notification>, <system-reminder>, etc.)
        r"^\[",             # Brackets
        r"^\{",             # JSON
        r"tool_result",
        r"tool_use_id",
        r"<command-",
        r"<task-notification>",
        r"<system-reminder>",
        r"This session is being continued",
        r"^Analysis:",
        r"^\*\*",           # Bold text
        r"^   -",           # Indented lists
    ] + ZMEM_INJECTION_MARKERS

    for pattern in skip_patterns:
        if re.search(pattern, text):
            return False

    return True


def extract_user_messages(transcript_path, corrections_only: bool = False) -> List[str]:
    """Extract user (non-metadata) message texts from a Claude Code transcript.

    CC-FORMAT-SPECIFIC: walks the CC JSONL shape (type == "user", isMeta
    filtered, string-or-list content). Other hosts' histories are out of scope
    here (zmem's host matrix; issue #46). Fail-open: any read/parse error returns
    ``[]`` and never raises — mirroring ``_failures_from_transcript``.

    Args:
        transcript_path: path to a CC session JSONL file (str or Path).
        corrections_only: if True, only return messages matching a coarse
            correction regex (fast first-pass filter).
    """
    path = transcript_path
    try:
        exists = path.exists() if hasattr(path, "exists") else Path(path).exists()
        if not exists:
            return []
    except (OSError, ValueError):
        return []

    messages: List[str] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue
                # Filter: type=user, not isMeta.
                if entry.get("type") != "user":
                    continue
                if entry.get("isMeta"):
                    continue
                # Extract text from content (can be string or list).
                content = entry.get("message", {}).get("content", [])
                if isinstance(content, str):
                    if content and should_include_message(content):
                        messages.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text = item.get("text", "")
                            if text and should_include_message(text):
                                messages.append(text)
    except OSError:
        return []

    if corrections_only:
        correction_pattern = (
            r"(no,? use|don't use|stop using|never use|that's wrong|that's incorrect|"
            r"not right|not correct|actually[,. ]|I meant|I said|I told you|"
            r"I already told|you should use|you need to use|use .+ not|not .+, use|remember:)"
        )
        messages = [m for m in messages if re.search(correction_pattern, m, re.IGNORECASE)]

    return messages
