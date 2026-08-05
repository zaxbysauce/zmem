# ZMem — Multi-Tier Memory for ZCode + Claude Code + Codex + Hermes

A local-first memory system that gives your agent persistent, cross-session
knowledge — shared box-wide across [ZCode](https://z.ai), Claude Code, Codex,
and [Hermes Agent](https://github.com/NousResearch/hermes-agent) workflows
that can reach the same physical store: always-on core memory,
FTS5-backed lesson recall, and reflection-on-failure. Zero cloud dependency.

## Box-wide shared memory

ZMem is designed to be **one memory brain for the whole box**, not a separate
copy per tool. ZCode and Claude Code point at the same store —
`~/.zmem/store.sqlite` + `~/.zmem/core.md` by default — Codex can use that
same store when the path is inside its writable roots or when a local broker
owns the store on Codex's behalf, and Hermes reads/writes the same store via
its memory-provider adapter (`hermes-plugin/`). A Hermes agent on a **different
machine** reaches the same store over the LAN through the bundled MCP server.
The host adapter (`hooks/zmem-launch.js`)
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
- Hermes Agent (optional), if you want Hermes to share the same store:
  - local Hermes: Python 3.11+ (Hermes already requires it); the
    `hermes-plugin/` adapter auto-detects `store.py` relative to this repo
  - remote Hermes (different machine): the MCP server needs
    `pip install -r hermes-plugin/server/requirements.txt` (`mcp` + `uvicorn`)
    on the store-host box; the remote box needs only the `mcp_servers:` config
    entry (Hermes bundles the MCP client SDK)
- Python 3.8+ with sqlite3 + FTS5 (standard in CPython; verify with
  `python -c "import sqlite3; sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')"` )
- Git Bash / Cygwin on Windows (for the ZCode/CC hook scripts); or any POSIX shell on macOS/Linux.
  The Hermes hooks are pure stdlib Python — no shell dependency.
- Node.js (the ZCode/CC cross-platform hook launcher; both tools already require it)

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

### Hermes Agent — local (memory provider + reflection hooks)

Hermes reads/writes the same canonical store via a `MemoryProvider` adapter.
Unlike ZCode/CC/Codex (which use the host adapter + bash hooks), Hermes has
its own first-class memory-provider ABC and a shell-hook system, so the
adapter is native Python: passive recall before each turn, explicit memory
tools (`zmem_add` / `zmem_search` / `zmem_supersede`), Tier-0 `core.md`
injection, and an optional reflection loop.

Hermes' plugin discovery scans `~/.hermes/plugins/memory/<name>/`, so install
the adapter there. The adapter auto-detects `store.py` relative to its own
location **when installed via symlink or junction** (it follows the link back
into this repo). If you copy instead (the Windows `cp -r` path), the copy
isn't inside the repo, so auto-detection fails silently and the provider stays
inactive — **copy users must set `ZMEM_HOME`** to this repo's checkout path
(see step 5).

1. Clone this repo to a stable path (if you haven't already for ZCode/CC).
2. Symlink or copy the adapter into Hermes' plugin dir:
   ```bash
   # symlink (recommended — stays in sync with this repo, auto-detect works):
   ln -s /path/to/zmem/hermes-plugin ~/.hermes/plugins/memory/zmem
   # or copy (auto-detect BREAKS — you MUST set ZMEM_HOME in step 5):
   cp -r /path/to/zmem/hermes-plugin ~/.hermes/plugins/memory/zmem
   ```
   On Windows (no symlinks without admin): a directory junction
   (`mklink /J C:\Users\you\.hermes\plugins\memory\zmem C:\code\zmem\hermes-plugin`)
   preserves auto-detection; a plain copy does not.
3. Enable the provider in `~/.hermes/config.yaml`:
   ```yaml
   memory:
     provider: zmem
   ```
4. (Optional, recommended) Enable the reflection loop. Find your absolute
   plugin path first:
   ```bash
   python -c "from hermes_constants import get_hermes_home; print(get_hermes_home())"
   ```
   Then add to `~/.hermes/config.yaml` (replace `<hh>` with that path):
   ```yaml
   hooks:
     post_tool_call:
       - command: "python <hh>/plugins/memory/zmem/hooks/zmem-hermes-convention.py"
         matcher: "^(terminal|file_edit|read_file|search_files|write_file|delegate_task)$"
         timeout: 15
     pre_llm_call:
       - command: "python <hh>/plugins/memory/zmem/hooks/zmem-hermes-reflect.py"
         timeout: 15
     pre_verify:
       - command: "python <hh>/plugins/memory/zmem/hooks/zmem-hermes-verify.py"
         timeout: 15

   # Required for the reflection hooks to register without a TTY prompt:
   hooks_auto_accept: true
   ```
   > **`hooks_auto_accept: true` is required.** Hermes gates shell hooks behind
   > a per-`(event, command)` consent prompt on first use. Without this flag
   > (or `HERMES_ACCEPT_HOOKS=1` / `--accept-hooks`), each hook prompts at the
   > TTY once and is **skipped silently** in non-TTY contexts (gateway, cron).
   >
   > The convention/failure hooks record signal on `post_tool_call` (an
   > observational event in Hermes — its results are discarded) and the
   > `pre_llm_call` reflect hook delivers the nudges on the next turn. This
   > split respects Hermes' hook-consumption contract.
5. **If you copied (not symlinked/junctioned) in step 2**, set `ZMEM_HOME` so
   the provider can locate `store.py`. Add to `~/.hermes/.env` (or your
   shell environment):
   ```ini
   ZMEM_HOME=/path/to/zmem
   ```
   Symlink/junction installs skip this — auto-detection handles it.

### Hermes Agent — remote (MCP server, different machine on the LAN)

A Hermes agent running on a **different machine** (e.g. a gateway box serving
Telegram/Discord) cannot read this box's `~/.zmem/store.sqlite` directly —
zmem deliberately refuses network-mounted paths (SQLite WAL corruption risk).
Instead, run the **zmem MCP server** on this box (the store host) and point
the remote Hermes at it. The remote gets the same `recall` / `add` / `search`
/ `supersede` / `recent` tools over the network.

**On the store-host box** (this machine, with `~/.zmem/store.sqlite`):

```bash
pip install -r hermes-plugin/server/requirements.txt   # mcp==1.28.1, uvicorn

# Generate a strong token:
python -c "import secrets; print(secrets.token_hex(32))"

# Start the server (auto-detects your LAN IP; refuses 0.0.0.0 by default):
ZMEM_MCP_TOKEN=<the-generated-secret> \
  python hermes-plugin/server/mcp_server.py --port 8765
```

**On the remote Hermes box**, add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  zmem:
    url: "http://192.168.1.X:8765/mcp"
    headers:
      Authorization: "Bearer <the-same-secret>"
    timeout: 30
```

The remote Hermes auto-discovers `mcp__zmem__recall`, `mcp__zmem__add`, etc.
No zmem checkout or plugin install needed on the remote box.

> **Security:** the Bearer token is the only authentication — generate a long
> random secret. Over plain HTTP on a trusted LAN the token travels in
> cleartext; for untrusted networks use TLS (`--tls-keyfile` / `--tls-certfile`,
> which must be provided together, or a reverse proxy). The server refuses to
> start without a token and refuses wildcard binds unless
> `ZMEM_MCP_ALLOW_INSECURE_BIND=1` is set. For Windows persistence, register
> the server as a Scheduled Task (`New-ScheduledTaskAction -Execute python.exe
> -Argument "hermes-plugin/server/mcp_server.py --port 8765"`) or wrap it with
> [nssm](https://nssm.cc/); set `ZMEM_MCP_TOKEN` and `ZMEM_HOME` as system env
> vars.
>
> **Limitation:** the remote Hermes gets explicit `mcp__zmem__*` tools (the
> model calls them when it needs a memory), not automatic passive recall
> before each turn. That's a planned v2.

#### Hermes adapter env vars

| Var | Purpose | Default |
|-----|---------|---------|
| `ZMEM_HOME` | Path to the zmem checkout (where `store.py` lives). **Required for copy installs;** optional for symlink/junction. | — |
| `ZMEM_DATA` | Override the store data directory (holds `store.sqlite` + `core.md`). | `~/.zmem` |
| `ZMEM_STORE` | Override the store SQLite path directly (wins over `ZMEM_DATA`). | — |
| `ZMEM_NAMESPACE` | Force a namespace for the local provider (e.g. `project:myrepo`). Default derives from gateway `user_id`. | derived |
| `ZMEM_CONVENTION_INTERVAL` | Fire the convention nudge every N successful tool calls. | `10` |
| `ZMEM_MCP_TOKEN` | Bearer token for the MCP server. **Required** to start the server. | — |
| `ZMEM_MCP_TOKEN_FILE` | Path to a file containing the token (alternative to `ZMEM_MCP_TOKEN`). | — |
| `ZMEM_MCP_ALLOW_INSECURE_BIND` | Set to `1` to allow `0.0.0.0` / `::` bind. | unset |

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
