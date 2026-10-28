"""pytest-session store isolation (issue #244).

CI runs each test file directly (`python tests/test_*.py`), where the
guard import inside each file carries the protection. Developers running
`pytest` get the same guard here, once per session, before any test
module imports. See tests/_store_guard.py for the rule and the opt-out.
"""
import _store_guard  # noqa: F401  (import runs the guard)
