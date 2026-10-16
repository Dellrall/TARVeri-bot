"""
Asynchronous SQLite database layer with WAL mode, indexing, schema versioning, and backup support.
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import aiosqlite
import discord

from tarveri.config import get_configured_tz, now_formatted
from tarveri.db import (
    BlacklistRepoMixin,
    DeadLetterRepoMixin,
    GuestRepoMixin,
    GuildSettingsRepoMixin,
    MassActionRepoMixin,
    SocietyBridgeRepoMixin,
    UptimeRepoMixin,
    VerificationRepoMixin,
)

logger = logging.getLogger("tarveri")

SCHEMA_VERSION = 1


def rotate_update_backups(update_dir: str, max_backups: int = 5) -> list[str]:
    """
    Keeps only the `max_backups` most recent pre-update backups in `update_dir` (e.g. backups/updates/).
    Any older update backups exceeding `max_backups` are permanently deleted.
    Returns the list of deleted backup file paths.
    """
    if not os.path.exists(update_dir) or max_backups <= 0:
        return []

    backup_files: list[str] = [
        os.path.join(update_dir, entry)
        for entry in os.listdir(update_dir)
        if os.path.isfile(os.path.join(update_dir, entry)) and entry.endswith(".db")
    ]

    # Sort files by modification time descending (newest first)
    backup_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    deleted: list[str] = []
    if len(backup_files) > max_backups:
        to_delete = backup_files[max_backups:]
        for path in to_delete:
            try:
                os.remove(path)
                deleted.append(path)
                logger.info(f"Deleted old pre-update backup: {path}")
            except OSError as e:
                logger.warning(f"Failed to remove pre-update backup '{path}': {e}")

    return deleted


def rotate_daily_backups(
    daily_dir: str,
    max_uncompressed: int = 5,
    max_archives: int = 30,
) -> tuple[list[str], list[str], list[str]]:
    """
    Manages daily backups in `daily_dir` (e.g. backups/daily/):
    1. Keeps the `max_uncompressed` most recent .db files uncompressed in `daily_dir`.
    2. Any .db files beyond `max_uncompressed` are compressed via gzip into `daily_dir/archives/` (.gz)
       and removed from the raw .db folder.
    3. Keeps up to `max_archives` compressed files in `daily_dir/archives/`, deleting older ones.

    Returns:
        (compressed_paths, deleted_db_paths, deleted_archive_paths)
    """
    if not os.path.exists(daily_dir):
        return [], [], []

    archives_dir = os.path.join(daily_dir, "archives")
    compressed_paths: list[str] = []
    deleted_db_paths: list[str] = []
    deleted_archive_paths: list[str] = []

    # 1. Scan uncompressed .db files in daily_dir (ignoring subdirectories)
    raw_db_files: list[str] = [
        os.path.join(daily_dir, entry)
        for entry in os.listdir(daily_dir)
        if os.path.isfile(os.path.join(daily_dir, entry)) and entry.endswith(".db")
    ]
    raw_db_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)

    # 2. If there are more than max_uncompressed, compress the older ones into archives/
    if len(raw_db_files) > max_uncompressed:
        os.makedirs(archives_dir, exist_ok=True)
        to_compress = raw_db_files[max_uncompressed:]
        for db_path in to_compress:
            base_name = os.path.basename(db_path)
            gz_path = os.path.join(archives_dir, f"{base_name}.gz")
            try:
                with open(db_path, "rb") as f_in, gzip.open(gz_path, "wb", compresslevel=9) as f_out:
                    shutil.copyfileobj(f_in, f_out)
                os.remove(db_path)
                compressed_paths.append(gz_path)
                deleted_db_paths.append(db_path)
                logger.info(f"Compressed older daily backup into archive: {gz_path}")
            except Exception as e:
                logger.warning(f"Failed to compress backup '{db_path}' to '{gz_path}': {e}")

    # 3. Rotate compressed archives in archives_dir
    if os.path.exists(archives_dir) and max_archives > 0:
        archive_files = [
            os.path.join(archives_dir, entry)
            for entry in os.listdir(archives_dir)
            if os.path.isfile(os.path.join(archives_dir, entry))
            and (entry.endswith(".gz") or entry.endswith(".tar.gz"))
        ]
        archive_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        if len(archive_files) > max_archives:
            to_delete = archive_files[max_archives:]
            for arch_path in to_delete:
                try:
                    os.remove(arch_path)
                    deleted_archive_paths.append(arch_path)
                    logger.info(f"Pruned old daily archive: {arch_path}")
                except OSError as e:
                    logger.warning(f"Failed to prune old archive '{arch_path}': {e}")

    return compressed_paths, deleted_db_paths, deleted_archive_paths


def rotate_backups(
    backup_dir: str = "backups",
    max_backups: int = 5,
    max_archives: int = 30,
) -> list[str]:
    """
    Unified backup rotation helper.
    Rotates daily backups (compressing >5 into archives/) and update backups (deleting >5).
    Also rotates legacy root .db files if present.
    Returns all deleted file paths.
    """
    if not os.path.exists(backup_dir) or max_backups <= 0:
        return []

    all_deleted: list[str] = []

    # 1. Rotate daily subfolder
    daily_dir = os.path.join(backup_dir, "daily")
    if os.path.isdir(daily_dir):
        _, del_dbs, del_archs = rotate_daily_backups(daily_dir, max_uncompressed=max_backups, max_archives=max_archives)
        all_deleted.extend(del_dbs)
        all_deleted.extend(del_archs)

    # 2. Rotate updates subfolder
    updates_dir = os.path.join(backup_dir, "updates")
    if os.path.isdir(updates_dir):
        del_updates = rotate_update_backups(updates_dir, max_backups=max_backups)
        all_deleted.extend(del_updates)

    # 3. Rotate legacy root .db files (if any exist)
    root_db_files = [
        os.path.join(backup_dir, entry)
        for entry in os.listdir(backup_dir)
        if os.path.isfile(os.path.join(backup_dir, entry)) and entry.endswith(".db")
    ]
    root_db_files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    if len(root_db_files) > max_backups:
        for path in root_db_files[max_backups:]:
            try:
                os.remove(path)
                all_deleted.append(path)
            except OSError as e:
                logger.warning(f"Failed to remove root backup file '{path}': {e}")

    return all_deleted


def list_backups(backup_dir: str = "backups") -> list[dict[str, Any]]:
    """
    Returns a comprehensive list of available backups across daily, archives, updates, and root.
    Sorted newest to oldest.
    Each item contains 'filename', 'path', 'category', 'is_compressed', 'mtime', 'size_bytes', and 'timestamp'.
    """
    if not os.path.exists(backup_dir):
        return []

    backup_files: list[dict[str, Any]] = []

    def _collect(directory: str, category: str, is_compressed: bool = False) -> None:
        if not os.path.isdir(directory):
            return
        for entry in os.listdir(directory):
            full_path = os.path.join(directory, entry)
            if not os.path.isfile(full_path):
                continue
            if is_compressed and (entry.endswith(".gz") or entry.endswith(".tar.gz")):
                mtime = os.path.getmtime(full_path)
                size = os.path.getsize(full_path)
                backup_files.append(
                    {
                        "filename": entry,
                        "path": full_path,
                        "category": category,
                        "is_compressed": True,
                        "mtime": mtime,
                        "size_bytes": size,
                        "timestamp": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )
            elif not is_compressed and entry.endswith(".db"):
                mtime = os.path.getmtime(full_path)
                size = os.path.getsize(full_path)
                backup_files.append(
                    {
                        "filename": entry,
                        "path": full_path,
                        "category": category,
                        "is_compressed": False,
                        "mtime": mtime,
                        "size_bytes": size,
                        "timestamp": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

    # Collect from subfolders
    _collect(os.path.join(backup_dir, "daily"), category="daily", is_compressed=False)
    _collect(os.path.join(backup_dir, "daily", "archives"), category="archive", is_compressed=True)
    _collect(os.path.join(backup_dir, "updates"), category="update", is_compressed=False)
    _collect(backup_dir, category="legacy", is_compressed=False)

    backup_files.sort(key=lambda x: x["mtime"], reverse=True)
    return backup_files


class Database(
    VerificationRepoMixin,
    GuildSettingsRepoMixin,
    GuestRepoMixin,
    BlacklistRepoMixin,
    MassActionRepoMixin,
    UptimeRepoMixin,
    DeadLetterRepoMixin,
    SocietyBridgeRepoMixin,
):
    """
    Database interface for TARVeri.
    Uses SQLite WAL mode for non-blocking concurrent reads during verification writes.
    """

    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        """Establishes connection, verifies schema version, and creates schema and indexes."""
        if self._conn:
            return

        self._conn = await aiosqlite.connect(self.path, timeout=60.0)
        await self._conn.execute("PRAGMA busy_timeout = 60000;")
        await self._conn.execute("PRAGMA foreign_keys = ON;")
        # WAL mode lets reads (e.g. admin queries on audit_log) proceed without
        # blocking on writes (verifications), which matters as guild count grows.
        await self._conn.execute("PRAGMA journal_mode = WAL;")
        await self._conn.execute("PRAGMA synchronous = NORMAL;")
        await self._conn.execute("PRAGMA cache_size = -4000;")  # 4MB in-memory page cache
        await self._conn.execute("PRAGMA temp_store = MEMORY;")  # Keep temp tables & sorts in RAM
        await self._conn.execute("PRAGMA mmap_size = 67108864;")  # 64MB memory-mapped I/O

        # Self-healing: verify database integrity upon connection
        try:
            cursor = await self._conn.execute("PRAGMA integrity_check;")
            rows = await cursor.fetchall()
            if rows == [("ok",)]:
                logger.debug("Database integrity check passed (ok).")
            else:
                logger.error(f"Database integrity check issue detected: {rows}")
        except Exception as e:
            logger.warning(f"Could not execute database integrity check: {e}")

        # Checkpoint WAL on startup (PASSIVE mode to avoid blocking or requiring exclusive lock)
        try:
            await self._conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
        except Exception as e:
            logger.debug(f"Initial WAL checkpoint notice: {e}")

        await self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS verifications (
                discord_user_id INTEGER PRIMARY KEY,
                student_id_hash TEXT UNIQUE NOT NULL,
                faculty_code TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                programme_code TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                event_type TEXT NOT NULL,
                guild_id INTEGER,
                guild_name TEXT,
                user_id INTEGER,
                message TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER PRIMARY KEY,
                welcome_channel_id INTEGER,
                help_channel_id INTEGER,
                guest_role_name TEXT DEFAULT 'Guest',
                review_channel_id INTEGER,
                admin_role_name TEXT,
                require_email_verification INTEGER DEFAULT 0,
                enforce_email_verification INTEGER DEFAULT 0,
                enable_src_roles INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS referral_codes (
                code TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                referrer_discord_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_by_discord_id INTEGER,
                used_at TEXT,
                status TEXT NOT NULL DEFAULT 'ACTIVE'
            );

            CREATE TABLE IF NOT EXISTS guest_tickets (
                ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                ticket_seq INTEGER,
                applicant_id INTEGER NOT NULL,
                referrer_id INTEGER,
                channel_id INTEGER NOT NULL,
                referral_code TEXT,
                reason TEXT,
                vouch_note TEXT,
                vouched_by_id INTEGER,
                vouched_at TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TEXT NOT NULL,
                closed_at TEXT,
                closed_by_admin_id INTEGER,
                close_reason TEXT,
                pinged_admin_ids TEXT,
                last_pinged_at TEXT
            );

            CREATE TABLE IF NOT EXISTS bot_created_roles (
                guild_id INTEGER NOT NULL,
                role_id INTEGER PRIMARY KEY,
                role_name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS verification_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id INTEGER NOT NULL,
                from_id_hash TEXT NOT NULL,
                from_faculty_code TEXT NOT NULL,
                from_campus_code TEXT NOT NULL,
                from_level_code TEXT NOT NULL,
                to_id_hash TEXT NOT NULL,
                to_faculty_code TEXT NOT NULL,
                to_campus_code TEXT NOT NULL,
                to_level_code TEXT NOT NULL,
                transitioned_at TEXT NOT NULL,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS guild_blacklists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                target_type TEXT NOT NULL,
                target_value TEXT NOT NULL,
                display_mask TEXT,
                reason TEXT,
                blacklisted_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(guild_id, target_type, target_value)
            );

            CREATE TABLE IF NOT EXISTS pending_mass_actions (
                action_id TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                action_type TEXT NOT NULL,
                user_ids_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                decided_by_id INTEGER,
                decided_at TEXT
            );

            CREATE TABLE IF NOT EXISTS bounced_emails (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_hash TEXT UNIQUE NOT NULL,
                email_encrypted TEXT,
                bounce_code INTEGER,
                bounce_reason TEXT,
                detected_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS service_uptime_heartbeat (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                session_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                last_heartbeat_at TEXT NOT NULL,
                clean_shutdown_at TEXT,
                system_version TEXT
            );

            CREATE TABLE IF NOT EXISTS service_downtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                downtime_type TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                duration_seconds REAL NOT NULL,
                reason TEXT,
                recorded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS service_uptime_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                uptime_seconds REAL NOT NULL,
                clean_shutdown INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS dead_letter_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                error_reason TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0,
                max_retries INTEGER NOT NULL DEFAULT 5,
                status TEXT NOT NULL DEFAULT 'PENDING',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                next_retry_at TEXT,
                metadata TEXT
            );

            CREATE TABLE IF NOT EXISTS paired_societies (
                society_guild_id INTEGER PRIMARY KEY,
                pairing_token_hash TEXT NOT NULL,
                paired_by_user_id INTEGER NOT NULL,
                paired_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                notes TEXT
            );

            CREATE TABLE IF NOT EXISTS console_admins (
                username TEXT PRIMARY KEY,
                role TEXT NOT NULL DEFAULT 'admin',
                added_at TEXT NOT NULL,
                added_by TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type);
            CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_audit_user_id ON audit_log(user_id);
            CREATE INDEX IF NOT EXISTS idx_verifications_faculty ON verifications(faculty_code);
            CREATE INDEX IF NOT EXISTS idx_referral_guild_referrer ON referral_codes(guild_id, referrer_discord_id);
            CREATE INDEX IF NOT EXISTS idx_referral_status ON referral_codes(status);
            CREATE INDEX IF NOT EXISTS idx_guest_tickets_guild ON guest_tickets(guild_id);
            CREATE INDEX IF NOT EXISTS idx_guest_tickets_channel ON guest_tickets(channel_id);
            CREATE INDEX IF NOT EXISTS idx_guest_tickets_applicant ON guest_tickets(applicant_id);
            CREATE INDEX IF NOT EXISTS idx_bot_created_roles_guild ON bot_created_roles(guild_id);
            CREATE INDEX IF NOT EXISTS idx_transitions_user ON verification_transitions(discord_user_id);
            CREATE INDEX IF NOT EXISTS idx_blacklist_lookup ON guild_blacklists(guild_id, target_type, target_value);
            CREATE INDEX IF NOT EXISTS idx_blacklist_guild ON guild_blacklists(guild_id);
            CREATE INDEX IF NOT EXISTS idx_pending_mass_actions_guild ON pending_mass_actions(guild_id, status);
            CREATE INDEX IF NOT EXISTS idx_bounced_emails_hash ON bounced_emails(email_hash);
            CREATE INDEX IF NOT EXISTS idx_downtime_started_at ON service_downtime_events(started_at);
            CREATE INDEX IF NOT EXISTS idx_downtime_ended_at ON service_downtime_events(ended_at);
            CREATE INDEX IF NOT EXISTS idx_uptime_sessions_started ON service_uptime_sessions(started_at);
            CREATE INDEX IF NOT EXISTS idx_dlq_status_next ON dead_letter_queue(status, next_retry_at);
            CREATE INDEX IF NOT EXISTS idx_dlq_type_status ON dead_letter_queue(queue_type, status);
            CREATE INDEX IF NOT EXISTS idx_dlq_created_at ON dead_letter_queue(created_at);
            """
        )

        # Migration helper for existing databases: ensure all expected columns exist
        # 1. guild_settings
        cursor = await self._conn.execute("PRAGMA table_info(guild_settings);")
        existing_guild_cols = {row[1] for row in await cursor.fetchall()}
        for col, col_def in [
            ("welcome_channel_id", "INTEGER"),
            ("help_channel_id", "INTEGER"),
            ("guest_role_name", "TEXT DEFAULT 'Guest'"),
            ("review_channel_id", "INTEGER"),
            ("admin_role_name", "TEXT"),
            ("require_email_verification", "INTEGER DEFAULT 0"),
            ("enforce_email_verification", "INTEGER DEFAULT 0"),
            ("enable_src_roles", "INTEGER DEFAULT 0"),
            ("updated_at", "TEXT DEFAULT ''"),
        ]:
            if col not in existing_guild_cols:
                await self._conn.execute(f"ALTER TABLE guild_settings ADD COLUMN {col} {col_def};")

        # 2. referral_codes
        cursor = await self._conn.execute("PRAGMA table_info(referral_codes);")
        existing_referral_cols = {row[1] for row in await cursor.fetchall()}
        for col, col_def in [
            ("used_by_discord_id", "INTEGER"),
            ("used_at", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
        ]:
            if col not in existing_referral_cols:
                await self._conn.execute(f"ALTER TABLE referral_codes ADD COLUMN {col} {col_def};")

        # 3. guest_tickets
        cursor = await self._conn.execute("PRAGMA table_info(guest_tickets);")
        existing_ticket_cols = {row[1] for row in await cursor.fetchall()}
        for col, col_def in [
            ("ticket_seq", "INTEGER"),
            ("referrer_id", "INTEGER"),
            ("referral_code", "TEXT"),
            ("reason", "TEXT"),
            ("vouch_note", "TEXT"),
            ("vouched_by_id", "INTEGER"),
            ("vouched_at", "TEXT"),
            ("status", "TEXT NOT NULL DEFAULT 'OPEN'"),
            ("closed_at", "TEXT"),
            ("closed_by_admin_id", "INTEGER"),
            ("close_reason", "TEXT"),
            ("pinged_admin_ids", "TEXT"),
            ("last_pinged_at", "TEXT"),
        ]:
            if col not in existing_ticket_cols:
                await self._conn.execute(f"ALTER TABLE guest_tickets ADD COLUMN {col} {col_def};")

        # 4. verifications (Alumni fields + Campus & Study Level fields + Expiry fields + Email fields + Programme Code)
        cursor = await self._conn.execute("PRAGMA table_info(verifications);")
        existing_veri_cols = {row[1] for row in await cursor.fetchall()}
        for col, col_def in [
            ("is_alumni", "INTEGER DEFAULT 0"),
            ("graduated_year", "INTEGER"),
            ("programme", "TEXT"),
            ("graduated_at", "TEXT"),
            ("campus_code", "TEXT"),
            ("level_code", "TEXT"),
            ("card_expiry_date", "TEXT"),
            ("lifecycle_prompt_status", "TEXT DEFAULT 'ACTIVE'"),
            ("last_lifecycle_prompt_at", "TEXT"),
            ("student_email_encrypted", "TEXT"),
            ("student_email_hash", "TEXT"),
            ("programme_code", "TEXT"),
        ]:
            if col not in existing_veri_cols:
                await self._conn.execute(f"ALTER TABLE verifications ADD COLUMN {col} {col_def};")

        await self._conn.execute("CREATE INDEX IF NOT EXISTS idx_verifications_alumni ON verifications(is_alumni);")
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verifications_expiry ON verifications(card_expiry_date);"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verifications_expiry_alumni ON verifications(is_alumni, card_expiry_date);"
        )
        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verifications_email_hash ON verifications(student_email_hash);"
        )

        # 5. One-time data migration: Backfill legacy verifications missing campus_code to 'W' (KL Main Campus)
        try:
            cursor = await self._conn.execute(
                """UPDATE verifications
                   SET campus_code = 'W'
                   WHERE campus_code IS NULL"""
            )
            if cursor.rowcount > 0:
                logger.info(
                    f"Migrated {cursor.rowcount} legacy student verification record(s) with default campus_code='W' (KL Main Campus)."
                )
        except Exception as e:
            logger.debug(f"Legacy campus_code migration notice: {e}")

        # Backfill programme_code if missing but campus/faculty available
        try:
            cursor = await self._conn.execute(
                """UPDATE verifications
                   SET programme_code = campus_code || faculty_code || COALESCE(level_code, 'R')
                   WHERE programme_code IS NULL AND campus_code IS NOT NULL AND faculty_code IS NOT NULL"""
            )
            if cursor.rowcount > 0:
                logger.info(f"Backfilled programme_code for {cursor.rowcount} verification record(s).")
        except Exception as e:
            logger.debug(f"Legacy programme_code backfill notice: {e}")

        # 6. One-time data migration: Backfill legacy active student verifications missing card_expiry_date
        try:
            cursor = await self._conn.execute(
                """SELECT discord_user_id, verified_at, level_code
                   FROM verifications
                   WHERE card_expiry_date IS NULL AND is_alumni = 0"""
            )
            rows = await cursor.fetchall()
            backfilled_count = 0
            for u_id, v_at, lvl_code in rows:
                calc_year = None
                if v_at:
                    try:
                        dt = datetime.fromisoformat(v_at.replace(" ", "T"))
                        calc_year = dt.year
                    except (ValueError, TypeError):
                        pass
                if not calc_year:
                    calc_year = datetime.now().year

                lvl = (lvl_code or "R").upper()
                if lvl == "F":
                    est_date = f"{calc_year + 1:04d}-05-31"
                elif lvl == "D":
                    est_date = f"{calc_year + 2:04d}-10-31"
                elif lvl == "R":
                    est_date = f"{calc_year + 3:04d}-10-31"
                elif lvl == "P":
                    est_date = f"{calc_year + 2:04d}-10-31"
                else:
                    est_date = f"{calc_year + 3:04d}-10-31"

                await self._conn.execute(
                    "UPDATE verifications SET card_expiry_date = ? WHERE discord_user_id = ?",
                    (est_date, u_id),
                )
                backfilled_count += 1

            if backfilled_count > 0:
                logger.info(
                    f"Backfilled estimated card_expiry_date for {backfilled_count} legacy student verification record(s)."
                )
        except Exception as e:
            logger.debug(f"Legacy card_expiry_date migration notice: {e}")

        cursor = await self._conn.execute("PRAGMA user_version;")
        row = await cursor.fetchone()
        current_version = row[0] if row else 0

        if current_version == 0:
            await self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION};")

        await self._conn.commit()

    async def close(self) -> None:
        """Flushes SQLite WAL to disk and closes the connection cleanly."""
        if self._conn:
            try:
                # Flush write-ahead log (WAL) into the main database file non-blockingly
                await self._conn.execute("PRAGMA wal_checkpoint(PASSIVE);")
                await self._conn.commit()
            except Exception as e:
                logger.warning(f"Failed to checkpoint WAL during database shutdown: {e}")
            finally:
                await self._conn.close()
                self._conn = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Async context manager providing atomic SQLite transaction semantics.

        Supports nested transaction calls via depth tracking.
        Automatically commits on the outermost block exit, and rolls back on exception.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        self._tx_depth = getattr(self, "_tx_depth", 0) + 1
        try:
            yield self._conn
            if self._tx_depth == 1:
                await self._conn.commit()
        except Exception:
            if self._conn:
                await self._conn.rollback()
            raise
        finally:
            self._tx_depth -= 1

    async def checkpoint_wal(self, mode: str = "PASSIVE") -> None:
        """Flushes the SQLite write-ahead log (WAL) into the main database file."""
        if self._conn:
            clean_mode = mode.upper() if mode.upper() in ("PASSIVE", "FULL", "RESTART", "TRUNCATE") else "PASSIVE"
            await self._conn.execute(f"PRAGMA wal_checkpoint({clean_mode});")

    async def prune_audit_logs(self, older_than_days: int = 90) -> int:
        """Prunes audit log rows older than the specified number of days."""
        cutoff = (datetime.now(get_configured_tz()) - timedelta(days=older_than_days)).strftime("%Y-%m-%d %H:%M:%S")
        async with self.transaction() as conn:
            cursor = await conn.execute("DELETE FROM audit_log WHERE timestamp < ?;", (cutoff,))
            return cursor.rowcount

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def create_backup(
        self,
        backup_dir: str = "backups",
        subfolder: str = "daily",
        max_backups: int = 5,
        max_archives: int = 30,
    ) -> str:
        """
        Creates a consistent, point-in-time snapshot of the database using SQLite VACUUM INTO.
        - If `subfolder == "daily"`: Saves to `backups/daily/tarveri_backup_*.db`. Keeps up to 5 uncompressed
          .db files, compresses older ones into `backups/daily/archives/*.db.gz`, and prunes archives > 30.
        - If `subfolder == "updates"`: Saves to `backups/updates/tarveri_pre_update_*.db`. Keeps up to 5 files,
          deleting older update backups.
        - Completely separates update backups from daily backups so updating never deletes daily backups.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        target_dir = os.path.join(backup_dir, subfolder) if subfolder else backup_dir
        os.makedirs(target_dir, exist_ok=True)

        timestamp = now_formatted(fmt="%Y%m%d_%H%M%S")
        prefix = "tarveri_pre_update" if subfolder == "updates" else "tarveri_backup"
        backup_filename = f"{prefix}_{timestamp}.db"
        backup_path = os.path.join(target_dir, backup_filename)

        if os.path.exists(backup_path):
            os.remove(backup_path)

        # VACUUM INTO safely creates an atomic copy of active database
        safe_path = backup_path.replace("'", "''")
        await self._conn.execute(f"VACUUM INTO '{safe_path}';")

        # Execute folder-specific rotation
        if subfolder == "daily":
            rotate_daily_backups(target_dir, max_uncompressed=max_backups, max_archives=max_archives)
        elif subfolder == "updates":
            rotate_update_backups(target_dir, max_backups=max_backups)
        elif max_backups > 0:
            rotate_backups(backup_dir=backup_dir, max_backups=max_backups, max_archives=max_archives)

        return backup_path

    def list_backups(self, backup_dir: str = "backups") -> list[dict[str, Any]]:
        """Instance helper to list available database backups."""
        return list_backups(backup_dir=backup_dir)

    async def restore_guild_settings_from_backup(self, backup_path: str, guild_id: int | None = None) -> dict[str, Any]:
        """
        Restores guild_settings from a specified backup database (.db or .gz archive) into the current active database.
        If guild_id is provided, only that guild's settings are restored; otherwise all guilds are restored.
        Returns a dictionary summarizing the restored settings.
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")

        candidate_path = backup_path
        if not os.path.isabs(candidate_path) and not os.path.exists(candidate_path):
            for candidate in (
                os.path.join("backups", candidate_path),
                os.path.join("backups", "daily", candidate_path),
                os.path.join("backups", "daily", "archives", candidate_path),
                os.path.join("backups", "updates", candidate_path),
            ):
                if os.path.exists(candidate):
                    candidate_path = candidate
                    break

        if not os.path.isfile(candidate_path):
            raise FileNotFoundError(f"Backup file not found at '{backup_path}'.")

        temp_decompressed: str | None = None
        db_to_open = candidate_path

        # If it's a gzip compressed archive (.gz), decompress to a temporary file
        if candidate_path.endswith(".gz"):
            import tempfile

            fd, temp_decompressed = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            with gzip.open(candidate_path, "rb") as f_in, open(temp_decompressed, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            db_to_open = temp_decompressed

        try:
            return await self._restore_guild_settings_from_db_file(db_to_open, guild_id=guild_id)
        finally:
            if temp_decompressed and os.path.exists(temp_decompressed):
                try:
                    os.remove(temp_decompressed)
                except OSError as exc:
                    logger.debug("Could not remove temp decompressed backup: %s", exc)

    async def _restore_guild_settings_from_db_file(self, db_path: str, guild_id: int | None = None) -> dict[str, Any]:
        restored_guilds = 0
        details: list[dict[str, Any]] = []

        async with aiosqlite.connect(db_path) as b_conn:
            cursor = await b_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='guild_settings';"
            )
            if not await cursor.fetchone():
                return {"restored_guilds": 0, "details": [], "message": "No guild_settings table found in backup."}

            query = (
                "SELECT guild_id, welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name, updated_at "
                "FROM guild_settings"
            )
            params: tuple = ()
            if guild_id is not None:
                query += " WHERE guild_id = ?"
                params = (guild_id,)

            cursor = await b_conn.execute(query, params)
            rows = await cursor.fetchall()

            async with self.transaction() as conn:
                for row in rows:
                    g_id, w_id, h_id, g_role, r_id, adm_role, u_at = row
                    await conn.execute(
                        """
                        INSERT INTO guild_settings (guild_id, welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(guild_id) DO UPDATE SET
                            welcome_channel_id = excluded.welcome_channel_id,
                            help_channel_id = excluded.help_channel_id,
                            guest_role_name = excluded.guest_role_name,
                            review_channel_id = excluded.review_channel_id,
                            admin_role_name = excluded.admin_role_name,
                            updated_at = excluded.updated_at;
                        """,
                        (g_id, w_id, h_id, g_role, r_id, adm_role, u_at or now_formatted()),
                    )
                    restored_guilds += 1
                    details.append(
                        {
                            "guild_id": g_id,
                            "welcome_channel_id": w_id,
                            "help_channel_id": h_id,
                            "guest_role_name": g_role,
                            "review_channel_id": r_id,
                            "admin_role_name": adm_role,
                        }
                    )

        return {"restored_guilds": restored_guilds, "details": details}

    async def restore_latest_guild_settings(
        self, guild_id: int | None = None, backup_dir: str = "backups"
    ) -> dict[str, Any] | None:
        """Restores guild settings from the newest available backup file in backup_dir."""
        backups = self.list_backups(backup_dir=backup_dir)
        if not backups:
            return None
        latest = backups[0]
        result = await self.restore_guild_settings_from_backup(latest["path"], guild_id=guild_id)
        result["backup_file"] = latest["filename"]
        result["backup_path"] = latest["path"]
        return result

    async def restore_full_database(self, backup_path: str) -> None:
        """
        Restores the entire active database from a backup snapshot.
        Safely closes active connection, replaces file, and reconnects.
        """
        import shutil

        if not os.path.isfile(backup_path):
            raise FileNotFoundError(f"Backup file not found at '{backup_path}'.")

        await self.close()
        shutil.copy2(backup_path, self.path)

        wal_file = f"{self.path}-wal"
        shm_file = f"{self.path}-shm"
        for f in (wal_file, shm_file):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError as exc:
                    logger.debug("Could not remove old wal/shm file %s during restore: %s", f, exc)

        await self.connect()

    async def record_bot_created_role(self, guild_id: int, role_id: int, role_name: str) -> None:
        """Records a role created by the bot so it can be distinguished from admin-created roles."""
        try:
            g_id = int(guild_id)
            r_id = int(role_id)
            r_name = str(role_name)
        except (ValueError, TypeError):
            return
        ts = now_formatted()
        async with self.transaction() as conn:
            await conn.execute(
                """
                INSERT INTO bot_created_roles (guild_id, role_id, role_name, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(role_id) DO UPDATE SET
                    role_name = excluded.role_name,
                    created_at = excluded.created_at;
                """,
                (g_id, r_id, r_name, ts),
            )

    async def get_bot_created_role_ids(self, guild_id: int) -> set[int]:
        """Returns set of role IDs in a guild that were created by the bot."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        try:
            g_id = int(guild_id)
        except (ValueError, TypeError):
            return set()
        cursor = await self._conn.execute(
            "SELECT role_id FROM bot_created_roles WHERE guild_id = ?",
            (g_id,),
        )
        rows = await cursor.fetchall()
        return {r[0] for r in rows}

    async def delete_bot_created_role(self, role_id: int) -> None:
        """Deletes a role tracking entry after the role is deleted."""
        try:
            r_id = int(role_id)
        except (ValueError, TypeError):
            return
        async with self.transaction() as conn:
            await conn.execute(
                "DELETE FROM bot_created_roles WHERE role_id = ?",
                (r_id,),
            )

    async def log(
        self,
        level: str,
        event_type: str,
        message: str,
        guild: discord.Guild | None = None,
        user_id: int | None = None,
    ) -> None:
        """Writes to both the DB audit table and standard application logger."""
        log_func = getattr(logger, level.lower(), logger.info)
        guild_ctx = f" [{guild.name}]" if guild else ""
        log_func(f"[{event_type}]{guild_ctx} {message}")

        if not self._conn:
            return

        ts = now_formatted()
        g_id = None
        g_name = None
        if guild is not None:
            try:
                g_id = int(guild.id)
            except (ValueError, TypeError, AttributeError):
                g_id = None
            try:
                g_name = str(guild.name)
            except (ValueError, TypeError, AttributeError):
                g_name = None

        u_id = None
        if user_id is not None:
            try:
                u_id = int(user_id)
            except (ValueError, TypeError, AttributeError):
                u_id = None

        try:
            async with self.transaction() as conn:
                await conn.execute(
                    """INSERT INTO audit_log
                       (timestamp, level, event_type, guild_id, guild_name, user_id, message)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        ts,
                        level,
                        event_type,
                        g_id,
                        g_name,
                        u_id,
                        message,
                    ),
                )
        except Exception as e:
            logger.error(f"Failed to insert audit log entry into DB: {e}")

    # =========================================================================
    # 🔐 Console Backend Admin Accounts
    # =========================================================================

    async def get_console_admins(self) -> list[dict[str, Any]]:
        """Returns all authorized console backend administrator usernames."""
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "SELECT username, role, added_at, added_by FROM console_admins ORDER BY added_at ASC;"
            )
            rows = await cursor.fetchall()
            return [
                {
                    "username": r[0],
                    "role": r[1],
                    "added_at": r[2],
                    "added_by": r[3],
                }
                for r in rows
            ]

    async def is_console_admin(self, username: str) -> bool:
        """Checks if a username is in the authorized console administrators table."""
        async with self.transaction() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM console_admins WHERE LOWER(username) = LOWER(?);",
                (username.strip(),),
            )
            return await cursor.fetchone() is not None

    async def has_any_console_admin(self) -> bool:
        """Returns True if at least one console administrator account exists."""
        async with self.transaction() as conn:
            cursor = await conn.execute("SELECT 1 FROM console_admins LIMIT 1;")
            return await cursor.fetchone() is not None

    async def add_console_admin(
        self, username: str, role: str = "admin", added_by: str | None = None
    ) -> bool:
        """Adds a username to the authorized console administrators table."""
        now_ts = now_formatted()
        try:
            async with self.transaction() as conn:
                await conn.execute(
                    """INSERT INTO console_admins (username, role, added_at, added_by)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(username) DO UPDATE SET role = excluded.role;""",
                    (username.strip(), role, now_ts, added_by),
                )
            return True
        except Exception as e:
            logger.error("Failed to add console admin '%s': %s", username, e)
            return False

    async def remove_console_admin(self, username: str) -> bool:
        """Removes a username from the authorized console administrators table."""
        try:
            async with self.transaction() as conn:
                cursor = await conn.execute(
                    "DELETE FROM console_admins WHERE LOWER(username) = LOWER(?);",
                    (username.strip(),),
                )
                return cursor.rowcount > 0
        except Exception as e:
            logger.error("Failed to remove console admin '%s': %s", username, e)
            return False
