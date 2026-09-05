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
  retrieved-nothing). Optional ONNX embeddings (all-MiniLM-L6-v2) add semantic
  dedup-on-write, hybrid vector recall, and embedding-seeded consolidation;
  without the embedding runtime, recall degrades gracefully to FTS5 (see
  "Embeddings" under Operations notes).
- **Reflection loop:** on session stop, if tool failures were detected and no
  lesson was captured, you're prompted to capture a grounded lesson. Non-blocking.
- **Relevance-based recall:** when you submit a prompt, matching memories are
  injected as context *before* the agent starts working — not just the 3 most
  recent at session start.
- **Live correction capture:** a `capture-correction` hook registered under
  `UserPromptSubmit` (Claude Code, ZCode, and Codex) silently queues mid-session
  user corrections ("no, use X", "don't refactor unrelated code",
  "remember: ...") to a namespace-scoped sidecar file — it never writes the store.
  The next session shows a pending count, and the closeout skill reviews the
  queue and writes only what clears the bar as `--signal user` rows.

Signal tiers set how trustworthy a memory is: `test/compile/lint` (high, grounded
in deterministic verification) > `reviewer/user` (medium) > `none` (low, below the
retrieval floor by default). This follows the finding that intrinsic self-correction
(lessons from the agent's own opinion, ungrounded) degrades accuracy.

### Retrieval debugger, lineage unfold, and honest eval (issue #82)

- `recall --explain [--target ID|fragment] [--json]` re-runs the real pipeline
  with ZERO writes and explains every verdict — found, below_limit, below_floor,
  omitted_injection, omitted_untrusted_web, namespace, superseded,
  not_valid_at_as_of, vec_lane_miss, not_in_pool, not_in_db — using zmem's own
  gates as first-class reasons.
- On explicit recall only, a change-intent query ("what changed about X") can
  append the tombstoned predecessors of a hit as budgeted `[PREVIOUSLY]` rows
  (never counted against the limit, never popularity-bumped). Hooks and other
  passive surfaces never unfold; `--no-unfold` opts out.
- The offline eval covers retraction, polarity, and change-intent buckets
  alongside the original six, and `scripts/eval_self_corpus.py` measures recall
  against a snapshot of your own corpus (home store refused by design).
- Every public capability claim is audited in `docs/CLAIMS-AUDIT.md`; scores
  never gate CI.

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
    on the store-host box; for semantic recall/dedup on that host, also install
    `pip install -r hermes-plugin/server/requirements-embeddings.txt`
    (optional — without it the server logs a startup warning and `add` stores
    rows without embeddings); the remote box needs only the `mcp_servers:`
    config entry (Hermes bundles the MCP client SDK)
- Python 3.11+ with sqlite3 + FTS5 (standard in CPython; verify with
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
namespace derivation, whether the expected host surfaces are present, the
size of the always-injected Tier-0 files (`core.md`, project `AGENTS.md`) so
an overgrown file cannot silently eat the context budget, and the Claude Code
transcript retention window (`cleanupPeriodDays`) that bounds how far back
the transcript-mining commands can see.

### Post-install canary

After any install or refresh, verify the delivery lane end to end (issue #108):

```bash
python scripts/host_canary.py --host claude --self-test
python scripts/host_canary.py --host codex --self-test   # also: zcode, hermes
```

The canary seeds one recognizable row into an **isolated** scratch store (your
real store is never touched — ambient `ZMEM_STORE`/`ZMEM_DATA` and the
plugin-data vars are stripped, so they cannot redirect it), drives the host's
SessionStart hook chain, and asserts a fresh `zmem-hook status=... reason=...`
decision line whose `ids=[...]` carries the seeded row, plus the served-tree
drift status from #107. Without `--self-test` it runs a real minimal session
of the host binary instead — a host binary that is absent yields
`verdict=skip reason=host-binary-absent` (exit 0); a hook that never fires
exits 2 (`reason=hook-not-fired`); a hook that fires but injects nothing
exits 3 (`reason=no-row-id`). The `drift=` field is informational only —
`drift=unknown` (including on pre-0.17.0 trees with no manifest) never fails
the run. Live mode ships one-shot session forms for `claude` and `codex`
today; `zcode`/`hermes` live runs exit 5
(`reason=host-session-unsupported`) until their session forms land in #96 —
their `--self-test` lanes work. On Codex the canary also reads
`.codex-plugin/plugin.json` before driving the chain: a manifest whose
`hooks` path lost the required `./` prefix fails
`reason=codex-manifest-contract` (exit 2) instead of green-lighting hooks
codex-cli >= 0.153.0 would silently ignore. On Codex, an install whose hook
trust has not been granted (interactive `/hooks` browser after a refresh)
correctly reports `hook-not-fired` until you re-trust — that is the canary
doing its job, not a canary bug. Hermes' `--self-test` exercises the shared hook machinery under
hermes identity; hermes' adapter-based delivery lane is tracked in #122.

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
tools (`zmem_add` / `zmem_search` / `zmem_update` / `zmem_invalidate` /
`zmem_supersede` / `zmem_session_start` / `zmem_session_end`), Tier-0 `core.md`
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
   (`mklink /J %USERPROFILE%\.hermes\plugins\memory\zmem C:\code\zmem\hermes-plugin`)
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
pip install -r hermes-plugin/server/requirements.txt   # mcp>=1.28.1,<2.0.0, uvicorn

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
> **Limitation (shipped):** the remote Hermes gets explicit `mcp__zmem__*`
> tools AND the passive prefetch — with Hindsight still enabled as
> `memory.provider`, the `pre_llm_call` shell hook (issue #71 A) fetches a
> `--no-bump` `session_start` prefetch over MCP before the model sees the
> turn. Kill the MCP server or the token and the turn proceeds without
> injection (fail-open).

**Remote passive prefetch (issue #71 A)** — on the remote Hermes box:

1. Install the `mcp` client library (REQUIRED — without it prefetch fails
   open silently): `pip install -r hermes-plugin/server/requirements.txt`
   (same file the store host uses for the server).
2. Copy the hook and EVERYTHING it resolves at runtime. Two supported
   layouts:
   - **Preferred:** copy the full checkout subset — `hermes-plugin/`
     (including `server/mcp_client.py`, which the prefetch subprocess
     invokes) and `skills/memory/scripts/` in full (the query-context lane
     additionally imports `ops_tokens` + `storelib`, and correction capture
     imports `corrections`). Keep the relative layout
     (`hermes-plugin/hooks/` next to `skills/memory/scripts/`).
   - **Minimal:** copy only `hermes-plugin/hooks/` +
     `hermes-plugin/server/mcp_client.py` +
     `skills/memory/scripts/{host.py,correction_queue.py,corrections.py}` —
     passive prefetch and correction capture work; the local query-context
     recall lane silently degrades to no-op (fail-open) without
     `ops_tokens`/`storelib`.
   Copy installs that break the relative layout must set `ZMEM_HOME`.
3. Wire the hook in `~/.hermes/config.yaml` (same `hooks:` block as the
   local install above — the `pre_llm_call` entry is the prefetcher) and set
   the remote env:
   ```ini
   ZMEM_MCP_URL=http://192.168.1.X:8765/mcp
   ZMEM_MCP_TOKEN=<the-same-secret>
   # optional: ZMEM_MCP_NAMESPACE=project:github.com/owner/repo  (else the
   # server default user:global), ZMEM_MCP_TIMEOUT=8
   ```
   **Scoped-token deployments MUST set `ZMEM_MCP_NAMESPACE`** (issue #71
   review): the server rejects a namespace the token is not scoped for, and
   an unset namespace resolves to `user:global` — name a namespace the token
   allows or every prefetch fails open.
4. `hermes mcp test zmem` must still discover the tools; if a running
   gateway must be restarted to load a config change, that is a Hermes
   runtime behavior — one restart, documented here. Caveat: upstream
   `pre_llm_call` is documented to fire in CLI and Gateway modes, but
   gateway-mode firing is being verified upstream (zmem issue #96); zmem's
   local delivery rides the same event, so a gateway-mode gap would affect
   both equally.

Remote corrections (issue #71 D): the same hook also captures user
corrections ("No, use X") into the remote box's own sidecar queue
(`~/.zmem/queue/` on that box) for review by the closeout skill there —
hooks never write the store over MCP. Corrections use the same namespace
chain as the prefetch (`ZMEM_MCP_NAMESPACE` → `ZMEM_NAMESPACE` →
`user:global`). `ZMEM_HERMES_CORRECTIONS=0` disables.


#### Hermes adapter env vars

Paths / store resolution:

| Var | Purpose | Default |
|-----|---------|---------|
| `ZMEM_HOME` | Path to the zmem checkout (where `store.py` lives). **Required for copy installs;** optional for symlink/junction. | — |
| `ZMEM_DATA` | Override the store data directory (holds `store.sqlite` + `core.md`). | `~/.zmem` |
| `ZMEM_STORE` | Override the store SQLite path directly (wins over `ZMEM_DATA`). | — |
| `ZMEM_BACKUP_DIR` | Override the directory snapshots are written to (`store.py backup`). Off-volume recommended for drive-loss protection. | `<store dir>/backups` |
| `ZMEM_CORE_MD` | Override the Tier-0 `core.md` path directly (wins over the store-dir default). | `<data dir>/core.md` |
| `ZMEM_SKILLS_DIRS` | Extra directories searched for skill-promotion scanning (`;`-delimited on Windows, `:` elsewhere). | derived |
| `ZMEM_PROMOTION_REVIEW_DIR` | Directory holding promotion-review artifacts (the `promote` flow). | derived |

Namespace / host:

| Var | Purpose | Default |
|-----|---------|---------|
| `ZMEM_NAMESPACE` | Force a namespace for the local provider (e.g. `project:myrepo`). Default derives from the git remote. | derived |
| `ZMEM_HOST` | Identify the host adapter (`zmem` / `claude` / `codex`) for host-specific gating (Tier-0 injection, native-memory nudge). | derived |
| `ZMEM_TIER0` | Tier-0 gating mode: `zmem` (inject core.md + AGENTS.md) vs `native` (CC native memory). | derived |
| `ZMEM_PROXY_FORGE_HOST` | Forward the local provider's tools through a host adapter's forge endpoint. | unset |
| `ZMEM_BASH_PATH` | Path to a bash binary (used when the default `bash` is not on PATH, e.g. some Windows setups). | `bash` |

Maintenance cadence (the session-start hook runs `backup --if-due`, `consolidate`, and `sweep` on these cadences):

| Var | Purpose | Default |
|-----|---------|---------|
| `ZMEM_BACKUP_INTERVAL_DAYS` | `backup --if-due` runs at most once per this many days. | `1` |
| `ZMEM_BACKUP_RETENTION` | Number of snapshots kept by `backup --retention`. | `7` |
| `ZMEM_CONSOLIDATE_MIN_INTERVAL_DAYS` | Minimum days between consolidate runs (unless `--force` or growth exceeds the threshold). | `7` |
| `ZMEM_CONSOLIDATE_GROWTH_THRESHOLD` | Consolidate runs early if live rows grew by at least this fraction since the last run. | `0.20` |
| `ZMEM_SENTINEL_SWEEP_DAYS` | `sweep` prunes per-session cooldown markers older than this many days. | `7` |
| `ZMEM_BG_LOG` | Set `0` to send session-start maintenance output (consolidate/backup/sweep) to `/dev/null` instead of `<data dir>/zmem-bg.log`. | `1` |

Consolidate / recall / dedup tuning:

| Var | Purpose | Default |
|-----|---------|---------|
| `ZMEM_CONSOLIDATE_THRESHOLD` | Cosine similarity above which two memories consolidate (embedding mode). | `0.80` |
| `ZMEM_CONSOLIDATE_LEXICAL_THRESHOLD` | Jaccard token-overlap threshold used in the no-embeddings lexical fallback. | `0.60` |
| `ZMEM_DEDUP_THRESHOLD` | Cosine similarity above which an incoming memory is deduped against an existing one. | `0.85` |
| `ZMEM_CTX_BUDGET` | Approx byte budget for the Tier-1 pack / context payload. Host-dependent when unset: `25000` (ZCode) vs `9000` (Claude Code / Codex). | `25000` / `9000` |
| `ZMEM_INJECT_TOKEN_BUDGET` | Token budget (default 1500, 4 chars/token heuristic) for hook/session_start memory injection: bullet admission stops at the budget, `decision`/`constraint` rows are never dropped, lowest-score `signal=none` rows drop first (issue #65, 10.9). | `1500` |
| `ZMEM_CONVENTION_INTERVAL` | Fire the convention nudge every N successful tool calls. | `10` |

Capture:

| Var | Purpose | Default |
|-----|---------|---------|
| `ZMEM_CAPTURE_MODE` | Capture policy for writes: `manual` (advisory secret warnings only, trusted local use) or `auto` (redact secret-like content, refuse secret-like provenance — the MCP/network default). | `manual` |

Embedding model (the model file is gitignored; these control how/whether it is obtained):

| Var | Purpose | Default |
|-----|---------|---------|
| `ZMEM_MODEL_AUTODOWNLOAD` | Set `1` to attempt a lazy download of the embedding model when absent. Defaults to `0` (off; the download is opt-in and the CI sets it to `0`). | `0` |
| `ZMEM_MODEL_URL` | URL fetched for the lazy download. The default Xenova export is NOT byte-identical to the pinned checksum (`_MODEL_SHA256` in `embeddings.py`, a deliberately hard-coded trust root — there is no env override for it); see the PLAN.md §7-P10 known gap. | Xenova HF URL |
| `ZMEM_MODELS_DIR` | Override the directory holding `minilm.onnx` (+ tokenizer/config). | `<checkout>/skills/memory/models` |

MCP server:

| Var | Purpose | Default |
|-----|---------|---------|
| `ZMEM_MCP_TOKEN` | Bearer token for the MCP server. **Required** to start the server. | — |
| `ZMEM_MCP_URL` | Remote mode (issue #71 A): point the Hermes `pre_llm_call` hook at the LAN MCP server; the passive prefetch rides `session_start` (`--no-bump`). Unset = local-store mode. | — |
| `ZMEM_MCP_TIMEOUT` | Prefetch subprocess timeout in seconds (the hook itself allows 15). | `8` |
| `ZMEM_MCP_NAMESPACE` | Namespace for the remote prefetch, correction capture, and query-context recall when `ZMEM_NAMESPACE` is unset (empty → server default `user:global`). **Required for scoped-token deployments** — name a namespace the token allows. | — |
| `ZMEM_MCP_DEFAULT_NS` | Opt-in configured default namespace for MCP `add` when the client omits it (issue #71 C; `user:global` otherwise — never a near-miss form like `global`). | — |
| `ZMEM_AUTO_REKEY` | `0` disables the automatic near-miss namespace rekey on store open (issue #71 C; `--no-auto-rekey` per invocation). | `1` |
| `ZMEM_HERMES_CORRECTIONS` | `0` disables Hermes correction capture on `pre_llm_call` (issue #71 D; default ON for parity with the other hosts). | `1` |
| `ZMEM_CODEX_MEMORY` | Codex MEMORY.md path for `mine-history --source codex` (issue #71 I). | `~/.codex/MEMORY.md` |
| `ZMEM_HERMES_SESSIONS` | Hermes session-JSONL root for `mine-history --source hermes` (issue #71 I). | `~/.hermes/sessions` |
| `ZMEM_MCP_TOKEN_FILE` | Path to a file containing the token (alternative to `ZMEM_MCP_TOKEN`). A bare text file is an UNSCOPED operator token (full access). A JSON file `{"token": "...", "namespaces": ["project:x", "user:global"]}` scopes the token: requests outside the allow-list fail closed with the stable `namespace_not_allowed` error, and scoped tokens must pass an allowed namespace explicitly on every read (issue #65, 10.2). Omitting `namespaces` — or setting it to `null` — means an UNSCOPED operator token (full access), exactly like a bare text file. | — |
| `ZMEM_MCP_ALLOW_INSECURE_BIND` | Set to `1` to allow `0.0.0.0` / `::` (and IPv4-mapped) wildcard binds. | unset |
| `ZMEM_MCP_MAX_CONCURRENT` | Cap on simultaneous `store.py` subprocesses the MCP server will run (overload protection). | `8` |
| `ZMEM_MCP_QUEUE_TIMEOUT_S` | How long a queued tool call waits for a concurrency slot before returning an overload error. | `60` |

> **Performance characteristics (inherent, not bugs):** `store.py backup` is
> O(store size) by nature — SQLite's Online Backup API copies the live database
> page-by-page. This is mitigated by the `--if-due` cadence (the session-start
> hook only snapshots once per `ZMEM_BACKUP_INTERVAL_DAYS`). Separately, the
> launcher's `fitEnvelope` content truncation is O(n log n) in content length
> (it binary-searches a truncation point, re-serializing the envelope each
> iteration), but only ever runs on content already over the context budget — a
> rare path bounded by `MAX_CONTENT_CHARS`. Neither is a defect; both are noted
> here so operators can reason about steady-state cost. (#37 L19/L20)

### Local directory (for testing / air-gapped)

1. Clone or copy this repo to a stable path.
2. In ZCode or Claude Code: point the plugin discovery/marketplace flow at the
   local directory instead of the GitHub URL.
3. Install + enable.

### Upgrade

The host tool (ZCode / Claude Code / Codex) discovers new versions by comparing
the `version` field in your installed plugin manifest against the marketplace
entry. Releases are **cut automatically**: whenever a version bump merges to
main, the [Release workflow](.github/workflows/release.yml) verifies that every
host-facing manifest agrees on the version and that
[`CHANGELOG.md`](CHANGELOG.md) carries the matching section, then tags the
merge commit and publishes the GitHub Release with those notes. A merge
without a version bump publishes nothing; a partial bump or a bump without
its changelog section fails the workflow.

To pick up a new release:

1. Re-run the same install/discovery flow you used above (the GitHub URL or the
   marketplace source). The host tool sees the higher `version` and offers the
   update.
2. Reinstall / update the **zmem** plugin and restart your session.

Notes:

- Plugin caches **pin a version directory** (e.g.
  `.../cache/zmem/zmem/<version>/`). A cache for an older version is not
  overwritten by a bump — it coexists until the host tool refreshes it, so after
  upgrading confirm the active path points at the new version directory.
- The plugin has no built-in "update available" notifier of its own; update
  signalling is handled entirely by the host tool's plugin manager comparing the
  marketplace `version` field.
- To pin a specific version, install from a checked-out git tag rather than the
  rolling `main` branch.

#### Detecting served-code drift and forcing a refresh (issue #107)

A version string cannot tell you which code is actually running: a partially
refreshed plugin cache can serve different bytes under the same `version`. Since
0.17.0 every release ships `release-manifest.json` — a sha-256 content hash of
the runtime surface (`hooks/**`, `skills/memory/scripts/**`, `skills/**/SKILL.md`,
`hermes-plugin/**`, `scripts/**` — the last joined in 0.18.0 so the canary is
integrity-visible), committed at the repo root and attached to the GitHub
Release. zmem compares the served tree against it in two places:

- **`doctor`** — the `served-drift` check reports `matched` (pass), `drifted`
  (warn, with the first 10 differing paths and both digests), or `unknown`/skip
  when the tree has no manifest (pre-0.17.0 caches, dev checkouts).
- **Session start** — on the first hook decision of a session, a drifted tree
  appends one line to `zmem-bg.log`
  (`zmem-drift served=<sha8> release=<sha8> files=<n>`) and shows the OPERATOR a
  `systemMessage` notice (never model context; it fires even with
  `ZMEM_INJECT=0`). Detection is log-only and never blocks a hook, and runs at
  most once per session id — each session id gets its own
  `.drift-checked-<key>` marker file in the data dir (the key appends a short
  hash of the full session id so distinct sessions can never collide), and
  hosts that supply no session id share one marker, so such boxes log at most
  one drift line total until the marker is cleared. Markers are reaped by the
  session sweep after the standard sentinel TTL (7 days,
  `ZMEM_SENTINEL_SWEEP_DAYS`), like the capture/convention prompt markers.

To force a refresh per host when doctor reports drift:

1. **ZCode / Claude Code / Codex**: re-run the install/discovery flow from the
   top of this README (or your marketplace update) and restart the session;
   then confirm the active cache path changed.
2. **Manual cache mirror** (robocopy/rsync flows): re-mirror the release tag's
   tree into the cache dir — a partial mirror is exactly what drift detects.
3. Verify: `python <plugin-root>/skills/memory/scripts/doctor.py` shows
   `served-drift ... pass (matched)`, and no new `zmem-drift` line appears in
   `zmem-bg.log` on the next session start.

Release maintainers: regenerate the manifest with
`python scripts/release_gate.py --emit-manifest` and commit it with the release;
the Release workflow runs `--verify-manifest` and refuses to publish a release
whose manifest does not describe its own tree. Run
`python scripts/release_gate.py --verify-manifest` locally as the pre-push
check on any release-prep branch (a manual `gh release create` bypasses the
workflow's verify, so verify before you push).

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

## Bootstrap / cold start

zmem installs empty and learns going forward (live capture, issue #47). If you
have existing Claude Code transcripts on this machine, `mine-history` (issue
#48) salvages the high-signal parts *for you to review* — it never auto-writes:

```bash
# Scan the current project's Claude Code transcripts (read-only)
python <store.py> mine-history --days 90 --json          # report only
# OR queue candidates for the closeout review flow (source=history-mine)
python <store.py> mine-history --days 90 --queue
# sweep everything, not just the current project
python <store.py> mine-history --all-projects --days 90 --queue
```

Then run the **closeout** skill (`/closeout`) to review the queued candidates:
the Step 2 closeout bar applies to mined candidates exactly as to live ones —
recall-before-write, supersede what's wrong, 0–5 rows per review sitting (a
first bootstrap may reasonably accept more). The queue lists `kind: "correction"`
(mined corrections, with an `occurrences` count) and `kind: "error_pattern"`
(recurring tool errors; rewrite each `suggested_guideline` draft as a `lesson`
with the real trigger condition). Mined candidates are reviewed and written with
the agent-assigned *honest* signal — repeated errors do not auto-qualify as
`test`/`compile` grounding.

Notes:
- **Input is Claude Code history only** (the sole transcript substrate zmem
  reads); ZCode/Codex/Hermes are out of scope for mining and gain coverage
  going forward via live capture. A box with no `~/.claude` exits cleanly with
  a message, not a crash.
- **Output is host-agnostic:** accepted rows land in the single shared
  `~/.zmem/store.sqlite`, so a bootstrap seeds memory for all four hosts
  (Claude Code, ZCode, Codex, Hermes) from one box.
- `mine-history` is read-only against transcripts AND the store; `--queue` is
  its only write surface (the sidecar review queue). `--queue` resolves the
  store namespace from the current project's git origin, so it may spawn one
  short `git` subprocess. `ZMEM_CAPTURE_MODE` applies to mined candidates too:
  `auto` redacts secret-like text in correction messages, error_pattern
  messages, and repeated-error samples; `manual`/`reviewed` keeps wording
  verbatim and flags those candidates with `secret_warning` for the reviewer.
- **Retention caveat:** Claude Code deletes transcripts after
  `cleanupPeriodDays` (default 30), so mining sees only what still exists. The
  `session-retention` doctor check reports this window (see *Preflight*).

## Where data lives

- **Store + core.md (box-wide default):** `~/.zmem/` — one shared, tool-neutral
  directory holding `store.sqlite` + `core.md`, read and written by both ZCode
  and Claude Code, and by Codex when that path is explicitly reachable. This is
  the box-wide model: a lesson captured in one host is recallable in the others.
  Subagent auto-recall/reflect is wired for Claude Code and Codex (which emit
  `SubagentStart`/`SubagentStop` hook events); ZCode supports exactly seven hook
  events and does **not** emit subagent lifecycle hooks, so on ZCode subagent
  memory is scoped to the parent session rather than getting its own recall/reflect
  cycle. Override with the `ZMEM_DATA` env var (or the CC plugin's `storeDirectory`
  userConfig option) if you want it elsewhere.
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
- **Live-capture correction queue:** `<store-data-dir>/queue/<namespace>.json`
  (default `~/.zmem/queue/<namespace>.json`; follows the same `ZMEM_STORE` /
  `ZMEM_DATA` override chain as the store, not a fixed path) — one file
  per namespace (namespace names are encoded filesystem-safely: `_` → `__`,
  `:` → `_c`, `/` → `_s`, `\` → `_b`, other chars → their UTF-8 bytes as
  `_x<hex>`). It holds
  candidates captured by the `capture-correction` hook until the closeout skill
  reviews them. The queue shares the store's data dir, so a candidate captured in
  one host is reviewable from any other host on the same box (same single-brain
  model as the store). `ZMEM_CAPTURE_FEEDBACK=1` makes the hook emit a one-line
  acknowledgment; `ZMEM_CAPTURE_MODE=auto` redacts secrets at capture time
  (default `manual` keeps the original wording and flags `secret_warning`). The
  queue is capped at 100 items (oldest first) and stale items are flagged, never
  auto-deleted. **Hermes is intentionally NOT wired for live capture** — its hook
  surface is `post_tool_call` (observational, results discarded) + `pre_llm_call`
  (context injected), so a Hermes correction-capture would use that flag pattern
  and is a separate follow-up.

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

### Disabling passive injection & rolling a host back (issue #110)

**Kill switch.** `ZMEM_INJECT=0` turns off every passive recall-injection
surface at once: the recall hooks (UserPromptSubmit, PreToolUse,
SubagentStart, PreCompact), the SessionStart hook (Tier 2 recall **and Tier
0** — under the switch SessionStart emits its empty `{}` envelope, so
`core.md`/`AGENTS.md` are not injected either), the Hermes provider
(`prefetch`, the `zmem_session_start` tool, and the system-prompt `core.md`
block), the Hermes reflect hook's delivery, and MCP `session_start`. Each
silenced surface emits its empty envelope and logs
`status=silent reason=disabled` (`zmem-bg.log` for the hook surfaces; the
Hermes/MCP loggers for theirs).
Only the literal `0` disables — `ZMEM_INJECT=false`/`no`/empty leave injection
ON (the `ZMEM_QUERY_CONTEXT` convention; `ZMEM_QUERY_CONTEXT` remains the
narrower ops-lane switch). Beware near-miss spellings: `0.0`, `00`, `False`
(case-sensitive), `off` do NOT disable — a typo'd value fails silently toward
injection staying ON, so verify with `doctor` (its `inject-switch` line shows
the live state) after setting the variable.

**What keeps running under the switch.** Every capture path: correction
capture, failure capture, the PostToolUse ops ring, convention counters, and
the SessionStart maintenance dispatch (session-cadence). The capture-side
PROMPT hooks also stay active by design — `zmem-reflect.sh` (Stop
reflection), `zmem-subagent-reflect.sh`, `zmem-convention-capture.sh`, and
the capture-failure/correction prompts — because they prompt the agent to
CAPTURE a lesson rather than inject recalled memory (issue #110 scopes the
switch to recall surfaces and states "capture paths are unaffected"). The
one documented divergence: the Hermes reflect hook (named in #110 as a gated
surface) silences its delivery nudges under the switch, since recalled
query-context rides the same delivery path there. Parked pre-tool fences and
armed nudge markers stay on disk and deliver on the first enabled run —
nothing is lost. `doctor` shows the state on its `inject-switch` line (WARN
when disabled), so a confused operator sees the reason immediately.

**Rolling a host back to a previous version.** Plugin caches pin a version
directory and never overwrite older ones (see [Upgrade](#upgrade)), so a
rollback is a re-pin, not a download:

- **Claude Code**: point the installed plugin back at the previous cache dir
  (`~/.claude/plugins/cache/zmem/zmem/<old-version>/`, recorded with its
  `gitCommitSha` in `~/.claude/plugins/installed_plugins.json`) or reinstall
  from the release git tag, then restart the session.
- **ZCode**: same shape under `~/.zcode/cli/plugins/cache/<marketplace>/zmem/
  <old-version>/` (+ the `installed_plugins.json` next to it); or reinstall
  from the git tag.
- **Codex** (shared-store/broker mode): there is no plugin cache — the
  checkout IS the install, so pin it by checking out the release tag
  (`git checkout v0.14.0`).

**Verifying a rollback (or a refresh).** The `zmem-bg.log` decision lines are
the discriminator — but read the RECALL-BODY line, not a session-start line:
trigger a prompt, then check the line that prompt just appended. On current
code every recall-body line carries `reason=`; a host stuck on a pre-#87 tree
writes `status=`-only recall lines. Session-start lines are a weaker
discriminator here: their `reason=` field is recent (#89/#114-era; verified
2026-09-05 — an older tree writes `status=`-only session-start lines too, and
the `reason=disabled` kill-switch line has existed the longest), so a
`status=`-only session-start line only proves the tree predates the
reason-era (that writer shape is where the #106 field proof originated,
which is why the recall-body line is the reliable discriminator). `doctor`
corroborates (version surfaces + `inject-switch` state). The per-host
injection canary that automates the delivery check — seed, fire, and assert
the fence — ships as **`python scripts/host_canary.py --host claude
--self-test`** (see *Post-install canary* above; issue #108).

### Embeddings (semantic recall / dedup)

ZMem's semantic features — semantic dedup-on-write, hybrid vector recall, and
embedding-seeded consolidation — run on an optional local ONNX model
(all-MiniLM-L6-v2, 384-dim). **The model file is deliberately NOT bundled**
(it is ~90MB and gitignored; CI asserts its absence in a fresh checkout). A
fresh install therefore runs in **degraded mode** (FTS5 keyword recall + lexical
token-overlap consolidation) until you provide the model. Degraded mode is fully
supported — `recall`, `consolidate`, and all writes keep working — but semantic
dedup and vector recall are off, and unembedded rows are skipped as consolidation
seeds.

**To enable embeddings:**

1. Make sure the embedding + vector-recall runtime is installed in the Python
   interpreter ZMem resolves (the hooks' interpreter, or the Hermes MCP server's
   interpreter): `onnxruntime`, `tokenizers`, `numpy`, and `sqlite-vec` (the
   `vec0` virtual table that powers vector recall and semantic dedup; without it
   the store degrades to FTS5 keyword recall). For the Hermes MCP store host,
   `pip install -r hermes-plugin/server/requirements-embeddings.txt` installs
   all four.
2. Obtain a checksum-verified `minilm.onnx` and place it at the resolved models
   dir (see "Check status" below for the exact path), OR set:
   - `ZMEM_MODEL_URL` to a source whose bytes match the pinned SHA-256, and
   - `ZMEM_MODEL_AUTODOWNLOAD=1` (off by default; ZMem never makes an
     unsolicited network call). On checksum mismatch the download is discarded
     and ZMem stays in degraded mode rather than loading an unverified binary.

   Note: the default `ZMEM_MODEL_URL` (the widely-used Xenova ONNX export) is
   NOT byte-identical to the pinned checksum — different ONNX export toolchains
   produce different bytes for the same weights — so an autodownload from the
   default URL will fetch-then-reject-checksum and leave you in degraded mode
   ~100% of the time. Either place a verified `minilm.onnx` manually, or point
   `ZMEM_MODEL_URL` at a source you have confirmed matches the pin.

`ZMEM_MODELS_DIR` overrides the models directory — point it at a shared,
populated, checksum-verified model cache to reuse one model across checkouts
(e.g. `~/.zmem/models`). This is a supported production knob, not just a test
affordance.

When `ZMEM_MODELS_DIR` is unset, ZMem resolves the models directory in this
order: (1) the plugin's bundled `<checkout>/skills/memory/models` if it
contains a model; (2) otherwise the box-wide shared cache at
`<store data dir>/../models` (the `models` sibling of the store `data`/`store`
file — e.g. `~/.zmem/models` for the default `~/.zmem` store) if it contains a
model; (3) otherwise the bundled directory, which reports `model_file_missing`.
The shared-cache fallback is what lets a checkout that does not ship the
gitignored model keep embeddings working with no env var, so a fresh checkout
or a host where the model is installed once under `~/.zmem` does not silently
lose semantic recall.

**Check status** (one command away from noticing drift):

- `python <store.py> stats` reports live embedding coverage
  (`with_embedding=` / `without_embedding=`), whether embeddings are available,
  the reason if not, and the resolved models dir.
- `python skills/memory/scripts/doctor.py` reports embedding availability + the
  reason + the resolved interpreter (so the multi-Python case is diagnosable).

**Backfill existing unembedded rows:** once the root cause is fixed, run
`python <store.py> reembed` to embed live rows that are missing embeddings.
   Use `reembed --all [--profile NAME]` to convert the whole store to another
   profile/dimension atomically (see SKILL.md "reembed" and "Embedding
   profiles"); profiles are selected with `ZMEM_EMBED_PROFILE`.
   `--profile` requires `--all`: without it the command refuses with exit 2
   rather than silently doing nothing.

   Shipped profiles: `minilm` (Xenova/all-MiniLM-L6-v2 ONNX, 384-d,
   checksum-pinned) and `fake` (16-d deterministic placeholders for
   model-absent tests/CI only). The pin covers the Xenova ONNX export —
   sentence-transformers PyTorch weights differ by design; verification has no
   override or bypass.
Backfilling before fixing the root cause only treats the backlog — new captures
keep landing unembedded until embeddings are available in the capturing
environment. When embeddings are unavailable, ZMem prints a one-time-per-process
warning naming the reason and the resolved models dir on the first unembedded
capture, so silent drift does not recur.

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
