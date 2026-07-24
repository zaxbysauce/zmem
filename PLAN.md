# ZMem → box-wide unified memory brain (Claude Code + ZCode) — Implementation Plan

**Status:** Independent critic review complete → **APPROVE-WITH-CHANGES**; all 2 blockers, 3 should-fixes, and omissions folded in (marked `CRITIC …` inline). Awaiting **user execution approval**. Do not execute until the user signs off.
**Repo:** `github.com/zaxbysauce/zmem` (local clone: `C:\Users\Brett\.graphify\repos\zaxbysauce\zmem`, HEAD `4ff06c8`, 1 behind origin `97ffe18`).
**Author of record:** Brett (ZaxbyHub). Windows-primary box.

---

## 0. Goal (verbatim intent)

1. **One memory brain for the whole box** — Claude Code and ZCode read/write a single shared store; a lesson learned in one tool is available in the other, including in delegated subagents.
2. **ZMem replaces Claude Code's native memory** — ZMem becomes the sole memory system, not an augmentation.
3. **One source of truth to maintain** — no forked "Claude version" vs "ZCode version" of the logic.

### What "one source of truth" actually means here
All **logic** (store engine, launcher, hook scripts, skill, templates, embeddings) is a **single shared set of files**. Only thin, static, per-host **declarations** are duplicated: `plugin.json` (×2), `hooks.json` (×2), `marketplace.json` (×2). These differ only in a manifest path, a plugin-root variable name, and which events are declared — a few dozen lines of JSON that rarely change and contain no logic. This is the monorepo: shared brain, thin host shims. No build step (see §9 for the fallback if a gate forces one).

### Honest ceiling on "replace" (RESOLVED — not a blocker)
A plugin **cannot** disable native memory by itself: Claude Code only honors the `agent` and `subagentStatusLine` keys from a plugin's bundled `settings.json` (source: `plugins-reference.md`). The off-switch (`autoMemoryEnabled:false`, or env `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, or `/memory` toggle — source: `memory.md`) must be applied by the **user in their own `~/.claude/settings.json`**. So "replace" = ZMem supplies all memory context + a **one-line documented user step** turns native off. This does **not** touch `CLAUDE.md` loading (separate, always-on mechanism) — the user's `~/.claude/CLAUDE.md` orchestration policy stays intact.

---

## 1. Research-confirmed facts (gate answers)

Sourced from current Claude Code docs (`hooks.md`, `memory.md`, `plugins-reference.md`, `settings.md`, `sub-agents.md`, fetched 2026-07-23) and direct inspection of the store/hook source on disk. **Payload field names are flagged version-sensitive** and get an empirical dump in Phase 0.5.

| Gate | Question | Answer | Source |
|---|---|---|---|
| **G0** | Can native memory be disabled? | **Yes** — `autoMemoryEnabled:false` / `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` / `/memory`. Plugin can't set it; user must. | `memory.md`, `plugins-reference.md` |
| **G2** | Do the events exist? | **All exist**, incl. `PostToolUseFailure`, `SubagentStart`, `SubagentStop`, `SessionStart`, `Stop`, `UserPromptSubmit`, `PostToolUse`. | `plugins-reference.md` |
| **G4** | Stop/SubagentStop payload has `transcript_path`? | **Yes** both. SubagentStop adds `agent_type`,`agent_id`. `last_assistant_message` also available. | `hooks.md` |
| **G5** | How does recall reach subagents? | **`SubagentStart`** fires per subagent, injects `hookSpecificOutput.additionalContext`. First-class. Cannot block startup (injection-only). | `hooks.md`, `sub-agents.md` |
| **G3** | Windows hook command resolution? | **Exec form** (`command:"node", args:[…]`) resolves `node.exe` on PATH — reliable. Bare `bash x.sh` unreliable (Git-Bash-dependent, falls back to PowerShell). | `hooks.md` |
| **G6** | additionalContext envelope + limits? | `{"hookSpecificOutput":{"hookEventName":"…","additionalContext":"…"}}`. **10,000-char cap** on hook output strings. | `hooks.md` |
| **G7** | Env vars for the store path? | `CLAUDE_PLUGIN_ROOT` (ephemeral), `CLAUDE_PLUGIN_DATA` (**deleted on uninstall**), `CLAUDE_PROJECT_DIR`. Use a **fixed path via env/userConfig**, not plugin-data. | `plugins-reference.md` |

### P0.5 EMPIRICAL RESULTS (2026-07-23, Claude Code 2.1.217, this Windows box)
Ran the throwaway dual-manifest probe plugin via `claude --plugin-dir <dir> -p …`. The nested session hit an OAuth-refresh error (child can't reuse the parent token) so the turn didn't complete — but SessionStart + UserPromptSubmit hooks **fired and dumped** before that, which answers the structural gates:
- **G1 — RESOLVED ✅** CC loaded the plugin with a **co-present `.zcode-plugin/`** dir present, no error; hooks fired. → the no-build-step dual-manifest approach is validated.
- **G9 — RESOLVED ✅ (static + live)** `plugin.json "hooks": "./hooks/hooks.claude.json"` (custom path) was honored; official example confirms it's a path field.
- **G8 — MOOT** we ship per-host hooks.json (§4); the probe used exactly that and it worked. No single-shared-hooks.json needed.
- **Field names — CONFIRMED** SessionStart → `{session_id, transcript_path, cwd, hook_event_name, source}`; UserPromptSubmit → `{…, prompt_id, permission_mode, prompt}`. Env present: `CLAUDE_PLUGIN_ROOT`, `CLAUDE_PLUGIN_DATA`, `CLAUDE_PROJECT_DIR`. Matches docs exactly.

### Still OPEN (low-risk; verify opportunistically during the phase that uses them)
- **Stop / PostToolUse / PostToolUseFailure / SubagentStart / SubagentStop field names** — doc-confirmed (`transcript_path`, `tool_name`/`tool_input`/`tool_response`, `agent_type`/`agent_id`) but not empirically dumped (the nested-auth error blocked a completed turn / tool use / subagent). Docs proved accurate on the 2 events verified, so risk is low; capture live while wiring P5 (failures) and P7 (subagent). `transcript_path` — the load-bearing field — is confirmed present on the events checked.
- **G10** — unknown-hook-event-name behavior (error vs ignore) undocumented; avoided by declaring only host-appropriate events per host.

### Store facts (verified in `store.py`)
- Already **WAL + `synchronous=NORMAL` + `busy_timeout=5000`** (`store.py:113-115`); versioned migrations via `meta.schema_version`, currently **v4**; `vec0`/sqlite-vec embedding table present (semantic recall scaffolded; degrades to FTS5 when the model is absent).
- Store resolution: `ZMEM_STORE > ZCODE_PLUGIN_DATA > ~/.zcode/memory` (`store.py:59-67`) — **no CC/box-neutral path yet**.
- Promotion target hardcoded `~/.zcode/skills/` (`store.py:958,971`).
- Installed store: schema **v4**, **383 live rows** (403 total). Namespace **totals** (all rows, sum=403): `user:global` 215, `project:opencode-swarm` 113, `project:trainingapp` 39, `project:ragappv3` 33, `project:ZCode` 2, `project:zmem` 1. **Live-only** (sum=383): 208 / 107 / 34 / 32 / 1 / 1 respectively.
- **Namespace split, quantified:** the 113 `project:opencode-swarm` memories were captured at `E:\ZCode\opencode-swarm`; the *same git remote* is also checked out at `E:\ClaudeCode\opencode-swarm-dev`. Under `basename()` keying a CC session there looks in `project:opencode-swarm-dev` and finds **zero** of the 113. Git-remote keying unifies them.
- Box is safe for the store: all drives local; OneDrive is on `D:`, not under `C:\Users\Brett` → `C:\Users\Brett\.zmem` is not sync-corruptible.

---

## 2. Architecture

### 2a. The shared store (foundation)
- **Location:** tool-neutral `C:\Users\Brett\.zmem\` holding `store.sqlite` + `core.md`. Chosen because `CLAUDE_PLUGIN_DATA`/`ZCODE_PLUGIN_DATA` are deleted on uninstall and are per-tool — wrong lifecycle for a shared brain (G7).
- **How both tools point at it:** the launcher sets canonical `ZMEM_DATA=C:\Users\Brett\.zmem` (overridable via a `userConfig` "store directory" option on CC and env on ZCode). `store.py` resolution chain becomes: `ZMEM_STORE > ZMEM_DATA > CLAUDE_PLUGIN_DATA > ZCODE_PLUGIN_DATA > ~/.zmem (new box-neutral default) > ~/.zcode/memory (legacy)`.
- **Concurrency (already 80% done):** keep WAL/`busy_timeout`. Add: (1) a **local-FS guard** — refuse to open a store on a UNC/network/`$OneDrive` path (prevents WAL corruption); (2) a bounded **retry-on-`SQLITE_BUSY`** wrapper on the write paths (`add`, `supersede`, `consolidate`, retrieval-count bumps) as belt-and-suspenders beyond `busy_timeout`; (3) confirm every write is a single committed transaction (idempotent via existing dedup/tombstone). Concurrent writers = multiple CC sessions + subagents + ZCode.
- **Store file perms:** create `~/.zmem` and `store.sqlite` with user-only perms (Windows ACL: owner-only). Box-wide plaintext aggregation raises secret/PII blast radius (§8).

### 2b. Namespace scheme (cross-tool stable) — NEW
- New key: `project:<canonical>` where `<canonical>` = normalized **git remote** (host+path, lowercased, `.git` stripped, both `git@host:org/repo.git` and `https://host/org/repo.git` forms normalized to `host/org/repo`) when a remote exists; else normalized **absolute repo root** (drive-letter-lowercased, forward-slashed). Worktrees and second clones of the same remote collapse to one namespace. `user:global` remains the deliberate cross-project bridge.
- Derivation is a **single canonical function** `host.py:resolve_namespace(project_dir)`, called both by the hook launcher (runtime recall) **and by the v5 migration** (see §8). It is the sole producer of namespace keys — nothing else constructs one.
- **NOTE (two real orgs):** `opencode-swarm`'s remote is `github.com/ZaxbyHub/…`; `zmem`'s is `github.com/zaxbysauce/…`. Different orgs — which is precisely why keys must be *derived from each checkout's own remote*, never hand-typed (see §8 blocker fix). `store.py` stays namespace-agnostic (takes `--namespace`).

### 2c. The host adapter — `zmem-launch.js` (single shared file, all host knowledge)
Already the universal entrypoint and already self-locates via `__dirname`. Extend it to own **detection, env normalization, stdin passthrough, and envelope translation** so the bash scripts and most of `store.py` never branch on host:
1. **Detect host:** explicit `ZMEM_HOST` wins; else `CLAUDE_PLUGIN_ROOT`→`claude`, `ZCODE_PLUGIN_ROOT`→`zcode`.
2. **Read stdin once**, parse `transcript_path`/`session_id`/`cwd`/`agent_type`, **then re-emit the exact bytes to the child's stdin** (today stdin is `inherit`; becomes buffer-and-replay). The child scripts still read the full payload.
3. **Export canonical env** consumed by scripts (no host branching downstream):

   | Canonical | From |
   |---|---|
   | `ZMEM_HOST` | detection |
   | `ZMEM_ROOT` | `{CLAUDE,ZCODE}_PLUGIN_ROOT` (or `__dirname/..`) |
   | `ZMEM_DATA` | userConfig/env → default `~/.zmem` |
   | `ZMEM_PROJECT` | `{CLAUDE,ZCODE}_PROJECT_DIR` / stdin `cwd` |
   | `ZMEM_SESSION` | `CLAUDE_SESSION_ID` / stdin `session_id` |
   | `ZMEM_TRANSCRIPT` | stdin `transcript_path` (claude) / '' |
   | `ZMEM_AGENT_TYPE` | stdin `agent_type` (SubagentStart/Stop) / '' |
   | `ZMEM_SKILLS_DIRS` | host default(s): claude→`~/.claude/skills`, zcode→`~/.zcode/skills`, promotion writes **both** |
   | `ZMEM_TIER0` | `native` (claude, since CLAUDE.md owns Tier 0) / `zmem` (zcode) |
   | `ZMEM_CTX_BUDGET` | `9000` (claude, under the 10K cap) / `25000` (zcode) |
4. **Envelope translation on stdout (defensive — CRITIC BLOCKER 2):** spawn child with stdout **piped** (not inherited), buffer it, and if `ZMEM_HOST=claude` rewrap `{additionalContext:X}` → `{hookSpecificOutput:{hookEventName:<derived from hook arg>, additionalContext:X}}`. **Bash scripts keep emitting bare `additionalContext` unchanged** — zero script edits for the envelope. Two hard requirements the naive "buffer-all-and-JSON.parse" version gets wrong:
   - **Parse defensively, fail open.** The hook script's real stdout can contain non-JSON noise — the backgrounded `consolidate` prints `[zmem] merged N…` to inherited stdout (`zmem-session-start.sh:121`), and any `set -x`/python warning would too. The launcher must extract the payload robustly (each hook script wraps its JSON in a `<<<ZMEM_JSON>>>…<<<END>>>` sentinel the launcher greps for; on any parse failure emit `{}` and exit 0). Never let stray stdout corrupt or drop the injection.
   - **Detach every background child's stdio.** Change the backgrounded consolidate to `>/dev/null 2>&1 &` (currently `2>/dev/null &`, stdout still inherited) so it (a) can't pollute the buffer and (b) doesn't hold the pipe open — otherwise the launcher sees EOF only when the 5 s kill fires (`session-start.sh:124-128`), adding ~5 s to every session start (and a true hang risk if the python grandchild isn't reaped on Windows/Git Bash).
   - **Budget on the ENCODED envelope (CRITIC SHOULD-FIX 5):** enforce the 9,000-char limit against `len(json.dumps(envelope))`, not raw content — JSON escaping of newline/quote-dense blocks (e.g. reflect's fenced error block, `zmem-reflect.sh:269`) inflates size. Truncate the content until the encoded envelope fits, appending a `[recall truncated]` marker.
   - Hook-arg→event map: `session-start`→SessionStart, `recall`→UserPromptSubmit, `subagent-recall`→SubagentStart, `reflect`→Stop, `subagent-reflect`→SubagentStop, `capture-failure`→PostToolUseFailure, `convention-capture`→PostToolUse.
5. **Invocation form (CRITIC SHOULD-FIX 3):** hooks.json uses **string form** — `"command": "node \"${<HOST>_PLUGIN_ROOT}/hooks/zmem-launch.js\" <hook>"` — matching the only two working on-disk examples (`security-guidance/hooks/hooks.json:9`, existing `hooks/hooks.json:9`). G3's real requirement is "invoke via `node`, not bare `bash`," which string form already satisfies; the args-array "exec form" is an unverified schema bet with no benefit here, so we do **not** use it.

### 2d. `host.py` (single shared file, Python-side host facts)
Owns: store-path resolution chain (2a), promotion dirs (`ZMEM_SKILLS_DIRS`), and `resolve_namespace()` (2b). Replaces the hardcodes at `store.py:59-67,958,971`. Everything else in `store.py` stays host-agnostic.

### 2e. Unified failure detection — `store.py failures`
New subcommand `store.py failures --session <id> [--transcript <path>]` → `{count, details[]}` JSON. If `--transcript` given → scan the JSONL for `"is_error":true` / `toolUseResult:"Error…"` (verified present in real CC transcripts); else → the existing `db.sqlite` query (ZCode). `reflect.sh`, `subagent-reflect.sh`, and `capture-failure.sh` call it, passing `$ZMEM_TRANSCRIPT`. The untrusted-error-text fencing (`reflect.sh:265-283`) moves into it intact.

---

## 3. Replace native Claude Code memory (clean path, CC-only)
1. ZMem's `SessionStart` injects all Tier 0 + Tier 2 memory from the shared store (`ZMEM_TIER0=native` means Tier 0 = ZMem's `core.md` content, since CC's own `memory/` is being turned off — NOT the AGENTS.md path).
2. **User step (documented, one line):** add `"autoMemoryEnabled": false` to `~/.claude/settings.json` (or set `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`). The install skill/README states this explicitly and explains the plugin can't set it itself.
3. The `memory` skill's instructions tell the model that ZMem (`store.py add/recall`) is the sole memory path; don't hand-write native memory files.
4. No stray-write-ingest hook needed (native writes are off). **Degrade mode is not clean (CRITIC 7):** if the user declines the toggle, ZMem's Tier 0 **and** native memory both load — genuine double-injection, the exact duplication "replace" avoids. We surface a one-time SessionStart nudge, but it can only read the `settings.json` file, not a live `/memory` runtime toggle, so it may misfire. Degrade mode is a soft fallback, explicitly *not* a co-equal supported mode; the plan recommends the user apply the toggle.

---

## 4. Repo / monorepo structure

```
zmem/
├─ .claude-plugin/
│  ├─ plugin.json          # CC manifest; hooks → ../hooks/hooks.claude.json (pending G9)
│  ├─ marketplace.json     # CC self-marketplace ({$schema,name,plugins:[{source:"."}]})
│  └─ userConfig: store-dir (type directory, default ~/.zmem)
├─ .zcode-plugin/plugin.json   # existing; hooks → hooks/hooks.zcode.json
├─ marketplace.json            # existing ZCode root marketplace
├─ hooks/
│  ├─ hooks.claude.json    # STRING form; ${CLAUDE_PLUGIN_ROOT}; events incl SubagentStart/Stop, PostToolUseFailure
│  ├─ hooks.zcode.json     # STRING form; ${ZCODE_PLUGIN_ROOT}; ZCode's supported events
│  ├─ zmem-launch.js       # SHARED adapter (detect/normalize/stdin-replay/envelope)
│  ├─ zmem-session-start.sh    # SHARED; Tier0 gated on ZMEM_TIER0; budget ZMEM_CTX_BUDGET
│  ├─ zmem-recall.sh           # SHARED; budget-aware
│  ├─ zmem-subagent-recall.sh  # NEW SHARED; SubagentStart → recall into subagent
│  ├─ zmem-reflect.sh          # SHARED; calls `store.py failures`
│  ├─ zmem-subagent-reflect.sh # NEW SHARED; SubagentStop capture
│  ├─ zmem-capture-failure.sh  # SHARED; calls `store.py failures`
│  └─ zmem-convention-capture.sh  # SHARED
├─ skills/memory/
│  ├─ SKILL.md             # SHARED (adds: box-wide model, autoMemory step, subagent recall)
│  ├─ scripts/{store.py, host.py(NEW), embeddings.py}   # SHARED
│  └─ models/…             # lazy-download, NOT committed (§7-P10)
├─ templates/              # SHARED
└─ tests/                  # NEW: store unit + envelope-translation + host-detect + namespace + failures
```

**Maintenance model:** all logic shared; only `plugin.json ×2`, `hooks.*.json ×2`, `marketplace.json ×2` are host-specific static declarations. If **G9** says CC forces `hooks/hooks.json` exactly, fall to §9.

---

## 5. Recall into subagents (the other half of box-wide)
- New `SubagentStart` hook → `zmem-subagent-recall.sh`: recalls from the shared store scoped to `ZMEM_PROJECT` (+ `user:global`), optionally biased by `ZMEM_AGENT_TYPE`, injects via `additionalContext` (9K encoded budget). This is how a dispatched `coder`/`explorer`/`reviewer` inherits box-wide memory.
- **Hook-path recall is READ-ONLY (CRITIC 6).** `recall` normally bumps `retrieval_count` (`store.py:730,800`); with heavy fan-out, a single dispatch would turn N subagents into N concurrent writers on the shared store. The `SubagentStart` (and `UserPromptSubmit`) recall paths pass a `--no-bump` flag so hook-driven recall does not write; retrieval-count accounting is deferred/batched (or only bumped by explicit skill-invoked `recall`). Directly reduces the multi-writer contention §2a guards against.
- `SubagentStop` hook → `zmem-subagent-reflect.sh`: runs unified failure detection on the subagent's `transcript_path`; captures a grounded lesson if the subagent failed — closing the gap where delegated failures currently evaporate.
- Empirical check (Phase 0.5): confirm a `SubagentStart` marker string actually appears in a subagent's context on this build.

---

## 6. Promotion across the box (Tier 4)
- `store.py promote` writes the SKILL.md to **every** dir in `ZMEM_SKILLS_DIRS` (`~/.claude/skills` + `~/.zcode/skills`), so a lesson promoted in one tool becomes a skill in both.
- **Hard gate:** the promotion-quality fix (Phase 9) must land first. Current drafts triplicate the content, truncate mid-word, and ship the literal "EDIT THIS DESCRIPTION…" in the `description:` (the trigger surface). Generate a real, distinct, pushy trigger description (and keep the human-in-the-loop `--dry-run`/`--confirm` gate) before writing into a second tool's skill dir.

---

## 7. Phased plan (each phase = its own PR, with done-criteria)

| Phase | Work | Done when | Gate |
|---|---|---|---|
| **P0** | Rebase local onto origin `97ffe18`; branch `feat/box-wide-memory`. | `git log` shows `97ffe18`; tests green on ZCode. | — |
| **P0.5** | **Empirical dump + packaging smoke test (CRITIC 4).** (a) throwaway hooks `cat > $ZMEM_DATA/hookdump-<event>.json` for SessionStart, UserPromptSubmit, PostToolUse, PostToolUseFailure, Stop, **SubagentStart**, **SubagentStop** — record real field names. (b) Install a **minimal real `.claude-plugin/{plugin,hooks}.json`** (string form) **with a co-present `.zcode-plugin/` dir** and confirm one hook fires under CC. | dump files captured; field names for transcript_path/session_id/agent_type/tool_response confirmed; **CC loads the plugin with the foreign manifest present and the hook fires** (G1/G9 answered before foundation spend). | **G1, G4, G5, G6, G9** |
| **P1** | Shared store: `host.py` resolution chain + box-neutral `~/.zmem`; local-FS guard; SQLITE_BUSY retry wrapper; owner-only perms. **Import (CRITIC 8):** `PRAGMA wal_checkpoint(TRUNCATE)` on the source (or copy its `-wal`/`-shm`) **then** copy `store.sqlite` **and `core.md`** (`store.py:78`) into `~/.zmem`; leave the original `zmem@zaxbyhub` store untouched. | both tools resolve `~/.zmem`; guard rejects a UNC/OneDrive path; all 403 rows (383 live) + core.md present in new store; source store unmodified. | — |
| **P2** | Namespace scheme + **v5 migration (CRITIC BLOCKER 1).** Keys are produced **only** by calling `host.py:resolve_namespace()` against each namespace's **live checkout path** — never a hand-typed literal. If a checkout for a `project:*` namespace isn't on disk, **refuse and report** rather than guess. | v5 applied; a shipped test asserts `resolve_namespace(<checkout>) == migrated_key` for all mappable namespaces; the 107 live opencode-swarm rows recall from **both** `E:\ZCode\opencode-swarm` and `E:\ClaudeCode\opencode-swarm-dev`. | — |
| **P3** | Adapter core in `zmem-launch.js`: detect + canonical env + stdin replay + defensive sentinel parse + envelope translation + encoded-budget truncation + detached background stdio. | **End-to-end test (CRITIC BLOCKER 2):** drive the *actual* `zmem-session-start.sh`/`recall.sh` through the launcher (not a synthetic single-JSON fixture) and assert correct per-host envelope even with consolidate noise on stdout; store resolves from all envs; session-start adds no ~5 s stall. | G3 |
| **P4** | Dual manifest + per-host hooks.json (exec form). Add `.claude-plugin/{plugin,marketplace}.json` + `hooks.claude.json`; keep ZCode side. | both manifests parse; **install each host, confirm hooks fire** (real-install check). | **G1, G8, G9** |
| **P5** | Unified `store.py failures` (transcript|db); rewire reflect/capture-failure. | CC: a failed Bash call triggers reflection sourced from transcript; ZCode: still from db.sqlite. | uses P0.5 |
| **P6** | Replace native memory (CC): Tier0 via ZMem; README/skill document `autoMemoryEnabled:false`; SessionStart detects toggle + nudges if still on. | with toggle off, no native memory double-load; nudge shows when on. | G0 |
| **P7** | `SubagentStart` recall hook + `SubagentStop` reflect hook. | subagent transcript shows injected memory marker; a failing subagent yields a capture prompt. | G5 (via P0.5) |
| **P8** | Convention-capture + session-start budget/Tier0 gating finalized for both hosts. | CC start ≤9K, no AGENTS.md double-inject; ZCode unchanged. | — |
| **P9** | Promotion quality fix + dual-target write. | promoted skill has a distinct non-truncated pushy description; lands in both skills dirs. | — |
| **P10** | Model hardening + CI: lazy-download `minilm.onnx` (checksum, out of git), lexical-cluster fallback; GitHub Actions matrix (windows + ubuntu) running `tests/`. | fresh clone <1MB installs; consolidation works with and without model; CI green both OS. | — |
| **P11** | **Backup/rotation (CRITIC 8c)** for the now-single-point-of-failure box-wide brain: periodic timestamped `store.sqlite` snapshot (checkpointed copy) with retention, plus a `store.py export/import` path for disaster recovery. | a scheduled/hooked snapshot exists; restore from snapshot verified. | — |

P0–P3 are the foundation and unblock everything. **P0.5 now also closes the CC-packaging gates (G1/G9) before any foundation spend.** P4/P6/P7 turn on their gates.

---

## 8. Data migration, secrets, safety

### v5 namespace migration (concrete — CRITIC BLOCKER 1)
- Migration is **data-lossless and reversible**: it only rewrites the `namespace` column (FTS mirror follows via existing triggers); a `meta` key records the pre-migration `{old→new}` map for rollback.
- **Keys are derived, never typed.** `source_ref` is `session:<id>` (`zmem-reflect.sh:278`), so the origin remote is genuinely unrecoverable from stored data — a mapping is unavoidable, but the *new key must be produced by the same `host.py:resolve_namespace()` the runtime uses*, run against each namespace's **live checkout path**. A hand-typed literal that differs from the runtime derivation by so much as `.git`, ssh-vs-https form, case, or trailing slash sends rows to a namespace no session ever queries — silent, and no later phase catches it.
- Operator supplies a `{old-basename → checkout-path}` map (6 entries); the migration calls `resolve_namespace(checkout-path)` to compute each new key and **asserts** it, e.g.:
  - `project:opencode-swarm` ← checkout `E:\ZCode\opencode-swarm` → `resolve_namespace(...)` (remote `github.com/ZaxbyHub/opencode-swarm`)
  - `project:trainingapp`, `project:ragappv3`, `project:zmem` ← their checkouts (zmem remote is `github.com/zaxbysauce/zmem` — a *different org* from ZaxbyHub; do not assume one org)
  - `project:ZCode` (2 rows, spurious parent-dir capture) → operator decision: fold or leave.
  - `user:global` → unchanged.
- **If a checkout path for a `project:*` namespace is not present on disk at migration time, the migration refuses** (reports the namespace, changes nothing) rather than guessing a key.
- Ship a test asserting `resolve_namespace(<checkout>) == migrated_key` for every mapped namespace. Recall keeps a **compat alias** (checks old basename *and* new key) for one release so nothing strands mid-migration; a row has exactly one namespace, so no double-counting.

### Secrets / PII (elevated — box-wide plaintext)
- One store now aggregates every project + both tools: a secret captured in project A is recallable in project B's sessions. The write-time scanner is **advisory only** (regex/entropy).
- Mitigations: owner-only file perms (P1); keep + tighten the scanner; the `memory` skill's "never store secrets" rule stated prominently; **decision below** on encryption-at-rest / stricter redaction.

### Safety / rollback
- Every phase is an isolated PR; the shared store is imported (checkpointed copy), original `zmem@zaxbyhub` store untouched until cutover.
- v5 migration keeps a rollback `{old→new}` map. Reflection/recall hooks are all fail-open (exit 0). Native memory is only turned off by an explicit reversible user setting.
- The box-wide store is a single point of failure → **P11** adds checkpointed snapshots + retention + export/import for disaster recovery.

---

## 9. Fallback (only if G8+G9 fail)
If CC forces `hooks/hooks.json` exactly AND won't expand `${ZCODE_PLUGIN_ROOT}` (so a single hooks.json can't serve both): add a ~15-line `build.mjs` that stamps the host-specific `${*_PLUGIN_ROOT}` + event set into `dist/claude/` and `dist/zcode/` from one `hooks.json.template`. Publish each dist. All logic stays single-source; only packaging gains a step. This is the only scenario that reintroduces a build.

---

## 10. Decisions — RESOLVED (user, 2026-07-23)
1. **Replace strength:** ✅ **Fully replace** native — inject-primary + documented `autoMemoryEnabled:false` user step (P6). No run-alongside as the target.
2. **Store location:** ✅ `C:\Users\Brett\.zmem` (local, non-OneDrive — verified safe).
3. **Namespace re-key:** ✅ **Approve derived v5 migration** — keys from `resolve_namespace()` against live checkouts, assertions + rollback (P2).
4. **Secrets posture:** ✅ **Floor** — advisory scanner + owner-only perms (P1). No encryption-at-rest / extra redaction for now.
5. **Promotion:** ✅ **Dual-write** both `~/.claude/skills` + `~/.zcode/skills` (P9), gated behind the quality fix.

---

## 10b. Execution log
- **P0 ✅** branch `feat/box-wide-memory` off `origin/main` (`97ffe18`); PLAN committed.
- **P0.5 ✅** dual-manifest probe validated on CC 2.1.217: G1 (loads w/ co-present `.zcode-plugin/`), G9-live (custom hooks path), SessionStart+UserPromptSubmit injection reach the model, real field names captured. No build step. (Prompt-type hooks are rejected on SessionStart — the plan's command-type launcher is the correct/only path; the user has a separate prompt-type Stop self-review hook that ZMem's fail-open reflect hook must coexist with — add a `stop_hook_active`-style loop guard.)
- **P1 ✅ CONFIRMED** (executor `7017f61`+`609e5ac`, independent verifier): `host.py` (6-link resolution + `assert_local_fs` + owner-only perms + `busy_retry`), `store.py` refactor, source-safe `import-store.py`, 18/18 tests; `~/.zmem` populated (403/383). Source never opened read-write.

### CUTOVER SEQUENCING (surfaced by P1 verifier — must honor)
The legacy `zmem@zaxbyhub` store is **live** (ZCode keeps writing to it) → `~/.zmem` is a snapshot that drifts, and the dropped auto-detect scan means bare-env `store.py` writes split from deployed-hook writes until P3 sets `ZMEM_DATA`. Therefore:
1. Between now and P3, treat `~/.zmem` as **dev/validation only**; don't rely on it as authoritative and avoid bare-env `store.py` writes you care about (they'd be lost on re-import).
2. At the P3/P4 cutover: **freeze or final `--force` re-import** the legacy store, then **re-run the v5 migration** (idempotent, schema-gated) on the fresh copy, THEN flip both hosts' launchers to `ZMEM_DATA=~/.zmem`. Only after that is `~/.zmem` the single source of truth.
3. P2's v5 migration must therefore be **re-runnable** (already designed schema-gated) — it will run once now for validation and again on the cutover re-import.

## 11. Effort / sequencing
Foundation (P0–P3) is the bulk of the design risk and ~half the work; P4–P7 are gated integrations; P8–P10 are hardening. Suggest executing P0→P3 first, re-verifying gates G1/G8/G9 at P4 on a real dual-install before committing to Shape 1 vs §9.
