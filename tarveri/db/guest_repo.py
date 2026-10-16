"""
Guest Verification and Referral Codes repository mixin.
"""

from __future__ import annotations

import logging
from typing import Any

from tarveri.config import now_formatted
from tarveri.db.base import BaseRepoMixin

logger = logging.getLogger("tarveri")


class GuestRepoMixin(BaseRepoMixin):
    async def create_referral_code(self, code: str, guild_id: int, referrer_discord_id: int, expires_at: str) -> None:
        """Saves a newly generated referral code."""
        ts = now_formatted()
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO referral_codes (code, guild_id, referrer_discord_id, created_at, expires_at, status)
                   VALUES (?, ?, ?, ?, ?, 'ACTIVE')""",
                (code, guild_id, referrer_discord_id, ts, expires_at),
            )

    async def get_referral_code(self, code: str, guild_id: int) -> dict[str, Any] | None:
        """Fetches referral code information."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT code, guild_id, referrer_discord_id, created_at, expires_at, used_by_discord_id, used_at, status
               FROM referral_codes WHERE code = ? AND guild_id = ?""",
            (code.strip().upper(), guild_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "code": row[0],
            "guild_id": row[1],
            "referrer_discord_id": row[2],
            "created_at": row[3],
            "expires_at": row[4],
            "used_by_discord_id": row[5],
            "used_at": row[6],
            "status": row[7],
        }

    async def count_active_referrals_for_user(self, guild_id: int, referrer_discord_id: int) -> int:
        """Counts how many active (unexpired, unused) referral codes a student currently holds."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        cursor = await self._conn.execute(
            """SELECT COUNT(*) FROM referral_codes
               WHERE guild_id = ? AND referrer_discord_id = ?
               AND status IN ('ACTIVE', 'PENDING_APPROVAL')
               AND expires_at > ?""",
            (guild_id, referrer_discord_id, ts),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_successful_referrals_by_user(self, referrer_discord_id: int) -> int:
        """Counts total guest referrals successfully used/approved across all guilds for a student."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM referral_codes WHERE referrer_discord_id = ? AND status = 'USED'",
            (referrer_discord_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_user_referrals(
        self, guild_id: int, referrer_discord_id: int, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Lists referral codes created by a user."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT code, created_at, expires_at, used_by_discord_id, status
               FROM referral_codes
               WHERE guild_id = ? AND referrer_discord_id = ?
               ORDER BY created_at DESC LIMIT ?""",
            (guild_id, referrer_discord_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "code": r[0],
                "created_at": r[1],
                "expires_at": r[2],
                "used_by_discord_id": r[3],
                "status": r[4],
            }
            for r in rows
        ]

    async def update_referral_code_status(
        self, code: str, guild_id: int, status: str, used_by_discord_id: int | None = None
    ) -> bool:
        """Updates referral code status (e.g., PENDING_APPROVAL, USED, REJECTED, ACTIVE)."""
        ts = now_formatted()
        async with self.transaction() as conn:
            if used_by_discord_id is not None:
                cursor = await conn.execute(
                    """UPDATE referral_codes
                       SET status = ?, used_by_discord_id = ?, used_at = ?
                       WHERE code = ? AND guild_id = ?""",
                    (status, used_by_discord_id, ts, code.strip().upper(), guild_id),
                )
            else:
                cursor = await conn.execute(
                    """UPDATE referral_codes
                       SET status = ?
                       WHERE code = ? AND guild_id = ?""",
                    (status, code.strip().upper(), guild_id),
                )
            return cursor.rowcount > 0

    async def get_next_guild_ticket_seq(self, guild_id: int) -> int:
        """Returns the next sequence number (1-indexed) for guest tickets in the given guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT COALESCE(MAX(ticket_seq), COUNT(*), 0) FROM guest_tickets WHERE guild_id = ?""",
            (guild_id,),
        )
        row = await cursor.fetchone()
        current_max = row[0] if row and row[0] is not None else 0
        return current_max + 1

    def _row_to_ticket(self, r: tuple) -> dict[str, Any]:
        """Maps a guest_tickets DB row tuple to a structured dictionary."""
        return {
            "ticket_id": r[0],
            "guild_id": r[1],
            "applicant_id": r[2],
            "referrer_id": r[3],
            "channel_id": r[4],
            "referral_code": r[5],
            "reason": r[6],
            "vouch_note": r[7],
            "vouched_by_id": r[8],
            "vouched_at": r[9],
            "status": r[10],
            "created_at": r[11],
            "closed_at": r[12],
            "closed_by_admin_id": r[13],
            "close_reason": r[14],
            "ticket_seq": r[15] if len(r) > 15 and r[15] is not None else r[0],
            "pinged_admin_ids": r[16] if len(r) > 16 else None,
            "last_pinged_at": r[17] if len(r) > 17 else None,
        }

    async def create_guest_ticket(
        self,
        guild_id: int,
        applicant_id: int,
        channel_id: int,
        referrer_id: int | None = None,
        referral_code: str | None = None,
        reason: str | None = None,
        ticket_seq: int | None = None,
        pinged_admin_ids: str | None = None,
        last_pinged_at: str | None = None,
    ) -> int:
        """Creates a guest ticket record and returns its ticket_id."""
        ts = now_formatted()
        seq = ticket_seq if ticket_seq is not None else await self.get_next_guild_ticket_seq(guild_id)
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """INSERT INTO guest_tickets
                   (guild_id, ticket_seq, applicant_id, referrer_id, channel_id, referral_code, reason, status, created_at, pinged_admin_ids, last_pinged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?)""",
                (
                    guild_id,
                    seq,
                    applicant_id,
                    referrer_id,
                    channel_id,
                    referral_code,
                    reason,
                    ts,
                    pinged_admin_ids,
                    last_pinged_at or ts,
                ),
            )
            return cursor.lastrowid or 0

    async def get_guest_ticket_by_channel(self, channel_id: int) -> dict[str, Any] | None:
        """Fetches guest ticket by thread/channel ID."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets WHERE channel_id = ?""",
            (channel_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ticket(row)

    async def get_guest_ticket_by_id(self, ticket_id: int) -> dict[str, Any] | None:
        """Fetches guest ticket by ticket ID."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets WHERE ticket_id = ?""",
            (ticket_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ticket(row)

    async def get_guest_ticket_by_seq(self, guild_id: int, ticket_seq: int) -> dict[str, Any] | None:
        """Fetches guest ticket by guild-scoped sequence number or ticket ID."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets
               WHERE guild_id = ? AND (ticket_seq = ? OR ticket_id = ?)
               ORDER BY ticket_id DESC LIMIT 1""",
            (guild_id, ticket_seq, ticket_seq),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ticket(row)

    async def get_open_guest_ticket_for_applicant(self, guild_id: int, applicant_id: int) -> dict[str, Any] | None:
        """Checks if the user already has an active open ticket in this guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets
               WHERE guild_id = ? AND applicant_id = ? AND status = 'OPEN'""",
            (guild_id, applicant_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ticket(row)

    async def get_latest_guest_ticket_for_user(self, guild_id: int, applicant_id: int) -> dict[str, Any] | None:
        """Fetches the newest guest ticket for an applicant in this guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets
               WHERE guild_id = ? AND applicant_id = ?
               ORDER BY ticket_id DESC LIMIT 1""",
            (guild_id, applicant_id),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return self._row_to_ticket(row)

    async def get_open_guest_tickets(self) -> list[dict[str, Any]]:
        """Fetches all tickets with status 'OPEN' across all guilds for escalation monitoring."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets WHERE status = 'OPEN' ORDER BY ticket_id ASC"""
        )
        rows = await cursor.fetchall()
        return [self._row_to_ticket(r) for r in rows]

    async def update_guest_ticket_escalation(
        self, ticket_id: int, pinged_admin_ids: str, last_pinged_at: str | None = None
    ) -> bool:
        """Updates the list of pinged admin IDs and last pinged timestamp for a ticket."""
        ts = last_pinged_at or now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE guest_tickets
                   SET pinged_admin_ids = ?, last_pinged_at = ?
                   WHERE ticket_id = ?""",
                (pinged_admin_ids, ts, ticket_id),
            )
            return cursor.rowcount > 0

    async def update_guest_ticket_vouch(
        self, ticket_id: int, vouch_note: str, vouched_by_id: int | None = None
    ) -> bool:
        """Saves a student vouch statement along with the voucher ID and timestamp on a guest ticket."""
        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE guest_tickets
                   SET vouch_note = ?, vouched_by_id = ?, vouched_at = ?
                   WHERE ticket_id = ?""",
                (vouch_note, vouched_by_id, ts, ticket_id),
            )
            return cursor.rowcount > 0

    async def close_guest_ticket(
        self,
        ticket_id: int,
        status: str,
        closed_by_admin_id: int | None = None,
        close_reason: str | None = None,
        only_if_open: bool = False,
    ) -> bool:
        """Closes a guest ticket with status ('APPROVED', 'REJECTED', 'EXPIRED'), admin ID, and reason/comment."""
        ts = now_formatted()
        where_clause = "WHERE ticket_id = ? AND status = 'OPEN'" if only_if_open else "WHERE ticket_id = ?"
        async with self.transaction() as conn:
            cursor = await conn.execute(
                f"""UPDATE guest_tickets
                   SET status = ?, closed_at = ?, closed_by_admin_id = ?, close_reason = ?
                   {where_clause}""",
                (status, ts, closed_by_admin_id, close_reason, ticket_id),
            )
            return cursor.rowcount > 0

    async def list_guest_tickets(
        self, guild_id: int, status: str | None = None, limit: int = 10
    ) -> list[dict[str, Any]]:
        """Returns recent guest tickets for a guild, optionally filtered by status."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        if status:
            cursor = await self._conn.execute(
                """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                          reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                          closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
                   FROM guest_tickets
                   WHERE guild_id = ? AND status = ?
                   ORDER BY ticket_id DESC LIMIT ?""",
                (guild_id, status.upper(), limit),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                          reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                          closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
                   FROM guest_tickets
                   WHERE guild_id = ?
                   ORDER BY ticket_id DESC LIMIT ?""",
                (guild_id, limit),
            )
        rows = await cursor.fetchall()
        return [self._row_to_ticket(r) for r in rows]

    async def get_resolved_guest_tickets(self, guild_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Returns recently resolved tickets (APPROVED, REJECTED, CLOSED) for a guild."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT ticket_id, guild_id, applicant_id, referrer_id, channel_id, referral_code,
                      reason, vouch_note, vouched_by_id, vouched_at, status, created_at, closed_at,
                      closed_by_admin_id, close_reason, ticket_seq, pinged_admin_ids, last_pinged_at
               FROM guest_tickets
               WHERE guild_id = ? AND status IN ('APPROVED', 'REJECTED', 'CLOSED')
               ORDER BY ticket_id DESC LIMIT ?""",
            (guild_id, limit),
        )
        rows = await cursor.fetchall()
        return [self._row_to_ticket(r) for r in rows]

    async def cleanup_expired_referrals(self) -> int:
        """Bulk updates all expired active referral codes to EXPIRED status."""
        if not self._conn:
            return 0
        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE referral_codes SET status = 'EXPIRED'
                   WHERE status = 'ACTIVE' AND expires_at <= ?""",
                (ts,),
            )
            return cursor.rowcount

    async def revoke_guest_tickets_for_user(
        self, guild_id: int, user_id: int, status: str = "REVOKED", close_reason: str | None = None
    ) -> int:
        """Revokes all active or approved guest tickets for a user in a guild."""
        if not self._conn:
            return 0
        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE guest_tickets
                   SET status = ?, closed_at = ?, close_reason = ?
                   WHERE guild_id = ? AND applicant_id = ? AND status IN ('OPEN', 'APPROVED')""",
                (status, ts, close_reason, guild_id, user_id),
            )
            return cursor.rowcount

    async def revoke_active_referrals_for_user(self, guild_id: int, user_id: int, status: str = "REVOKED") -> int:
        """Revokes all active referral codes generated by a user in a guild."""
        if not self._conn:
            return 0
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE referral_codes
                   SET status = ?
                   WHERE guild_id = ? AND referrer_discord_id = ? AND status = 'ACTIVE'""",
                (status, guild_id, user_id),
            )
            return cursor.rowcount

    async def cancel_open_tickets_referred_by_user(
        self, guild_id: int, referrer_id: int, close_reason: str = "Referring student left or was removed from server"
    ) -> int:
        """Cancels open review tickets where the referring student left or was banned."""
        if not self._conn:
            return 0
        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE guest_tickets
                   SET status = 'REVOKED', closed_at = ?, close_reason = ?
                   WHERE guild_id = ? AND referrer_id = ? AND status = 'OPEN'""",
                (ts, close_reason, guild_id, referrer_id),
            )
            return cursor.rowcount

    async def get_all_active_referrals(self) -> list[dict[str, Any]]:
        """Fetches all referral codes with status 'ACTIVE' or 'PENDING_APPROVAL' for maintenance reconciliation."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT code, guild_id, referrer_discord_id, created_at, expires_at,
                      used_by_discord_id, used_at, status
               FROM referral_codes WHERE status IN ('ACTIVE', 'PENDING_APPROVAL')"""
        )
        rows = await cursor.fetchall()
        return [
            {
                "code": r[0],
                "guild_id": r[1],
                "referrer_discord_id": r[2],
                "created_at": r[3],
                "expires_at": r[4],
                "used_by_discord_id": r[5],
                "used_at": r[6],
                "status": r[7],
            }
            for r in rows
        ]

    # =========================================================================
    # 🚫 Guild Blacklist Management
    # =========================================================================
