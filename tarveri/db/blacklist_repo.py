"""
Server Blacklist repository mixin.
"""

from __future__ import annotations

import logging
from typing import Any

from tarveri.config import now_formatted
from tarveri.db.base import BaseRepoMixin

logger = logging.getLogger("tarveri")


class BlacklistRepoMixin(BaseRepoMixin):
    async def add_to_blacklist(
        self,
        guild_id: int,
        target_type: str,
        target_value: str,
        display_mask: str | None = None,
        reason: str | None = None,
        blacklisted_by: int = 0,
    ) -> bool:
        """Adds or updates a target (USER, STUDENT_ID, EMAIL) on a guild's blacklist."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        clean_type = target_type.strip().upper()
        clean_value = str(target_value).strip()
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO guild_blacklists (guild_id, target_type, target_value, display_mask, reason, blacklisted_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(guild_id, target_type, target_value) DO UPDATE SET
                       display_mask = excluded.display_mask,
                       reason = excluded.reason,
                       blacklisted_by = excluded.blacklisted_by,
                       created_at = excluded.created_at""",
                (
                    guild_id,
                    clean_type,
                    clean_value,
                    display_mask or clean_value,
                    reason or "No reason specified",
                    blacklisted_by,
                    ts,
                ),
            )
        return True

    async def remove_from_blacklist(
        self,
        guild_id: int,
        target_type: str,
        target_value: str,
    ) -> bool:
        """Removes a target from a guild's blacklist."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        clean_type = target_type.strip().upper()
        clean_value = str(target_value).strip()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """DELETE FROM guild_blacklists
                   WHERE guild_id = ? AND target_type = ? AND target_value = ?""",
                (guild_id, clean_type, clean_value),
            )
            return cursor.rowcount > 0

    async def get_blacklist_match(
        self,
        guild_id: int,
        user_id: int | None = None,
        student_id_hash: str | None = None,
        email_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Returns full match details if a user, student ID hash, or email hash is blacklisted in the guild.
        Returns dict with keys: reason, target_type, target_value, display_mask, created_at, or None.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        try:
            clean_guild_id = int(guild_id)
        except (ValueError, TypeError):
            return None

        conditions = []
        params: list[Any] = [clean_guild_id]

        if user_id is not None:
            try:
                clean_uid = int(user_id)
                conditions.append("(target_type = 'USER' AND target_value = ?)")
                params.append(str(clean_uid))
            except (ValueError, TypeError):
                pass

        if student_id_hash is not None and isinstance(student_id_hash, str) and student_id_hash.strip():
            conditions.append("(target_type = 'STUDENT_ID' AND target_value = ?)")
            params.append(student_id_hash.strip())

        if email_hash is not None and isinstance(email_hash, str) and email_hash.strip():
            conditions.append("(target_type = 'EMAIL' AND target_value = ?)")
            params.append(email_hash.strip())

        if not conditions:
            return None

        query = f"""SELECT reason, target_type, target_value, display_mask, created_at FROM guild_blacklists
                    WHERE guild_id = ? AND ({" OR ".join(conditions)})
                    LIMIT 1"""

        cursor = await self._conn.execute(query, tuple(params))
        row = await cursor.fetchone()
        if row:
            return {
                "reason": row[0],
                "target_type": row[1],
                "target_value": row[2],
                "display_mask": row[3] or row[2],
                "created_at": row[4],
            }
        return None

    async def is_blacklisted(
        self,
        guild_id: int,
        user_id: int | None = None,
        student_id_hash: str | None = None,
        email_hash: str | None = None,
    ) -> tuple[bool, str | None]:
        """
        Checks if a user, student ID hash, or email hash is blacklisted in the specified guild.
        Returns (True, reason) if blacklisted, else (False, None).
        """
        match = await self.get_blacklist_match(
            guild_id=guild_id,
            user_id=user_id,
            student_id_hash=student_id_hash,
            email_hash=email_hash,
        )
        if match:
            return True, match["reason"]
        return False, None

    async def get_guild_blacklist(
        self,
        guild_id: int,
        target_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Retrieves paginated blacklist records for a guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        if target_type:
            cursor = await self._conn.execute(
                """SELECT id, guild_id, target_type, target_value, display_mask, reason, blacklisted_by, created_at
                   FROM guild_blacklists
                   WHERE guild_id = ? AND target_type = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (guild_id, target_type.strip().upper(), limit, offset),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT id, guild_id, target_type, target_value, display_mask, reason, blacklisted_by, created_at
                   FROM guild_blacklists
                   WHERE guild_id = ?
                   ORDER BY id DESC LIMIT ? OFFSET ?""",
                (guild_id, limit, offset),
            )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "guild_id": r[1],
                "target_type": r[2],
                "target_value": r[3],
                "display_mask": r[4],
                "reason": r[5],
                "blacklisted_by": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]

    async def count_guild_blacklist(
        self,
        guild_id: int,
        target_type: str | None = None,
    ) -> int:
        """Returns the total number of blacklisted entries for a guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        if target_type:
            cursor = await self._conn.execute(
                """SELECT COUNT(*) FROM guild_blacklists WHERE guild_id = ? AND target_type = ?""",
                (guild_id, target_type.strip().upper()),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT COUNT(*) FROM guild_blacklists WHERE guild_id = ?""",
                (guild_id,),
            )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def clear_guild_blacklist(self, guild_id: int) -> int:
        """Clears all blacklist entries for a guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM guild_blacklists WHERE guild_id = ?",
                (guild_id,),
            )
            return cursor.rowcount

    # ==========================================
    # 🛡️ 13. Mass Action Approvals & Circuit Breakers
    # ==========================================
