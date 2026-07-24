# ZMem box-wide memory — CUTOVER CHECKLIST

Build status: **P0–P11 complete, each independently verified** on branch `feat/box-wide-memory`.
Nothing below has been done yet — these are the steps that touch the live box. Do them in order.

---

## 0. Pre-flight

```bash
cd C:\Users\Brett\.graphify\repos\zaxbysauce\zmem
git log --oneline origin/main..feat/box-wide-memory
```
Nothing is pushed. `main` is untouched at `4ff06c8`.

**Quiesce first:** close ZCode and any other Claude Code sessions before step 2 so the legacy store isn't mid-write.

---

## 1. Verify ZCode still loads its hooks  ⚠ only source-confirmed

`hooks/hooks.json` was renamed to `hooks/hooks.zcode.json` (required — Claude Code auto-loads any default-named `hooks/hooks.json` *in addition to* the manifest path, which caused a double-load + module errors). ZCode's loader reads `manifest.hooks` as a custom path (confirmed in `zcode.cjs.patched`, fn `ckt`), but this was **never runtime-tested in a real ZCode session**.

Start one ZCode session and confirm its ZMem hooks still fire. If they don't, revert to `"hooks": "hooks"` in `.zcode-plugin/plugin.json` and keep the file named `hooks.zcode.json` (never `hooks.json`).

---

## 2. Final re-import of the drifted legacy store

`~/.zmem` is a **point-in-time copy** taken during P1; ZCode has kept writing to the legacy store since.

```bash
python skills/memory/scripts/import-store.py --force
python skills/memory/scripts/store.py stats
```

The import is source-safe (never opens the legacy store read-write; asserts its sha256 is unchanged). The v5 namespace migration is idempotent and schema-gated — it re-runs automatically on the fresh copy and reproduces identical keys.

This also wipes two stray test rows that verifier agents accidentally wrote to `~/.zmem` during the build (`f371017d` canary — already superseded; `d191bcb3` "test memory degrade check alpha" — still live). No real memories were affected (superseded count held at 21 throughout).

---

## 3. Turn off Claude Code's native memory  ← the "replace" step

A plugin **cannot** set this; only you can. Add to `~/.claude/settings.json`:

```json
"autoMemoryEnabled": false
```

(or set `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`). `CLAUDE.md` loading is a separate, always-on mechanism and is unaffected.

If you skip this, ZMem still works but runs *alongside* native memory — both inject Tier 0, which is the duplication the design avoids. ZMem shows a one-time nudge in that case.

---

## 4. Install the plugin on both hosts

- **Claude Code:** `/plugin marketplace add C:\Users\Brett\.graphify\repos\zaxbysauce\zmem` then install `zmem`. (Requires an interactive terminal; `/plugin` is unavailable in some app surfaces. `claude plugin install` also exists.)
- **ZCode:** Settings → Plugin Management → point at the same directory; reinstall so it picks up `hooks.zcode.json`.

Verify: a new CC session should register **7 hooks** (SessionStart, UserPromptSubmit, PostToolUse, PostToolUseFailure, Stop, SubagentStart, SubagentStop).

---

## 5. Re-promote the old skills

`~/.zcode/skills/` holds ~24 `zmem-*` skills, ~20 still carrying the OLD broken draft (triplicated text, mid-word truncation, and the literal `EDIT THIS DESCRIPTION` leaking into the frontmatter `description:` — the model-facing trigger surface). P9 fixed the generator but deliberately did not rewrite existing files.

```bash
python skills/memory/scripts/store.py promote --dry-run
python skills/memory/scripts/store.py promote --id <uuid> --confirm            # regenerate
python skills/memory/scripts/store.py promote --id <uuid> --description "..." --confirm   # hand-written trigger
```
Promotion now dual-writes to **both** `~/.claude/skills` and `~/.zcode/skills` (all-or-nothing on collision). Delete the stale versions first, or promotion will refuse on collision.

---

## 6. Host the embedding model (optional but recommended)

`minilm.onnx` (90 MB) is no longer git-tracked (still on disk here). The lazy-download default URL points at a HuggingFace ONNX export that is **not byte-identical** to the pinned sha256 `bbd7b466f6d58e646fdc2bd5fd67b2f5e93c0b687011bd4548c420f7bd46f0c5`, so a fresh clone will fail the checksum and **fail open to lexical mode** (recall still works via FTS5; consolidation via Jaccard token overlap).

To make fresh installs get real embeddings: upload this box's `skills/memory/models/minilm.onnx` as a GitHub release asset and set that URL (+ its sha) as the default, or set `ZMEM_MODEL_URL`.

---

## 7. Optional / deferred

- **Git history rewrite** — `git rm --cached` stopped future tracking, but the 90 MB blob is still in history. A `filter-repo`/BFG purge + force-push is disruptive and deliberately left to you.
- **`userConfig.storeDirectory`** — the manifest field parses and validates, but is not yet wired to `ZMEM_DATA` at runtime; env remains the real override.
- **Teardown throwaway probes:** `C:\Users\Brett\zmem-p05-smoketest`, `C:\Users\Brett\.zmem-p05`.

---

## Operating notes

- **Backups:** auto-snapshot runs at most once/day (`ZMEM_BACKUP_INTERVAL_DAYS`), keeping 7 (`store.py backup --retention N`). Restore with `store.py restore --from <snap> --force` — it takes a pre-restore safety backup first, so a restore is itself undoable. Run restores when no session is actively writing.
- **A bare `store.py add` with no `ZMEM_DATA` writes the shared store** (`~/.zmem`) by default. Always set `ZMEM_DATA` to a temp dir when experimenting.
- **Secrets:** the store is local plaintext, now aggregating every project and both tools. The write-time scanner is advisory only. Store dir is owner-only ACL.
- **Namespaces** key on git remote, so worktrees and second clones of one repo share memory (e.g. `E:\ZCode\opencode-swarm` and `E:\ClaudeCode\opencode-swarm-dev` → the same 107 lessons).
