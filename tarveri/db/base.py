"""
Base repository mixin defining database connection contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


class BaseRepoMixin:
    """Base mixin providing type awareness of SQLite connection to repository slices."""

    _conn: aiosqlite.Connection | None
