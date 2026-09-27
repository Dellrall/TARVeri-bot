"""
Storage Guard Service.
Monitors SQLite database, WAL, backup, and log storage usage against configurable limits,
reporting to Sentry and executing proactive pruning/self-healing if thresholds are exceeded.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tarveri.config import Settings
from tarveri.database import Database

if TYPE_CHECKING:
    import discord

logger = logging.getLogger("tarveri")


class StorageLimitExceededError(Exception):
    """Raised/reported to Sentry when storage usage exceeds the configured threshold."""


@dataclass(slots=True)
class StorageUsage:
    db_bytes: int
    wal_bytes: int
    shm_bytes: int
    backups_bytes: int
    logs_bytes: int
    total_bytes: int
    max_bytes: int

    @property
    def total_mb(self) -> float:
        return round(self.total_bytes / (1024 * 1024), 2)

    @property
    def max_mb(self) -> float:
        return round(self.max_bytes / (1024 * 1024), 2)

    @property
    def usage_percent(self) -> float:
        if self.max_bytes <= 0:
            return 0.0
        return round((self.total_bytes / self.max_bytes) * 100, 1)

    @property
    def is_critical(self) -> bool:
        return self.total_bytes >= self.max_bytes

    @property
    def is_warning(self) -> bool:
        return self.total_bytes >= (self.max_bytes * 0.8)


class StorageGuardService:
    """
    Monitors storage consumption across database, WAL journal, rotated backups, and logs.
    Automatically captures events to Sentry when approaching or exceeding limits,
    and executes self-healing disk space reclamation (WAL checkpointing, log/backup pruning).
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        bot: discord.Client | None = None,
    ) -> None:
        self.db = db
        self.settings = settings
        self.bot = bot
        self.max_storage_mb = max(1, settings.max_storage_mb)
        self.interval_hours = max(1, settings.storage_check_interval_hours)
        self._task: asyncio.Task[None] | None = None
        self._running: bool = False

    def start(self) -> None:
        """Starts the background storage monitor loop."""
        if self._running or (self._task and not self._task.done()):
            return
        self._running = True
        self._task = asyncio.create_task(self._guard_loop(), name="tarveri_storage_guard")
        logger.info(
            f"Storage guard watchdog started (limit: {self.max_storage_mb}MB, interval: {self.interval_hours}h)."
        )

    def stop(self) -> None:
        """Stops the background storage monitor loop gracefully."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
        logger.info("Storage guard watchdog stopped.")

    def get_storage_usage(self) -> StorageUsage:
        """Calculates current storage footprint across DB, WAL, SHM, backups, and logs."""
        db_path = self.settings.db_path
        wal_path = f"{db_path}-wal"
        shm_path = f"{db_path}-shm"

        def _file_size(path: str) -> int:
            try:
                return os.path.getsize(path) if os.path.isfile(path) else 0
            except OSError:
                return 0

        def _dir_size(dir_path: str) -> int:
            total = 0
            try:
                if os.path.isdir(dir_path):
                    for root, _, files in os.walk(dir_path):
                        for f in files:
                            try:
                                total += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
            except OSError:
                pass
            return total

        db_bytes = _file_size(db_path)
        wal_bytes = _file_size(wal_path)
        shm_bytes = _file_size(shm_path)
        backups_bytes = _dir_size(self.settings.backup_dir)
        logs_bytes = _dir_size(self.settings.logs_dir)

        total_bytes = db_bytes + wal_bytes + shm_bytes + backups_bytes + logs_bytes
        max_bytes = self.max_storage_mb * 1024 * 1024

        return StorageUsage(
            db_bytes=db_bytes,
            wal_bytes=wal_bytes,
            shm_bytes=shm_bytes,
            backups_bytes=backups_bytes,
            logs_bytes=logs_bytes,
            total_bytes=total_bytes,
            max_bytes=max_bytes,
        )

    async def check_storage_now(self) -> dict[str, Any]:
        """
        Executes an on-demand storage audit, emits Sentry telemetry on threshold breach,
        and triggers self-healing pruning if necessary.
        """
        usage = self.get_storage_usage()
        result: dict[str, Any] = {
            "usage": usage,
            "status": "OK",
            "self_healed": False,
            "reclaimed_mb": 0.0,
        }

        if usage.is_critical or usage.is_warning:
            status_label = "CRITICAL" if usage.is_critical else "WARNING"
            result["status"] = status_label
            msg = (
                f"🚨 Storage Guard [{status_label}]: Total storage {usage.total_mb}MB exceeds "
                f"{'limit' if usage.is_critical else '80% threshold'} of {usage.max_mb}MB "
                f"({usage.usage_percent}% used). Details: DB={round(usage.db_bytes / 1048576, 2)}MB, "
                f"WAL={round(usage.wal_bytes / 1048576, 2)}MB, Backups={round(usage.backups_bytes / 1048576, 2)}MB, "
                f"Logs={round(usage.logs_bytes / 1048576, 2)}MB."
            )
            if usage.is_critical:
                logger.error(msg)
            else:
                logger.warning(msg)

            # Report to Sentry if active
            self._report_to_sentry(usage, is_critical=usage.is_critical)

            # Execute self-healing pruning
            reclaimed_bytes = await self._self_heal_storage()
            result["self_healed"] = True
            result["reclaimed_mb"] = round(reclaimed_bytes / (1024 * 1024), 2)
        else:
            logger.debug(f"Storage guard check passed: {usage.total_mb}MB / {usage.max_mb}MB ({usage.usage_percent}%).")

        return result

    def _report_to_sentry(self, usage: StorageUsage, is_critical: bool) -> None:
        """Captures a telemetry event in Sentry with storage metrics."""
        try:
            import sentry_sdk

            if not getattr(sentry_sdk, "is_initialized", lambda: False)() and not self.settings.sentry_dsn:
                return

            with sentry_sdk.push_scope() as scope:
                scope.set_tag("component", "storage_guard")
                scope.set_tag("storage_status", "CRITICAL" if is_critical else "WARNING")
                scope.set_extra("total_mb", usage.total_mb)
                scope.set_extra("max_mb", usage.max_mb)
                scope.set_extra("usage_percent", usage.usage_percent)
                scope.set_extra("db_mb", round(usage.db_bytes / 1048576, 2))
                scope.set_extra("wal_mb", round(usage.wal_bytes / 1048576, 2))
                scope.set_extra("backups_mb", round(usage.backups_bytes / 1048576, 2))
                scope.set_extra("logs_mb", round(usage.logs_bytes / 1048576, 2))

                error = StorageLimitExceededError(
                    f"Storage usage {usage.total_mb}MB reached {usage.usage_percent}% of {usage.max_mb}MB limit."
                )
                sentry_sdk.capture_exception(error)
        except Exception as e:
            logger.warning(f"Could not report storage event to Sentry: {e}", exc_info=True)

    async def _self_heal_storage(self) -> int:
        """Performs emergency storage reclamation."""
        before = self.get_storage_usage().total_bytes
        logger.info("⚡ Storage guard self-healing initiated: truncating WAL and pruning old records/backups...")

        try:
            # 1. Truncate SQLite WAL
            await self.db.checkpoint_wal()
        except Exception as e:
            logger.warning(f"Storage guard failed to checkpoint WAL: {e}", exc_info=True)

        try:
            # 2. Prune old audit logs (keep 30 days)
            await self.db.prune_audit_logs(older_than_days=30)
        except Exception as e:
            logger.warning(f"Storage guard failed to prune audit logs: {e}", exc_info=True)

        try:
            # 3. Rotate backups keeping only top 3
            from tarveri.database import rotate_backups

            rotate_backups(self.settings.backup_dir, max_backups=max(3, self.settings.max_backups // 2))
        except Exception as e:
            logger.warning(f"Storage guard failed to prune backups: {e}", exc_info=True)

        after = self.get_storage_usage().total_bytes
        reclaimed = max(0, before - after)
        logger.info(f"Storage guard self-healing finished. Reclaimed {round(reclaimed / (1024 * 1024), 2)}MB.")
        return reclaimed

    async def _guard_loop(self) -> None:
        await asyncio.sleep(15)  # Initial warmup delay
        while self._running:
            try:
                await self.check_storage_now()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error during storage guard check: {e}", exc_info=True)

            try:
                await asyncio.sleep(self.interval_hours * 3600)
            except asyncio.CancelledError:
                break
