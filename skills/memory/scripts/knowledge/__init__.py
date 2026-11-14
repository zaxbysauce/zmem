"""Knowledge companion package (PR1).

Submodules (import them directly — the package ``__init__`` stays light so
importing ``knowledge.documents`` or ``knowledge.journal`` never pulls the
storelib import chain by itself):

- ``knowledge.config``    — strict reader for the optional ``knowledge``
  object of the store-side ``config.json`` (off by default; loads through
  ``storelib.config.load_config``).
- ``knowledge.documents`` — content admission (secret scan -> LF
  normalize -> full SHA-256), logical identity (``knowledge:<target>:<key>``),
  deterministic immutable locators, and the two target clients
  (``MarkdownStore`` filesystem vault, ``KBClient`` fleet HTTP endpoint).
- ``knowledge.journal``   — the metadata-only, bounded, atomic,
  crash-recovery intent journal.
- ``knowledge.workflow``  — the end-to-end capture pipeline (screen ->
  journal -> create -> pointer CAS -> applied), read/check/status, and the
  local/remote mode law (remote NEVER opens a local store).
- ``knowledge.delivery`` — the capability-gated SEPARATE delivery queue
  (metadata only) plus the workflow's receipt/intent sidecar; the hub must
  advertise ``knowledge_link_v1`` before anything is enqueued, and the
  core op queue is never used.

The 2026-11-13 approved plan gates everything behind explicit operator
configuration; with no ``knowledge`` config object the package is inert.
"""

__all__ = ["config", "documents", "journal", "workflow", "delivery"]
