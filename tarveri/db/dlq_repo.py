"""
Dead Letter Queue (DLQ) repository mixin.
"""

from __future__ import annotations

import json
import logging
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from tarveri.config import get_configured_tz, now_formatted
from tarveri.db.base import BaseRepoMixin

logger = logging.getLogger("tarveri")


class DeadLetterRepoMixin(BaseRepoMixin):
    async def enqueue_dead_letter(
        self,
        queue_type: str,
        payload: dict[str, Any] | str,
        error_reason: str | None = None,
        max_retries: int = 5,
        metadata: dict[str, Any] | None = None,
        next_retry_delay_seconds: int = 60,
    ) -> int:
        """
        Enqueues a failed operation (email, outage alert, exception alert, security alert)
        into the dead letter queue for retry and auditing.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        payload_str = payload if isinstance(payload, str) else json.dumps(payload)
        metadata_str = json.dumps(metadata) if metadata is not None else None
        ts = now_formatted()
        tz = get_configured_tz()
        next_retry = (datetime.now(tz=tz) + timedelta(seconds=max(0, next_retry_delay_seconds))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        async with self.transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO dead_letter_queue (
                    queue_type, payload, error_reason, retry_count, max_retries,
                    status, created_at, updated_at, next_retry_at, metadata
                ) VALUES (?, ?, ?, 0, ?, 'PENDING', ?, ?, ?, ?)
                """,
                (queue_type, payload_str, error_reason, max_retries, ts, ts, next_retry, metadata_str),
            )
            return cursor.lastrowid

    def _row_to_dead_letter(self, row: tuple) -> dict[str, Any]:
        """Maps dead letter SQLite row tuple to dictionary."""
        payload_data: Any = row[2]
        with suppress(Exception):
            payload_data = json.loads(row[2])

        meta_data: Any = None
        if row[10]:
            meta_data = row[10]
            with suppress(Exception):
                meta_data = json.loads(row[10])

        return {
            "id": row[0],
            "queue_type": row[1],
            "payload": payload_data,
            "error_reason": row[3],
            "retry_count": row[4],
            "max_retries": row[5],
            "status": row[6],
            "created_at": row[7],
            "updated_at": row[8],
            "next_retry_at": row[9],
            "metadata": meta_data,
        }

    async def get_pending_dead_letters(
        self,
        queue_type: str | None = None,
        limit: int = 50,
        due_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetches pending dead letter records ready for retry processing."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        conditions = ["status IN ('PENDING', 'RETRYING')"]
        params: list[Any] = []

        if due_only:
            ts = now_formatted()
            conditions.append("(next_retry_at IS NULL OR next_retry_at <= ?)")
            params.append(ts)

        if queue_type:
            conditions.append("queue_type = ?")
            params.append(queue_type)

        where_clause = "WHERE " + " AND ".join(conditions)
        params.append(limit)

        cursor = await self._conn.execute(
            f"""
            SELECT id, queue_type, payload, error_reason, retry_count, max_retries,
                   status, created_at, updated_at, next_retry_at, metadata
            FROM dead_letter_queue
            {where_clause}
            ORDER BY id ASC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dead_letter(r) for r in rows]

    async def get_dead_letter_by_id(self, dlq_id: int) -> dict[str, Any] | None:
        """Retrieves a single dead letter record by ID."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """
            SELECT id, queue_type, payload, error_reason, retry_count, max_retries,
                   status, created_at, updated_at, next_retry_at, metadata
            FROM dead_letter_queue
            WHERE id = ?
            """,
            (dlq_id,),
        )
        row = await cursor.fetchone()
        return self._row_to_dead_letter(row) if row else None

    async def mark_dead_letter_retried(
        self,
        dlq_id: int,
        success: bool,
        error_reason: str | None = None,
        next_retry_delay_seconds: int | None = None,
    ) -> bool:
        """
        Updates a dead letter record after a retry attempt.
        If success, marks as RESOLVED.
        If failure, increments retry_count and sets next_retry_at or marks as DEAD if max_retries reached.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        ts = now_formatted()
        if success:
            async with self.transaction() as conn:
                cursor = await conn.execute(
                    """
                    UPDATE dead_letter_queue
                    SET status = 'RESOLVED', error_reason = COALESCE(?, error_reason), updated_at = ?
                    WHERE id = ?
                    """,
                    (error_reason, ts, dlq_id),
                )
                return cursor.rowcount > 0

        # Failure path: fetch current retry_count and max_retries
        item = await self.get_dead_letter_by_id(dlq_id)
        if not item:
            return False

        new_count = item["retry_count"] + 1
        max_r = item["max_retries"]
        if new_count >= max_r:
            new_status = "DEAD"
            next_retry = None
        else:
            new_status = "PENDING"
            delay = next_retry_delay_seconds if next_retry_delay_seconds is not None else min(1800, 60 * (2**new_count))
            tz = get_configured_tz()
            next_retry = (datetime.now(tz=tz) + timedelta(seconds=delay)).strftime("%Y-%m-%d %H:%M:%S")

        async with self.transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE dead_letter_queue
                SET retry_count = ?, status = ?, error_reason = COALESCE(?, error_reason),
                    next_retry_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_count, new_status, error_reason, next_retry, ts, dlq_id),
            )
            return cursor.rowcount > 0

    async def resolve_dead_letter(self, dlq_id: int, note: str | None = "Manually resolved") -> bool:
        """Marks a dead letter as RESOLVED."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE dead_letter_queue
                SET status = 'RESOLVED', error_reason = COALESCE(?, error_reason), updated_at = ?
                WHERE id = ?
                """,
                (note, ts, dlq_id),
            )
            return cursor.rowcount > 0

    async def mark_dead_letter_dead(self, dlq_id: int, error_reason: str = "Manually marked dead") -> bool:
        """Marks a dead letter as DEAD (will not be automatically retried)."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        ts = now_formatted()
        async with self.transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE dead_letter_queue
                SET status = 'DEAD', error_reason = ?, updated_at = ?
                WHERE id = ?
                """,
                (error_reason, ts, dlq_id),
            )
            return cursor.rowcount > 0

    async def purge_dead_letters(self, days: int = 30, status: str | None = None) -> int:
        """Deletes dead letter queue records older than specified days."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        tz = get_configured_tz()
        cutoff = (datetime.now(tz=tz) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        where_clauses = ["created_at < ?"]
        params: list[Any] = [cutoff]

        if status:
            where_clauses.append("status = ?")
            params.append(status)

        query = f"DELETE FROM dead_letter_queue WHERE {' AND '.join(where_clauses)};"
        async with self.transaction() as conn:
            cursor = await conn.execute(query, params)
            return cursor.rowcount

    async def get_dead_letter_stats(self) -> dict[str, Any]:
        """Calculates dead letter queue statistics by status and queue type."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        cursor = await self._conn.execute(
            """
            SELECT status, COUNT(*) FROM dead_letter_queue GROUP BY status;
            """
        )
        status_rows = await cursor.fetchall()
        status_counts = {r[0]: r[1] for r in status_rows}

        type_cursor = await self._conn.execute(
            """
            SELECT queue_type, status, COUNT(*) FROM dead_letter_queue GROUP BY queue_type, status;
            """
        )
        type_rows = await type_cursor.fetchall()
        by_type: dict[str, dict[str, int]] = {}
        for q_type, st, cnt in type_rows:
            by_type.setdefault(q_type, {})[st] = cnt

        total = sum(status_counts.values())
        pending = status_counts.get("PENDING", 0) + status_counts.get("RETRYING", 0)
        resolved = status_counts.get("RESOLVED", 0)
        dead = status_counts.get("DEAD", 0)

        return {
            "total": total,
            "pending": pending,
            "resolved": resolved,
            "dead": dead,
            "status_counts": status_counts,
            "by_type": by_type,
        }

    async def list_dead_letters(
        self,
        queue_type: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Lists recent dead letter records with optional filtering."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        where_clauses: list[str] = []
        params: list[Any] = []

        if queue_type:
            where_clauses.append("queue_type = ?")
            params.append(queue_type)
        if status:
            where_clauses.append("status = ?")
            params.append(status)

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        params.append(limit)

        cursor = await self._conn.execute(
            f"""
            SELECT id, queue_type, payload, error_reason, retry_count, max_retries,
                   status, created_at, updated_at, next_retry_at, metadata
            FROM dead_letter_queue
            {where_sql}
            ORDER BY id DESC
            LIMIT ?
            """,
            params,
        )
        rows = await cursor.fetchall()
        return [self._row_to_dead_letter(r) for r in rows]
