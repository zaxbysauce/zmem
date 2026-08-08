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


CURRENT_SCHEMA_VERSION = 5
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


def _check_python() -> dict:
    version = sys.version.split()[0]
    if sys.version_info < (3, 8):
        return _check(
            "python",
            "fail",
            f"Python {version} is too old; zmem requires Python 3.8+.",
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
    status = "pass" if st["available"] else "warn"
    if st["available"]:
        summary = "Embeddings available; semantic recall/dedup active."
    else:
        summary = (
            f"Embeddings unavailable (reason={st['reason']}); semantic "
            "recall/dedup disabled — degraded FTS5/lexical mode is supported."
        )
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
            "No store schema found yet; the first writable run will initialize v5.",
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
            "Run the first writable zmem command only after the shared store path is correct; that first run may need to initialize or migrate schema v5."
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
    checks.append(_check_claude_native_memory(Path.home()))
    checks.extend(_check_codex_memory_and_trust(Path.home(), project, repo_root))
    namespace_check = _check_namespace(project)
    checks.append(namespace_check)
    checks.append(_check_surfaces(repo_root))
    checks.append(_check_embeddings())

    counts = {"pass": 0, "warn": 0, "fail": 0, "skip": 0}
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
