# ZMem — Multi-Tier Memory for ZCode

A local-first memory system for [ZCode](https://z.ai) that gives your agent
persistent, cross-session knowledge: always-on core memory, FTS5-backed lesson
recall, and reflection-on-failure. Zero cloud dependency.

## What it does

- **Tier 0 — Core:** `core.md` (user-level) + `<repo>/AGENTS.md` (project-level)
  are auto-injected into context at every session start. Stable rules and
  conventions, always present.
- **Tier 2 — Semantic:** a SQLite store (FTS5 + tombstone supersession) for
  cross-task lessons, facts, conventions, and preferences. Keyword recall with
  a confidence floor — high-precision-first (retrieved-wrong hurts more than
  retrieved-nothing).
- **Reflection loop:** on session stop, if tool failures were detected and no
  lesson was captured, you're prompted to capture a grounded lesson. Non-blocking.
- **Relevance-based recall:** when you submit a prompt, matching memories are
  injected as context *before* the agent starts working — not just the 3 most
  recent at session start.

Signal tiers set how trustworthy a memory is: `test/compile/lint` (high, grounded
in deterministic verification) > `reviewer/user` (medium) > `none` (low, below the
retrieval floor by default). This follows the finding that intrinsic self-correction
(lessons from the agent's own opinion, ungrounded) degrades accuracy.

## Requirements

- ZCode (the plugin registers hooks + a skill via the native plugin system)
- Python 3.8+ with sqlite3 + FTS5 (standard in CPython; verify with
  `python -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"` )
- Git Bash / Cygwin on Windows (for the hook scripts); or any POSIX shell on macOS/Linux

## Install

### From this GitHub repo (recommended)

1. In ZCode: **Settings → Plugin Management → Discover → `+`**
2. Paste this repository's GitHub URL.
3. Install the **zmem** plugin. It enables by default.
4. Restart your session (or start a new one). On first start, the hook seeds a
   default `core.md` in the plugin data dir from the template — edit it to taste.

### Local directory (for testing / air-gapped)

1. Clone or copy this repo to a stable path.
2. In ZCode: **Settings → Plugin Management → Discover → `+`** → point at the
   local directory.
3. Install + enable.

## Project-level memory

ZMem injects `<repo>/AGENTS.md` if present. Copy
[`templates/AGENTS.md.template`](templates/AGENTS.md.template) into each repo
where you want project-scoped conventions, and fill it in (build commands, gotchas,
standards). This file is repo-owned, not plugin-owned.

## Usage

The SessionStart hook injects the absolute path to `store.py` into context each
session — use that exact path. Common operations:

```bash
# Recall relevant lessons before a task (scoped to current project)
python <store.py> recall --query "FTS5 sqlite" --namespace "project:myrepo"

# Capture a lesson (signal=test means a test verified it)
python <store.py> add \
  --namespace "project:myrepo" --type lesson \
  --content "This repo uses pytest, not unittest." \
  --tags "python,testing" --signal test

# See what's stored
python <store.py> list --namespace "project:myrepo"
python <store.py> stats

# Tombstone a stale lesson (keeps history)
python <store.py> supersede --id <uuid> --reason "no longer applies"
```

The full command reference is in the `memory` skill (type `/memory` in ZCode).

## Where data lives

- **Store + core.md:** `${ZCODE_PLUGIN_DATA}/` (per-plugin writable dir, managed
  by ZCode). On Windows typically
  `C:\Users\<you>\.zcode\cli\plugins\data\zmem@<marketplace>\`.
- **Episodic memory (read-only):** `~/.zcode/cli/db/db.sqlite` — ZCode's own
  session/tool-call database. ZMem reads this for failure detection; never writes it.

## Security notes

- The store is a **local plaintext SQLite file**. Do not store secrets, credentials,
  or PII in it. The write-time secret scanner is an advisory heuristic (regex +
  entropy), **not a guarantee**.
- All memory stays on your machine. No telemetry, no cloud calls.

## Windows troubleshooting (WSL bash conflict)

The hook commands use bare `bash`, which on Windows may resolve to WSL's
`C:\Windows\System32\bash.exe` instead of Git Bash. WSL bash cannot run these
scripts (no `cygpath`, incompatible path resolution). Symptoms: hooks silently
fail (`hook.run.failed` in `~/.zcode/cli/log/`), no memory injection or recall.

**Fix:** edit `hooks/hooks.json` and replace each `bash` with the full Git Bash
path, e.g.:

```json
"command": "\"C:\\Program Files\\Git\\usr\\bin\\bash.exe\" \"${ZCODE_PLUGIN_ROOT}/hooks/zmem-session-start.sh\""
```

Verify Git Bash exists at that path (`ls "C:\Program Files\Git\usr\bin\bash.exe"`).
If your Git installation is elsewhere, adjust accordingly. This is a Windows-only
override; Linux/macOS users need no changes.

**Note:** this edit lives in the plugin cache
(`~/.zcode/cli/plugins/cache/<marketplace>/zmem/<version>/hooks/hooks.json`) and
will be reverted on plugin reinstall. Re-apply after each update, or upvote
[the upstream issue](https://github.com/zaxbysauce/zmem/issues) for a
runtime-detection fix.

## License

MIT
