#!/usr/bin/env python3
"""Issue one fleet principal and append it to the hub's principal map.

Issue #291 (Workstream T PR 12). Runs ON THE HUB. Appends an entry to the
``ZMEM_MCP_TOKENS_FILE`` map, prints the generated token EXACTLY ONCE (only
after the map write succeeded), and keeps the map owner-only.

Design constraints (plan REV 4):

* STRICT FAIL-CLOSED issuance locking — ``host.acquire_lock`` is
  NONBLOCKING and its degraded path returns the truthy ``_NO_LOCK_TOKEN``:
  contention (``None``) and degraded (``_NO_LOCK_TOKEN``) both exit 3 with
  nothing written and nothing printed; only a real token proceeds, held
  through the atomic replacement. This mirrors the strict-wrapper
  precedent at storelib/schema.py.
* The token file's DACL is hardened BEFORE content lands (Windows:
  icacls /inheritance:r + owner grant on the empty file, then write,
  then replace; POSIX: mode 0600) and re-verified after replacement.
* Duplicate principal ids are refused under the lock; unknown fields are
  refused; a world/group-readable existing map (POSIX mode bits) is
  refused — the installer-side refusal's issuance-side twin.

Exit codes: 0 success (token on stdout), 2 validation/refusal, 3 lock
contention or degraded lock, 4 map write failed (no token printed).
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "skills" / "memory" / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import host  # noqa: E402
from schema_meta import is_valid_namespace  # noqa: E402

_ALLOWED_FIELDS = frozenset({
    "principal", "host", "harness", "namespaces", "capabilities",
    "revoked",
})
_REQUIRED_FIELDS = {"principal", "host", "harness"}
_MAX_PRINCIPALS = 64
_LOCK_STALE_S = 120
# Hub-parser grammar parity (review round 1): the hub refuses maps on these
# rules, so the issuer enforces the same ones — it must never mint an entry
# the hub would exit-2 on.
import re as _re  # noqa: E402

_PRINCIPAL_ID_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_PRINCIPAL_ID_CHARS = 64
_MAX_TAG_VALUE_CHARS = 128
_ALLOWED_CAPABILITIES = frozenset(
    {"read", "passive", "write", "mutate", "admin"})

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_LOCK = 3
EXIT_WRITE_FAILED = 4


def _refuse(message: str) -> int:
    print(f"issue-principal: {message}", file=sys.stderr)
    return EXIT_REFUSED


def _map_is_world_readable(path: Path) -> bool:
    """POSIX mode-bit refusal (Windows: stat modes are not POSIX — the
    icacls ACL check below is the Windows gate, so this returns False
    there instead of reading the synthesized 0666 as world-writable)."""
    if os.name == "nt":
        return False
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return bool(mode & 0o077)


def _harden_windows(path: Path) -> None:
    """Owner-only DACL on an EXISTING (empty) file, before content lands."""
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if not user:
        return
    subprocess.run(
        ["icacls", str(path), "/inheritance:r",
         "/grant:r", f"{user}:F"],
        capture_output=True, timeout=10, check=False)


def _windows_acl_clean(path: Path) -> bool:
    """True when no broad allow ACE (Users/Everyone/Authenticated Users)."""
    try:
        out = subprocess.run(
            ["icacls", str(path)], capture_output=True, text=True,
            timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot determine — doctor/installer surface reports
    text = (out.stdout or "") + (out.stderr or "")
    broad = ("Users:" in text or "Everyone:" in text
             or "Authenticated Users:" in text)
    return not broad


def _validate_entry(entry: dict) -> list[str]:
    problems: list[str] = []
    unknown = sorted(set(entry) - _ALLOWED_FIELDS)
    if unknown:
        problems.append(f"unknown field(s) {unknown}")
    missing = _REQUIRED_FIELDS - set(entry)
    if missing:
        problems.append(f"missing required field(s) {sorted(missing)}")
    pid = entry.get("principal")
    if not isinstance(pid, str) or not pid.strip():
        problems.append("'principal' must be a non-empty string")
    elif (len(pid.strip()) > _MAX_PRINCIPAL_ID_CHARS
            or not _PRINCIPAL_ID_RE.match(pid.strip())):
        problems.append(
            f"'principal' must match [A-Za-z0-9][A-Za-z0-9._-]* and be at "
            f"most {_MAX_PRINCIPAL_ID_CHARS} characters")
    for field in ("host", "harness"):
        value = entry.get(field)
        if not isinstance(value, str) or not value.strip():
            problems.append(
                f"'{field}' must be a non-empty string matching "
                "[A-Za-z0-9][A-Za-z0-9._-]*")
        elif (len(value.strip()) > _MAX_TAG_VALUE_CHARS
                or not _PRINCIPAL_ID_RE.match(value.strip())):
            problems.append(
                f"'{field}' must match [A-Za-z0-9][A-Za-z0-9._-]* and be at "
                f"most {_MAX_TAG_VALUE_CHARS} characters")
    if "revoked" in entry and not isinstance(entry["revoked"], bool):
        problems.append("'revoked' must be a boolean")
    namespaces = entry.get("namespaces")
    if namespaces is not None and (
            not isinstance(namespaces, list)
            or not namespaces
            or not all(isinstance(n, str) and n.strip() for n in namespaces)):
        problems.append(
            "'namespaces' must be a NON-EMPTY list of non-empty strings")
    elif namespaces is not None:
        for ns in namespaces:
            if not is_valid_namespace(ns):
                problems.append(
                    f"namespace {ns[:40]!r} is not a valid scoped "
                    "namespace (the hub parser would refuse it)")
    capabilities = entry.get("capabilities")
    if capabilities is not None and (
            not isinstance(capabilities, list) or not capabilities
            or not all(isinstance(c, str) for c in capabilities)
            or any(c not in _ALLOWED_CAPABILITIES for c in capabilities)):
        problems.append(
            "'capabilities' must be a NON-EMPTY list from "
            + ", ".join(sorted(_ALLOWED_CAPABILITIES)))
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Issue one fleet principal into the hub's principal map "
        "(issue #291). Prints the token exactly once, after the write.")
    ap.add_argument("--map", required=True,
                    help="path to the principal map JSON (ZMEM_MCP_TOKENS_FILE)")
    ap.add_argument("--principal", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--harness", required=True)
    ap.add_argument("--namespaces", default="",
                    help="comma-separated namespace allow-list (optional)")
    ap.add_argument("--capabilities", default="",
                    help="comma-separated capabilities (optional)")
    ap.add_argument("--preset", choices=("off-tailnet",),
                    help="off-tailnet: capabilities read,passive")
    args = ap.parse_args(argv)

    entry: dict = {
        "principal": args.principal.strip(),
        "host": args.host.strip(),
        "harness": args.harness.strip(),
    }
    if args.namespaces.strip():
        ns_list = [n.strip() for n in args.namespaces.split(",") if n.strip()]
        if not ns_list:
            return _refuse(
                "--namespaces parsed to an empty list (only separators?) — "
                "omit the flag or name at least one namespace")
        entry["namespaces"] = ns_list
    caps = ([c.strip() for c in args.capabilities.split(",") if c.strip()]
            if args.capabilities.strip() else [])
    if args.preset == "off-tailnet":
        caps = ["read", "passive"]
    if caps:
        entry["capabilities"] = caps

    problems = _validate_entry(entry)
    if problems:
        return _refuse("; ".join(problems))

    map_path = Path(args.map).expanduser()
    if map_path.exists():
        if _map_is_world_readable(map_path):
            return _refuse(
                f"existing map ({map_path}) is group/world-readable — "
                "fix its permissions first (chmod 0600)")
        if os.name == "nt" and not _windows_acl_clean(map_path):
            return _refuse(
                f"existing map ({map_path}) grants a broad ACE "
                "(Users/Everyone/Authenticated Users) — restrict it to the "
                "owner first")
        try:
            raw = map_path.read_text(encoding="utf-8")
            existing = json.loads(raw) if raw.strip() else {}
        except (OSError, ValueError) as exc:
            return _refuse(f"existing map unreadable/invalid: {exc}")
        if not isinstance(existing, dict):
            return _refuse("existing map must be a JSON object")
        for tok, other in existing.items():
            if (not isinstance(tok, str) or not tok.strip()
                    or len(tok.encode("utf-8")) > 4096):
                return _refuse(
                    "existing map carries a token key the hub parser "
                    "would refuse (empty or over 4096 UTF-8 bytes); fix "
                    "the map first")
            if (isinstance(other, dict)
                    and other.get("principal") == entry["principal"]):
                return _refuse(
                    f"principal id {entry['principal']!r} already exists "
                    "in the map")
            if not isinstance(other, dict) or _validate_entry(other):
                return _refuse(
                    "existing map carries an entry the hub parser would "
                    "refuse (grammar/shape); fix the map first — the issuer "
                    "never appends onto a map the hub would exit on")
        if len(existing) + 1 > _MAX_PRINCIPALS:
            return _refuse(f"map would exceed {_MAX_PRINCIPALS} principals")
    else:
        existing = {}

    # STRICT FAIL-CLOSED lock (see module docstring): contention and
    # degraded-lock both refuse WITHOUT printing anything.
    lock_path = map_path.with_name(map_path.name + ".issue.lock")
    lock_token = host.acquire_lock(str(lock_path), _LOCK_STALE_S)
    if lock_token is None:
        print("issue-principal: another issuance holds the map lock; "
              "retry when it completes", file=sys.stderr)
        return EXIT_LOCK
    if lock_token == host._NO_LOCK_TOKEN:  # noqa: SLF001
        print("issue-principal: lock mechanism unavailable — refusing to "
              "issue unlocked", file=sys.stderr)
        return EXIT_LOCK
    try:
        # Re-validate duplicates UNDER the lock (a concurrent issuer may
        # have appended while we validated).
        if map_path.exists():
            try:
                raw_now = map_path.read_text(encoding="utf-8")
                existing = json.loads(raw_now) if raw_now.strip() else {}
                if not isinstance(existing, dict):
                    return _refuse("map changed to a non-object under the lock")
                for _tok, other in existing.items():
                    if (isinstance(other, dict)
                            and other.get("principal") == entry["principal"]):
                        return _refuse(
                            f"principal id {entry['principal']!r} was issued "
                            "concurrently")
            except (OSError, ValueError) as exc:
                return _refuse(f"map re-read under lock failed: {exc}")

        token = secrets.token_urlsafe(32)
        existing[token] = dict(entry)

        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(map_path.parent), prefix=".map-tmp-", suffix=".json")
        os.close(tmp_fd)
        tmp_path = Path(tmp_name)
        try:
            if os.name == "nt":
                _harden_windows(tmp_path)
            else:
                os.chmod(tmp_path, 0o600)
            with tmp_path.open("w", encoding="utf-8") as fh:
                json.dump(existing, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp_path, map_path)
        except OSError as exc:
            tmp_path.unlink(missing_ok=True)
            print(f"issue-principal: map write failed: {exc}", file=sys.stderr)
            return EXIT_WRITE_FAILED
        if os.name == "nt" and not _windows_acl_clean(map_path):
            print("issue-principal: WARNING — post-replacement DACL check "
                  "found a broad ACE; restrict it manually.", file=sys.stderr)
    finally:
        host.release_lock(str(lock_path), lock_token)

    # The ONE place the token is printed — after the locked write succeeded.
    print(token)
    print(
        f"issue-principal: appended principal {entry['principal']!r} to "
        f"{map_path}; deliver the token to the broker machine via a secure "
        "channel and run install-broker there.", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
