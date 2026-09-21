"""
Asynchronous SQLite database layer with WAL mode, indexing, schema versioning, and backup support.
"""

from __future__ import annotations

import gzip
import json
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
            if os.path.isfile(os.path.join(archives_dir, entry)) and (entry.endswith(".gz") or entry.endswith(".tar.gz"))
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
        _, del_dbs, del_archs = rotate_daily_backups(
            daily_dir, max_uncompressed=max_backups, max_archives=max_archives
        )
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


class Database:
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

        await self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_verifications_alumni ON verifications(is_alumni);"
        )
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
                logger.info(
                    f"Backfilled programme_code for {cursor.rowcount} verification record(s)."
                )
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

    async def restore_guild_settings_from_backup(
        self, backup_path: str, guild_id: int | None = None
    ) -> dict[str, Any]:
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

    async def _restore_guild_settings_from_db_file(
        self, db_path: str, guild_id: int | None = None
    ) -> dict[str, Any]:
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

    async def get_academic_transitions_for_user(
        self, discord_user_id: int
    ) -> list[dict[str, Any]]:
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

    async def get_expired_student_verifications(
        self, before_date: str | None = None
    ) -> list[dict[str, Any]]:
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
        cursor = await self._conn.execute(
            "SELECT discord_user_id FROM verifications WHERE is_alumni = 1"
        )
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
            cursor = await conn.execute(
                "DELETE FROM verifications WHERE discord_user_id = ?", (discord_user_id,)
            )
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

    async def get_guild_settings(
        self, guild_id: int
    ) -> tuple[int | None, int | None, str | None, int | None, str | None, int, int] | None:
        """Returns (welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name, require_email_verification, enforce_email_verification) for the given guild, or None."""
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            """SELECT welcome_channel_id, help_channel_id, guest_role_name, review_channel_id, admin_role_name, COALESCE(require_email_verification, 0), COALESCE(enforce_email_verification, 0)
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

    async def get_email_verification_stats(self, guild_id: int | Any = None) -> dict[str, Any]:
        """
        Returns verification telemetry regarding student email verifications.
        Includes total verified students, count verified with email OTP, percentage,
        and guild opt-in status (if guild_id is provided).
        """
        if not self._conn:
            raise RuntimeError("Database connection is not open.")
        cursor = await self._conn.execute(
            "SELECT COUNT(*), COUNT(student_email_hash) FROM verifications"
        )
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


    async def create_referral_code(
        self, code: str, guild_id: int, referrer_discord_id: int, expires_at: str
    ) -> None:
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
                (guild_id, seq, applicant_id, referrer_id, channel_id, referral_code, reason, ts, pinged_admin_ids, last_pinged_at or ts),
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

    async def get_open_guest_ticket_for_applicant(
        self, guild_id: int, applicant_id: int
    ) -> dict[str, Any] | None:
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

    async def get_latest_guest_ticket_for_user(
        self, guild_id: int, applicant_id: int
    ) -> dict[str, Any] | None:
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

    async def revoke_active_referrals_for_user(
        self, guild_id: int, user_id: int, status: str = "REVOKED"
    ) -> int:
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
                (guild_id, clean_type, clean_value, display_mask or clean_value, reason or "No reason specified", blacklisted_by, ts),
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
                    WHERE guild_id = ? AND ({' OR '.join(conditions)})
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



