"""
Guild Settings repository mixin.
"""

from __future__ import annotations

import logging
from typing import Any

from tarveri.config import now_formatted
from tarveri.db.base import BaseRepoMixin

logger = logging.getLogger("tarveri")


class GuildSettingsRepoMixin(BaseRepoMixin):
    async def get_guild_settings(
        self, guild_id: int
    ) -> tuple[int | None, int | None, str | None, int | None, str | None, int, int, int] | None:
        """Returns (welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name, require_email_verification, enforce_email_verification, enable_src_roles) for the given guild, or None."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name, COALESCE(require_email_verification, 0), COALESCE(enforce_email_verification, 0), COALESCE(enable_src_roles, 0)
               FROM guild_settings WHERE guild_id = ?""",
            (guild_id,),
        )
        return await cursor.fetchone()

    async def is_guild_email_verification_enabled(self, guild_id: int | Any) -> bool:
        """Checks if student email OTP verification is mandated for the specified guild (default: False / opt-out)."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        if not isinstance(guild_id, int):
            try:
                guild_id = int(guild_id)
            except (ValueError, TypeError):
                return False
        cursor = await self._conn.execute(
            "SELECT require_email_verification FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return False
        return bool(row[0])

    async def set_guild_email_verification(self, guild_id: int | Any, enabled: bool) -> None:
        """Sets the per-guild email verification requirement (opt-in = True, opt-out = False)."""
        if not isinstance(guild_id, int):
            try:
                guild_id = int(guild_id)
            except (ValueError, TypeError):
                return
        ts = now_formatted()
        val = 1 if enabled else 0
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO guild_settings (guild_id, require_email_verification, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       require_email_verification = excluded.require_email_verification,
                       updated_at = excluded.updated_at""",
                (guild_id, val, ts),
            )

    async def is_guild_email_enforcement_enabled(self, guild_id: int | Any) -> bool:
        """Checks if retroactive role removal for non-email-verified members is enforced for this guild (default: False)."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        if not isinstance(guild_id, int):
            try:
                guild_id = int(guild_id)
            except (ValueError, TypeError):
                return False
        cursor = await self._conn.execute(
            "SELECT COALESCE(enforce_email_verification, 0) FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return False
        return bool(row[0])

    async def set_guild_email_enforcement(self, guild_id: int | Any, enabled: bool) -> None:
        """Sets whether retroactive role removal for non-email-verified members is enforced (True or False)."""
        if not isinstance(guild_id, int):
            try:
                guild_id = int(guild_id)
            except (ValueError, TypeError):
                return
        ts = now_formatted()
        val = 1 if enabled else 0
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO guild_settings (guild_id, enforce_email_verification, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       enforce_email_verification = excluded.enforce_email_verification,
                       updated_at = excluded.updated_at""",
                (guild_id, val, ts),
            )

    async def is_guild_src_roles_enabled(self, guild_id: int | Any) -> bool:
        """Checks if faculty SRC role management is explicitly enabled for the specified guild (default: False / opt-out)."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        if not isinstance(guild_id, int):
            try:
                guild_id = int(guild_id)
            except (ValueError, TypeError):
                return False
        cursor = await self._conn.execute(
            "SELECT enable_src_roles FROM guild_settings WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        if not row or row[0] is None:
            return False
        return bool(row[0])

    async def set_guild_src_roles_enabled(self, guild_id: int | Any, enabled: bool) -> None:
        """Enables or disables faculty SRC role management for a guild."""
        if not isinstance(guild_id, int):
            try:
                guild_id = int(guild_id)
            except (ValueError, TypeError):
                return
        val = 1 if enabled else 0
        ts = now_formatted()
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO guild_settings (guild_id, enable_src_roles, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       enable_src_roles = excluded.enable_src_roles,
                       updated_at = excluded.updated_at""",
                (guild_id, val, ts),
            )

    async def get_email_verification_stats(self, guild_id: int | Any = None) -> dict[str, Any]:
        """
        Returns verification telemetry regarding student email verifications.
        Includes total verified students, count verified with email OTP, percentage,
        and guild opt-in status (if guild_id is provided).
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute("SELECT COUNT(*), COUNT(student_email_hash) FROM verifications")
        row = await cursor.fetchone()
        total_students = row[0] if row else 0
        email_verified_students = row[1] if row else 0
        email_verified_rate = (email_verified_students / total_students * 100.0) if total_students > 0 else 0.0

        is_opted_in = False
        if guild_id is not None:
            is_opted_in = await self.is_guild_email_verification_enabled(guild_id)

        # Count total guilds opted in vs opted out
        g_cursor = await self._conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN require_email_verification = 1 THEN 1 ELSE 0 END) FROM guild_settings"
        )
        g_row = await g_cursor.fetchone()
        total_configured_guilds = g_row[0] if g_row and g_row[0] else 0
        opted_in_guilds = g_row[1] if g_row and g_row[1] else 0

        return {
            "total_students": total_students,
            "email_verified_students": email_verified_students,
            "email_verified_rate": round(email_verified_rate, 1),
            "guild_opted_in": is_opted_in,
            "opted_in_guilds": opted_in_guilds,
            "total_configured_guilds": total_configured_guilds,
        }

    async def set_guild_welcome_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Sets or clears the welcome channel ID for a guild."""
        ts = now_formatted()
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO guild_settings (guild_id, welcome_channel_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       welcome_channel_id = excluded.welcome_channel_id,
                       updated_at = excluded.updated_at""",
                (guild_id, channel_id, ts),
            )

    async def set_guild_help_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Sets or clears the help channel ID for a guild."""
        ts = now_formatted()
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO guild_settings (guild_id, help_channel_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       help_channel_id = excluded.help_channel_id,
                       updated_at = excluded.updated_at""",
                (guild_id, channel_id, ts),
            )

    async def set_guild_guest_role(self, guild_id: int, guest_role_name: str | None) -> None:
        """Sets or clears the custom guest role name for a guild (defaults to 'Guest' if None)."""
        ts = now_formatted()
        role_to_set = guest_role_name.strip() if guest_role_name else "Guest"
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO guild_settings (guild_id, guest_role_name, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       guest_role_name = excluded.guest_role_name,
                       updated_at = excluded.updated_at""",
                (guild_id, role_to_set, ts),
            )

    async def set_guild_review_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Sets or clears the designated parent review channel for private guest threads."""
        ts = now_formatted()
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO guild_settings (guild_id, review_channel_id, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       review_channel_id = excluded.review_channel_id,
                       updated_at = excluded.updated_at""",
                (guild_id, channel_id, ts),
            )

    async def set_guild_admin_role(self, guild_id: int, admin_role_name: str | None) -> None:
        """Sets or clears the custom admin role name for a guild."""
        ts = now_formatted()
        role_to_set = admin_role_name.strip() if admin_role_name else None
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO guild_settings (guild_id, admin_role_name, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                       admin_role_name = excluded.admin_role_name,
                       updated_at = excluded.updated_at""",
                (guild_id, role_to_set, ts),
            )

    async def clear_stale_channel_setting(self, guild_id: int, channel_type: str) -> bool:
        """
        Clears a deleted or invalid channel setting (welcome, help, or review) from guild_settings.
        Returns True if a setting was successfully cleared.
        """
        col_map = {
            "welcome": "welcome_channel_id",
            "welcome_channel_id": "welcome_channel_id",
            "help": "help_channel_id",
            "help_channel_id": "help_channel_id",
            "review": "review_channel_id",
            "review_channel_id": "review_channel_id",
            "admin": "admin_role_name",
            "admin_role_name": "admin_role_name",
        }
        normalized = channel_type.strip().lower()
        target_col = col_map.get(normalized)
        if not target_col:
            raise ValueError(f"Invalid channel/setting type: {channel_type}")

        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                f"""UPDATE guild_settings
                    SET {target_col} = NULL, updated_at = ?
                    WHERE guild_id = ? AND {target_col} IS NOT NULL""",
                (ts, guild_id),
            )
            cleared = cursor.rowcount > 0

        if cleared:
            await self.log(
                "INFO",
                "STALE_SETTING_CLEARED",
                f"Cleared stale guild setting '{target_col}' for guild ID {guild_id}",
            )
        return cleared
