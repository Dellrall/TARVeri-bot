"""
Student Verification, Alumni, and Audit Logging repository mixin.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from tarveri.config import get_configured_tz, now_formatted
from tarveri.db.base import BaseRepoMixin

logger = logging.getLogger("tarveri")


class VerificationRepoMixin(BaseRepoMixin):
    async def get_verification_by_user(self, discord_user_id: int) -> tuple[str, str, str] | None:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT student_id_hash, faculty_code, verified_at FROM verifications WHERE discord_user_id = ?",
            (discord_user_id,),
        )
        return await cursor.fetchone()

    async def get_verification_by_id_hash(self, student_id_hash: str) -> tuple[int] | None:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT discord_user_id FROM verifications WHERE student_id_hash = ?",
            (student_id_hash,),
        )
        return await cursor.fetchone()

    async def get_verification_by_email_hash(self, email_hash: str) -> tuple[int] | None:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT discord_user_id FROM verifications WHERE student_email_hash = ?",
            (email_hash,),
        )
        return await cursor.fetchone()

    async def record_verification(
        self,
        discord_user_id: int,
        student_id_hash: str,
        faculty_code: str,
        campus_code: str | None = None,
        level_code: str | None = None,
        card_expiry_date: str | None = None,
        lifecycle_prompt_status: str = "ACTIVE",
        student_email_encrypted: str | None = None,
        student_email_hash: str | None = None,
        programme_code: str | None = None,
    ) -> None:
        ts = now_formatted()
        async with self.transaction() as conn:
            await conn.execute(
                """INSERT INTO verifications (
                       discord_user_id, student_id_hash, faculty_code, verified_at,
                       campus_code, level_code, card_expiry_date, lifecycle_prompt_status,
                       student_email_encrypted, student_email_hash, programme_code
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    discord_user_id,
                    student_id_hash,
                    faculty_code,
                    ts,
                    campus_code,
                    level_code,
                    card_expiry_date,
                    lifecycle_prompt_status,
                    student_email_encrypted,
                    student_email_hash,
                    programme_code,
                ),
            )

    async def get_verification_details(self, discord_user_id: int) -> dict[str, Any] | None:
        """Retrieves complete verification details (faculty, campus, level, expiry, alumni status, email, programme code) for a user."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT student_id_hash, faculty_code, verified_at, is_alumni, graduated_year,
                      programme, graduated_at, campus_code, level_code, card_expiry_date,
                      lifecycle_prompt_status, last_lifecycle_prompt_at,
                      student_email_encrypted, student_email_hash, programme_code
               FROM verifications WHERE discord_user_id = ?""",
            (discord_user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "student_id_hash": row[0],
            "faculty_code": row[1],
            "verified_at": row[2],
            "is_alumni": bool(row[3]) if row[3] is not None else False,
            "graduated_year": row[4],
            "programme": row[5],
            "graduated_at": row[6],
            "campus_code": row[7],
            "level_code": row[8],
            "card_expiry_date": row[9],
            "lifecycle_prompt_status": row[10] or "ACTIVE",
            "last_lifecycle_prompt_at": row[11],
            "student_email_encrypted": row[12],
            "student_email_hash": row[13],
            "programme_code": row[14],
        }

    async def backfill_legacy_verifications(self, default_campus: str = "W") -> int:
        """Backfills legacy verifications missing campus_code or programme_code."""
        total_backfilled = 0
        async with self.transaction() as conn:
            cursor1 = await conn.execute(
                """UPDATE verifications
                   SET campus_code = ?
                   WHERE campus_code IS NULL""",
                (default_campus,),
            )
            total_backfilled += cursor1.rowcount
            cursor2 = await conn.execute(
                """UPDATE verifications
                   SET programme_code = campus_code || faculty_code || COALESCE(level_code, 'R')
                   WHERE programme_code IS NULL AND campus_code IS NOT NULL AND faculty_code IS NOT NULL"""
            )
            total_backfilled += cursor2.rowcount
            return total_backfilled

    async def update_verification_details(
        self,
        discord_user_id: int,
        campus_code: str | None = None,
        level_code: str | None = None,
        card_expiry_date: str | None = None,
        lifecycle_prompt_status: str | None = None,
        last_lifecycle_prompt_at: str | None = None,
        student_email_encrypted: str | None = None,
        student_email_hash: str | None = None,
        programme_code: str | None = None,
    ) -> bool:
        """Updates optional fields for an existing verified student."""
        updates: list[str] = []
        params: list[Any] = []
        if campus_code is not None:
            updates.append("campus_code = ?")
            params.append(campus_code)
        if level_code is not None:
            updates.append("level_code = ?")
            params.append(level_code)
        if card_expiry_date is not None:
            updates.append("card_expiry_date = ?")
            params.append(card_expiry_date)
        if lifecycle_prompt_status is not None:
            updates.append("lifecycle_prompt_status = ?")
            params.append(lifecycle_prompt_status)
        if last_lifecycle_prompt_at is not None:
            updates.append("last_lifecycle_prompt_at = ?")
            params.append(last_lifecycle_prompt_at)
        if student_email_encrypted is not None:
            updates.append("student_email_encrypted = ?")
            params.append(student_email_encrypted)
        if student_email_hash is not None:
            updates.append("student_email_hash = ?")
            params.append(student_email_hash)
        if programme_code is not None:
            updates.append("programme_code = ?")
            params.append(programme_code)
        if not updates:
            return False
        params.append(discord_user_id)
        sql = f"UPDATE verifications SET {', '.join(updates)} WHERE discord_user_id = ?"
        async with self.transaction() as conn:
            cursor = await conn.execute(sql, tuple(params))
            return cursor.rowcount > 0

    async def record_academic_transition(
        self,
        discord_user_id: int,
        from_id_hash: str,
        from_faculty_code: str,
        from_campus_code: str,
        from_level_code: str,
        to_id_hash: str,
        to_faculty_code: str,
        to_campus_code: str,
        to_level_code: str,
        notes: str | None = None,
    ) -> int:
        """Archives a student's previous academic level/faculty profile into transition history."""
        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """INSERT INTO verification_transitions (
                       discord_user_id, from_id_hash, from_faculty_code, from_campus_code, from_level_code,
                       to_id_hash, to_faculty_code, to_campus_code, to_level_code, transitioned_at, notes
                   )
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    discord_user_id,
                    from_id_hash,
                    from_faculty_code,
                    from_campus_code,
                    from_level_code,
                    to_id_hash,
                    to_faculty_code,
                    to_campus_code,
                    to_level_code,
                    ts,
                    notes,
                ),
            )
            return cursor.lastrowid or 0

    async def get_academic_transitions_for_user(self, discord_user_id: int) -> list[dict[str, Any]]:
        """Retrieves full academic progression history for a student."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT id, discord_user_id, from_id_hash, from_faculty_code, from_campus_code, from_level_code,
                      to_id_hash, to_faculty_code, to_campus_code, to_level_code, transitioned_at, notes
               FROM verification_transitions
               WHERE discord_user_id = ?
               ORDER BY id ASC""",
            (discord_user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "discord_user_id": r[1],
                "from_id_hash": r[2],
                "from_faculty_code": r[3],
                "from_campus_code": r[4],
                "from_level_code": r[5],
                "to_id_hash": r[6],
                "to_faculty_code": r[7],
                "to_campus_code": r[8],
                "to_level_code": r[9],
                "transitioned_at": r[10],
                "notes": r[11],
            }
            for r in rows
        ]

    async def update_verification_profile(
        self,
        discord_user_id: int,
        student_id_hash: str | None = None,
        faculty_code: str | None = None,
        campus_code: str | None = None,
        level_code: str | None = None,
        card_expiry_date: str | None = None,
        lifecycle_prompt_status: str | None = None,
        last_lifecycle_prompt_at: str | None = None,
        student_email_encrypted: str | None = None,
        student_email_hash: str | None = None,
        programme_code: str | None = None,
    ) -> bool:
        """Updates the active verification record during an academic level transition or lifecycle prompt update."""
        updates: list[str] = []
        params: list[Any] = []

        if student_id_hash is not None:
            updates.append("student_id_hash = ?")
            params.append(student_id_hash)
            updates.append("is_alumni = 0")
            updates.append("graduated_year = NULL")
            updates.append("programme = NULL")
            updates.append("graduated_at = NULL")
            updates.append("verified_at = ?")
            params.append(now_formatted())

        if faculty_code is not None:
            updates.append("faculty_code = ?")
            params.append(faculty_code)

        if campus_code is not None:
            updates.append("campus_code = ?")
            params.append(campus_code)

        if level_code is not None:
            updates.append("level_code = ?")
            params.append(level_code)

        if programme_code is not None:
            updates.append("programme_code = ?")
            params.append(programme_code)

        if card_expiry_date is not None:
            updates.append("card_expiry_date = ?")
            params.append(card_expiry_date)

        if lifecycle_prompt_status is not None:
            updates.append("lifecycle_prompt_status = ?")
            params.append(lifecycle_prompt_status)

        if last_lifecycle_prompt_at is not None:
            updates.append("last_lifecycle_prompt_at = ?")
            params.append(last_lifecycle_prompt_at)

        if student_email_encrypted is not None:
            updates.append("student_email_encrypted = ?")
            params.append(student_email_encrypted)

        if student_email_hash is not None:
            updates.append("student_email_hash = ?")
            params.append(student_email_hash)

        if not updates:
            return False

        params.append(discord_user_id)
        sql = f"UPDATE verifications SET {', '.join(updates)} WHERE discord_user_id = ?"
        async with self.transaction() as conn:
            cursor = await conn.execute(sql, tuple(params))
            return cursor.rowcount > 0

    async def get_expired_student_verifications(self, before_date: str | None = None) -> list[dict[str, Any]]:
        """
        Retrieves active verified students (is_alumni = 0) whose card_expiry_date is on or before before_date.
        Defaults before_date to today (YYYY-MM-DD in Asia/Kuala_Lumpur).
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        if not before_date:
            from tarveri.config import get_configured_tz

            now_dt = datetime.now(get_configured_tz())
            before_date = now_dt.strftime("%Y-%m-%d")

        cursor = await self._conn.execute(
            """SELECT discord_user_id, student_id_hash, faculty_code, campus_code, level_code,
                      card_expiry_date, lifecycle_prompt_status, last_lifecycle_prompt_at, verified_at
               FROM verifications
               WHERE is_alumni = 0
                 AND card_expiry_date IS NOT NULL
                 AND card_expiry_date <= ?
               ORDER BY card_expiry_date ASC""",
            (before_date,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "discord_user_id": r[0],
                "student_id_hash": r[1],
                "faculty_code": r[2],
                "campus_code": r[3],
                "level_code": r[4],
                "card_expiry_date": r[5],
                "lifecycle_prompt_status": r[6] or "ACTIVE",
                "last_lifecycle_prompt_at": r[7],
                "verified_at": r[8],
            }
            for r in rows
        ]

    async def record_alumni_claim(
        self,
        discord_user_id: int,
        graduated_year: int,
        programme: str | None = None,
    ) -> bool:
        """Records a verified student's transition to alumni status."""
        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE verifications
                   SET is_alumni = 1, graduated_year = ?, programme = ?, graduated_at = ?
                   WHERE discord_user_id = ?""",
                (graduated_year, programme, ts, discord_user_id),
            )
            return cursor.rowcount > 0

    async def revoke_alumni_status(self, discord_user_id: int) -> bool:
        """Revokes alumni status from a student in the database."""
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """UPDATE verifications
                   SET is_alumni = 0, graduated_year = NULL, programme = NULL, graduated_at = NULL
                   WHERE discord_user_id = ?""",
                (discord_user_id,),
            )
            return cursor.rowcount > 0

    async def get_alumni_info_by_user(self, discord_user_id: int) -> dict[str, Any] | None:
        """Retrieves alumni details for a user if they have claimed alumni status."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT is_alumni, graduated_year, programme, graduated_at, faculty_code, verified_at
               FROM verifications WHERE discord_user_id = ?""",
            (discord_user_id,),
        )
        row = await cursor.fetchone()
        if not row or not row[0]:
            return None
        return {
            "is_alumni": bool(row[0]),
            "graduated_year": row[1],
            "programme": row[2],
            "graduated_at": row[3],
            "faculty_code": row[4],
            "verified_at": row[5],
        }

    async def get_all_alumni_user_ids(self) -> list[int]:
        """Returns a list of all user IDs with active alumni status."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute("SELECT discord_user_id FROM verifications WHERE is_alumni = 1")
        rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def count_alumni(self) -> int:
        """Returns total count of registered alumni students."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute("SELECT COUNT(*) FROM verifications WHERE is_alumni = 1")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def delete_verification(self, discord_user_id: int) -> bool:
        """Unlinks a Discord account from its student ID. Returns True if record existed."""
        async with self.transaction() as conn:
            cursor = await conn.execute("DELETE FROM verifications WHERE discord_user_id = ?", (discord_user_id,))
            return cursor.rowcount > 0

    async def total_verified(self) -> int:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute("SELECT COUNT(*) FROM verifications")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_all_verifications(self) -> list[tuple[int, str, str, str]]:
        """Returns all verified student records as (discord_user_id, student_id_hash, faculty_code, verified_at)."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT discord_user_id, student_id_hash, faculty_code, verified_at FROM verifications"
        )
        return await cursor.fetchall()

    async def counts_by_faculty(self) -> list[tuple[str, int]]:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT faculty_code, COUNT(*) FROM verifications GROUP BY faculty_code ORDER BY COUNT(*) DESC"
        )
        return await cursor.fetchall()

    async def verified_in_last(self, hours: int) -> int:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cutoff = (datetime.now(get_configured_tz()) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        cursor = await self._conn.execute(
            "SELECT COUNT(*) FROM verifications WHERE verified_at >= ?",
            (cutoff,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def recent_audit(
        self, limit: int = 10, offset: int = 0, event_type: str | None = None
    ) -> list[tuple[str, str, str, str | None, int | None, str]]:
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        if event_type:
            cursor = await self._conn.execute(
                """SELECT timestamp, level, event_type, guild_name, user_id, message
                   FROM audit_log
                   WHERE event_type = ?
                   ORDER BY id DESC
                   LIMIT ? OFFSET ?""",
                (event_type, limit, offset),
            )
        else:
            cursor = await self._conn.execute(
                """SELECT timestamp, level, event_type, guild_name, user_id, message
                   FROM audit_log
                   ORDER BY id DESC
                   LIMIT ? OFFSET ?""",
                (limit, offset),
            )
        return await cursor.fetchall()

    async def get_user_audit_logs(
        self, user_id: int, limit: int = 10, offset: int = 0
    ) -> list[tuple[str, str, str, str | None, int | None, str]]:
        """Retrieves audit log entries specifically associated with a user ID."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT timestamp, level, event_type, guild_name, user_id, message
               FROM audit_log
               WHERE user_id = ?
               ORDER BY id DESC
               LIMIT ? OFFSET ?""",
            (user_id, limit, offset),
        )
        return await cursor.fetchall()
