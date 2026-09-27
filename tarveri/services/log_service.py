"""
Log Rotation, Daily Separation & 10-Day Period Tar.Gz Compression Engine for TARVeri.
Groups daily log files older than 10 days into 10-day decade periods by year and month,
compressing them into gzip-compressed tarballs (.tar.gz).
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import os
import re
import shutil
import tarfile
import tempfile
from datetime import date, datetime
from typing import Any

from tarveri.config import get_configured_tz

logger = logging.getLogger("tarveri")

LOG_DATE_PATTERN = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def get_10day_period(d: date) -> tuple[str, str, str]:
    """
    Groups a calendar date into its corresponding 10-day decade period for its year and month:
      - Days 01–10: Part 1 (01 to 10)
      - Days 11–20: Part 2 (11 to 20)
      - Days 21–End: Part 3 (21 to 28/29/30/31)

    Returns (period_tag, start_date_str, end_date_str).
    Example:
      2026-09-05 -> ("2026-09-01_to_2026-09-10", "2026-09-01", "2026-09-10")
      2026-09-15 -> ("2026-09-11_to_2026-09-20", "2026-09-11", "2026-09-20")
      2026-09-28 -> ("2026-09-21_to_2026-09-30", "2026-09-21", "2026-09-30")
    """
    year = d.year
    month = d.month
    day = d.day

    if day <= 10:
        start_day = 1
        end_day = 10
    elif day <= 20:
        start_day = 11
        end_day = 20
    else:
        start_day = 21
        end_day = calendar.monthrange(year, month)[1]

    start_str = f"{year:04d}-{month:02d}-{start_day:02d}"
    end_str = f"{year:04d}-{month:02d}-{end_day:02d}"
    period_tag = f"{start_str}_to_{end_str}"
    return period_tag, start_str, end_str


def get_archive_filename(period_tag: str, prefix: str = "tarveri-logs") -> str:
    """Returns standard .tar.gz archive filename for a given 10-day period tag."""
    return f"{prefix}-{period_tag}.tar.gz"


def parse_log_date(
    filename: str,
    file_path: str | None = None,
    tz_name: str | None = None,
) -> date | None:
    """
    Extracts date from a log filename (e.g. tarveri-2026-09-10.log).
    If filename has no date and file_path exists, falls back to file modification time.
    """
    match = LOG_DATE_PATTERN.search(filename)
    if match:
        try:
            year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            return date(year, month, day)
        except ValueError:
            pass

    if file_path and os.path.isfile(file_path):
        try:
            tz = get_configured_tz(tz_name)
            mtime = os.path.getmtime(file_path)
            return datetime.fromtimestamp(mtime, tz=tz).date()
        except OSError:
            pass

    return None


def archive_old_logs(
    logs_dir: str = "logs",
    older_than_days: int = 10,
    archive_dir: str | None = None,
    tz_name: str | None = None,
    reference_date: date | None = None,
    prefix: str = "tarveri-logs",
) -> list[dict[str, Any]]:
    """
    Identifies daily logs in `logs_dir` older than `older_than_days` relative to current date,
    groups them into 10-day period buckets by year and month, and compresses them into .tar.gz archives.

    Original uncompressed .log files are safely deleted after successful tarball creation/update.
    Returns list of summary dicts for each archived 10-day group.
    """
    if not os.path.exists(logs_dir):
        return []

    target_archive_dir = archive_dir or os.path.join(logs_dir, "archives")
    os.makedirs(target_archive_dir, exist_ok=True)

    tz = get_configured_tz(tz_name)
    today = reference_date or datetime.now(tz=tz).date()

    # Find all .log files in logs_dir (excluding archives subfolder)
    log_candidates: list[tuple[str, str, date]] = []
    for entry in os.listdir(logs_dir):
        full_path = os.path.join(logs_dir, entry)
        if not os.path.isfile(full_path) or not entry.endswith(".log"):
            continue

        log_d = parse_log_date(entry, file_path=full_path, tz_name=tz_name)
        if log_d is None:
            continue

        # Check if log is older than `older_than_days`
        delta_days = (today - log_d).days
        if delta_days > older_than_days:
            log_candidates.append((entry, full_path, log_d))

    if not log_candidates:
        return []

    # Group candidate log files by their 10-day period
    grouped: dict[str, list[tuple[str, str, date]]] = {}
    for entry, full_path, log_d in log_candidates:
        period_tag, _, _ = get_10day_period(log_d)
        grouped.setdefault(period_tag, []).append((entry, full_path, log_d))

    results: list[dict[str, Any]] = []

    for period_tag, files in grouped.items():
        archive_name = get_archive_filename(period_tag, prefix=prefix)
        archive_path = os.path.join(target_archive_dir, archive_name)

        # Collect uncompressed files and byte count
        files_to_add: dict[str, str] = {}
        total_uncompressed_bytes = 0
        for entry, full_path, _ in files:
            files_to_add[entry] = full_path
            try:
                total_uncompressed_bytes += os.path.getsize(full_path)
            except OSError:
                pass

        try:
            # If archive already exists, safely merge existing members and new members into temp archive
            with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False, dir=target_archive_dir) as tmp_f:
                tmp_archive_path = tmp_f.name

            try:
                existing_member_names: set[str] = set()
                with tarfile.open(tmp_archive_path, "w:gz") as tar_out:
                    if os.path.isfile(archive_path):
                        with tarfile.open(archive_path, "r:gz") as tar_in:
                            for member in tar_in.getmembers():
                                if member.name not in files_to_add:
                                    extracted = tar_in.extractfile(member)
                                    if extracted is not None:
                                        tar_out.addfile(member, extracted)
                                        existing_member_names.add(member.name)

                    # Add new log files
                    for entry_name, src_path in files_to_add.items():
                        tar_out.add(src_path, arcname=entry_name)
                        existing_member_names.add(entry_name)

                # Verify temporary archive is readable and contains all members
                with tarfile.open(tmp_archive_path, "r:gz") as verify_tar:
                    members = {m.name for m in verify_tar.getmembers()}
                    for entry_name in files_to_add:
                        if entry_name not in members:
                            raise OSError(f"Verification failed: {entry_name} missing from created archive")

                # Atomically replace target archive
                shutil.move(tmp_archive_path, archive_path)
            finally:
                if os.path.exists(tmp_archive_path):
                    try:
                        os.remove(tmp_archive_path)
                    except OSError:
                        pass

            # Safe cleanup of original uncompressed .log files
            deleted_files: list[str] = []
            for entry_name, src_path in files_to_add.items():
                try:
                    os.remove(src_path)
                    deleted_files.append(src_path)
                except OSError as e:
                    logger.warning(f"Failed to remove uncompressed log file {src_path} after archiving: {e}")

            archive_size = os.path.getsize(archive_path)
            space_saved = max(0, total_uncompressed_bytes - archive_size)

            logger.info(
                f"📦 [LogRotator] Compressed {len(files_to_add)} log(s) for period {period_tag} into {archive_name} "
                f"({archive_size / 1024:.1f} KB, saved {space_saved / 1024:.1f} KB)."
            )

            results.append(
                {
                    "period_tag": period_tag,
                    "archive_name": archive_name,
                    "archive_path": archive_path,
                    "files_archived": list(files_to_add.keys()),
                    "deleted_paths": deleted_files,
                    "uncompressed_bytes": total_uncompressed_bytes,
                    "archive_bytes": archive_size,
                    "space_saved_bytes": space_saved,
                }
            )

        except Exception as e:
            logger.error(f"❌ [LogRotator] Failed to archive logs for period {period_tag}: {e}", exc_info=True)

    return results


def list_log_archives(
    logs_dir: str = "logs",
    archive_dir: str | None = None,
) -> list[dict[str, Any]]:
    """
    Lists all compressed .tar.gz log archives in archive_dir and logs_dir, sorted newest to oldest.
    """
    target_archive_dir = archive_dir or os.path.join(logs_dir, "archives")
    search_dirs = [target_archive_dir]
    if os.path.abspath(logs_dir) != os.path.abspath(target_archive_dir) and os.path.exists(logs_dir):
        search_dirs.append(logs_dir)

    archives: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    for d in search_dirs:
        if not os.path.exists(d):
            continue
        for entry in os.listdir(d):
            full_path = os.path.join(d, entry)
            if not os.path.isfile(full_path) or not entry.endswith(".tar.gz"):
                continue
            if full_path in seen_paths:
                continue
            seen_paths.add(full_path)

            try:
                stat = os.stat(full_path)
                file_count = 0
                members: list[str] = []
                try:
                    with tarfile.open(full_path, "r:gz") as tar:
                        members = [m.name for m in tar.getmembers() if m.isfile()]
                        file_count = len(members)
                except (tarfile.TarError, OSError) as exc:
                    logger.debug("Failed reading tar archive %s members: %s", full_path, exc)

                archives.append(
                    {
                        "filename": entry,
                        "path": full_path,
                        "size_bytes": stat.st_size,
                        "mtime": stat.st_mtime,
                        "timestamp": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                        "file_count": file_count,
                        "members": members,
                    }
                )
            except OSError:
                pass

    archives.sort(key=lambda a: a["mtime"], reverse=True)
    return archives


def list_daily_logs(
    logs_dir: str = "logs",
    tz_name: str | None = None,
) -> list[dict[str, Any]]:
    """
    Lists all active uncompressed daily .log files in `logs_dir`, sorted newest to oldest.
    """
    if not os.path.exists(logs_dir):
        return []

    logs: list[dict[str, Any]] = []
    for entry in os.listdir(logs_dir):
        full_path = os.path.join(logs_dir, entry)
        if not os.path.isfile(full_path) or not entry.endswith(".log"):
            continue

        log_d = parse_log_date(entry, file_path=full_path, tz_name=tz_name)
        try:
            stat = os.stat(full_path)
            line_count = 0
            try:
                with open(full_path, encoding="utf-8", errors="replace") as f:
                    line_count = sum(1 for _ in f)
            except OSError:
                pass

            logs.append(
                {
                    "filename": entry,
                    "path": full_path,
                    "date": log_d.strftime("%Y-%m-%d") if log_d else "Unknown",
                    "size_bytes": stat.st_size,
                    "lines": line_count,
                    "mtime": stat.st_mtime,
                    "timestamp": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
        except OSError:
            pass

    logs.sort(key=lambda item: (item["date"], item["mtime"]), reverse=True)
    return logs


class LogRotationService:
    """
    Periodic background service for automated daily log rotation and 10-day period archival.
    """

    def __init__(
        self,
        logs_dir: str = "logs",
        older_than_days: int = 10,
        archive_dir: str | None = None,
        tz_name: str = "Asia/Kuala_Lumpur",
        check_interval_seconds: int = 21600,  # 6 hours
    ):
        self.logs_dir = logs_dir
        self.older_than_days = max(1, older_than_days)
        self.archive_dir = archive_dir or os.path.join(logs_dir, "archives")
        self.tz_name = tz_name
        self.check_interval_seconds = max(60, check_interval_seconds)
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Starts the background log rotation task."""
        if not self.is_running:
            self._task = asyncio.create_task(self._rotation_loop(), name="tarveri_log_rotator")
            logger.info(
                f"Log rotation service started: logs_dir='{self.logs_dir}', "
                f"archiving logs >{self.older_than_days} days old grouped by 10th-day periods."
            )

    def stop(self) -> None:
        """Cancels the background log rotation task."""
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
            logger.info("Log rotation service stopped.")

    def trigger_rotation(self, reference_date: date | None = None) -> list[dict[str, Any]]:
        """Synchronously triggers log archiving and returns summary list."""
        return archive_old_logs(
            logs_dir=self.logs_dir,
            older_than_days=self.older_than_days,
            archive_dir=self.archive_dir,
            tz_name=self.tz_name,
            reference_date=reference_date,
        )

    async def _rotation_loop(self) -> None:
        """Periodic loop executing log rotation and archiving."""
        # Initial run on startup
        try:
            self.trigger_rotation()
        except Exception as e:
            logger.warning(f"Initial log rotation run encountered an error: {e}")

        try:
            while True:
                await asyncio.sleep(self.check_interval_seconds)
                try:
                    self.trigger_rotation()
                except Exception as e:
                    logger.warning(f"Periodic log rotation check encountered an error: {e}")
        except asyncio.CancelledError:
            pass
