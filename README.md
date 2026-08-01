# ZMem — Multi-Tier Memory for ZCode + Claude Code

A local-first memory system that gives your agent persistent, cross-session
knowledge — shared box-wide across [ZCode](https://z.ai), Claude Code, and
Codex workflows that can reach the same physical store: always-on core memory,
FTS5-backed lesson recall, and reflection-on-failure. Zero cloud dependency.

## Box-wide shared memory

ZMem is designed to be **one memory brain for the whole box**, not a separate
copy per tool. ZCode and Claude Code point at the same store —
`~/.zmem/store.sqlite` + `~/.zmem/core.md` by default — and Codex can use that
same store when the path is inside its writable roots or when a local broker
owns the store on Codex's behalf. The host adapter (`hooks/zmem-launch.js`)
detects which tool is running and sets a canonical env (`ZMEM_HOST`,
`ZMEM_DATA`, `ZMEM_TIER0`, etc.) so the hook scripts and `store.py` never need
to branch on host. Override the shared store location with the `ZMEM_DATA` env
var (or the CC plugin's `storeDirectory` userConfig setting) if `~/.zmem`
isn't where you want it.

**Phase-1 limitation:** keep one canonical physical store path on one machine.
Do not let different hosts silently fan out to different physical stores.

## What it does

- **Tier 0 — Core:** `core.md` (user-level) is auto-injected into context at
  every session start on both hosts. `<repo>/AGENTS.md` (project-level) is also
  injected on ZCode; on Claude Code, project-level Tier 0 is CC's own
  `CLAUDE.md` instead (see "Project-level memory" below), so `AGENTS.md` is
  skipped there to avoid injecting the same tier twice.
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

- ZCode and/or Claude Code (the plugin registers hooks + a skill via each
  tool's native plugin system)
- Codex, if you want Codex to share the same store, with either:
  - the shared store path added as a writable root, or
  - a local broker process that owns the store and exposes read/write actions
- Python 3.8+ with sqlite3 + FTS5 (standard in CPython; verify with
  `python -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"` )
- Git Bash / Cygwin on Windows (for the hook scripts); or any POSIX shell on macOS/Linux
- Node.js (the cross-platform hook launcher; both tools already require it)

## Install

### Preflight

Before cutover, run the read-only doctor:

```bash
python skills/memory/scripts/doctor.py --project <repo> --format human
```

It checks the resolved store path, split-brain env/config risks, native-memory
conflicts, schema compatibility, Windows shell requirements, canonical
namespace derivation, and whether the expected host surfaces are present.

### ZCode — from this GitHub repo (recommended)

1. In ZCode: **Settings → Plugin Management → Discover → `+`**
2. Paste this repository's GitHub URL.
3. Install the **zmem** plugin. It enables by default.
4. Restart your session (or start a new one). On first start, the hook seeds a
   default `core.md` in the shared store from the template — edit it to taste.

### Claude Code — from this GitHub repo

1. Add this repository as a plugin marketplace/source and install the **zmem**
   plugin (see Claude Code's plugin docs for the current install flow).
2. **Turn off Claude Code's native memory** by adding to your own
   `~/.claude/settings.json`:
   ```json
   { "autoMemoryEnabled": false }
   ```
   (or set env `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`). **A plugin cannot set this
   for you** — Claude Code only honors a plugin-bundled `settings.json`'s
   `agent`/`subagentStatusLine` keys, not `autoMemoryEnabled` — so this is a
   one-time manual step you do yourself. Without it, CC's native memory and
   ZMem both inject context every session: genuine double-memory, not a
   supported mode. If you forget, ZMem's SessionStart hook shows a one-time
   nudge reminding you.
3. Restart your session. `<repo>/CLAUDE.md` (CC's own always-on project memory)
   is untouched by ZMem — it keeps working exactly as it always has.

### Codex — shared-store skill / broker mode

Codex does not need a second physical store. Point it at the same canonical
store directory the plugin hosts use.

1. Disable Codex native memories in your own `~/.codex/config.toml` before
   cutover. The current doctor inspects these keys read-only:
   ```toml
   [features]
   memories = false

   [memories]
   use_memories = false
   generate_memories = false
   ```
   Do this yourself; zmem should never auto-edit Codex config.
2. Make the canonical shared store path writable from Codex. If Codex cannot
   write that path directly, add it as a writable root or use a small local
   broker that owns the store.
3. Use `ZMEM_TIER0=native` for Codex cutover: keep Codex project instructions
   as the host's native Tier 0 and let zmem own the shared Tier 2 store.
4. If you add a repo-local Codex hook surface later, trust the project and
   reapprove hooks after the cutover change. The current repo's Claude/ZCode
   plugin surfaces are first-class now; repo-local Codex adapter files may lag
   behind and are treated as optional by `doctor.py`.

### Local directory (for testing / air-gapped)

1. Clone or copy this repo to a stable path.
2. In ZCode or Claude Code: point the plugin discovery/marketplace flow at the
   local directory instead of the GitHub URL.
3. Install + enable.

## Project-level memory

On ZCode, ZMem injects `<repo>/AGENTS.md` if present — this is project-level
Tier 0. On Claude Code, `AGENTS.md` is **not** injected by ZMem; CC already has
its own always-on project-level memory (`CLAUDE.md`), and injecting both would
double up the same tier. Copy
[`templates/AGENTS.md.template`](templates/AGENTS.md.template) into each repo
where you want project-scoped conventions (ZCode only), and fill it in
(build commands, gotchas, standards). This file is repo-owned, not plugin-owned.

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

- **Store + core.md (box-wide default):** `~/.zmem/` — one shared, tool-neutral
  directory holding `store.sqlite` + `core.md`, read and written by both ZCode
  and Claude Code (and their subagents), and by Codex when that path is
  explicitly reachable. This is the box-wide model: a lesson captured in one
  host is recallable in the others. Override with the
  `ZMEM_DATA` env var (or the CC plugin's `storeDirectory` userConfig option)
  if you want it elsewhere.
- **Legacy per-plugin data dirs** (`${ZCODE_PLUGIN_DATA}` /
  `${CLAUDE_PLUGIN_DATA}`) still work as a fallback if `ZMEM_DATA` isn't set
  and the plugin runner injects one, but these are per-tool and deleted on
  uninstall — not the box-wide store.
- **Bare/manual-install default changed:** a manual invocation of `store.py`
  with none of the above set now resolves to `~/.zmem` (previously
  `~/.zcode/memory`). If you have an existing store at the old path, run
  `skills/memory/scripts/import-store.py` to copy it (checkpointed, read-only
  on the source) into `~/.zmem` — this is the supported migration path, not a
  manual file copy.
- **Legacy project namespaces:** before the first v5 open, set
  `ZMEM_NS_MIGRATION_MAP` to a JSON object from each old namespace to a live
  checkout, for example `{"project:oldname":"C:/src/owner/repo"}`. ZMem uses
  each checkout's current remote to derive the canonical key. Missing entries
  are retried on later opens but remain under the old key until supplied.
- **Episodic memory (read-only):** `~/.zcode/cli/db/db.sqlite` — ZCode's own
  session/tool-call database. ZMem reads this for failure detection; never writes it.
  On Claude Code, failure detection instead scans the session transcript
  (`transcript_path`) — no separate db.sqlite exists there.

## Cloud sessions

The store is local-first, so a session with no filesystem access to this box
— a Claude Code cloud session, Claude Code Remote (CCR), a GitHub Action —
can't reach it directly. Three supported tiers cover that, from a read-only
committed snapshot up to a full sync-repo read/write loop: see
[`docs/CLOUD.md`](docs/CLOUD.md).

## Security notes

- The store is a **local plaintext SQLite file**. Do not store secrets, credentials,
  or PII in it. The write-time secret scanner is an advisory heuristic (regex +
  entropy), **not a guarantee**.
- All memory stays on your machine. No telemetry, no cloud calls.
- Remote harvests, sync outboxes, and any future broker inputs are **untrusted
  until reviewed**. Only promote or ingest reviewed content into the shared
  store.
- **Tier 3 sync changes that.** If you wire up a private sync repo
  (`docs/CLOUD.md`), write access to that repo is effectively write access to
  the *content* of your store — including `user:global` rows, which are
  injected into every future session on this box. Read the "Trust model"
  section of [`docs/CLOUD.md`](docs/CLOUD.md) before setting it up.

## Operations notes

- Keep **one canonical physical store path** per machine. If two hosts resolve
  to different physical stores, that is a cutover failure, not a supported mode.
- Backups and restores are first-class maintenance commands:
  `python <store.py> backup` and `python <store.py> restore --from <snapshot> --force`.
  Run restores when no session is actively writing.
- Review skill promotion before writing it. `promote --confirm` writes into the
  host skill surfaces and should stay a reviewed step, not an automatic one.

## Cross-platform hook execution

Hook commands are launched via `node hooks/zmem-launch.js`, not `bash` directly.
This avoids a Windows-specific issue where bare `bash` resolves to WSL's
`bash.exe` instead of Git Bash (WSL bash cannot run these scripts). Node.js is
guaranteed to be on the PATH (ZCode is a Node app), so it resolves reliably on
all platforms. The launcher auto-detects the correct bash and execs the hook
script under it — no manual configuration needed.

If the auto-detection fails (non-standard Git install), set the
`ZMEM_BASH_PATH` environment variable to your bash executable path.

## License

MIT
