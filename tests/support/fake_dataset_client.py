"""Deterministic Hugging Face Hub client double for dataset publish tests
(issue #134). Programmed per the spec's ``hub-client-stub.json`` shape:
``{"parents": [...], "publish_private": true}``.

Sequence semantics (the issue's declared CAS exercises):

- the first ``head()`` returns ``parents[0]`` (``"old"``);
- every ``"conflict"`` token makes the NEXT ``commit()`` attempt raise
  :class:`ParentConflict` (the head moved);
- a retry ``head()`` after the first conflict returns ``"conflict"`` — the
  moved head;
- the first commit attempt that is not scripted to conflict succeeds and
  returns the revision (``"new"``).

So ``["old", "conflict", "new"]`` = one conflict then success (exactly two
commit attempts), and ``["old", "conflict", "conflict"]`` = two conflicts
(exactly two commit attempts, never a third). Counters pin every Hub
interaction.
"""

from __future__ import annotations


class ParentConflict(RuntimeError):
    """The Hub head moved between the caller's read and its commit."""


class FakeDatasetClient:
    def __init__(self, parents=(), existing_manifest=None,
                 revision="new", existing_private=True) -> None:
        parents = list(parents)
        self._conflicts_remaining = parents.count("conflict")
        self._head_values = ["old"] + ["conflict"] * (1 if parents else 0)
        self.revision = revision
        self.existing_manifest = existing_manifest
        self.existing_private = existing_private
        self.head_reads = 0
        self.commit_attempts = 0
        self.commits = 0
        self.private_creates = 0
        self.committed_trees: list[tuple[str, str, dict]] = []

    def head(self, target: str) -> str:
        self.head_reads += 1
        idx = min(self.head_reads - 1, len(self._head_values) - 1)
        if idx < 0:
            return ""
        return self._head_values[idx]

    def manifest(self, target: str) -> dict:
        if self.existing_manifest:
            return dict(self.existing_manifest)
        return {}

    def is_private(self, target: str) -> bool:
        return bool(self.existing_private)

    def create_private(self, target: str) -> None:
        self.private_creates += 1

    def commit(self, target: str, parent: str, tree: dict) -> str:
        self.commit_attempts += 1
        if self._conflicts_remaining > 0:
            self._conflicts_remaining -= 1
            raise ParentConflict(
                f"parent commit {parent!r} did not match: head moved")
        self.commits += 1
        self.committed_trees.append((target, parent, dict(tree)))
        return f"{self.revision}-{self.commits}"
