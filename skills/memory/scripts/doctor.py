#!/usr/bin/env python
"""Read-only install diagnostics for ZMem.

This command is deliberately side-effect free:
  - never writes the store or its parent directory
  - never edits host config
  - opens sqlite databases read-only when it inspects them

Usage:
  python doctor.py [--project PATH] [--repo-root PATH] [--format human|json|both]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

try:
    import tomllib as _tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - exercised on older Pythons
    _tomllib = None

try:
    import host
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    import host  # type: ignore


# Single source of truth: import the schema version from schema_meta (the same
# module store.py uses) so doctor and store can never disagree. A stale local
# copy here once made every healthy v7 store FAIL doctor's schema gate (#36 M11).
try:
    from schema_meta import SUPPORTED_SCHEMA_VERSION as CURRENT_SCHEMA_VERSION
except ImportError:
    sys.path.insert(0, os.path.dirname(__file__))
    from schema_meta import SUPPORTED_SCHEMA_VERSION as CURRENT_SCHEMA_VERSION  # type: ignore # noqa: E501
STATUS_ORDER = {"fail": 3, "warn": 2, "pass": 1, "skip": 0}
WINDOWS_BASH_CANDIDATES = (
    Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    Path(r"C:\Program Files\Git\bin\bash.exe"),
    Path.home() / "AppData" / "Local" / "Programs" / "Git" / "usr" / "bin" / "bash.exe",
    Path.home() / "AppData" / "Local" / "Programs" / "Git" / "bin" / "bash.exe",
    Path(r"C:\Program Files (x86)\Git\usr\bin\bash.exe"),
    Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
)


def _norm_path(path: str | Path) -> str:
    return str(Path(os.path.abspath(str(path)))).replace("/", "\\").lower()


def _bool_env(name: str) -> bool:
    value = os.environ.get(name, "")
    return value.strip().lower() not in ("", "0", "false", "no", "off")


def _display_path(value: str | Path | None) -> str | None:
    if value is None:
        return None
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:220] + "..." if len(text) > 220 else text


def _check(check_id: str, status: str, summary: str, **details) -> dict:
    return {
        "id": check_id,
        "status": status,
        "summary": summary,
        "details": details,
    }


def _run_version(argv: list[str], timeout: int = 5) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return False, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    return result.returncode == 0, output[:220]


def _load_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None, "missing"
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return data if isinstance(data, dict) else None, None


def _parse_scalar(raw: str):
    value = raw.strip()
    if value.startswith('"') and value.endswith('"') and len(value) >= 2:
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return value


def _load_toml_fallback(path: Path) -> dict:
    data: dict = {}
    current: dict = data
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return data

    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            current = data
            parts: list[str] = []
            buf = []
            in_quote = False
            for ch in section:
                if ch == "'":
                    in_quote = not in_quote
                if ch == "." and not in_quote:
                    parts.append("".join(buf))
                    buf = []
                    continue
                buf.append(ch)
            if buf:
                parts.append("".join(buf))
            for part in parts:
                key = part.strip()
                if key.startswith("'") and key.endswith("'") and len(key) >= 2:
                    key = key[1:-1]
                current = current.setdefault(key, {})
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip()] = _parse_scalar(value)
    return data


def _load_toml(path: Path) -> dict | None:
    try:
        if _tomllib is not None:
            with path.open("rb") as fh:
                data = _tomllib.load(fh)
            return data if isinstance(data, dict) else None
        return _load_toml_fallback(path)
    except FileNotFoundError:
        return None
    except Exception:
        return _load_toml_fallback(path)


def _candidate_store_sources(repo_root: Path) -> tuple[list[dict], str | None]:
    candidates: list[dict] = []

    def add(source: str, raw: str | None, path: Path | None, active: bool) -> None:
        if raw is None or path is None:
            return
        candidates.append(
            {
                "source": source,
                "raw": _display_path(raw),
                "path": _display_path(path),
                "active": active,
            }
        )

    env_store = os.environ.get("ZMEM_STORE")
    env_data = os.environ.get("ZMEM_DATA")
    claude_plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA")
    zcode_plugin_data = os.environ.get("ZCODE_PLUGIN_DATA")
    claude_store_dir = os.environ.get("CLAUDE_PLUGIN_OPTION_STOREDIRECTORY")

    add("ZMEM_STORE", env_store, Path(env_store).expanduser() if env_store else None, bool(env_store))
    add("ZMEM_DATA", env_data, Path(env_data).expanduser() / "store.sqlite" if env_data else None, bool(env_data))
    add(
        "CLAUDE_PLUGIN_DATA",
        claude_plugin_data,
        Path(claude_plugin_data).expanduser() / "store.sqlite" if claude_plugin_data else None,
        bool(claude_plugin_data),
    )
    add(
        "ZCODE_PLUGIN_DATA",
        zcode_plugin_data,
        Path(zcode_plugin_data).expanduser() / "store.sqlite" if zcode_plugin_data else None,
        bool(zcode_plugin_data),
    )
    add(
        "CLAUDE_PLUGIN_OPTION_STOREDIRECTORY",
        claude_store_dir,
        Path(claude_store_dir).expanduser() / "store.sqlite" if claude_store_dir else None,
        bool(claude_store_dir),
    )

    manifest_default = None
    manifest_path = repo_root / ".claude-plugin" / "plugin.json"
    plugin_json, plugin_err = _load_json(manifest_path)
    if plugin_json and isinstance(plugin_json.get("userConfig"), dict):
        store_cfg = plugin_json["userConfig"].get("storeDirectory")
        if isinstance(store_cfg, dict):
            manifest_default = store_cfg.get("default")
    if isinstance(manifest_default, str) and manifest_default.strip():
        add(
            ".claude-plugin/plugin.json default",
            manifest_default,
            Path(manifest_default).expanduser() / "store.sqlite",
            False,
        )
    elif plugin_err and plugin_err != "missing":
        candidates.append(
            {
                "source": ".claude-plugin/plugin.json",
                "raw": None,
                "path": None,
                "active": False,
                "error": plugin_err,
            }
        )
    return candidates, manifest_default


def _check_store_resolution(repo_root: Path, resolved_store: Path) -> dict:
    candidates, _ = _candidate_store_sources(repo_root)
    active = [c for c in candidates if c.get("active")]
    distinct_active = {
        c["path"] for c in active if isinstance(c.get("path"), str) and c.get("path")
    }
    resolved_text = _display_path(resolved_store)

    split_brain = len(distinct_active) > 1
    fallback_divergence = any(
        c.get("source") in ("CLAUDE_PLUGIN_DATA", "ZCODE_PLUGIN_DATA")
        and c.get("path")
        and c.get("path") != resolved_text
        for c in active
    )

    if split_brain:
        status = "fail"
        summary = "Multiple active store path sources disagree; this is a split-brain risk."
    elif fallback_divergence:
        status = "warn"
        summary = "Resolved store path is stable, but host-specific fallback paths still diverge."
    else:
        status = "pass"
        summary = f"Resolved store path: {resolved_text}"

    return _check(
        "store-resolution",
        status,
        summary,
        resolved_store=resolved_text,
        candidates=candidates,
    )


def _check_local_path(resolved_store: Path) -> dict:
    try:
        host.assert_local_fs(resolved_store)
    except Exception as exc:
        return _check(
            "store-location-safety",
            "fail",
            str(exc),
            resolved_store=_display_path(resolved_store),
        )
    return _check(
        "store-location-safety",
        "pass",
        "Resolved store path is local and not under a OneDrive/UNC location.",
        resolved_store=_display_path(resolved_store),
    )


PYTHON_FLOOR = (3, 11)


def _check_python() -> dict:
    version = sys.version.split()[0]
    if sys.version_info < PYTHON_FLOOR:
        return _check(
            "python",
            "warn",
            f"Python {version} is below the supported floor; zmem is tested on "
            f"Python {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]}+ (CI and the Hermes lane "
            "both run 3.11).",
            version=version,
            interpreter=sys.executable,
        )
    return _check(
        "python",
        "pass",
        f"Python {version}",
        version=version,
        interpreter=sys.executable,
    )


def _check_sqlite_fts5() -> dict:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            conn.close()
    except Exception as exc:
        return _check(
            "sqlite-fts5",
            "fail",
            f"SQLite FTS5 is unavailable: {type(exc).__name__}: {exc}",
        )
    return _check("sqlite-fts5", "pass", "SQLite FTS5 is available.")


def _check_embeddings() -> dict:
    """Embedding availability (semantic recall/dedup).

    Degraded embeddings are a SUPPORTED state (FTS5 recall + lexical
    consolidate keep working), so unavailability is `warn`, not `fail` — it
    must not flip the report's top-level `ok` (which is `fail count == 0`).
    Pure presence check via embeddings.availability_status(): no store access,
    no network, no model load, no checksum hash — read-only and side-effect
    free, per doctor's contract.
    """
    try:
        saved_path = sys.path[:]
        sys.path.insert(0, os.path.dirname(__file__))
        try:
            import embeddings  # type: ignore
        finally:
            sys.path[:] = saved_path
    except Exception as exc:
        return _check(
            "embeddings",
            "warn",
            "Embeddings module is not importable; semantic recall/dedup disabled.",
            reason="embeddings_module_missing",
            error=f"{type(exc).__name__}: {exc}",
            interpreter=sys.executable,
        )
    try:
        st = embeddings.availability_status()
    except Exception as exc:
        return _check(
            "embeddings",
            "warn",
            "Embedding availability could not be determined.",
            reason="probe_failed",
            error=f"{type(exc).__name__}: {exc}",
            interpreter=sys.executable,
        )
    # Issue #63, 8.1: doctor OWNS the pin verdict. The module-level probe is
    # presence-only by design ("never hashes"), but in a fresh doctor process
    # nothing has ever attempted a LOAD, so `_model_checksum_ok` is still None
    # and a tampered file would silently read as healthy/None forever — exactly
    # the blind spot this PR closes. Hash it here (~one pass over the file,
    # diagnostic command): verdict becomes authoritative in the JSON.
    if (
        st.get("available")
        and st.get("profile") != "fake"
        and not st.get("missing_imports")
        and st.get("models_dir")
    ):
        try:
            ck_ok = embeddings.verify_checksum_cached(
                Path(st["models_dir"]) / "minilm.onnx"
            )
        except Exception:
            ck_ok = None
        if ck_ok is False:
            note = ""
            try:
                import embed_profiles as _ep

                note = _ep.PROFILES[_ep.DEFAULT_PROFILE]["notes"]
            except Exception:
                pass
            st = {
                **st,
                "available": False,
                "reason": "model_checksum_mismatch",
                "checksum_ok": False,
                **({"note": note} if note else {}),
            }
        elif ck_ok is True and st.get("checksum_ok") is None:
            st = {**st, "checksum_ok": True}

    status = "pass" if st["available"] else "warn"
    if st["available"]:
        summary = "Embeddings available; semantic recall/dedup active."
    else:
        summary = (
            f"Embeddings unavailable (reason={st['reason']}); semantic "
            "recall/dedup disabled — degraded FTS5/lexical mode is supported."
        )
    # Issue #63, 8.1/8.4: surface the profile dimension and the checksum
    # verdict with the Xenova-vs-sentence-transformers NOTE so an operator is
    # never left reasoning that disabling verification is the fix.
    profile = st.get("profile")
    if profile == "fake":
        summary += " (test profile: ZMEM_EMBED_PROFILE=fake — placeholder vectors)"
    return _check(
        "embeddings",
        status,
        summary,
        available=st["available"],
        reason=st["reason"],
        missing_imports=st.get("missing_imports", []),
        models_dir=st.get("models_dir"),
        interpreter=st.get("interpreter"),
        model_file=st.get("model_file"),
        tokenizer_file=st.get("tokenizer_file"),
        profile=profile,
        dim=st.get("dim"),
        checksum_ok=st.get("checksum_ok"),
        **({"note": st["note"]} if st.get("note") else {}),
    )


def _find_windows_bash() -> tuple[Path | None, str]:
    env_bash = os.environ.get("ZMEM_BASH_PATH")
    if env_bash:
        p = Path(env_bash).expanduser()
        if p.exists():
            return p, "ZMEM_BASH_PATH"

    for candidate in WINDOWS_BASH_CANDIDATES:
        if candidate.exists():
            return candidate, "known-path"
        for suffix in (".cmd", ".bat"):
            alt = Path(str(candidate.with_suffix(suffix)))
            if alt.exists():
                return alt, "known-path"

    git_path = shutil.which("git")
    if git_path:
        git_dir = Path(git_path).parent
        roots = [git_dir.parent, git_dir.parent.parent]
        for root in roots:
            for rel in ("usr/bin/bash.exe", "bin/bash.exe", "usr/bin/bash.cmd", "bin/bash.cmd"):
                candidate = root / Path(rel)
                if candidate.exists():
                    return candidate, "derived-from-git"

    bash_path = shutil.which("bash")
    if bash_path:
        return Path(bash_path), "PATH"
    return None, "missing"


def _check_node_and_bash() -> list[dict]:
    checks: list[dict] = []
    node_path = shutil.which("node")
    if not node_path:
        checks.append(_check("node", "fail", "Node.js is not on PATH."))
    else:
        ok, version = _run_version([node_path, "--version"])
        checks.append(
            _check(
                "node",
                "pass" if ok else "fail",
                f"Node.js {version}" if ok else f"Node.js invocation failed: {version}",
                path=_display_path(node_path),
                version=version,
            )
        )

    if os.name != "nt":
        bash_path = shutil.which("bash")
        if not bash_path:
            checks.append(_check("bash", "fail", "bash is not on PATH."))
        else:
            ok, version = _run_version([bash_path, "--version"])
            checks.append(
                _check(
                    "bash",
                    "pass" if ok else "fail",
                    "bash is available." if ok else f"bash invocation failed: {version}",
                    path=_display_path(bash_path),
                    version=version,
                )
            )
        return checks

    bash_path, source = _find_windows_bash()
    if bash_path is None:
        checks.append(
            _check(
                "windows-bash",
                "fail",
                "No usable Git Bash/Cygwin shell was found for Windows hooks.",
            )
        )
        return checks

    ok, version = _run_version([str(bash_path), "--version"])
    normalized = _norm_path(bash_path)
    looks_usable = any(token in normalized for token in ("\\git\\", "\\cygwin", "\\msys"))
    if not looks_usable and normalized.endswith("\\system32\\bash.exe"):
        reason = "Windows found system bash.exe, which is usually the WSL shim rather than Git Bash/Cygwin."
        status = "fail"
    elif ok and looks_usable:
        reason = f"Usable Windows shell found via {source}."
        status = "pass"
    elif ok:
        reason = f"bash runs, but the path is not recognized as Git Bash/Cygwin: {bash_path}"
        status = "fail"
    else:
        reason = f"bash invocation failed: {version}"
        status = "fail"

    checks.append(
        _check(
            "windows-bash",
            status,
            reason,
            path=_display_path(bash_path),
            version=version,
            source=source,
        )
    )
    return checks


def _check_store_access(resolved_store: Path) -> dict:
    store_exists = resolved_store.exists()
    existing_parent = resolved_store.parent
    while not existing_parent.exists() and existing_parent.parent != existing_parent:
        existing_parent = existing_parent.parent

    directory_read = os.access(existing_parent, os.R_OK | os.X_OK)
    directory_write = os.access(existing_parent, os.W_OK | os.X_OK)
    store_read = None
    if store_exists:
        try:
            uri = resolved_store.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("SELECT 1").fetchone()
            conn.close()
            store_read = True
        except Exception:
            store_read = False

    if store_exists:
        can_write = os.access(resolved_store, os.W_OK) and directory_write
    else:
        can_write = directory_write

    if store_exists and store_read is False:
        status = "fail"
        summary = "Store file exists but cannot be opened read-only."
    elif not directory_read:
        status = "fail"
        summary = "Store directory is not readable/traversable."
    elif not can_write:
        status = "fail"
        summary = (
            "Shared store path is not writable from this process. In Codex, add the store "
            "directory as a writable root or use a local broker that owns the shared store."
        )
    elif not store_exists:
        status = "warn"
        summary = "Store file does not exist yet; parent directory looks writable for first-time init."
    else:
        status = "pass"
        summary = "Store path looks readable and writable."

    return _check(
        "store-access",
        status,
        summary,
        store_exists=store_exists,
        store_path=_display_path(resolved_store),
        checked_directory=_display_path(existing_parent),
        directory_read=directory_read,
        directory_write=directory_write,
        store_read=store_read,
        store_write=can_write,
    )


def _read_schema_version(store_path: Path) -> tuple[int | None, str | None]:
    if not store_path.exists():
        return None, None
    try:
        uri = store_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"

    if not row:
        return None, "meta.schema_version missing"
    try:
        return int(row[0]), None
    except Exception:
        return None, f"unparseable schema_version={row[0]!r}"


def _check_schema(store_path: Path, access_check: dict) -> dict:
    version, err = _read_schema_version(store_path)
    writable = bool(access_check["details"].get("store_write"))
    if err:
        return _check(
            "schema-version",
            "fail",
            f"Could not inspect schema version read-only: {err}",
            expected=CURRENT_SCHEMA_VERSION,
        )
    if version is None:
        return _check(
            "schema-version",
            "warn",
            f"No store schema found yet; the first writable run will initialize v{CURRENT_SCHEMA_VERSION}.",
            expected=CURRENT_SCHEMA_VERSION,
            actual=None,
        )
    if version == CURRENT_SCHEMA_VERSION:
        return _check(
            "schema-version",
            "pass",
            f"Store schema version is v{version}.",
            expected=CURRENT_SCHEMA_VERSION,
            actual=version,
        )
    if version > CURRENT_SCHEMA_VERSION:
        return _check(
            "schema-version",
            "fail",
            f"Store schema is v{version}, newer than this checkout's expected v{CURRENT_SCHEMA_VERSION}.",
            expected=CURRENT_SCHEMA_VERSION,
            actual=version,
        )
    if writable:
        return _check(
            "schema-version",
            "warn",
            f"Store schema is v{version}; current checkout expects v{CURRENT_SCHEMA_VERSION} and will need a writable migration.",
            expected=CURRENT_SCHEMA_VERSION,
            actual=version,
        )
    return _check(
        "schema-version",
        "fail",
        f"Store schema is v{version}; current checkout expects v{CURRENT_SCHEMA_VERSION}, but this process cannot write the migration.",
        expected=CURRENT_SCHEMA_VERSION,
        actual=version,
    )


# Operational-health cadence thresholds. These are the same defaults store.py
# uses (CONSOLIDATE_MIN_INTERVAL_DAYS=7.0, ZMEM_BACKUP_INTERVAL_DAYS=1.0); they
# are WARN thresholds here, not enforcement, so a local copy is safe (doctor is
# a read-only diagnostic). doctor warns at 2x the cadence — "more than two
# intervals overdue" is a clear "maintenance has stopped running" signal without
# false-alarming on a single missed cadence tick. Env overrides are honored so
# an operator who lengthened the cadence does not get a spurious warn (#37 L23).
def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _cadence_days(name: str, default: float) -> float:
    """Read a cadence env var and clamp negative/NaN values to the default.

    This matches store.py's `_backup_interval_days` (which clamps with
    `v if v >= 0 else 1.0`) so doctor's backup warn threshold and the backup
    writer's --if-due gate agree on a malformed `ZMEM_BACKUP_INTERVAL_DAYS`.
    store.py's consolidate cadence (`CONSOLIDATE_MIN_INTERVAL_DAYS`) is NOT
    clamped upstream (a negative value there makes consolidate always run),
    so doctor deliberately normalizes it here to a sensible warn threshold
    rather than mirroring the unclamped upstream behavior (PRR-002).
    """
    v = _float_env(name, default)
    if v != v or v < 0:  # NaN check (v != v) + negative check
        v = default
    return v


def _consolidate_warn_days() -> float:
    return _cadence_days("ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS", 7.0) * 2.0


def _backup_warn_days() -> float:
    return _cadence_days("ZMEM_BACKUP_INTERVAL_DAYS", 1.0) * 2.0


def _open_store_ro(store_path: Path) -> sqlite3.Connection | None:
    """Open the store read-only. None if absent or unreadable.

    doctor is read-only by contract; the mode=ro URI guarantees no write can
    ever happen here, and reuses the same pattern as _read_schema_version.
    """
    if not store_path.exists():
        return None
    try:
        uri = store_path.resolve().as_uri() + "?mode=ro"
        return sqlite3.connect(uri, uri=True)
    except Exception:
        return None


def _meta_ts_days_ago(conn: sqlite3.Connection, key: str) -> tuple[float | None, str | None]:
    """Return (days since the meta `key` ISO timestamp, value) — (None, None) if
    the row is absent, (None, err) if unreadable. Uses the same calendar/timegm
    parsing store.py's cadence gate uses."""
    import calendar as _cal
    import time as _time
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not row or not row[0]:
        return None, None
    ts = row[0]
    try:
        # ts is guaranteed truthy here: the `if not row or not row[0]` guard
        # above already returned (None, None) for an absent/empty cell.
        epoch = _cal.timegm(_time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        return None, f"unparseable {key}={ts!r}"
    days = ((_time.time() - epoch) / 86400.0) if epoch > 0 else 999.0
    return days, ts


def _parse_ns_migration_map() -> dict[str, str]:
    """Parse ZMEM_NS_MIGRATION_MAP (old-namespace -> checkout-path) without
    importing store.py — doctor.py must stay dependency-free (schema_meta.py
    documents this: importing store.py pulls its host-path/env logic). Mirrors
    store.py._load_ns_migration_checkouts's validation: strip, JSON decode,
    require a dict[str, str]. Returns {} on any error (including unconfigured),
    never raises.
    """
    raw = os.environ.get("ZMEM_NS_MIGRATION_MAP", "").strip()
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in loaded.items():
        if isinstance(k, str) and isinstance(v, str):
            out[k] = v
    return out


def _check_ns_migration(resolved_store: Path) -> dict:
    """Read-only preview of pending namespace migrations (#39 E8).

    store.py's _retry_pending_ns_migration runs on EVERY migrate() and silently
    re-keys any namespace still carrying an old-style key whose checkout is now
    present. That self-heal is invisible to the operator. This check probes the
    SAME read-only SELECT (minus the re-key) so doctor surfaces how many
    namespaces are currently stranded — a dry-run of the retry. Status:
      pass  no migration map configured, OR no stranded rows
      warn  N namespace(s) would be re-keyed on next store.py invocation
      skip  store absent/unreadable (already flagged by store-access)
    """
    migration_map = _parse_ns_migration_map()
    if not migration_map:
        return _check(
            "ns-migration",
            "pass",
            "ZMEM_NS_MIGRATION_MAP not configured — namespace-migration self-heal "
            "is inactive (fine unless this store predates the v5 box-wide re-key).",
        )
    # Match store.py's _retry_pending_ns_migration / _rekey_namespaces: only
    # namespaces whose checkout dir is PRESENT on disk are actually re-keyed.
    # An absent checkout is skipped and retried later (PRR-008). Filter the map
    # so the preview only counts namespaces that would really be re-keyed.
    actionable = {
        old_ns: checkout for old_ns, checkout in migration_map.items()
        if Path(checkout).is_dir()
    }
    skipped_absent = len(migration_map) - len(actionable)
    conn = _open_store_ro(resolved_store)
    if conn is None:
        return _check(
            "ns-migration",
            "skip",
            "Store not available; skipped namespace-migration preview.",
        )
    try:
        if not actionable:
            count = 0
        else:
            placeholders = ",".join("?" * len(actionable))
            try:
                count = conn.execute(
                    f"SELECT COUNT(DISTINCT namespace) FROM memory "
                    f"WHERE namespace IN ({placeholders})",
                    list(actionable.keys()),
                ).fetchone()[0]
            except Exception as exc:
                return _check(
                    "ns-migration",
                    "warn",
                    f"Could not probe namespace-migration state: "
                    f"{type(exc).__name__}: {exc}",
                )
    finally:
        conn.close()
    if count == 0:
        note = ""
        if skipped_absent:
            note = (f" ({skipped_absent} mapped namespace(s) have an absent "
                    f"checkout dir — their rows will be skipped until the "
                    f"checkout appears, then re-keyed automatically.)")
        return _check(
            "ns-migration",
            "pass",
            "No namespaces stranded under old-style keys with a present checkout "
            "— the v5 self-heal has nothing left to do." + note,
        )
    return _check(
        "ns-migration",
        "warn",
        f"{count} namespace(s) still carry old-style keys and would be re-keyed "
        f"on the next store.py invocation (the retry self-heals automatically). "
        f"Old keys: {', '.join(sorted(actionable)[:5])}"
        + (" ..." if len(actionable) > 5 else ""),
        stranded_count=count,
    )


def _check_operational_health(resolved_store: Path) -> list[dict]:
    """Backup + consolidation cadence health from the `meta` table (#37 L23).

    Reads last_backup / last_consolidation read-only and warns when maintenance
    has never run or is more than 2x its cadence overdue. Skips (not fails) when
    the store is absent or unreadable — a missing store is already flagged by
    the store-access check, and an unreadable one must not wedge doctor.
    """
    conn = _open_store_ro(resolved_store)
    if conn is None:
        return [
            _check("operational-health", "skip",
                   "Store not available; skipped backup/consolidation cadence checks."),
        ]
    checks: list[dict] = []
    try:
        for name, key, warn_days in (
            ("backup", "last_backup", _backup_warn_days()),
            ("consolidation", "last_consolidation", _consolidate_warn_days()),
        ):
            days, ts_or_err = _meta_ts_days_ago(conn, key)
            if ts_or_err and days is None:
                # unreadable row — warn (do not fail the whole report)
                checks.append(_check(
                    f"operational-health-{name}", "warn",
                    f"last_{name} present but unreadable: {ts_or_err}",
                    key=key,
                ))
                continue
            if days is None:
                checks.append(_check(
                    f"operational-health-{name}", "warn",
                    f"last_{name}: (never) — maintenance has not run yet. The "
                    f"session-start hook fires {name} on a cadence; if this store "
                    f"predates the hook or the hook is disabled, run "
                    f"`store.py {'backup --if-due' if name == 'backup' else 'consolidate'}` manually.",
                    key=key,
                ))
                continue
            if days > warn_days:
                checks.append(_check(
                    f"operational-health-{name}", "warn",
                    f"last_{name}: {ts_or_err} ({days:.1f}d ago, more than "
                    f"{warn_days:.1f}d / 2x cadence overdue). Maintenance may not "
                    f"be running on the session-start hook.",
                    key=key, last_value=ts_or_err, days_ago=round(days, 1),
                    warn_days=round(warn_days, 1),
                ))
            else:
                # "within warning threshold" (not "within cadence"): the pass
                # threshold is 2x the real cadence, so a value between 1x and
                # 2x may already be past the writer's --if-due gate. Phrase the
                # status as healthy (under the 2x warn line) without implying
                # the cadence tick is fully current (PRR-007).
                checks.append(_check(
                    f"operational-health-{name}", "pass",
                    f"last_{name}: {ts_or_err} ({days:.1f}d ago, under the "
                    f"{warn_days:.1f}d warning threshold).",
                    key=key, last_value=ts_or_err, days_ago=round(days, 1),
                ))
    finally:
        conn.close()
    return checks


def _check_claude_native_memory(home: Path) -> dict:
    inspected: list[dict] = []
    if _bool_env("CLAUDE_CODE_DISABLE_AUTO_MEMORY"):
        return _check(
            "claude-native-memory",
            "pass",
            "Claude native memory is disabled via CLAUDE_CODE_DISABLE_AUTO_MEMORY.",
            inspected=inspected,
        )

    settings_dir = home / ".claude"
    for name in ("settings.json", "settings.local.json"):
        path = settings_dir / name
        data, err = _load_json(path)
        inspected.append({"path": _display_path(path), "status": err or "ok"})
        if isinstance(data, dict) and data.get("autoMemoryEnabled") is False:
            return _check(
                "claude-native-memory",
                "pass",
                f"Claude native memory is disabled in {name}.",
                inspected=inspected,
            )
        if isinstance(data, dict) and data.get("autoMemoryEnabled") is True:
            return _check(
                "claude-native-memory",
                "fail",
                f"Claude native memory is explicitly enabled in {name}; disable it for ZMem cutover.",
                inspected=inspected,
            )

    return _check(
        "claude-native-memory",
        "fail",
        "Claude native memory still looks enabled; set autoMemoryEnabled=false or CLAUDE_CODE_DISABLE_AUTO_MEMORY=1.",
        inspected=inspected,
    )


def _lookup_project_trust(codex_cfg: dict, project: Path) -> str | None:
    projects = codex_cfg.get("projects")
    if not isinstance(projects, dict):
        return None
    target = _norm_path(project)
    for raw_key, value in projects.items():
        if _norm_path(raw_key) == target and isinstance(value, dict):
            trust = value.get("trust_level")
            return trust if isinstance(trust, str) else None
    return None


def _hook_state_for_repo(codex_cfg: dict, repo_root: Path) -> list[str]:
    hooks = codex_cfg.get("hooks")
    if not isinstance(hooks, dict):
        return []
    state = hooks.get("state")
    if not isinstance(state, dict):
        return []
    repo_norm = _norm_path(repo_root)
    hits = []
    for key in state.keys():
        if repo_norm in _norm_path(key):
            hits.append(str(key))
    return hits


def _check_codex_memory_and_trust(home: Path, project: Path, repo_root: Path) -> list[dict]:
    checks: list[dict] = []
    config_path = home / ".codex" / "config.toml"
    cfg = _load_toml(config_path)
    if cfg is None:
        checks.append(
            _check(
                "codex-native-memory",
                "warn",
                "No Codex config.toml was found for read-only inspection.",
                config_path=_display_path(config_path),
            )
        )
        checks.append(
            _check(
                "codex-hook-trust",
                "skip",
                "No Codex config.toml was found; project trust/hook approval could not be inspected.",
                config_path=_display_path(config_path),
            )
        )
        return checks

    features = cfg.get("features") if isinstance(cfg, dict) else {}
    memories = cfg.get("memories") if isinstance(cfg, dict) else {}
    feature_memories = bool(features.get("memories")) if isinstance(features, dict) else False
    use_memories = bool(memories.get("use_memories")) if isinstance(memories, dict) else False
    generate_memories = bool(memories.get("generate_memories")) if isinstance(memories, dict) else False
    memory_enabled = feature_memories or use_memories or generate_memories

    if memory_enabled:
        checks.append(
            _check(
                "codex-native-memory",
                "fail",
                "Codex native memories still look enabled in config.toml; disable them before using ZMem as the sole durable memory system.",
                config_path=_display_path(config_path),
                feature_memories=feature_memories,
                use_memories=use_memories,
                generate_memories=generate_memories,
            )
        )
    else:
        checks.append(
            _check(
                "codex-native-memory",
                "pass",
                "Codex native memories look disabled in config.toml.",
                config_path=_display_path(config_path),
                feature_memories=feature_memories,
                use_memories=use_memories,
                generate_memories=generate_memories,
            )
        )

    trust_level = _lookup_project_trust(cfg, project)
    repo_has_codex_hooks = (repo_root / ".codex" / "hooks.json").exists()
    hook_state_hits = _hook_state_for_repo(cfg, repo_root)
    if trust_level == "trusted":
        if repo_has_codex_hooks and not hook_state_hits:
            status = "warn"
            summary = "Project is trusted in Codex, but repo-local hooks are not yet approved; reapprove them after cutover."
        else:
            status = "pass"
            summary = "Codex project trust is present."
    elif trust_level:
        status = "warn"
        summary = f"Codex project trust is {trust_level!r}; trusted is recommended before hook-based cutover."
    else:
        status = "warn"
        summary = "Codex project trust is not recorded in config.toml; trust the project before enabling hooks."

    checks.append(
        _check(
            "codex-hook-trust",
            status,
            summary,
            config_path=_display_path(config_path),
            project=_display_path(project),
            trust_level=trust_level,
            repo_has_codex_hooks=repo_has_codex_hooks,
            approved_hook_entries=len(hook_state_hits),
        )
    )
    return checks


def _check_namespace(project: Path) -> dict:
    if not project.exists():
        return _check(
            "canonical-namespace",
            "fail",
            f"Project path does not exist: {project}",
            project=_display_path(project),
        )
    try:
        namespace = host.resolve_namespace(project)
    except Exception as exc:
        return _check(
            "canonical-namespace",
            "fail",
            f"Could not resolve project namespace: {type(exc).__name__}: {exc}",
            project=_display_path(project),
        )
    return _check(
        "canonical-namespace",
        "pass",
        f"Canonical namespace: {namespace}",
        project=_display_path(project),
        namespace=namespace,
    )


def _check_surfaces(repo_root: Path) -> dict:
    required = {
        "claude_plugin": [
            repo_root / ".claude-plugin" / "plugin.json",
            repo_root / "hooks" / "hooks.claude.json",
        ],
        "zcode_plugin": [
            repo_root / ".zcode-plugin" / "plugin.json",
            repo_root / "hooks" / "hooks.zcode.json",
        ],
        "memory_skill": [
            repo_root / "skills" / "memory" / "SKILL.md",
        ],
    }
    optional_codex = [
        repo_root / ".codex" / "hooks.json",
        repo_root / ".codex" / "AGENTS.md",
    ]
    details = {}
    missing_required = []
    for key, files in required.items():
        present = [p for p in files if p.exists()]
        details[key] = {
            "required": [_display_path(p) for p in files],
            "present": [_display_path(p) for p in present],
        }
        if len(present) != len(files):
            missing_required.append(key)

    optional_present = [_display_path(p) for p in optional_codex if p.exists()]
    details["codex_adapter_optional"] = {
        "expected_examples": [_display_path(p) for p in optional_codex],
        "present": optional_present,
    }

    if missing_required:
        status = "fail"
        summary = f"Required host surfaces are missing: {', '.join(missing_required)}."
    elif optional_present:
        status = "pass"
        summary = "Claude plugin, ZCode plugin, and memory skill surfaces are present; optional Codex adapter files are also present."
    else:
        status = "pass"
        summary = "Claude plugin, ZCode plugin, and memory skill surfaces are present; optional Codex adapter files are not in this repo yet."

    return _check("host-surfaces", status, summary, surfaces=details)


# The three v9 (issue #59, 4.1) append-only lineage columns. A healthy store
# post-migration carries all three; their ABSENCE means the store is pre-v9 and
# waiting for a writable migration run (the schema-version check already flags
# that case separately).
V9_COLUMNS = ("valid_until", "update_of", "taint")


def _check_v9_columns(resolved_store: Path) -> dict:
    """Confirm the v9 append-only lineage columns exist (issue #59, 4.1).

    Read-only probe of PRAGMA table_info(memory) for valid_until / update_of /
    taint. Pass when all three are present; warn when the store is pre-v9
    (migrate() adds them on the next WRITABLE store.py run, which is exactly
    what the schema-version check already warns about); skip when the store is
    absent/unreadable (already flagged by store-access). Never writes.
    """
    conn = _open_store_ro(resolved_store)
    if conn is None:
        return _check(
            "v9-columns", "skip",
            "Store not available; skipped v9 columns check.",
        )
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
    except Exception as exc:
        return _check(
            "v9-columns", "warn",
            f"Could not inspect memory table columns: {type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()
    missing = [c for c in V9_COLUMNS if c not in cols]
    if not missing:
        return _check(
            "v9-columns", "pass",
            "memory table carries all v9 lineage columns "
            "(valid_until, update_of, taint).",
            columns=list(V9_COLUMNS),
        )
    return _check(
        "v9-columns", "warn",
        f"memory table is missing v9 column(s): {', '.join(missing)}; a writable "
        f"store.py run will add them via migration (issue #59, 4.1).",
        missing=missing, expected=list(V9_COLUMNS),
    )


# v10 (issue #60, 5.1): the entity identity tables. Doctor probes presence +
# row counts so a dead entity lane (tables present but permanently empty on a
# store with memories — e.g. a migration whose backfill never ran) is one
# command away from being noticed. The deeper inspection surface is
# `store.py entity-list`.
V10_ENTITY_TABLES = ("entity", "entity_alias", "memory_entity")


def _check_entity_tables(resolved_store: Path) -> dict:
    """Confirm the v10 entity tables exist and are not vacuously empty.

    Read-only probe of sqlite_master + COUNT(*). Pass when all three tables
    exist AND (the store has no memories OR at least one entity/link row — an
    entity-free store is legitimate only when nothing was ever extracted,
    e.g. a fresh store or one whose memories mention no eligible tokens).
    Warn when the tables are missing (pre-v10; the next writable store.py run
    migrates and backfills) or when memories exist but no entity was ever
    derived (the extractor or its wiring went dark). Skip when the store is
    absent/unreadable (already flagged by store-access). Never writes.
    """
    conn = _open_store_ro(resolved_store)
    if conn is None:
        return _check(
            "entity-tables", "skip",
            "Store not available; skipped entity tables check.",
        )
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        missing = [t for t in V10_ENTITY_TABLES if t not in tables]
        if missing:
            return _check(
                "entity-tables", "warn",
                f"entity table(s) missing: {', '.join(missing)}; a writable "
                f"store.py run will create them and backfill via migration "
                f"(issue #60, 5.1).",
                missing=missing, expected=list(V10_ENTITY_TABLES),
            )
        n_memory = conn.execute("SELECT count(*) FROM memory").fetchone()[0]
        n_entities = conn.execute("SELECT count(*) FROM entity").fetchone()[0]
        n_links = conn.execute("SELECT count(*) FROM memory_entity").fetchone()[0]
    except Exception as exc:
        return _check(
            "entity-tables", "warn",
            f"Could not inspect entity tables: {type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()
    if n_memory > 0 and n_entities == 0 and n_links == 0:
        return _check(
            "entity-tables", "warn",
            f"entity tables exist but are empty on a store with {n_memory} "
            f"memory row(s) — the deterministic extractor should have derived "
            f"at least namespace/tag entities on write (issue #60, 5.2). "
            f"Re-run a writable store.py command to trigger the migration "
            f"backfill, then inspect with `store.py entity-list`.",
            entities=n_entities, links=n_links, memories=n_memory,
        )
    return _check(
        "entity-tables", "pass",
        f"entity identity tables present (entities={n_entities}, "
        f"links={n_links}); inspect with `store.py entity-list`.",
        entities=n_entities, links=n_links, memories=n_memory,
    )


# v11 (issue #61, 6.1): the associative-link surface. Doctor probes the
# memory_link table + the memory.trust_score column and sanity-reads the
# trust range, so the link lane's health is one command away via
# `store.py links`. An EMPTY link table is legitimate: links accumulate from
# new writes only (the v11 migration deliberately runs no backfill — the
# phase-7 `organize` command owns bulk backfill), so a just-migrated store
# with zero edges is healthy.
V11_LINK_TABLE = "memory_link"


def _check_link_tables(resolved_store: Path) -> dict:
    """Confirm the v11 link surface exists and trust_score is in range.

    Read-only probe of sqlite_master + PRAGMA table_info(memory) + MIN/MAX.
    Pass when memory_link exists, memory.trust_score exists, and every
    trust_score is within [0.0, 1.0] (adjust_trust clamps in SQL, so an
    out-of-range value means a hand-edited store). Warn when the table or
    column is missing (pre-v11; the next writable store.py run migrates) or
    the range drifted. Skip when the store is absent/unreadable (already
    flagged by store-access). Never writes.
    """
    conn = _open_store_ro(resolved_store)
    if conn is None:
        return _check(
            "link-tables", "skip",
            "Store not available; skipped link tables check.",
        )
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if V11_LINK_TABLE not in tables:
            return _check(
                "link-tables", "warn",
                f"{V11_LINK_TABLE} table missing; a writable store.py run "
                "will create it via migration (issue #61, 6.1).",
                missing=[V11_LINK_TABLE], expected=[V11_LINK_TABLE],
            )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(memory)")}
        if "trust_score" not in cols:
            return _check(
                "link-tables", "warn",
                "memory.trust_score column missing; a writable store.py run "
                "will add it via migration (issue #61, 6.1).",
                missing=["trust_score"], expected=["trust_score"],
            )
        n_links = conn.execute(
            f"SELECT count(*) FROM {V11_LINK_TABLE}"
        ).fetchone()[0]
        lo, hi = conn.execute(
            "SELECT MIN(trust_score), MAX(trust_score) FROM memory"
        ).fetchone()
    except Exception as exc:
        return _check(
            "link-tables", "warn",
            f"Could not inspect link tables: {type(exc).__name__}: {exc}",
        )
    finally:
        conn.close()
    if lo is not None and (lo < 0.0 or hi > 1.0):
        return _check(
            "link-tables", "warn",
            f"trust_score range [{lo}, {hi}] outside [0.0, 1.0] — writes "
            "clamp in SQL, so this store was hand-edited; inspect with "
            "`store.py get --json`.",
            trust_min=lo, trust_max=hi,
        )
    return _check(
        "link-tables", "pass",
        f"memory_link table present (edges={n_links}); trust_score in range "
        f"[{lo if lo is not None else 'n/a'}, {hi if hi is not None else 'n/a'}]; "
        "inspect with `store.py links --id <uuid>`.",
        edges=n_links, trust_min=lo, trust_max=hi,
    )


# Tier-0 size guard thresholds (issue #49 C). core.md is injected into EVERY
# session on EVERY hook host, so an overgrown file silently eats context
# budget and dilutes instruction-following (~150-200 reliably-handled
# instructions, per claude-reflect's memory-file health threshold; zmem's
# bound is slightly more generous). Fixed constants, not env-tunable, matching
# CONSOLIDATE_MAX_ROWS_PER_NAMESPACE's rationale in store.py: a misconfigured
# knob silently disabling a health guard is worse than a conservative bound.
TIER0_WARN_LINES = 200
TIER0_WARN_BYTES = 16 * 1024


def _tier0_file_stats(path: Path) -> dict | None:
    """Line/byte stats for one Tier-0 file, or None when absent/unreadable.
    Never raises (doctor is read-only, fail-open diagnostics). Bytes come from
    stat() and lines are counted incrementally, so an oversized file — the
    exact case this guard exists to catch — is measured without ever holding
    its full contents in memory (PR feedback PRR-002)."""
    try:
        if not path.is_file():
            return None
        size = path.stat().st_size
        lines = 0
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _line in fh:
                lines += 1
        return {
            "path": _display_path(path),
            "lines": lines,
            "bytes": size,
        }
    except Exception:
        return None


def _check_tier0_size(project: Path) -> dict:
    """Tier-0 always-injected surface size (issue #49 C): core.md, resolved
    the same canonical way the session hook resolves it
    (host.resolve_core_md_path: ZMEM_CORE_MD override, else <store dir>/
    core.md), plus the ZCode project-level AGENTS.md when present — both are
    injected every session and compete with recall output for the context
    budget. Measured paths appear in the summary so what was measured is
    visible (the AGENTS.md scope is the --project argument, matching how the
    session hook scopes PROJECT/AGENTS.md)."""
    files: list[dict] = []
    try:
        core_path = host.resolve_core_md_path()
    except Exception:
        # Doctor is fail-open diagnostics: a hostile/unresolvable store env
        # must never traceback the whole report (the docstring's "never
        # raises" covers the resolution step too, not just the file read).
        core_path = None
    core_stats = _tier0_file_stats(core_path) if core_path is not None else None
    if core_stats is not None:
        files.append(core_stats)
    agents_stats = _tier0_file_stats(project / "AGENTS.md")
    if agents_stats is not None:
        files.append(agents_stats)

    if not files:
        return _check(
            "tier0-size",
            "skip",
            "No Tier-0 core.md (or project AGENTS.md) found — nothing "
            "always-injected to size-check.",
        )

    described = "; ".join(
        f"{f['path']}: {f['lines']} lines / {f['bytes']} bytes" for f in files
    )
    over = [
        f for f in files
        if f["lines"] > TIER0_WARN_LINES or f["bytes"] > TIER0_WARN_BYTES
    ]
    if over:
        return _check(
            "tier0-size",
            "warn",
            f"Tier-0 file(s) exceed the {TIER0_WARN_LINES}-line / "
            f"{TIER0_WARN_BYTES // 1024}KB context-budget guideline ({described}). "
            "Prune them, or move durable knowledge into the store (retrieved on "
            "relevance, not always-injected).",
            files=files, warn_lines=TIER0_WARN_LINES, warn_bytes=TIER0_WARN_BYTES,
        )
    return _check(
        "tier0-size",
        "pass",
        f"Tier-0 always-injected file(s) within the size guideline ({described}).",
        files=files, warn_lines=TIER0_WARN_LINES, warn_bytes=TIER0_WARN_BYTES,
    )


def _check_session_retention(home: Path) -> dict:
    """Claude Code transcript retention (issue #49 C): CC deletes transcripts
    after `cleanupPeriodDays` (default 30) from ~/.claude/settings.json, and
    zmem's transcript-based features (failures on Stop; the #46/#48 mining
    commands) lose history accordingly. Concept ported from MIT claude-reflect
    (scripts/session_start_reminder.py + reflect_utils.get_cleanup_period_days).
    Deliberately `pass` (never warn or fail): the default is a host policy,
    not a misconfiguration, and the remediation only matters if the user wants
    historical mining. Claude-Code scoped — a box with no ~/.claude reports a
    clean not-applicable skip."""
    settings_dir = home / ".claude"
    if not settings_dir.is_dir():
        return _check(
            "session-retention",
            "skip",
            "not applicable — no Claude Code installation detected",
        )

    # settings.local.json overrides settings.json when the key is present
    # there; a non-int value counts as unset (fail toward the default note,
    # never an error — doctor is read-only diagnostics).
    days = None
    source = None
    inspected: list[dict] = []
    for name in ("settings.json", "settings.local.json"):
        path = settings_dir / name
        data, err = _load_json(path)
        inspected.append({"path": _display_path(path), "status": err or "ok"})
        if isinstance(data, dict) and "cleanupPeriodDays" in data:
            value = data["cleanupPeriodDays"]
            # Non-positive ints count as unset (PR feedback PRR-019): CC has
            # no meaningful <=0 retention, and echoing "-5 day(s)" into the
            # summary would read as valid configuration. An INVALID value is
            # also non-destructive: it must not clobber a valid value already
            # read from the other file (feedback reviewer finding) — an
            # invalid local override just fails to override.
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                days = value
                source = name

    if days is None:
        return _check(
            "session-retention",
            "pass",
            "Claude Code transcript retention is unset (or unreadable/invalid), so the "
            "30-day default applies. Only matters if you want historical "
            "transcript mining; to extend it, set "
            '{"cleanupPeriodDays": <larger int>} in ~/.claude/settings.json.',
            cleanup_period_days=None, configured=False, default=30,
            inspected=inspected,
        )
    if days <= 30:
        return _check(
            "session-retention",
            "pass",
            f"Claude Code deletes transcripts after {days} day(s) ({source}) — "
            "default-like retention. Only matters if you want historical "
            "transcript mining; to extend it, raise cleanupPeriodDays in "
            "~/.claude/settings.json.",
            cleanup_period_days=days, configured=True, default=30,
            inspected=inspected,
        )
    return _check(
        "session-retention",
        "pass",
        f"Claude Code retains transcripts for {days} day(s) ({source}) — "
        "transcript mining history preserved.",
        cleanup_period_days=days, configured=True, default=30,
        inspected=inspected,
    )


def _recommendations(checks: list[dict]) -> list[str]:
    by_id = {check["id"]: check for check in checks}
    notes: list[str] = []

    if by_id.get("store-access", {}).get("status") == "fail":
        notes.append(
            "If Codex cannot write the shared store path, add that directory as a writable root or route store operations through a local broker that owns the store."
        )
    if by_id.get("claude-native-memory", {}).get("status") == "fail":
        notes.append(
            "Disable Claude native memory yourself; never let zmem auto-edit ~/.claude/settings*.json."
        )
    if by_id.get("codex-native-memory", {}).get("status") == "fail":
        notes.append(
            "Disable Codex native memories in ~/.codex/config.toml before cutover; keep ZMem as the only durable memory layer."
        )
    if by_id.get("codex-hook-trust", {}).get("status") in ("warn", "fail"):
        notes.append(
            "After installing any Codex hook surface, trust the project and reapprove hooks so the new path is explicit and reviewable."
        )
    if by_id.get("schema-version", {}).get("status") == "warn":
        notes.append(
            f"Run the first writable zmem command only after the shared store path is correct; that first run may need to initialize or migrate schema v{CURRENT_SCHEMA_VERSION}."
        )
    emb = by_id.get("embeddings", {})
    if emb.get("status") == "warn":
        reason = emb.get("details", {}).get("reason")
        details = emb.get("details", {})
        if reason == "imports_missing":
            # Name ONLY the modules actually reported missing (a partial install
            # may lack just one), not all three unconditionally.
            missing = details.get("missing_imports") or []
            if missing:
                pkgs = ", ".join(missing)
                notes.append(
                    f"Embeddings are disabled because the Python interpreter zmem "
                    f"resolves is missing: {pkgs}. Install the missing package(s) "
                    "in that interpreter to enable semantic recall/dedup; degraded "
                    "FTS5/lexical operation is supported meanwhile."
                )
            else:
                notes.append(
                    "Embeddings are disabled because the Python interpreter zmem "
                    "resolves is missing one or more of onnxruntime/tokenizers/numpy. "
                    "Install them in that interpreter to enable semantic "
                    "recall/dedup; degraded FTS5/lexical operation is supported meanwhile."
                )
        elif reason in ("model_file_missing", "tokenizer_missing"):
            # Both files are required — say so explicitly so a tokenizer-only
            # outage isn't mis-diagnosed as "add the model".
            notes.append(
                "Embeddings are disabled because minilm.onnx and/or tokenizer.json "
                "are absent from the resolved models dir. Both files are required: "
                "place a checksum-verified minilm.onnx AND tokenizer.json there "
                "(or set ZMEM_MODEL_URL to a source matching the pinned SHA-256 "
                "plus ZMEM_MODEL_AUTODOWNLOAD=1). After enabling, run `reembed` "
                "to backfill existing rows. Degraded FTS5/lexical operation is "
                "supported meanwhile."
            )
        elif reason == "model_checksum_mismatch":
            # Issue #63, 8.1: name WHY a hash can differ from what the operator
            # expected, so nobody 'fixes' verification by disabling it. There
            # is deliberately NO unverified-load escape hatch.
            note = details.get("note") or ""
            notes.append(
                "The installed minilm.onnx FAILED its checksum pin. " + note +
                " Restore the correct Xenova ONNX export (or point "
                "ZMEM_MODEL_URL at a source matching the pinned SHA-256 with "
                "ZMEM_MODEL_AUTODOWNLOAD=1); the model stays refused until then."
            )
        else:
            notes.append(
                "Embeddings are disabled; semantic recall/dedup is off (degraded "
                "FTS5/lexical mode is supported). See the README 'Embeddings' "
                "section to enable them, then run `reembed` to backfill."
            )
    return notes


def _render_human(report: dict) -> str:
    lines = []
    summary = report["summary"]
    lines.append(
        "zmem doctor: "
        f"{'OK' if report['ok'] else 'BLOCKED'} "
        f"(pass={summary['pass']} warn={summary['warn']} fail={summary['fail']} skip={summary['skip']})"
    )
    lines.append(f"Resolved store: {report['resolved_store']}")
    lines.append(f"Project namespace: {report.get('namespace') or '(not resolved)'}")
    lines.append("")
    for check in report["checks"]:
        lines.append(f"[{check['status'].upper()}] {check['id']}: {check['summary']}")
    if report["recommendations"]:
        lines.append("")
        lines.append("Next steps:")
        for note in report["recommendations"]:
            lines.append(f"- {note}")
    return "\n".join(lines)


def _check_vec_ns_overfetch(resolved_store: Path, namespace: str | None = None) -> dict:
    """Report live-in-namespace vec rows vs ZMEM_VEC_NS_OVERFETCH (issue #58, 3.7).

    Warns when the ratio of live vec rows in the CURRENT namespace to the
    configured over-fetch factor is < 1, meaning the recall path's vec KNN
    window may not have enough rows to guarantee a same-namespace hit.
    PR-review fix PRR-031: the count is scoped to the resolved namespace when
    one is available (an all-namespaces count let unrelated-namespace volume
    mask the current namespace having none). Falls back to the all-namespace
    count when no namespace was resolved. Skipped when the store is
    absent/unreadable (already flagged by store-access).
    """
    from storelib.schema import ZMEM_VEC_NS_OVERFETCH_DEFAULT, ZMEM_VEC_NS_OVERFETCH_ENV, _load_vec
    conn = _open_store_ro(resolved_store)
    if conn is None:
        return _check(
            "vec-ns-overfetch", "skip",
            "Store not available; skipped vec-ns-overfetch check.",
        )
    # The vec0 virtual table needs the sqlite-vec extension loaded into the
    # connection — _open_store_ro (deliberately minimal) does not load it, so
    # without this the query below raises "no such module: vec0" and the
    # check could never count anything on a real store (review follow-up to
    # PRR-002: the check was dead-on-arrival, not merely crash-prone).
    try:
        _load_vec(conn)
    except Exception:
        conn.close()
        return _check(
            "vec-ns-overfetch", "skip",
            "sqlite-vec extension unavailable; vec-ns-overfetch skipped.",
        )
    ns_filter = ""
    params: list = []
    if namespace:
        ns_filter = " AND m.namespace = ? "
        params.append(namespace)
    try:
        # PRR-002 fix: _open_store_ro returns default (tuple) rows — read the
        # aggregate positionally; the previous fetchone()["c"] raised TypeError
        # on every readable store containing memory_vec.
        row = conn.execute(
            "SELECT count(*) FROM memory_vec mv "
            "JOIN memory m ON m.id = mv.memory_id "
            "WHERE m.superseded_at IS NULL"
            + ns_filter,
            params,
        ).fetchone()
        ns_rows = int(row[0]) if row else 0
    except (sqlite3.OperationalError, TypeError, ValueError):
        return _check(
            "vec-ns-overfetch", "skip",
            "memory_vec table not present; vec-ns-overfetch skipped.",
        )
    finally:
        conn.close()

    # PRR-005 fix: reject non-finite overrides (nan/inf parse as floats but
    # poison every downstream int()/ratio computation); fall back to default.
    raw_env = os.environ.get(ZMEM_VEC_NS_OVERFETCH_ENV, "")
    overfetch = float(ZMEM_VEC_NS_OVERFETCH_DEFAULT)
    if raw_env:
        try:
            candidate = float(raw_env)
            if candidate == candidate and candidate not in (
                float("inf"), float("-inf")
            ):
                overfetch = candidate
        except ValueError:
            pass
    ratio = (ns_rows / overfetch) if overfetch > 0 else 0.0
    status = "pass" if ratio >= 1.0 else "warn"
    summary = (
        f"vec-ns-overfetch: live_vec_rows(namespace={namespace or 'ALL'})={ns_rows} "
        f"ZMEM_VEC_NS_OVERFETCH={overfetch:g} ratio={ratio:.2f}"
    )
    return _check(
        "vec-ns-overfetch", status, summary,
        namespace=namespace,
        live_vec_rows=ns_rows,
        zmem_vec_ns_overfetch=overfetch,
        ratio=ratio,
    )


def _check_hybrid_default() -> dict:
    """Report whether the hybrid default can fire (issue #58, 3.7, 3.3).

    Surfaces the embeddings availability so the operator can see whether
    `recall` will use hybrid (default when embeddings are available) or
    lexical-only. Status is `pass` when embeddings are available, `info`
    when unavailable (the default still works — it just falls back to
    lexical, a SUPPORTED degraded state, so it must not flip `ok`), `warn`
    when the probe itself errors.

    PRR-001R fix: `embeddings` is NOT importable at module scope (its import
    may fail on a bare interpreter); use the same sys.path-guarded local
    import `_check_embeddings` uses. The previous module-level reference
    NameError'd into the except branch on every box, permanently reporting
    "probe failed: NameError" and leaving the "info" path (and its counts
    aggregation) dead code.
    """
    saved_path = sys.path[:]
    sys.path.insert(0, os.path.dirname(__file__))
    try:
        import embeddings  # type: ignore
    except Exception as exc:
        return _check(
            "hybrid-default", "warn",
            f"hybrid-default: embeddings module not importable "
            f"({type(exc).__name__}); recall defaults to lexical.",
        )
    finally:
        sys.path[:] = saved_path
    try:
        st = embeddings.availability_status()
    except Exception as exc:
        return _check(
            "hybrid-default", "warn",
            f"hybrid-default: availability probe failed: {type(exc).__name__}: {exc}",
        )
    available = bool(st.get("available"))
    reason = st.get("reason") or "unknown"
    status = "pass" if available else "info"
    summary = (
        f"hybrid-default: embeddings.available={available} reason={reason}"
    )
    return _check(
        "hybrid-default", status, summary,
        available=available,
        reason=reason,
        missing_imports=st.get("missing_imports", []),
    )


def _store_path_is_temp(resolved_store: Path) -> bool:
    """True when the resolved store lives under the system temp directory —
    the ONLY place where running the test-only `fake` profile counts as sane.
    Best-effort: any resolution error means 'not temp' (fail loud)."""
    import tempfile as _tempfile

    try:
        return resolved_store.resolve().is_relative_to(
            Path(_tempfile.gettempdir()).resolve())
    except Exception:
        return False


def _embedding_health_warnings(
    *,
    active_profile,
    embeddings_available: bool,
    matches_store,  # bool | None
    total_live,     # int | None
    with_emb,       # int | None
    store_is_temp: bool,
) -> list:
    """Pure decision core of _check_embeddings_health warnings (unit-tested
    directly because the real conditions need a provisioned runtime that
    model-absent CI cannot provide)."""
    warnings: list = []
    if active_profile == "fake" and not store_is_temp:
        warnings.append(
            "ZMEM_EMBED_PROFILE=fake is active on a NON-temporary store — "
            "rows written now are deterministic placeholders, not "
            "semantic vectors. Unset it for real use."
        )
    if (
        embeddings_available
        and active_profile != "fake"
        and matches_store is not False
        and total_live
        and with_emb == 0
    ):
        warnings.append(
            "hybrid recall default is ON but this store has ZERO embedded "
            "rows — the vector lane has nothing to fuse. Run "
            "`store.py reembed` to backfill once the model files are in place."
        )
    return warnings


def _check_embeddings_health(resolved_store: Path) -> dict:
    """Store-side embedding health (issue #63, 8.4).

    One read-only aggregation over the resolved store: active profile vs the
    dimension actually committed to the store, rows with/without embeddings,
    shipped-profile inventory, and two operator-safety warnings:
      - fake profile on anything NOT under the system temp dir (a forgotten
        ZMEM_EMBED_PROFILE=fake export must be loud, wherever the store lives);
      - hybrid default is available but zero live rows carry embeddings.
    Advisory only: warn/info statuses, never `fail`.
    """
    import tempfile as _tempfile

    try:
        import embeddings  # type: ignore
    except Exception as exc:
        return _check(
            "embeddings_health", "skip",
            f"embeddings module unavailable ({type(exc).__name__})")

    st = embeddings.availability_status()
    details: dict = {
        "active_profile": st.get("profile"),
        "dim": st.get("dim"),
        "checksum_ok": st.get("checksum_ok"),
    }
    if st.get("note"):
        details["note"] = st["note"]

    total_live = with_emb = without_emb = live_dim = declared_dim = None
    meta_profile = None
    store_ok = False
    if resolved_store.exists():
        try:
            uri = resolved_store.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True)
            try:
                row = conn.execute(
                    "SELECT COUNT(*), "
                    "SUM(embedding IS NOT NULL), SUM(embedding IS NULL) "
                    "FROM memory WHERE superseded_at IS NULL"
                ).fetchone()
                if row is not None:
                    total_live = int(row[0] or 0)
                    with_emb = int(row[1] or 0)
                    without_emb = int(row[2] or 0)
                row2 = conn.execute(
                    "SELECT length(embedding)/4 FROM memory "
                    "WHERE embedding IS NOT NULL LIMIT 1"
                ).fetchone()
                live_dim = int(row2[0]) if row2 and row2[0] else None
                try:
                    row3 = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' "
                        "AND name='memory_vec'"
                    ).fetchone()
                    if row3 and row3[0]:
                        m = re.search(r"float\[(\d+)\]", row3[0],
                                      re.IGNORECASE)
                        declared_dim = int(m.group(1)) if m else None
                except sqlite3.Error:
                    pass
                try:
                    row4 = conn.execute(
                        "SELECT value FROM meta WHERE key='embedding_profile'"
                    ).fetchone()
                    meta_profile = row4[0] if row4 else None
                except sqlite3.Error:
                    pass
            finally:
                conn.close()
            store_ok = True
        except sqlite3.Error:
            store_ok = False

    details.update(
        store_read=store_ok,
        live_memories=total_live,
        rows_with_embedding=with_emb,
        rows_without_embedding=without_emb,
        stored_dim=live_dim,
        declared_vec_dim=declared_dim,
        last_rebuilt_profile=meta_profile,
        matches_store=(
            None
            if live_dim is None or st.get("dim") is None
            else st["dim"] == live_dim
        ),
    )

    shipped = []
    try:
        import embed_profiles as ep

        models_dir = Path(str(st.get("models_dir", "")))
        for name, entry in sorted(ep.PROFILES.items()):
            if name == "fake":
                installed = True
            else:
                mf = entry.get("model_file", "")
                installed = bool(mf) and (models_dir / mf).is_file()
            shipped.append({
                "name": name,
                "hf_id": entry.get("hf_id", ""),
                "dim": entry.get("dim"),
                "installed": installed,
            })
    except Exception:
        shipped = []
    details["shipped_profiles"] = shipped

    active = st.get("profile")
    warnings = _embedding_health_warnings(
        active_profile=active,
        embeddings_available=bool(st.get("available")),
        matches_store=details["matches_store"],
        total_live=total_live,
        with_emb=with_emb,
        store_is_temp=_store_path_is_temp(resolved_store),
    )
    if details["matches_store"] is False:
        status = "warn"
        summary = (
            f"profile '{active}' dim {st.get('dim')} does NOT match the "
            f"store's committed {live_dim}-dim data; commands that embed are "
            "refused until `reembed --all` converts the store."
        )
    elif warnings:
        status = "warn"
        summary = "; ".join(warnings)
    elif not resolved_store.exists():
        status = "info"
        summary = "no store yet at the resolved path - nothing to report."
    else:
        status = "pass"
        parts = [
            f"profile '{active}'",
            f"stored {live_dim}-dim" if live_dim
            else "no committed vectors yet",
            (f"{with_emb}/{total_live} rows embedded"
             if total_live is not None else ""),
        ]
        summary = ", ".join(x for x in parts if x)
        if active == "fake":
            summary += " (test profile on a temporary store)"
    return _check("embeddings_health", status, summary, **details)


def build_report(project: Path, repo_root: Path) -> dict:
    resolved_store = host.resolve_store_path()
    checks: list[dict] = []
    checks.append(_check_store_resolution(repo_root, resolved_store))
    checks.append(_check_local_path(resolved_store))
    checks.append(_check_python())
    checks.append(_check_sqlite_fts5())
    checks.extend(_check_node_and_bash())
    access_check = _check_store_access(resolved_store)
    checks.append(access_check)
    checks.append(_check_schema(resolved_store, access_check))
    checks.append(_check_v9_columns(resolved_store))
    checks.append(_check_entity_tables(resolved_store))
    # v11 (issue #61, 6.1): link surface + trust range probe.
    checks.append(_check_link_tables(resolved_store))
    checks.append(_check_claude_native_memory(Path.home()))
    checks.append(_check_session_retention(Path.home()))
    checks.extend(_check_codex_memory_and_trust(Path.home(), project, repo_root))
    namespace_check = _check_namespace(project)
    checks.append(namespace_check)
    checks.append(_check_surfaces(repo_root))
    checks.append(_check_tier0_size(project))
    checks.append(_check_embeddings())
    # Issue #63, 8.4: store-side embedding health (profile/dim/coverage).
    checks.append(_check_embeddings_health(resolved_store))
    # Issue #58, 3.7: hybrid-default (3.3) + vec-ns-overfetch (3.1).
    # PRR-031: scope the vec count to the resolved namespace when available.
    checks.append(_check_hybrid_default())
    ns_for_vec = (
        namespace_check["details"].get("namespace")
        if namespace_check["status"] == "pass" else None
    )
    checks.append(_check_vec_ns_overfetch(resolved_store, namespace=ns_for_vec))
    # Operational health (backup/consolidation cadence) — read-only, best-effort
    # skip if the store is absent/unreadable (#37 L23).
    checks.extend(_check_operational_health(resolved_store))
    # Pending namespace-migration preview (#39 E8) — read-only dry-run of the
    # self-heal retry, so stranded namespaces are visible before they're fixed.
    checks.append(_check_ns_migration(resolved_store))

    # "info" is a supported non-ok-flipping status (hybrid-default's
    # embeddings-unavailable branch: lexical fallback works — PRR-001R fix;
    # previously the missing key raised KeyError in the aggregation below).
    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0, "info": 0}
    for check in checks:
        counts[check["status"]] += 1

    recommendations = _recommendations(checks)
    namespace = namespace_check["details"].get("namespace") if namespace_check["status"] == "pass" else None
    return {
        "doctor_version": 1,
        "ok": counts["fail"] == 0,
        "summary": counts,
        "project": _display_path(project),
        "repo_root": _display_path(repo_root),
        "resolved_store": _display_path(resolved_store),
        "namespace": namespace,
        "checks": checks,
        "recommendations": recommendations,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only install diagnostics for zmem")
    ap.add_argument("--project", default=os.getcwd(), help="project path used for canonical namespace resolution")
    ap.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parents[3]),
        help="repo root used for plugin/skill surface checks",
    )
    ap.add_argument(
        "--format",
        choices=("human", "json", "both"),
        default="human",
        help="output format",
    )
    args = ap.parse_args(argv)

    report = build_report(Path(args.project).expanduser(), Path(args.repo_root).expanduser())
    human = _render_human(report)
    payload = json.dumps(report, indent=2, sort_keys=True)

    if args.format == "human":
        print(human)
    elif args.format == "json":
        print(payload)
    else:
        print(human)
        print("")
        print(payload)

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
