"""Episode storage (issue #65, 10.7): bounded session containers.

An episode groups the memories captured during one working session so the
operator can list, close, and summarize them. It is a CONTAINER, not a memory
type — ``episode`` is deliberately absent from ``ALLOWED_TYPES`` (issue #65
out-of-scope list). Tables (schema v13):

    episode(id, namespace, started_at, ended_at, summary_memory_id, token_count)
    episode_memory(episode_id, memory_id, added_at)

Contract highlights:
- ``episode-open`` is what creates rows — there is no empty-table-on-init
  guarantee beyond the schema itself; doctor reports the counts.
- ``episode-add`` requires an OPEN episode and a LIVE memory
  (``superseded_at IS NULL``); memberships are validated at write time only
  and never retroactively removed when a member is later superseded.
- ``episode-close`` is append-only: a closed episode cannot be re-closed or
  re-opened. ``token_count`` is the sum of ``row_token_cost`` (the SAME
  admission cost storelib.inject.apply_token_budget uses) over the LIVE
  members at close time. With ``--summary`` an extractive first-sentence
  summary row is written via ``add_memory`` (capture-mode ``auto`` so the
  shared redaction helper runs on every write path, issue #65 10.8) and linked
  via ``summary_memory_id``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from storelib.inject import row_token_cost
from storelib.promote import _first_sentence
from storelib.schema import now_iso
from storelib.write import _validate_namespace, add_memory

# Structural identity of an episode summary row, mirroring organize's
# "summary,topic" convention (tags are a marker, merged_from is the key).
EPISODE_SUMMARY_TAGS = "summary,episode"
EPISODE_SUMMARY_MAX_CHARS = 8000


class EpisodeError(ValueError):
    """Operational refusal (unknown id, closed episode, tombstoned memory).
    The CLI maps this to a stable exit-2 ``[zmem]`` line like the other
    append-only refusals (supersede/invalidate)."""


def _episode_row(conn: sqlite3.Connection, episode_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM episode WHERE id=?", (episode_id,)
    ).fetchone()


def episode_open(conn: sqlite3.Connection, *, namespace: str) -> dict:
    """Create and return a new open episode for ``namespace``."""
    ns = _validate_namespace(conn, namespace)
    eid = str(uuid.uuid4())

    conn.execute(
        "INSERT INTO episode (id, namespace, started_at) VALUES (?,?,?)",
        (eid, ns, now_iso()),
    )
    conn.commit()
    return episode_get(conn, eid)


def episode_get(conn: sqlite3.Connection, episode_id: str) -> dict:
    """Return one episode dict with its member count."""
    row = _episode_row(conn, episode_id)
    if row is None:
        raise EpisodeError(f"[zmem] episode-get: no episode with id {episode_id}")
    d = dict(row)
    d["member_count"] = conn.execute(
        """SELECT count(*) AS c FROM episode_memory em JOIN memory m ON m.id = em.memory_id WHERE em.episode_id=? AND m.superseded_at IS NULL""",
        (episode_id,),
    ).fetchone()["c"]
    return d


def episode_add(conn: sqlite3.Connection, *, episode_id: str, memory_id: str) -> dict:
    """Attach a LIVE memory to an OPEN episode. Idempotent per pair."""
    ep = _episode_row(conn, episode_id)
    if ep is None:
        raise EpisodeError(
            f"[zmem] episode-add: no episode with id {episode_id}"
        )
    if ep["ended_at"]:
        raise EpisodeError(
            f"[zmem] episode-add: episode {episode_id} is already closed "
            f"(at {ep['ended_at']}); memberships are append-only"
        )
    mem = conn.execute(
        "SELECT id, superseded_at FROM memory WHERE id=?", (memory_id,)
    ).fetchone()
    if mem is None:
        raise EpisodeError(
            f"[zmem] episode-add: no memory with id {memory_id}"
        )
    if mem["superseded_at"] is not None:
        # A tombstoned member would pin dead content into the episode and
        # count its tokens at close — refuse at write time (memberships are
        # never retroactively removed once written).
        raise EpisodeError(
            f"[zmem] episode-add: memory {memory_id} is tombstoned; cannot add "
            "to episode (attach the live replacement row instead)"
        )
    cur = conn.execute(
        "INSERT OR IGNORE INTO episode_memory (episode_id, memory_id, added_at) "
        "VALUES (?,?,?)",
        # added_at written explicitly in the store's canonical now_iso()
        # format — the column DEFAULT (datetime('now')) is only a guard for
        # hand-SQL, and its "YYYY-MM-DD HH:MM:SS" shape would fail the JSONL
        # ISO-8601 validator on round-trip.
        (episode_id, memory_id, now_iso()),
    )
    conn.commit()
    return {
        "episode": episode_id,
        "memory": memory_id,
        "added": bool(cur.rowcount),
    }


def build_extractive_summary(contents: list[str]) -> str:
    """First-sentence extractive summary bullets, one per member content.

    Deterministic (input order preserved — callers pass LIVE members in
    ``added_at`` order), deduplicated on the normalized sentence, capped at
    ``EPISODE_SUMMARY_MAX_CHARS``. Mirrors organize's topic-summary builder
    (same whole-sentence rule) without forking its topic machinery.
    """
    from storelib.schema import normalize_content
    bullets: list[str] = []
    seen: set[str] = set()
    for content in contents:
        first = _first_sentence(content or "", max_len=400)
        norm = normalize_content(first)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        bullets.append(f"- {first}\n")
    out: list[str] = []
    total = 0
    for b in bullets:
        if total + len(b) > EPISODE_SUMMARY_MAX_CHARS:
            break
        out.append(b)
        total += len(b)
    return "".join(out)


def episode_close(
    conn: sqlite3.Connection, *, episode_id: str, with_summary: bool = False
) -> dict:
    """Close an open episode; optionally attach an extractive summary row."""

    ep = _episode_row(conn, episode_id)
    if ep is None:
        raise EpisodeError(f"[zmem] episode-close: no episode with id {episode_id}")
    if ep["ended_at"]:
        raise EpisodeError(
            f"[zmem] episode-close: episode {episode_id} is already closed "
            f"(at {ep['ended_at']}); history is append-only"
        )

    # token_count = admission cost over LIVE members at close time (same
    # row_token_cost the inject budget uses — one definition everywhere).
    members = conn.execute(
        """SELECT m.content FROM episode_memory em
           JOIN memory m ON m.id = em.memory_id
           WHERE em.episode_id=? AND m.superseded_at IS NULL
           ORDER BY em.added_at, em.memory_id""",
        (episode_id,),
    ).fetchall()
    token_count = sum(row_token_cost({"content": r["content"]}) for r in members)

    summary_memory_id = ""
    if with_summary and members:
        # LIVE member ids for the fold rule below (members were selected with
        # superseded_at IS NULL above).
        member_ids = {
            r["memory_id"] for r in conn.execute(
                "SELECT memory_id FROM episode_memory WHERE episode_id=?",
                (episode_id,),
            ).fetchall()
        }
        bullets = build_extractive_summary([r["content"] for r in members])
        if bullets:
            # Route through add_memory so the SHARED capture policy (secret
            # redaction in auto mode, injection tagging) runs on this write
            # path too (issue #65, 10.8). taint trusted_internal: the summary
            # derives from store content, not untrusted input.
            res = add_memory(
                conn,
                namespace=ep["namespace"],
                type_="fact",
                content=bullets,
                tags=EPISODE_SUMMARY_TAGS,
                source_ref=f"episode:{episode_id}",
                signal="none",
                taint="trusted_internal",
                capture_mode="auto",
                link_attr_propagate=False,
            )
            if res.warnings:
                for w in res.warnings:
                    print(f"[zmem] episode summary: {w['message']}")
            if res.deduped and str(res) in member_ids:
                # The summary quoted its member closely enough to dedup-fold
                # INTO that member (a 1-2 member episode's bullets are mostly
                # the members' own first sentences). The member already says
                # what the summary would say: pin the summary link to it and
                # NEVER touch its tags/merged_from (organize's no-clobber
                # rule for structural markers).
                summary_memory_id = str(res)
                # Union (never replace) a discoverable marker tag so the
                # operator can tell this row doubles as the episode summary
                # — get/recall surface tags, not episode.summary_memory_id.
                from storelib.write import _merge_tag_strings
                row = conn.execute(
                    "SELECT tags FROM memory WHERE id=?", (summary_memory_id,)
                ).fetchone()
                if row is not None:
                    conn.execute(
                        "UPDATE memory SET tags=? WHERE id=?",
                        (_merge_tag_strings(row["tags"] or "", "episode-summary"),
                         summary_memory_id),
                    )
                print(
                    f"[zmem] episode summary: folded into member memory "
                    f"{res}; linked as this episode's summary"
                )
            elif res.deduped:
                # Folded into an existing NON-member row: do not pin another
                # row's lineage to this episode (merged_from is the other
                # summary's key — the organize 'summary update folded'
                # convention).
                print(
                    f"[zmem] episode summary: deduped into existing memory "
                    f"{res}; no summary row pinned to episode {episode_id}"
                )
            else:
                summary_memory_id = str(res)
                # Union (never replace) the structural marker so a
                # capture-policy marker like auto-redacted survives, and
                # record the lineage key like organize's summaries
                # (final-critic A4).
                from storelib.write import _merge_tag_strings
                srow = conn.execute(
                    "SELECT tags FROM memory WHERE id=?", (summary_memory_id,)
                ).fetchone()
                pinned_tags = _merge_tag_strings(
                    srow["tags"] if srow else "", EPISODE_SUMMARY_TAGS)
                conn.execute(
                    "UPDATE memory SET tags=?, merged_from=? WHERE id=?",
                    (pinned_tags, episode_id, summary_memory_id),
                )
    conn.execute(
        "UPDATE episode SET ended_at=?, token_count=?, summary_memory_id=? "
        "WHERE id=?",
        (now_iso(), token_count, summary_memory_id, episode_id),
    )
    conn.commit()
    return episode_get(conn, episode_id)


def episode_list(
    conn: sqlite3.Connection, *, namespace: str | None = None, as_json: bool = False
) -> list[dict]:
    """List episodes (newest first) with member counts."""
    params: list = []
    where = ""
    if namespace:
        where = "WHERE e.namespace=?"
        params.append(namespace)
    rows = conn.execute(
        f"""SELECT e.*, (
                SELECT count(*) FROM episode_memory em
                JOIN memory m ON m.id = em.memory_id
                WHERE em.episode_id = e.id AND m.superseded_at IS NULL
            ) AS member_count
            FROM episode e {where}
            ORDER BY e.started_at DESC, e.id""",
        params,
    ).fetchall()
    episodes = [dict(r) for r in rows]
    if as_json:
        print(json.dumps(episodes, indent=2))
        return episodes
    if not episodes:
        print("[zmem] (no episodes)")
        return episodes
    for e in episodes:
        status = "closed" if e["ended_at"] else "open"
        summary = e["summary_memory_id"] or "-"
        print(f"[{e['id']}] {status} ns={e['namespace']} started={e['started_at']} "
              f"members={e['member_count']} tokens={e['token_count']} "
              f"summary={summary}")
    return episodes
