"""
Pending Mass Actions and Bounced Emails repository mixin.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tarveri.config import now_formatted
from tarveri.db.base import BaseRepoMixin

logger = logging.getLogger("tarveri")


class MassActionRepoMixin(BaseRepoMixin):
    async def create_pending_mass_action(
        self,
        action_id: str,
        guild_id: int,
        action_type: str,
        user_ids: list[int],
        reason: str,
    ) -> bool:
        """Stages a mass action requiring explicit administrator approval before execution."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        user_ids_json = json.dumps(user_ids)
        try:
            async with self.transaction() as conn:
                await conn.execute(
                    """INSERT INTO pending_mass_actions (
                        action_id, guild_id, action_type, user_ids_json, reason, created_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING')""",
                    (action_id, int(guild_id), action_type, user_ids_json, reason, ts),
                )
            return True
        except Exception:
            return False

    async def get_pending_mass_action(self, action_id: str) -> dict[str, Any] | None:
        """Retrieves a staged mass action by its unique identifier."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT action_id, guild_id, action_type, user_ids_json, reason, created_at, status, decided_by_id, decided_at
               FROM pending_mass_actions WHERE action_id = ?""",
            (action_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            uids = json.loads(row[3])
        except Exception:
            uids = []
        return {
            "action_id": row[0],
            "guild_id": row[1],
            "action_type": row[2],
            "user_ids": uids,
            "reason": row[4],
            "created_at": row[5],
            "status": row[6],
            "decided_by_id": row[7],
            "decided_at": row[8],
        }

    async def get_active_pending_mass_action_for_guild(
        self,
        guild_id: int,
        action_type: str = "EMAIL_POLICY_REVOCATION",
    ) -> dict[str, Any] | None:
        """Checks if there is an active PENDING mass action of the given type for a guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT action_id, guild_id, action_type, user_ids_json, reason, created_at, status, decided_by_id, decided_at
               FROM pending_mass_actions
               WHERE guild_id = ? AND action_type = ? AND status = 'PENDING'
               ORDER BY created_at DESC LIMIT 1""",
            (int(guild_id), action_type),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        try:
            uids = json.loads(row[3])
        except Exception:
            uids = []
        return {
            "action_id": row[0],
            "guild_id": row[1],
            "action_type": row[2],
            "user_ids": uids,
            "reason": row[4],
            "created_at": row[5],
            "status": row[6],
            "decided_by_id": row[7],
            "decided_at": row[8],
        }

    async def update_pending_mass_action_status(
        self,
        action_id: str,
        status: str,
        decided_by_id: int | None = None,
    ) -> bool:
        """Updates the status of a pending mass action (e.g. APPROVED, REJECTED, EXPIRED)."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE pending_mass_actions
                   SET status = ?, decided_by_id = ?, decided_at = ?
                   WHERE action_id = ?""",
                (status, decided_by_id, ts, action_id),
            )
            return cursor.rowcount > 0

    async def list_pending_mass_actions(
        self,
        guild_id: int | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Lists pending mass actions, optionally filtered by guild and/or status."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        conditions = []
        params: list[Any] = []

        if guild_id is not None:
            conditions.append("guild_id = ?")
            params.append(int(guild_id))

        if status is not None:
            conditions.append("status = ?")
            params.append(status)

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        query = f"""SELECT action_id, guild_id, action_type, user_ids_json, reason, created_at, status, decided_by_id, decided_at
                    FROM pending_mass_actions
                    {where_clause}
                    ORDER BY created_at DESC LIMIT ?"""
        params.append(limit)

        cursor = await self._conn.execute(query, tuple(params))
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            try:
                uids = json.loads(row[3])
            except Exception:
                uids = []
            result.append(
                {
                    "action_id": row[0],
                    "guild_id": row[1],
                    "action_type": row[2],
                    "user_ids": uids,
                    "reason": row[4],
                    "created_at": row[5],
                    "status": row[6],
                    "decided_by_id": row[7],
                    "decided_at": row[8],
                }
            )
        return result

    async def record_bounced_email(
        self,
        email_hash: str,
        email_encrypted: str | None = None,
        bounce_code: int | None = None,
        bounce_reason: str | None = None,
    ) -> None:
        """Records or updates a bounced email address."""
        ts = now_formatted()
        async with self.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO bounced_emails (email_hash, email_encrypted, bounce_code, bounce_reason, detected_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(email_hash) DO UPDATE SET
                    email_encrypted = COALESCE(excluded.email_encrypted, bounced_emails.email_encrypted),
                    bounce_code = COALESCE(excluded.bounce_code, bounced_emails.bounce_code),
                    bounce_reason = COALESCE(excluded.bounce_reason, bounced_emails.bounce_reason),
                    detected_at = excluded.detected_at;
                """,
                (email_hash, email_encrypted, bounce_code, bounce_reason, ts),
            )

    async def is_email_bounced(self, email_hash: str) -> bool:
        """Checks if an email hash is recorded as bounced."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT 1 FROM bounced_emails WHERE email_hash = ? LIMIT 1;",
            (email_hash,),
        )
        return (await cursor.fetchone()) is not None

    async def get_bounced_email(self, email_hash: str) -> dict[str, Any] | None:
        """Retrieves details of a bounced email."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT email_hash, email_encrypted, bounce_code, bounce_reason, detected_at FROM bounced_emails WHERE email_hash = ?;",
            (email_hash,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "email_hash": row[0],
            "email_encrypted": row[1],
            "bounce_code": row[2],
            "bounce_reason": row[3],
            "detected_at": row[4],
        }

    async def list_bounced_emails(self, limit: int = 100) -> list[dict[str, Any]]:
        """Lists recently detected bounced emails."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT email_hash, email_encrypted, bounce_code, bounce_reason, detected_at FROM bounced_emails ORDER BY id DESC LIMIT ?;",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "email_hash": r[0],
                "email_encrypted": r[1],
                "bounce_code": r[2],
                "bounce_reason": r[3],
                "detected_at": r[4],
            }
            for r in rows
        ]

    async def remove_bounced_email(self, email_hash: str) -> bool:
        """Removes an email from the bounced email registry."""
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM bounced_emails WHERE email_hash = ?;",
                (email_hash,),
            )
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Uptime Tracking & Downtime Analytics
    # ------------------------------------------------------------------
