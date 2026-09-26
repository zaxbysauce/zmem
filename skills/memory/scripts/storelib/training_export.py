"""Compatibility entry point for the issue #135 training exporter.

The implementation lives in :mod:`storelib.training` so older local callers
that used the approved-plan name keep working while the CLI can import the
explicit ``training_export`` module.
"""

from storelib.training import (  # noqa: F401
    PREFERENCE_COLUMNS,
    SFT_COLUMNS,
    TrainingExportError,
    build_preference_rows,
    build_sft_rows,
    write_training_views,
)

__all__ = [
    "PREFERENCE_COLUMNS", "SFT_COLUMNS", "TrainingExportError",
    "build_preference_rows", "build_sft_rows", "write_training_views",
]
