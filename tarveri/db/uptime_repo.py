"""
Uptime, Heartbeat, and Outage SLA repository mixin.
"""

from __future__ import annotations

import logging
from typing import Any

from tarveri.db.base import BaseRepoMixin

logger = logging.getLogger("tarveri")


class UptimeRepoMixin(BaseRepoMixin):
    async def get_uptime_heartbeat(self) -> dict[str, Any] | None:
        """Retrieves the latest service heartbeat and session status."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT session_id, started_at, last_heartbeat_at, clean_shutdown_at, system_version FROM service_uptime_heartbeat WHERE id = 1;"
        )
        row = await cursor.fetchone()
        if not row:
            return None
        return {
            "session_id": row[0],
            "started_at": row[1],
            "last_heartbeat_at": row[2],
            "clean_shutdown_at": row[3],
            "system_version": row[4],
        }

    async def upsert_uptime_heartbeat(
        self,
        session_id: str,
        started_at: str,
        last_heartbeat_at: str,
        system_version: str | None = None,
        clean_shutdown_at: str | None = None,
    ) -> None:
        """Initializes or updates the single service uptime heartbeat row."""
        async with self.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO service_uptime_heartbeat (id, session_id, started_at, last_heartbeat_at, clean_shutdown_at, system_version)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id = excluded.session_id,
                    started_at = excluded.started_at,
                    last_heartbeat_at = excluded.last_heartbeat_at,
                    clean_shutdown_at = excluded.clean_shutdown_at,
                    system_version = excluded.system_version;
                """,
                (session_id, started_at, last_heartbeat_at, clean_shutdown_at, system_version),
            )

    async def update_uptime_heartbeat_timestamp(self, last_heartbeat_at: str) -> None:
        """Updates the periodic heartbeat timestamp for the active session."""
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE service_uptime_heartbeat SET last_heartbeat_at = ? WHERE id = 1;",
                (last_heartbeat_at,),
            )

    async def record_clean_shutdown(self, clean_shutdown_at: str) -> None:
        """Marks the current heartbeat record with a clean shutdown timestamp."""
        async with self.transaction() as conn:
            await conn.execute(
                "UPDATE service_uptime_heartbeat SET clean_shutdown_at = ?, last_heartbeat_at = ? WHERE id = 1;",
                (clean_shutdown_at, clean_shutdown_at),
            )

    async def record_downtime_event(
        self,
        downtime_type: str,
        started_at: str,
        ended_at: str,
        duration_seconds: float,
        reason: str | None = None,
    ) -> int:
        """Records a detected downtime incident."""
        now_ts = started_at
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO service_downtime_events (downtime_type, started_at, ended_at, duration_seconds, reason, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?);
                """,
                (downtime_type, started_at, ended_at, duration_seconds, reason, now_ts),
            )
            return cursor.lastrowid or 0

    async def get_recent_downtime_events(self, limit: int = 10) -> list[dict[str, Any]]:
        """Retrieves recently recorded downtime incidents."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """
            SELECT id, downtime_type, started_at, ended_at, duration_seconds, reason, recorded_at
            FROM service_downtime_events
            ORDER BY id DESC
            LIMIT ?;
            """,
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "downtime_type": r[1],
                "started_at": r[2],
                "ended_at": r[3],
                "duration_seconds": r[4],
                "reason": r[5],
                "recorded_at": r[6],
            }
            for r in rows
        ]

    async def get_downtime_events_in_range(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        """Retrieves all downtime events overlapping with the specified time range."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """
            SELECT id, downtime_type, started_at, ended_at, duration_seconds, reason, recorded_at
            FROM service_downtime_events
            WHERE ended_at >= ? AND started_at <= ?
            ORDER BY started_at ASC;
            """,
            (start_iso, end_iso),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0],
                "downtime_type": r[1],
                "started_at": r[2],
                "ended_at": r[3],
                "duration_seconds": r[4],
                "reason": r[5],
                "recorded_at": r[6],
            }
            for r in rows
        ]

    async def record_uptime_session(
        self,
        session_id: str,
        started_at: str,
        ended_at: str,
        uptime_seconds: float,
        clean_shutdown: bool = False,
    ) -> None:
        """Inserts or updates a completed or tracked service uptime session record."""
        async with self.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO service_uptime_sessions (session_id, started_at, ended_at, uptime_seconds, clean_shutdown)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    ended_at = excluded.ended_at,
                    uptime_seconds = excluded.uptime_seconds,
                    clean_shutdown = excluded.clean_shutdown;
                """,
                (session_id, started_at, ended_at, max(0.0, float(uptime_seconds)), 1 if clean_shutdown else 0),
            )

    async def get_uptime_sessions_aggregate(self, exclude_session_id: str | None = None) -> dict[str, Any]:
        """Calculates all-time aggregate uptime metrics and finds the longest historical uptime record."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        where_clause = "WHERE session_id != ?" if exclude_session_id else ""
        params: tuple[Any, ...] = (exclude_session_id,) if exclude_session_id else ()

        cursor = await self._conn.execute(
            f"""
            SELECT
                COALESCE(SUM(uptime_seconds), 0.0),
                COALESCE(MAX(uptime_seconds), 0.0),
                COUNT(*)
            FROM service_uptime_sessions
            {where_clause};
            """,
            params,
        )
        row = await cursor.fetchone()
        total_sec = float(row[0]) if row and row[0] is not None else 0.0
        max_sec = float(row[1]) if row and row[1] is not None else 0.0
        total_sessions = int(row[2]) if row and row[2] is not None else 0

        best_cursor = await self._conn.execute(
            f"""
            SELECT session_id, started_at, ended_at, uptime_seconds, clean_shutdown
            FROM service_uptime_sessions
            {where_clause}
            ORDER BY uptime_seconds DESC
            LIMIT 1;
            """,
            params,
        )
        best_row = await best_cursor.fetchone()
        best_session = None
        if best_row:
            best_session = {
                "session_id": best_row[0],
                "started_at": best_row[1],
                "ended_at": best_row[2],
                "uptime_seconds": float(best_row[3]),
                "clean_shutdown": bool(best_row[4]),
            }

        return {
            "total_uptime_seconds": total_sec,
            "longest_uptime_seconds": max_sec,
            "total_sessions": total_sessions,
            "longest_session": best_session,
        }

    async def get_total_downtime_seconds(self) -> float:
        """Calculates all-time total recorded downtime duration in seconds."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute("SELECT COALESCE(SUM(duration_seconds), 0.0) FROM service_downtime_events;")
        row = await cursor.fetchone()
        return float(row[0]) if row and row[0] is not None else 0.0

    # ==========================================
    # 📬 Dead Letter Queue (DLQ)
    # ==========================================
