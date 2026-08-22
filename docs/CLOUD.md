# Cloud / GitHub Sessions

ZMem's store (`~/.zmem/store.sqlite`) is local-first and box-wide by design —
which means a session with no filesystem access to that box (a Claude Code
cloud session, Claude Code Remote/CCR, a GitHub Action, or any other machine)
cannot read or write it directly. This doc covers the three supported ways
to give those sessions useful memory anyway, roughly in order of effort and
capability:

| Tier | What it gives the cloud session | Effort |
|---|---|---|
| 1 — Memory pack | Read-only snapshot of top project + global memories, committed to the repo | Lowest |
| 2 — closeout-remote + /ingest-harvest | Write path, but staged through human/agent review before it lands | Medium |
| 3 — Private sync repo | Full read (FTS-only recall) + write (outbox) loop, no manual copy/paste | Highest |

**Quick decision tree:**

- Need the cloud session to just *know* your conventions (read-only)? → **Tier 1**
- Need it to occasionally *write back* discoveries for review before they land? → **Tier 2**
- Need a frequent, no-copy-paste read + write loop? → **Tier 3**

Pick the lowest tier that solves your problem. Tier 1 alone is enough for
"the cloud session should know our conventions." Tier 2 is enough for
occasional cloud sessions that discover something worth keeping. Tier 3 is
for cloud sessions that run often enough that a manual harvest loop is
friction.

Codex sandbox note: a local Codex session that cannot write the canonical
shared store path behaves like a cloud/remote session for store access
purposes. Do **not** silently point it at a second physical store. Either:
- add the canonical store directory as a writable root, or
- keep one canonical store on the machine and use a local broker that owns it
  while Codex calls into that broker.

This doc still assumes **single-machine phase 1**: one canonical physical
store path on one machine. The tiers below are hand-off/sync mechanisms, not a
license to let each host invent its own store path.

These cloud-session "Tiers 1-3" are a distinct numbering from zmem's own
memory tiers (Tier 0 core.md, Tier 2 semantic store) described in
`skills/memory/SKILL.md` — the two schemes happen to overlap in the numbers
1 and 2, but they classify different things (cloud hand-off mechanism here vs.
where memory physically lives there) and the overlap is coincidental.

## Tier 1 — Memory pack (read-only snapshot, committed to the repo)

The simplest option: periodically export the store's most relevant memories
for a project into a single markdown file, commit it, and point the repo's
`CLAUDE.md` at it. Any session that can read the repo — cloud or local — then
gets that memory for free, with no store connection at all.

```bash
python <store.py> export-pack \
  --namespace project:github.com/<org>/<repo> \
  --out <repo>/.zmem-pack.md \
  [--project-limit 50] \
  [--global-limit 15] \
  [--min-confidence 0.6] \
  [--max-bytes 32768]
```

- `--namespace` — required; the project namespace to export (see `resolve_namespace`
  in `skills/memory/scripts/host.py` for how that key is derived from a git
  remote).
- `--out` — where to write the pack (default: stdout). Commit this file to
  the repo.
- `--project-limit` — max rows pulled from the project namespace (default 50).
- `--global-limit` — max rows pulled from `user:global` (default 15), so
  portable lessons ride along without drowning out project-specific ones.
- `--min-confidence` — floor below which a memory is not worth shipping into
  a pack a cloud session can't independently verify (default 0.6).
- `--max-bytes` — budget, in UTF-8 bytes, over the **whole rendered pack**
  (default 32768), so a large store can't silently balloon the pack into
  something that eats context budget. Precisely:
  - Each bullet is tested against the pack as rendered so far — structural
    framing (the auto-generated header comment, the title, the section
    headings, any `(none)` placeholder) counts toward the cap, so at an
    absurdly small `--max-bytes` every bullet is omitted and only the
    framing renders. The only text appended after the budget walk is the
    trailing omitted-count note and, if a later section is empty, that
    section's heading + `(none)` — which is why the file can exceed the
    budget by that trailing framing alone, never by an earlier bullet.
  - A bullet is emitted whole or not at all — never truncated.
  - A bullet that would exceed the remaining budget is skipped and counted,
    and the walk continues: later, smaller rows still make it in. One long
    memory does not evict the rest of the pack.
  - Skipped rows are reported in a trailing
    `*(N row(s) omitted to stay within --max-bytes=…)*` note.
- **Exit code** — `export-pack` exits `2` when the pack would be empty (no
  live rows at/above `--min-confidence` in `--namespace` **and** none in
  `user:global` — e.g. a wrong or not-yet-populated namespace), and `0`
  otherwise. Check the exit code before committing `--out`'s file rather than
  assuming a nonempty write.

`export-pack` ships in zmem 0.5.0.

### Wiring it in

1. Generate the pack (command above) and commit `.zmem-pack.md` to the repo
   root (or wherever your `CLAUDE.md` conventions expect generated docs to
   live).
2. Add a line to the repo's `CLAUDE.md`:
   ```markdown
   Read .zmem-pack.md before non-trivial work.
   ```
3. Refresh on a cadence. Two good triggers:
   - **On closeout** — add an `export-pack` call to the end of your local
     `closeout` routine (or a wrapper script) so the pack updates whenever
     new memory is captured for that project.
   - **Scheduled task** — a cron job / Task Scheduler entry / CI workflow
     that runs `export-pack` and opens a PR (or commits directly, if your
     branch protection allows it) on a fixed interval (daily/weekly is
     usually enough — memory doesn't change that fast).

The pack is a **snapshot**, not a live connection: a cloud session reading it
never sees anything captured after the last refresh, and it cannot write
back through this path at all. That's what Tiers 2 and 3 are for.

## Tier 2 — closeout-remote + /ingest-harvest

For a cloud/CCR session that discovers something worth keeping mid-session,
Tier 1 doesn't help (it's read-only and stale). Tier 2 gives a write path,
but staged: the remote session drafts a harvest, and a session **with** store
access reviews and dedups it before anything is actually written. Nothing a
remote session claims is trusted blind.

### The contract

1. **On the cloud/remote session**, at end-of-session (or whenever the user
   says "closeout" there), invoke the **`closeout-remote`** skill
   (`skills/closeout-remote/SKILL.md`). It applies the same high capture bar
   as the local `closeout` skill — zero captures is a fine outcome — and
   emits a single fenced JSON array, one object per captured item:
   ```json
   [
     {
       "namespace": "user:global",
       "type": "lesson",
       "content": "...",
       "tags": "a,b",
       "signal": "test",
       "why": "..."
     }
   ]
   ```
   Namespace defaults to `user:global`; a `project:github.com/<org>/<repo>`
   namespace is only used when verified from a real `github.com` remote URL
   — remote/CCR sessions see a loopback proxy remote instead
   (`http://local_proxy@127.0.0.1:<port>/git/<org>/<repo>`), and the skill
   either derives org/repo from that path (stating so in `why`) or falls
   back to `user:global` when unsure.
2. **Delivery.** The skill pastes the JSON directly in the reply. If the
   remote session has a working branch, it *also* writes the same JSON to
   `zmem-harvest/<short-session-id>.json` in the repo and includes that file
   in the branch — so the harvest survives after the conversation ends and
   can be picked up later (e.g. when the branch is checked out locally, or
   the PR is reviewed).
3. **On a session with store access**, run the **`/ingest-harvest`**
   command (`commands/ingest-harvest.md`) against either the pasted block or
   the `zmem-harvest/*.json` file. That command:
   - parses the harvest,
   - runs a dedup recall (`store.py recall --query ... --namespace ...`)
     for each item and reads the near-hits,
   - merges/trims/drops overlapping items (capturing nothing from an item is
     valid),
   - ingests the survivors via `scripts/ingest_harvest.py <file> --source-ref
     session:<batch-tag>` (or per-item `store.py add`),
   - verifies with a follow-up recall and reports added/merged/dropped with
     reasons.

The capture bar is applied **twice**: once by the remote session drafting
the harvest, and again — against the real store, which the remote session
could never see — by the ingesting agent. That second pass is not optional;
it's the only thing standing between a remote session's self-assessment and
what actually lands in the shared store.

### Why the on-branch copy matters

A pasted JSON block lives only in that conversation's transcript. If the
conversation is closed before anyone runs `/ingest-harvest`, it's gone.
Writing `zmem-harvest/<short-session-id>.json` into the branch means the
harvest rides along with the PR — reviewable, diffable, and pickable-up by
whoever merges or later checks out that branch, with no dependency on the
original conversation still being open.

## Tier 3 — Private sync repo (`export-jsonl` / `ingest-jsonl`)

For cloud sessions that run often enough that Tier 2's manual harvest loop
becomes friction, Tier 3 gives the cloud session its own real (if smaller)
copy of the store, kept in sync through a private git repo — no manual
copy/paste, no waiting for a human to run `/ingest-harvest`.

### Trust model — read this before setting Tier 3 up

**Write access to the sync repo is write access to the content of your local
memory store.** There is no weaker way to state it. Anyone (or anything) that
can push a JSONL file the local side later ingests can:

- insert arbitrary rows into any namespace they name — **including
  `user:global`**, which is injected into the context of *every* future
  session on this box, in every project;
- choose the `confidence` and `signal` those rows carry, and therefore how
  highly they rank in recall and whether they clear `export-pack`'s
  `--min-confidence` floor into a committed pack;
- with `--allow-tombstones`, kill live local rows by id.

That is a persistent instruction-injection channel into your own agent, not
just "some data got synced." zmem reduces the blast radius — incoming rows
are validated and clamped, remote content is sanitized before it is rendered
into a pack, tombstones against live rows are refused unless you explicitly
opt in — but none of that makes an attacker-controlled sync repo safe. It
makes an *honest* sync repo survivable when a row is corrupt.

So: keep the sync repo **private**, restrict who and what can push to it,
prefer a PR (reviewable) over a direct push for the outbox, and give it the
same care you give the store itself. Treat a compromise of the sync repo as a
compromise of the store, and rebuild rather than "clean up" — see the
`--allow-tombstones` rule below for which direction of ingest is trusted.

### The pattern

- **Local side** (the box that owns the real `~/.zmem` store) periodically
  exports the store to JSONL and pushes it to a **private** sync repo (never
  the public/working repo — this is your memory, not shippable content):
  ```bash
  python <store.py> export-jsonl \
    --out sync/memory.jsonl \
    [--namespace NS] \
    [--include-superseded]
  # commit + push sync/memory.jsonl to the private sync repo, on a cadence
  ```
  Omit `--namespace` to export everything; `--include-superseded` also ships
  tombstones (useful if the cloud side should know something was corrected,
  not just see it silently vanish).

- **Cloud side** clones both the `zmem` plugin repo and the private sync
  repo, points `ZMEM_DATA` at a workspace-local directory (never a shared
  path), and initializes + ingests:
  ```bash
  export ZMEM_DATA=/workspace/.zmem-cloud
  python zmem/skills/memory/scripts/store.py init
  python zmem/skills/memory/scripts/store.py ingest-jsonl \
    --in sync-repo/sync/memory.jsonl \
    --allow-tombstones \
    [--source-ref REF]
  ```
  `--allow-tombstones` belongs **here** and only here in this loop: this file
  is your own box's export, the authoritative source for these ids, flowing
  downstream into a throwaway cloud store. A row you superseded locally must
  actually die in the cloud copy, or the cloud session keeps recalling
  something you already corrected. (See "Which direction gets
  `--allow-tombstones`" below.)

  **Exit code** — `ingest-jsonl` exits `2` when `--in` is missing or otherwise
  inaccessible (permission denied, is a directory, etc.) or contains no data
  lines whatsoever (empty or whitespace-only). It exits `0` for every other
  outcome, including a file full of malformed rows: a bad row is never fatal
  to the run, it is counted and reported by line number on stderr, and the
  summary line (`added=… tombstoned=… tombstones_refused=… capture_refused=…
  deduped=… skipped=… malformed=…`) always prints on stdout so "the run finished" and
  "every row landed" are never confused for each other. Check
  `malformed`/`tombstones_refused`/`capture_refused` in that summary, not just the exit code, to
  know whether the file was clean. `capture_refused` is non-zero only under
  `--capture-mode auto` when a row's `source_ref` looked like a secret and was
  refused (not stored) — see the ingest-jsonl capture policy. (A file that is present and readable but
  not valid UTF-8 is a separate, unhandled failure mode outside this: it
  raises past `ingest-jsonl`'s own guard and exits `1` with a traceback.)

  This gives the cloud session real `recall`/`search`/`list` against that
  data. Skip installing the ONNX embedding runtime on the cloud box — plain
  `recall` (and `--hybrid` when embeddings are unavailable) already falls
  back to FTS5 keyword matching, which is enough for a cloud session that
  mostly needs "what does the project already know", without paying for a
  model runtime on every ephemeral cloud workspace.

  **Cross-project recall:** `recall`/`recent`/`search` accept
  `--include-global` (with `--global-limit`, default 3) to union the
  `user:global` tier into a project-scoped query, project-first so global rows
  never crowd out project rows (issue #18). This is what the automatic hooks
  use, so a cloud project-scoped session inherits cross-project lessons
  alongside the project ones. Going unscoped (no `--namespace`) still searches
  everything.

- **Cloud writes** go to an **outbox JSONL** rather than back into the real
  store directly, so the local side controls what actually merges into it
  (same principle as Tier 2's ingesting-agent review, just automated instead
  of manual). `export-jsonl` has no "only what changed this run" filter — it
  exports everything matching `--namespace` (or the whole store, if omitted)
  — so this necessarily re-exports rows the cloud store already ingested
  from the sync file, not just what the cloud session newly wrote:
  ```bash
  python zmem/skills/memory/scripts/store.py export-jsonl \
    --out sync-repo/outbox/<cloud-session-id>.jsonl \
    --namespace <whatever the cloud session wrote to>
  # commit + push to the sync repo
  ```
  That overlap is harmless, not wasteful-but-broken: re-shipping a row the
  receiving store already has does not duplicate it (details under "What
  re-ingesting the same row actually does"). It costs a little bandwidth, not
  correctness. If bandwidth matters, scope `--namespace` tightly to just the
  namespace(s) the cloud session actually wrote to.

- **Local side ingests the outbox** on its own cadence — **without**
  `--allow-tombstones`:
  ```bash
  python <store.py> ingest-jsonl --in sync-repo/outbox/<file>.jsonl \
    --source-ref "cloud:<cloud-session-id>"
  ```
  Repeated ingestion of the same outbox file — or overlap between two cloud
  sessions' outboxes — is safe (again, see below). Same exit-code contract as
  the cloud-side ingest above: `2` only for a missing/inaccessible or
  empty/whitespace-only outbox file, `0` otherwise (malformed rows never fail
  the run). A cloud outbox is remote-authored data: ingest always tags
  `prompt-injection-risk` on rows matching injection patterns (in every mode),
  and `--capture-mode auto` additionally redacts secret-like content and
  refuses rows whose `source_ref` looks like a secret (counted as
  `capture_refused`). Prefer `auto` for an outbox you do not fully control.

- **Re-embed after ingesting an outbox.** Ingest only computes an embedding
  when the embedding runtime is available on the ingesting box; rows that
  land without one are FTS-only until they are embedded, so semantic recall
  and dedup silently under-perform on exactly the newest knowledge:
  ```bash
  python <store.py> reembed
  ```
  Cheap and idempotent — it backfills only live rows that are missing an
  embedding. Put it at the end of your ingest cadence.

### Which direction gets `--allow-tombstones`

`ingest-jsonl` will not let an incoming superseded row kill a **live local
row** unless you pass `--allow-tombstones`. The rule is about *authority over
those ids*, not about convenience:

| Ingest | Flag | Why |
|---|---|---|
| Local store ← your own export (rebuild, restore, box-to-box move) | `--allow-tombstones` | The file is authoritative for those ids; a supersession you made must propagate. |
| Cloud store ← `sync/memory.jsonl` (your box's export) | `--allow-tombstones` | Same: your box owns those ids, the cloud copy is downstream. |
| Local store ← `outbox/*.jsonl` (cloud/remote session output) | **no flag** | A remote session is not authoritative over your local rows. Tombstoning is the one irreversible thing an outbox could ask for. |

Without the flag, such a row is left alone and counted in the summary as
`tombstones_refused=N`, with a single stderr note naming the first refused
id. Nothing is lost — re-run with the flag if you decide the file is
authoritative after all. A brand-new id that arrives *already* tombstoned is
still inserted as history in both modes: it was never live here, so nothing
is destroyed, and keeping it makes future syncs consistent.

### What re-ingesting the same row actually does

Two different mechanisms, easy to conflate:

- **Same `id` already present locally** → the row is **skipped**. Local
  content is never overwritten by a sync import; the local row is not
  mutated, not refreshed, not re-ranked. (The one exception is the tombstone
  path above.) This is what makes re-ingesting the same file idempotent.
- **Different `id`, duplicate content in the same namespace** → the row is
  **deduped**: the same dedup-on-write `add` uses (semantic when embeddings
  are available, exact-match otherwise) merges it into the existing local
  row, taking the higher confidence and the stronger signal and unioning
  tags, instead of inserting a second copy.

Neither case duplicates the row, which is the property the outbox loop relies
on — but only the second one updates anything.

`export-jsonl` / `ingest-jsonl` ship in zmem 0.5.0; `--allow-tombstones` and
the `tombstones_refused=` summary field were added in the hardening pass on
top of it.

### Minimal GitHub Actions snippet (cloud side)

This example syncs a cloud/CI job's memory read side from the private sync
repo before running an agent task, and pushes anything the job wrote back
to the outbox afterward. Adjust checkout refs/secrets to your setup.

```yaml
name: agent-task
on: workflow_dispatch

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout zmem plugin
        uses: actions/checkout@v4
        with:
          repository: zaxbysauce/zmem
          path: zmem

      - name: Checkout private sync repo
        uses: actions/checkout@v4
        with:
          repository: <your-org>/<private-sync-repo>
          token: ${{ secrets.SYNC_REPO_TOKEN }}
          path: sync-repo

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Initialize workspace store and ingest latest sync
        # --allow-tombstones: sync/memory.jsonl is the owning box's own
        # export, authoritative for these ids, and this workspace store is a
        # throwaway downstream copy. The LOCAL side's ingest of outbox/*.jsonl
        # must NOT use the flag -- see "Which direction gets
        # --allow-tombstones".
        env:
          ZMEM_DATA: ${{ github.workspace }}/.zmem-cloud
        run: |
          python zmem/skills/memory/scripts/store.py init
          python zmem/skills/memory/scripts/store.py ingest-jsonl \
            --in sync-repo/sync/memory.jsonl \
            --allow-tombstones \
            --source-ref "sync:${{ github.run_id }}"

      # ... run your agent task here, recalling/adding against ZMEM_DATA ...

      - name: Export the workspace store to the outbox
        # Re-exports the whole workspace store (export-jsonl has no
        # "only what changed this run" filter), including rows this run
        # ingested from sync/memory.jsonl. Harmless: on the local side those
        # ids are already known, so ingest-jsonl skips them -- it does not
        # duplicate them (and does not update them either). Add --namespace
        # here to scope it down if bandwidth matters.
        env:
          ZMEM_DATA: ${{ github.workspace }}/.zmem-cloud
        run: |
          python zmem/skills/memory/scripts/store.py export-jsonl \
            --out sync-repo/outbox/run-${{ github.run_id }}.jsonl

      - name: Commit outbox back to the sync repo
        working-directory: sync-repo
        run: |
          git config user.name "zmem-cloud-sync"
          git config user.email "actions@users.noreply.github.com"
          git add outbox/run-${{ github.run_id }}.jsonl
          git commit -m "cloud outbox: run ${{ github.run_id }}" || echo "nothing to commit"
          git push
```

The local side then runs its own `ingest-jsonl` pass against
`sync-repo/outbox/*.jsonl` on its normal cadence (a scheduled task, or as
part of `closeout`) to fold cloud-session writes into the real store — with
no `--allow-tombstones`, and followed by a `reembed`.

### `ZMEM_PROXY_FORGE_HOST` — namespace keys behind a loopback git proxy

Remote/CCR sessions reach their repo through a local HTTP proxy
(`http://local_proxy@127.0.0.1:<port>/git/<org>/<repo>`). The ephemeral port
would otherwise land in the `project:*` namespace key, fragmenting one repo's
memory across sessions, so `resolve_namespace` collapses a loopback remote
whose path starts with `git/` (matched case-insensitively) to
`<forge>/<org>/<repo>`. The rewrite recognizes all four loopback forms a
proxy URL might use as the host — `127.0.0.1`, `localhost`, `::1`, and
`[::1]` — not just `127.0.0.1`. The env var has
FOUR states:

| `ZMEM_PROXY_FORGE_HOST` | Behavior |
|---|---|
| unset | Rewrite to `github.com/<org>/<repo>` (the default; CCR proxies GitHub today). |
| set, **valid** `host[:port]` | Rewrite to `<value>/<org>/<repo>`, using the value VERBATIM (lowercased, including `:port` if present) -- for a proxy fronting another forge, e.g. `gitlab.example.com` or `gitea.internal:3000`. |
| set, non-empty, **unparseable** | **Disabled**, silently (no stderr -- host.py runs in a hook context). No rewrite; the remote keeps its literal `127.0.0.1:<port>/git/...` (or `localhost`/`::1`/`[::1]`) key. |
| set but **empty** (`""` or whitespace) | **Opt out.** No rewrite; the remote keeps its literal `127.0.0.1:<port>/git/...` (or `localhost`/`::1`/`[::1]`) key. |

A "valid" value must parse as `host[:port]`: the optional port is 1-5 digits,
and the host is one or more dot-separated labels, each matching
`^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?$` (letters, digits, and underscores at each
label's start/end; hyphens allowed only in the middle; no empty labels). Examples:
`gitlab.internal:8443` and `my_forge.internal` are valid; `a..b`, `-a.com`, and
`a-.com` are not. The port is kept verbatim (not stripped) because ordinary,
non-proxy remote normalization also keeps `host:port` in the key
(`https://gitea.example:3000/org/repo` -> `gitea.example:3000/org/repo`) -- if
the proxy override dropped the port, the same forge would key differently
depending on whether it was reached through the proxy or cloned directly.

A value that is non-empty but fails that grammar is **not** silently coerced to
`github.com` -- that would wrongly attribute a private forge's repos to the
public one's namespace -- and it is **not** emitted verbatim into the key either,
since an unparseable value could produce a malformed key. Instead the rewrite
is disabled entirely, exactly like the empty-string opt-out below: falling
through to the legacy loopback key is wrong-but-stable and never collides with
any real forge's namespace.

The empty-string opt-out exists for a genuine **local** git server that serves
repos under a `/git/` prefix — Gitea's default layout, for instance. Those are
not proxies for a public forge, and collapsing them onto `github.com/<org>/<repo>`
would merge an unrelated local repo's memory into a public repo's namespace.
Set `ZMEM_PROXY_FORGE_HOST=` (empty) in that environment.

### Never

- Never point `ZMEM_DATA` at a shared/synced path from a cloud job — always
  a workspace-local temp/ephemeral directory that dies with the job. The
  private sync repo, not a shared `ZMEM_DATA`, is the sync mechanism.
- Never "fix" a Codex writable-root problem by aiming Codex at a different
  local store path than the plugin hosts use. Add the writable root or use a
  broker; do not create split-brain memory on the same machine.
- Never make the sync repo public — it carries plaintext memory content,
  same as the real store (see the Security notes in the main `README.md`),
  and write access to it is write access to the store's content (see "Trust
  model" above).
- Never pass `--allow-tombstones` when ingesting an outbox, or any other file
  a session that is not authoritative over your ids produced.
- Never skip the outbox review discipline just because it's automated: a
  cloud job's `export-jsonl` output is still only as trustworthy as
  whatever wrote it. If a cloud task's memory writes need human review
  before they count, gate the local-side `ingest-jsonl` step behind that
  review (a PR against the sync repo, not a direct push) rather than
  auto-ingesting every outbox on arrival.
