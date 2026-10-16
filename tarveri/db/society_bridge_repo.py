"""
Cross-Bot Society Bridge repository mixin for TARVeri.
"""

from __future__ import annotations

import logging
from typing import Any

from tarveri.config import now_formatted
from tarveri.db.base import BaseRepoMixin

logger = logging.getLogger("tarveri")


class SocietyBridgeRepoMixin(BaseRepoMixin):
    """Manages pairing tokens and cross-bot validation queries with TARClub."""

    async def create_society_pairing(
        self,
        society_guild_id: int,
        token_hash: str,
        paired_by: int,
        notes: str | None = None,
    ) -> None:
        """Register or update a paired society guild with its active HMAC token hash."""
        ts = now_formatted()
        async with self.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO paired_societies (
                    society_guild_id, pairing_token_hash, paired_by_user_id, paired_at, is_active, notes
                ) VALUES (?, ?, ?, ?, 1, ?)
                ON CONFLICT(society_guild_id) DO UPDATE SET
                    pairing_token_hash = excluded.pairing_token_hash,
                    paired_by_user_id = excluded.paired_by_user_id,
                    paired_at = excluded.paired_at,
                    is_active = 1,
                    notes = excluded.notes;
                """,
                (society_guild_id, token_hash, paired_by, ts, notes),
            )

    async def get_paired_society(self, society_guild_id: int) -> dict[str, Any] | None:
        """Fetch pairing record for a society guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """
            SELECT society_guild_id, pairing_token_hash, paired_by_user_id, paired_at, is_active, notes
            FROM paired_societies
            WHERE society_guild_id = ?;
            """,
            (society_guild_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "society_guild_id": row[0],
            "pairing_token_hash": row[1],
            "paired_by_user_id": row[2],
            "paired_at": row[3],
            "is_active": bool(row[4]),
            "notes": row[5],
        }

    async def list_paired_societies(self) -> list[dict[str, Any]]:
        """List all registered and paired society guilds."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """
            SELECT society_guild_id, paired_by_user_id, paired_at, is_active, notes
            FROM paired_societies
            ORDER BY paired_at DESC;
            """
        )
        rows = await cursor.fetchall()
        return [
            {
                "society_guild_id": r[0],
                "paired_by_user_id": r[1],
                "paired_at": r[2],
                "is_active": bool(r[3]),
                "notes": r[4],
            }
            for r in rows
        ]

    async def revoke_society_pairing(self, society_guild_id: int) -> bool:
        """Deactivate a society guild pairing."""
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE paired_societies
                SET is_active = 0
                WHERE society_guild_id = ?;
                """,
                (society_guild_id,),
            )
            return cursor.rowcount > 0

    async def get_student_eligibility_for_society(self, discord_user_id: int) -> dict[str, Any] | None:
        """Fetch verified student profile specifically for TARClub committee validation.

        Returns None if not verified as a TAR UMT student.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """
            SELECT faculty_code, campus_code, level_code, is_alumni, graduated_year,
                   programme_code, verified_at, card_expiry_date
            FROM verifications
            WHERE discord_user_id = ?;
            """,
            (discord_user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "discord_user_id": discord_user_id,
            "is_verified": True,
            "faculty_code": row[0],
            "campus_code": row[1],
            "level_code": row[2],
            "is_alumni": bool(row[3]) if row[3] is not None else False,
            "graduated_year": row[4],
            "programme_code": row[5],
            "verified_at": row[6],
            "card_expiry_date": row[7],
        }
