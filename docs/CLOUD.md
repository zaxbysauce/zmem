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

Pick the lowest tier that solves your problem. Tier 1 alone is enough for
"the cloud session should know our conventions." Tier 2 is enough for
occasional cloud sessions that discover something worth keeping. Tier 3 is
for cloud sessions that run often enough that a manual harvest loop is
friction.

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
- `--max-bytes` — hard cap on the generated file size (default 32768), so a
  large store can't silently balloon the pack into something that eats
  context budget.

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
    [--source-ref REF]
  ```
  This gives the cloud session real `recall`/`search`/`list` against that
  data. Skip installing the ONNX embedding runtime on the cloud box — plain
  `recall` (and `--hybrid` when embeddings are unavailable) already falls
  back to FTS5 keyword matching, which is enough for a cloud session that
  mostly needs "what does the project already know", without paying for a
  model runtime on every ephemeral cloud workspace.

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
  That overlap is harmless, not wasteful-but-broken: `ingest-jsonl`'s
  dedup-on-write (below) refreshes an already-known row instead of
  duplicating it, so re-shipping the same rows costs a little bandwidth, not
  correctness. If bandwidth matters, scope `--namespace` tightly to just the
  namespace(s) the cloud session actually wrote to.

- **Local side ingests the outbox** on its own cadence:
  ```bash
  python <store.py> ingest-jsonl --in sync-repo/outbox/<file>.jsonl \
    --source-ref "cloud:<cloud-session-id>"
  ```
  `ingest-jsonl`'s dedup-on-write (the same semantic/exact-match dedup
  `add` uses) means repeated ingestion of the same outbox file — or overlap
  between two cloud sessions' outboxes — is safe: re-ingesting an unchanged
  row refreshes it rather than duplicating it.

`export-jsonl` / `ingest-jsonl` ship in zmem 0.5.0.

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
        env:
          ZMEM_DATA: ${{ github.workspace }}/.zmem-cloud
        run: |
          python zmem/skills/memory/scripts/store.py init
          python zmem/skills/memory/scripts/store.py ingest-jsonl \
            --in sync-repo/sync/memory.jsonl \
            --source-ref "sync:${{ github.run_id }}"

      # ... run your agent task here, recalling/adding against ZMEM_DATA ...

      - name: Export the workspace store to the outbox
        # Re-exports the whole workspace store (export-jsonl has no
        # "only what changed this run" filter), including rows this run
        # ingested from sync/memory.jsonl. Harmless: ingest-jsonl's
        # dedup-on-write refreshes those rather than duplicating them. Add
        # --namespace here to scope it down if bandwidth matters.
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
part of `closeout`) to fold cloud-session writes into the real store.

### Never

- Never point `ZMEM_DATA` at a shared/synced path from a cloud job — always
  a workspace-local temp/ephemeral directory that dies with the job. The
  private sync repo, not a shared `ZMEM_DATA`, is the sync mechanism.
- Never make the sync repo public — it carries plaintext memory content,
  same as the real store (see the Security notes in the main `README.md`).
- Never skip the outbox review discipline just because it's automated: a
  cloud job's `export-jsonl` output is still only as trustworthy as
  whatever wrote it. If a cloud task's memory writes need human review
  before they count, gate the local-side `ingest-jsonl` step behind that
  review (a PR against the sync repo, not a direct push) rather than
  auto-ingesting every outbox on arrival.
