"""zmem fleet package (Workstream T, issue #287).

The per-machine broker and the shared transport every harness adapter uses
for remote passive memory.  Importable as ``fleet`` with
``skills/memory/scripts`` on ``sys.path``; the Hermes plugin re-exports
``fleet.transport`` from ``hermes-plugin/transport.py``.
"""
