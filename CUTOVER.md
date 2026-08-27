# ZMem Cutover Checklist

This is the supported operator cutover path for moving to one shared zmem
store. It is intentionally generic. Historical one-off workstation commands are
not part of the main flow.

## 1. Pick one canonical physical store path

- Choose one local, non-OneDrive, non-UNC path for the shared store.
- Keep that same physical path across ZCode, Claude Code, and Codex.
- Phase 1 is **single-machine only**. Do not let different hosts on the same
  box silently fan out to different stores.

Default:

```bash
~/.zmem/store.sqlite
~/.zmem/core.md
```

## 2. Run the read-only doctor first

```bash
python skills/memory/scripts/doctor.py --project <repo> --format human
```

`doctor.py` never repairs anything. It only inspects:
- store-path resolution and split-brain env/config risk
- local/non-OneDrive path safety
- Python, SQLite FTS5, Node, and Windows shell prerequisites
- best-effort read/write access to the target path
- schema compatibility against current v12
- `embeddings_health` check: active profile vs stored vector dimension,
  rows with/without embeddings, shipped-profile inventory, and warnings
  when a non-temporary store runs `ZMEM_EMBED_PROFILE=fake` (issue #63)
- v9 append-only lineage columns present (`valid_until`/`update_of`/`taint`)
- v10 entity identity tables present and non-vacuous (`entity`/`entity_alias`/
  `memory_entity` — inspect deeper with `store.py entity-list`)
- v11 link surface present (`memory_link` table + `memory.trust_score` in
  range [0,1] — inspect deeper with `store.py links --id <uuid>`)
- Claude and Codex native-memory conflicts
- canonical namespace derivation for the target project
- required host surfaces and optional Codex adapter files

Do not proceed on a failing doctor run until the blockers are understood.

## 3. Disable native memory explicitly

zmem should not auto-edit host config. Disable native memory yourself before
cutover.

### Claude Code

Set either:

```json
{ "autoMemoryEnabled": false }
```

in `~/.claude/settings.json` or `~/.claude/settings.local.json`, or set:

```bash
CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
```

### Codex

Disable the current Codex memory knobs in `~/.codex/config.toml`:

```toml
[features]
memories = false

[memories]
use_memories = false
generate_memories = false
```

Target shape for Claude Code and Codex cutover is `ZMEM_TIER0=native`: keep the
host's native project-instruction surface, and let zmem own the shared Tier 2
store.

## 4. Make the canonical path reachable from every host

- **ZCode / Claude Code:** use the plugin install surface and point both hosts
  at the same canonical directory. Claude's `storeDirectory` option is wired at
  runtime; `ZMEM_DATA` still wins if both are set.
- **Codex:** do not assume Codex can write `~/.zmem`. If the canonical path is
  outside Codex's writable roots, add a writable root or use a small local
  broker that owns the store and exposes read/write actions.
- Never "solve" a writable-root problem by giving Codex a different physical
  store path. That creates split-brain memory on the same machine.

## 5. Install and trust the host surfaces

Required surfaces in this repo today:
- Claude Code plugin surface
- ZCode plugin surface
- Memory skill surface

Optional surface:
- Repo-local Codex adapter files, if and when that lane lands

After any hook-surface change:
- trust the project again if your host requires it
- reapprove hooks if your host tracks per-hook trust

This matters most for Codex cutover, where repo-local hook files may arrive
after the plugin surfaces.

## 6. Import or migrate legacy data once

If you still have a legacy store, import it into the canonical shared path:

```bash
python skills/memory/scripts/import-store.py --source <legacy-store.sqlite> --dest-dir <canonical-dir> --force
```

Before opening a v4 store with a newer runtime, provide every legacy project
namespace that must be re-keyed (this is the historical v4→v5 namespace
migration mechanism; the current runtime still honors it):

```powershell
$env:ZMEM_NS_MIGRATION_MAP='{"project:oldname":"C:/src/owner/repo"}'
```

The value is a JSON object of `{old_namespace: live_checkout_path}`. ZMem reads
the checkout's current Git remote to derive the canonical namespace. Omitted
entries are retried on later opens, but they remain under their old namespace
until the map is supplied, so verify each expected project before cutover.

Then re-run:

```bash
python skills/memory/scripts/doctor.py --project <repo> --format human
python skills/memory/scripts/store.py stats
```

The goal is one canonical store, one canonical namespace key per project, and
no fallback to host-specific plugin data directories.

## 7. Verify first live use

Check at least:

```bash
python skills/memory/scripts/store.py recall --query "<known phrase>" --namespace "<canonical namespace>" --json
python skills/memory/scripts/store.py stats
```

On Windows plugin hosts, also confirm a usable Git Bash/Cygwin shell is the one
running hooks, not the WSL `bash.exe` shim.

## 8. Keep maintenance explicit

- Back up regularly:
  ```bash
  python skills/memory/scripts/store.py backup
  ```
- Restore only when no session is actively writing:
  ```bash
  python skills/memory/scripts/store.py restore --from <snapshot.sqlite> --force
  ```
  A crashed writer lease is deliberately treated as live for up to 300 seconds
  (`ZMEM_WRITER_LEASE_STALE_SECONDS`) because deleting a merely slow writer's
  lease could let restore overwrite an active store. Wait for that bounded
  fail-safe window rather than lowering it casually.
- Treat promotion as a reviewed step. `promote --confirm` writes into the host
  skill surfaces and should not be made an unattended background action.

## 9. Respect the trust and plaintext risks

- The store is a **local plaintext SQLite file**. Do not put secrets, creds, or
  PII in it.
- Remote harvests, sync repos, and any broker-fed content are **untrusted until
  reviewed**.
- If a sync repo or broker is compromised, treat that as a memory-store content
  compromise, not a minor metadata issue.

## Historical notes

Any personal workstation paths, branch names, and one-time remediation commands
from earlier development are intentionally excluded from this checklist. Keep
them in issue notes or rollout logs, not in the supported operator path.
