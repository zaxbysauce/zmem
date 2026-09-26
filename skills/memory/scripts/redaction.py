"""Dependency-free secret-like text redaction shared by capture surfaces."""

from __future__ import annotations

import re


SECRET_CREDENTIAL_PATTERNS = [
    # Authorization headers occur in prompts, rendered fences, and operation
    # transcripts.  Keep this ahead of the generic token patterns so the whole
    # credential (rather than an arbitrary suffix) is replaced consistently.
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._+/~-]{16,}\b"),
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|pwd|private[_-]?key)\s*[:=]\s*\S{8,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{10,}\b"),
]
SECRET_GENERIC_PATTERNS = [
    re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
]
SECRET_PATTERNS = SECRET_CREDENTIAL_PATTERNS + SECRET_GENERIC_PATTERNS


def redact_secret_like_text(text: str) -> tuple[str, int]:
    """Replace secret-like tokens with ``[REDACTED_SECRET]``."""

    redacted = text or ""
    count = 0
    for pattern in SECRET_PATTERNS:
        redacted, changed = pattern.subn("[REDACTED_SECRET]", redacted)
        count += changed
    return redacted, count
