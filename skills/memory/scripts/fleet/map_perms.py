#!/usr/bin/env python3
"""Principal-map file permission verdicts (issue #406).

Single shared implementation for doctor's fleet-principal lane and the hub's
``load_principal_map`` refusal path. Pure stdlib; no storelib imports (this
module is loaded by the hermes server through a selected-checkout dynamic
loader and must stay dependency-free).

Verdict tiers (pinned by tests/test_t12_map_perms.py):
- POSIX mode bits: 0o600 -> ok; ANY group/world read or write bit -> fail
  (issue #406 AC1: a group-readable map is already over-permissive — the
  former group-read-only `warn` tier let a 0640 map load).
- Windows: allow ACEs granted to the broad trustees (Everyone,
  Authenticated Users, BUILTIN-Users) with read-capable rights -> fail.
  ACEs are enumerated BY SID via ``icacls <path> /save`` (SDDL text) — the
  human-readable ``icacls <path>`` output names trustees in the locale's
  language and prints specific-rights forms the name scan missed, so the
  SID-based parse is primary and the legacy name/text parse is only a
  fallback when the SDDL probe cannot run.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from typing import Optional, Tuple

# Well-known SIDs (locale-independent): BUILTIN\Users S-1-5-32-545,
# Everyone S-1-1-0, NT AUTHORITY\Authenticated Users S-1-5-11.
_BROAD_ACE_TOKENS = ("S-1-5-32-545", "S-1-1-0", "S-1-5-11",
                     "Users:", "Everyone:", "Authenticated Users:")

READ_MARKERS = ("(R)", "(RX)", "(M)", "(F)", "(RD)")

# SDDL trustee aliases for the same broad set (well-known SID shorthand)
# plus the raw SID strings themselves.
_BROAD_SDDL_TRUSTEES = ("WD", "AU", "BU", "S-1-1-0", "S-1-5-11",
                        "S-1-5-32-545")
# Read-capable SDDL right codes: generic (FA all / FR read / M modify /
# RX execute+read) plus the generic-read bit GR.
_BROAD_READ_SDDL_RIGHTS = ("FA", "M", "FR", "RX", "GR")
# FILE_GENERIC_READ (0x120089) and FILE_GENERIC_modify-ish masks: any hex
# rights value carrying a read-capable bit.
_SDDL_HEX_READ_MASK = 0x120089 | 0x1301bf

_SDDL_ACE_RE = re.compile(r"\((A;[^;]*(?:;[^;]*){2,4};[^;)]+)\)")


def mode_bits_verdict(mode_bits: int) -> str:
    """Verdict for POSIX permission bits (stat.S_IMODE shape)."""
    if mode_bits & 0o077 == 0:
        return "ok"
    return "fail"


def _mode_details(mode_bits: int) -> str:
    return "mode 0o%o" % (mode_bits & 0o7777)


def icacls_verdict(icacls_output: str) -> str:
    """Verdict for ``icacls <path>`` output text (Windows path)."""
    for line in icacls_output.splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        # lines look like "<path> BUILTIN\Users:(R)" or SID-prefixed grants
        for token in _BROAD_ACE_TOKENS:
            if token in stripped:
                if any(marker in stripped for marker in READ_MARKERS):
                    return "fail"
    return "ok"


def _icacls_details(icacls_output: str) -> str:
    for line in icacls_output.splitlines():
        stripped = line.strip()
        for token in _BROAD_ACE_TOKENS:
            if token in stripped and any(m in stripped for m in READ_MARKERS):
                return "broad ACE: %s" % stripped[:120]
    return "owner-only ACL"


def _sddl_ace_is_broad_read(ace_body: str) -> bool:
    """True when an SDDL allow-ACE body grants a broad trustee read-capable
    rights. Body is everything inside ``(A;...;rights;;;trustee)`` after
    the leading ``A;`` — rights is the third field, trustee the last."""
    parts = ace_body.split(";")
    if len(parts) < 5:
        return False
    trustee = parts[-1].strip()
    if trustee.upper() not in _BROAD_SDDL_TRUSTEES:
        return False
    rights = parts[2].strip()
    for code in _BROAD_READ_SDDL_RIGHTS:
        if code in rights:
            return True
    hex_match = re.fullmatch(r"0x([0-9a-fA-F]+)", rights)
    if hex_match and (int(hex_match.group(1), 16)
                      & _SDDL_HEX_READ_MASK):
        return True
    return False


def sddl_verdict(sddl_text: str, path: str = "") -> Tuple[str, str]:
    """Verdict from ``icacls <path> /save`` output (SDDL ace strings)."""
    for match in _SDDL_ACE_RE.finditer(sddl_text):
        body = match.group(1)
        if _sddl_ace_is_broad_read(body):
            return "fail", "broad ACE (SID) on %s: %s" % (path, body[:120])
    return "ok", "owner-only ACL"


def _probe_sddl(path: str) -> Optional[str]:
    """SDDL text for path via ``icacls /save`` (None when unavailable)."""
    # The sink lives in the SYSTEM temp dir: a service may be able to READ
    # the configured map without write access to its directory, and a
    # probe failure must not silently restore the localized-name fallback.
    directory = tempfile.gettempdir()
    try:
        fd, sink = tempfile.mkstemp(prefix="zmem-sddl-", suffix=".sddl",
                                    dir=directory)
        os.close(fd)
        try:
            out = subprocess.run(
                ["icacls", path, "/save", sink], capture_output=True,
                text=True, timeout=10)
            if out.returncode != 0:
                return None
            with open(sink, "rb") as fh:
                raw = fh.read()
            # icacls /save writes UTF-16LE (no BOM); decoding as utf-8
            # interleaves NULs and the SDDL ACEs never match.
            text = raw.decode("utf-16-le", errors="replace")
            if "(" not in text:
                text = raw.decode("utf-8", errors="replace")
            return text.replace("\x00", "")
        finally:
            try:
                os.unlink(sink)
            except OSError:
                pass
    except (OSError, subprocess.SubprocessError):
        return None


def map_permission_verdict(path: str) -> Tuple[str, str]:
    """(verdict, details) for a principal-map file on this platform.

    verdict is one of ok|fail. Never raises for permission-shape reasons;
    an unrunnable probe degrades to ok with a disclosure (the callers'
    content validation remains the load gate).
    """
    if os.name == "posix":
        try:
            bits = stat.S_IMODE(os.stat(path).st_mode)
        except OSError as exc:
            return "ok", "stat unavailable (%s)" % exc.__class__.__name__
        return mode_bits_verdict(bits), _mode_details(bits)
    # Primary probe: enumerate allow ACEs BY SID via the SDDL /save form
    # (locale-independent; catches specific-rights grants the name scan
    # missed). Fallback: the legacy human-output text parse.
    sddl = _probe_sddl(path)
    if sddl and "(" in sddl:
        return sddl_verdict(sddl, path)
    try:
        out = subprocess.run(["icacls", path], capture_output=True,
                             text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return "ok", "icacls unavailable"
    text = out.stdout or ""
    if out.returncode != 0 and not text:
        return "ok", "icacls exit %d" % out.returncode
    return icacls_verdict(text), _icacls_details(text)


def permission_refusal_message(path: str, details: str) -> str:
    """The stderr line both the hub refusal and doctor render."""
    return ("principal map %s is over-permissive (%s); tighten to "
            "owner-only before serving its tokens (issue #406)"
            % (path, details))


def check_refusal(path: str) -> Optional[str]:
    """Refusal message when the map must not be loaded/served, else None."""
    verdict, details = map_permission_verdict(path)
    if verdict == "fail":
        return permission_refusal_message(path, details)
    return None
