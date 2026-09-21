import os
import sqlite3

import aiosqlite
import pytest

from tarveri.database import Database


@pytest.mark.asyncio
async def test_database_crud(tmp_path):
    db_file = str(tmp_path / "test.db")
    db = Database(db_file)
    await db.connect()

    assert db.is_connected

    # Initial stats
    assert await db.total_verified() == 0

    # Insert verification
    user_id = 10001
    id_hash = "abcde12345hash"
    faculty = "M"
    await db.record_verification(user_id, id_hash, faculty)

    assert await db.total_verified() == 1

    # Fetch by user
    record = await db.get_verification_by_user(user_id)
    assert record is not None
    assert record[0] == id_hash
    assert record[1] == faculty

    # Fetch by id hash
    record_by_hash = await db.get_verification_by_id_hash(id_hash)
    assert record_by_hash is not None
    assert record_by_hash[0] == user_id

    # Faculty counts
    counts = await db.counts_by_faculty()
    assert len(counts) == 1
    assert counts[0] == ("M", 1)

    # Activity in last 24 hours
    assert await db.verified_in_last(24) == 1

    # Delete verification
    deleted = await db.delete_verification(user_id)
    assert deleted is True
    assert await db.total_verified() == 0

    # Logging test
    await db.log("INFO", "TEST_EVENT", "Test audit message", user_id=user_id)
    audit = await db.recent_audit(limit=10, event_type="TEST_EVENT")
    assert len(audit) == 1
    assert audit[0][2] == "TEST_EVENT"
    assert audit[0][4] == user_id
    assert audit[0][5] == "Test audit message"

    await db.close()
    assert not db.is_connected


@pytest.mark.asyncio
async def test_database_backup(tmp_path):
    db_file = str(tmp_path / "original.db")
    backup_dir = str(tmp_path / "backups")
    db = Database(db_file)
    await db.connect()

    await db.record_verification(2001, "hash2001", "M")
    backup_path = await db.create_backup(backup_dir=backup_dir)

    assert os.path.exists(backup_path)

    # Verify backup contains the record
    backup_db = Database(backup_path)
    await backup_db.connect()
    record = await backup_db.get_verification_by_user(2001)
    assert record is not None
    assert record[0] == "hash2001"

    await backup_db.close()
    await db.close()


@pytest.mark.asyncio
async def test_database_backup_rotation(tmp_path):
    from tarveri.database import (
        list_backups,
        rotate_daily_backups,
        rotate_update_backups,
    )
    db_file = str(tmp_path / "original_rot.db")
    backup_dir = str(tmp_path / "backups_rot")
    os.makedirs(backup_dir, exist_ok=True)

    db = Database(db_file)
    await db.connect()
    await db.record_verification(3001, "hash3001", "M")

    # 1. Test Daily Backup Rotation & Compression (Keep 5 raw, compress older into archives/)
    daily_dir = os.path.join(backup_dir, "daily")
    os.makedirs(daily_dir, exist_ok=True)
    for i in range(12):
        b_file = os.path.join(daily_dir, f"tarveri_backup_20260908_{i:02d}0000.db")
        with open(b_file, "w") as f:
            f.write(f"daily backup content {i}")
        os.utime(b_file, (1700000000 + i * 100, 1700000000 + i * 100))

    comp, del_db, del_arch = rotate_daily_backups(daily_dir=daily_dir, max_uncompressed=5, max_archives=10)
    assert len(comp) == 7
    assert len(del_db) == 7

    # Exactly 5 uncompressed .db files remain in daily/
    raw_dbs = [f for f in os.listdir(daily_dir) if f.endswith(".db")]
    assert len(raw_dbs) == 5

    # 7 compressed .gz files exist in daily/archives/
    archives_dir = os.path.join(daily_dir, "archives")
    assert os.path.exists(archives_dir)
    arch_files = [f for f in os.listdir(archives_dir) if f.endswith(".gz")]
    assert len(arch_files) == 7

    # 2. Test Update Backup Rotation (Keep 5, delete older, separate folder)
    updates_dir = os.path.join(backup_dir, "updates")
    os.makedirs(updates_dir, exist_ok=True)
    for i in range(8):
        u_file = os.path.join(updates_dir, f"tarveri_pre_update_20260908_{i:02d}0000.db")
        with open(u_file, "w") as f:
            f.write(f"update backup content {i}")
        os.utime(u_file, (1700000000 + i * 100, 1700000000 + i * 100))

    del_updates = rotate_update_backups(update_dir=updates_dir, max_backups=5)
    assert len(del_updates) == 3

    remaining_updates = [f for f in os.listdir(updates_dir) if f.endswith(".db")]
    assert len(remaining_updates) == 5

    # Verify daily backups were completely unaffected by update rotation
    assert len([f for f in os.listdir(daily_dir) if f.endswith(".db")]) == 5

    # 3. Test list_backups categories
    all_backups = list_backups(backup_dir=backup_dir)
    categories = {b["category"] for b in all_backups}
    assert "daily" in categories
    assert "archive" in categories
    assert "update" in categories

    # 4. Test create_backup() with daily and update subfolders
    created_daily = await db.create_backup(backup_dir=backup_dir, subfolder="daily", max_backups=5)
    assert "daily" in created_daily
    assert os.path.exists(created_daily)

    created_update = await db.create_backup(backup_dir=backup_dir, subfolder="updates", max_backups=5)
    assert "updates" in created_update
    assert os.path.exists(created_update)

    await db.close()


@pytest.mark.asyncio
async def test_database_restore_from_compressed_archive(tmp_path):
    db_file = str(tmp_path / "restore_arch.db")
    backup_dir = str(tmp_path / "backups_arch")
    db = Database(db_file)
    await db.connect()

    await db.set_guild_welcome_channel(777, 1001)
    await db.set_guild_help_channel(777, 1002)
    await db.set_guild_guest_role(777, "Guest(Verified)")
    await db.set_guild_review_channel(777, 1003)

    # Create backup with max_backups=0 to automatically compress into archives/
    await db.create_backup(backup_dir=backup_dir, subfolder="daily", max_backups=0)
    archives_dir = os.path.join(backup_dir, "daily", "archives")
    assert os.path.exists(archives_dir)
    arch_files = [f for f in os.listdir(archives_dir) if f.endswith(".gz")]
    assert len(arch_files) == 1
    archive_path = os.path.join(archives_dir, arch_files[0])
    assert archive_path.endswith(".gz")

    # Clear current settings
    await db._conn.execute("DELETE FROM guild_settings;")
    await db._conn.commit()

    # Restore from the .gz compressed archive
    result = await db.restore_guild_settings_from_backup(archive_path, guild_id=777)
    assert result["restored_guilds"] == 1

    settings = await db.get_guild_settings(777)
    assert settings is not None
    assert settings[0] == 1001
    assert settings[2] == "Guest(Verified)"

    await db.close()


@pytest.mark.asyncio
async def test_database_unique_constraints(tmp_path):
    db_file = str(tmp_path / "test_constraint.db")
    db = Database(db_file)
    await db.connect()

    await db.record_verification(1001, "hash_one", "M")

    # Duplicate user_id should raise IntegrityError
    with pytest.raises((sqlite3.IntegrityError, aiosqlite.IntegrityError)):
        await db.record_verification(1001, "hash_two", "G")

    # Duplicate hash should raise IntegrityError
    with pytest.raises((sqlite3.IntegrityError, aiosqlite.IntegrityError)):
        await db.record_verification(1002, "hash_one", "G")

    await db.close()


@pytest.mark.asyncio
async def test_database_guild_settings(tmp_path):
    db_file = str(tmp_path / "test_guild_settings.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 999111
    # Initially None
    assert await db.get_guild_settings(guild_id) is None
    assert await db.is_guild_email_verification_enabled(guild_id) is False

    # Set welcome channel
    await db.set_guild_welcome_channel(guild_id, 123456)
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, None, "Guest", None, None, 0, 0)

    # Set help channel
    await db.set_guild_help_channel(guild_id, 654321)
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, 654321, "Guest", None, None, 0, 0)

    # Set guest role, review channel, and admin role
    await db.set_guild_guest_role(guild_id, "Guest (Approved)")
    await db.set_guild_review_channel(guild_id, 999000)
    await db.set_guild_admin_role(guild_id, "Special Staff")
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, 654321, "Guest (Approved)", 999000, "Special Staff", 0, 0)

    # Opt-in to email verification
    await db.set_guild_email_verification(guild_id, True)
    assert await db.is_guild_email_verification_enabled(guild_id) is True
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, 654321, "Guest (Approved)", 999000, "Special Staff", 1, 0)

    # Enable email role enforcement
    await db.set_guild_email_enforcement(guild_id, True)
    assert await db.is_guild_email_enforcement_enabled(guild_id) is True
    settings = await db.get_guild_settings(guild_id)
    assert settings == (123456, 654321, "Guest (Approved)", 999000, "Special Staff", 1, 1)

    # Opt-out of email verification
    await db.set_guild_email_verification(guild_id, False)
    assert await db.is_guild_email_verification_enabled(guild_id) is False

    # Reset welcome channel and admin role
    await db.set_guild_welcome_channel(guild_id, None)
    await db.set_guild_admin_role(guild_id, None)
    settings = await db.get_guild_settings(guild_id)
    assert settings == (None, 654321, "Guest (Approved)", 999000, None, 0, 1)

    # Email stats telemetry
    stats = await db.get_email_verification_stats(guild_id)
    assert "total_students" in stats
    assert "email_verified_students" in stats
    assert "guild_opted_in" in stats

    await db.close()


@pytest.mark.asyncio
async def test_guest_tickets_reason_giver_and_comments(tmp_path):
    db_file = str(tmp_path / "test_guest_comments.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 112233
    applicant_id = 5555
    referrer_id = 6666
    channel_id = 7777

    # 1. Create ticket with applicant reason
    ticket_id = await db.create_guest_ticket(
        guild_id=guild_id,
        applicant_id=applicant_id,
        referrer_id=referrer_id,
        channel_id=channel_id,
        referral_code="TAR-COMM1",
        reason="Attending TARUMT Hackathon 2026 as mentor",
    )
    assert ticket_id > 0

    ticket = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket["applicant_id"] == applicant_id
    assert ticket["ticket_seq"] == 1
    assert ticket["reason"] == "Attending TARUMT Hackathon 2026 as mentor"
    assert ticket["vouch_note"] is None
    assert ticket["vouched_by_id"] is None
    assert ticket["closed_by_admin_id"] is None
    assert ticket["close_reason"] is None

    # 2. Voucher submits comments/statement (Reason Giver: referrer_id)
    vouch_note = "Confirmed industry speaker and mentor for our team."
    await db.update_guest_ticket_vouch(ticket_id, vouch_note, vouched_by_id=referrer_id)

    ticket_vouched = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_vouched["vouch_note"] == vouch_note
    assert ticket_vouched["vouched_by_id"] == referrer_id
    assert ticket_vouched["vouched_at"] is not None

    # 3. Admin closes/approves ticket with comment (Reason Giver: admin_id)
    admin_id = 9999
    admin_comment = "Verified external mentor credentials."
    await db.close_guest_ticket(ticket_id, "APPROVED", closed_by_admin_id=admin_id, close_reason=admin_comment)

    ticket_approved = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_approved["status"] == "APPROVED"
    assert ticket_approved["closed_by_admin_id"] == admin_id
    assert ticket_approved["close_reason"] == admin_comment
    assert ticket_approved["closed_at"] is not None

    # 4. Another ticket: Admin rejection with reason/comment
    ticket_id_rej = await db.create_guest_ticket(
        guild_id=guild_id,
        applicant_id=8888,
        channel_id=9999,
        reason="Random guest",
    )
    rej_reason = "Unverified affiliation and unresponsive."
    await db.close_guest_ticket(ticket_id_rej, "REJECTED", closed_by_admin_id=admin_id, close_reason=rej_reason)

    ticket_rej = await db.get_guest_ticket_by_id(ticket_id_rej)
    assert ticket_rej["status"] == "REJECTED"
    assert ticket_rej["ticket_seq"] == 2
    assert ticket_rej["closed_by_admin_id"] == admin_id
    assert ticket_rej["close_reason"] == rej_reason

    # 5. Revocation reason when leaving server
    await db.revoke_guest_tickets_for_user(guild_id, applicant_id, status="LEFT_SERVER", close_reason="User left server")
    ticket_revoked = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_revoked["status"] == "LEFT_SERVER"
    assert ticket_revoked["close_reason"] == "User left server"

    await db.close()


@pytest.mark.asyncio
async def test_database_backwards_compatibility_migration(tmp_path):
    """Verifies that an existing database created with an older legacy schema migrates smoothly without losing data."""
    db_file = str(tmp_path / "legacy_v1.db")

    # Manually create a legacy v1 schema database with minimal columns
    conn = sqlite3.connect(db_file)
    cursor = conn.cursor()
    cursor.execute(
        """CREATE TABLE verifications (
            discord_user_id INTEGER PRIMARY KEY,
            student_id_hash TEXT UNIQUE NOT NULL,
            faculty_code TEXT NOT NULL,
            verified_at TEXT NOT NULL
        );"""
    )
    cursor.execute(
        """CREATE TABLE guild_settings (
            guild_id INTEGER PRIMARY KEY,
            welcome_channel_id INTEGER,
            help_channel_id INTEGER,
            updated_at TEXT NOT NULL
        );"""
    )
    cursor.execute(
        """CREATE TABLE guest_tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER NOT NULL,
            applicant_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );"""
    )
    cursor.execute(
        "INSERT INTO verifications VALUES (11111, 'hash_legacy', 'M', '2025-01-01 10:00:00');"
    )
    cursor.execute(
        "INSERT INTO guild_settings VALUES (99999, 12345, 67890, '2025-01-01 10:00:00');"
    )
    cursor.execute(
        "INSERT INTO guest_tickets (guild_id, applicant_id, channel_id, created_at) VALUES (99999, 22222, 33333, '2025-01-01 10:00:00');"
    )
    conn.commit()
    conn.close()

    # Now open with TARVeri Database class
    db = Database(db_file)
    await db.connect()

    # Verify existing legacy data is intact
    verif = await db.get_verification_by_user(11111)
    assert verif is not None
    assert verif[0] == "hash_legacy"
    assert verif[1] == "M"

    settings = await db.get_guild_settings(99999)
    assert settings is not None
    assert settings[0] == 12345
    assert settings[1] == 67890
    # Newly added guest_role_name and review_channel_id default gracefully
    assert settings[2] == "Guest" or settings[2] is None

    ticket = await db.get_guest_ticket_by_id(1)
    assert ticket is not None
    assert ticket["applicant_id"] == 22222
    assert ticket["status"] == "OPEN"

    # Verify newly added columns can be written to
    await db.update_guest_ticket_vouch(1, "Backwards compatible vouch", vouched_by_id=55555)
    updated_ticket = await db.get_guest_ticket_by_id(1)
    assert updated_ticket["vouch_note"] == "Backwards compatible vouch"
    assert updated_ticket["vouched_by_id"] == 55555

    await db.close()


@pytest.mark.asyncio
async def test_database_closed_connection_errors():
    db = Database("unopened.db")
    assert not db.is_connected

    with pytest.raises(RuntimeError, match="Database connection is not open"):
        await db.get_verification_by_user(12345)

    with pytest.raises(RuntimeError, match="Database connection is not open"):
        await db.record_verification(12345, "hash", "M")

    with pytest.raises(RuntimeError, match="Database connection is not open"):
        await db.delete_verification(12345)

    with pytest.raises(RuntimeError, match="Database connection is not open"):
        await db.create_backup()


@pytest.mark.asyncio
async def test_database_context_manager(tmp_path):
    db_file = str(tmp_path / "ctx_test.db")
    async with Database(db_file) as db:
        assert db.is_connected
        await db.record_verification(123, "hash123", "M")
        assert await db.total_verified() == 1

    assert not db.is_connected


def test_rotate_backups_edge_cases(tmp_path):
    from tarveri.database import rotate_backups

    # Non-existent dir returns []
    assert rotate_backups(str(tmp_path / "non_existent"), max_backups=5) == []

    # max_backups <= 0 returns []
    empty_dir = str(tmp_path / "empty")
    os.makedirs(empty_dir, exist_ok=True)
    assert rotate_backups(empty_dir, max_backups=0) == []
    assert rotate_backups(empty_dir, max_backups=-1) == []


@pytest.mark.asyncio
async def test_database_cleanup_expired_referrals(tmp_path):
    db_file = str(tmp_path / "cleanup_test.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 111
    # Create 2 active referrals with past expiration date
    await db.create_referral_code("TAR-EXP1", guild_id, 101, "2020-01-01 00:00:00")
    await db.create_referral_code("TAR-EXP2", guild_id, 102, "2020-01-01 00:00:00")
    # Create 1 active referral with future expiration date
    await db.create_referral_code("TAR-ACT1", guild_id, 103, "2099-01-01 00:00:00")

    cleaned_count = await db.cleanup_expired_referrals()
    assert cleaned_count == 2

    exp1 = await db.get_referral_code("TAR-EXP1", guild_id)
    assert exp1["status"] == "EXPIRED"

    exp2 = await db.get_referral_code("TAR-EXP2", guild_id)
    assert exp2["status"] == "EXPIRED"

    act1 = await db.get_referral_code("TAR-ACT1", guild_id)
    assert act1["status"] == "ACTIVE"

    await db.close()


@pytest.mark.asyncio
async def test_database_clear_stale_channel_setting(tmp_path):
    db_file = str(tmp_path / "stale_channel_test.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 445566
    await db.set_guild_welcome_channel(guild_id, 1001)
    await db.set_guild_help_channel(guild_id, 1002)
    await db.set_guild_review_channel(guild_id, 1003)
    await db.set_guild_admin_role(guild_id, "Test Admin")

    settings = await db.get_guild_settings(guild_id)
    assert settings == (1001, 1002, "Guest", 1003, "Test Admin", 0, 0)

    # Clear welcome channel
    cleared_welcome = await db.clear_stale_channel_setting(guild_id, "welcome")
    assert cleared_welcome is True
    settings = await db.get_guild_settings(guild_id)
    assert settings[0] is None
    assert settings[1] == 1002

    # Clear help channel using column name
    cleared_help = await db.clear_stale_channel_setting(guild_id, "help_channel_id")
    assert cleared_help is True
    settings = await db.get_guild_settings(guild_id)
    assert settings[1] is None

    # Clear review channel
    cleared_review = await db.clear_stale_channel_setting(guild_id, "review")
    assert cleared_review is True
    settings = await db.get_guild_settings(guild_id)
    assert settings[3] is None

    # Clearing again returns False because setting is already NULL
    cleared_again = await db.clear_stale_channel_setting(guild_id, "review")
    assert cleared_again is False

    # Invalid setting raises ValueError
    with pytest.raises(ValueError, match="Invalid channel/setting type"):
        await db.clear_stale_channel_setting(guild_id, "invalid_setting")

    await db.close()


@pytest.mark.asyncio
async def test_database_get_all_verifications(tmp_path):
    db_file = str(tmp_path / "all_verif_test.db")
    db = Database(db_file)
    await db.connect()

    # Empty initially
    all_v = await db.get_all_verifications()
    assert all_v == []

    # Record 2 verifications
    await db.record_verification(1001, "hash_user_1", "M")
    await db.record_verification(1002, "hash_user_2", "B")

    all_v = await db.get_all_verifications()
    assert len(all_v) == 2
    u_ids = {row[0] for row in all_v}
    assert u_ids == {1001, 1002}
    faculties = {row[2] for row in all_v}
    assert faculties == {"M", "B"}

    await db.close()


@pytest.mark.asyncio
async def test_bot_created_roles_tracking(tmp_path):
    db_file = str(tmp_path / "bot_roles.db")
    db = Database(db_file)
    await db.connect()

    guild_id = 998877
    assert await db.get_bot_created_role_ids(guild_id) == set()

    # Record 2 bot created roles
    await db.record_bot_created_role(guild_id, 1111, "FOCS")
    await db.record_bot_created_role(guild_id, 2222, "Guest(Approved)")
    # Record for another guild
    await db.record_bot_created_role(888888, 3333, "FAFB")

    role_ids = await db.get_bot_created_role_ids(guild_id)
    assert role_ids == {1111, 2222}

    # Delete one
    await db.delete_bot_created_role(1111)
    role_ids = await db.get_bot_created_role_ids(guild_id)
    assert role_ids == {2222}

    await db.close()


@pytest.mark.asyncio
async def test_database_backup_listing_and_restoration(tmp_path):
    orig_db_file = str(tmp_path / "active.db")
    backup_dir = str(tmp_path / "backups_test")
    db = Database(orig_db_file)
    await db.connect()

    guild_1 = 1001
    await db.set_guild_welcome_channel(guild_1, 5001)
    await db.set_guild_help_channel(guild_1, 5002)
    await db.set_guild_guest_role(guild_1, "Verified Guest")
    await db.set_guild_review_channel(guild_1, 5003)
    await db.set_guild_admin_role(guild_1, "TARVeri Admin")
    await db.record_verification(9001, "hash_9001", "M")

    # Create backup snapshot
    backup_path = await db.create_backup(backup_dir=backup_dir)
    assert os.path.isfile(backup_path)

    # Test list_backups
    backups = db.list_backups(backup_dir=backup_dir)
    assert len(backups) == 1
    assert backups[0]["path"] == backup_path
    assert backups[0]["size_bytes"] > 0

    # Simulate settings loss / corruption in active DB
    await db.clear_stale_channel_setting(guild_1, "welcome")
    await db.clear_stale_channel_setting(guild_1, "help")
    await db.set_guild_guest_role(guild_1, "Guest")
    corrupted = await db.get_guild_settings(guild_1)
    assert corrupted[0] is None  # welcome cleared
    assert corrupted[1] is None  # help cleared
    assert corrupted[2] == "Guest"

    # Restore settings from latest backup
    res = await db.restore_latest_guild_settings(guild_id=guild_1, backup_dir=backup_dir)
    assert res is not None
    assert res["restored_guilds"] == 1

    restored = await db.get_guild_settings(guild_1)
    assert restored == (5001, 5002, "Verified Guest", 5003, "TARVeri Admin", 0, 0)

    # Full database restore
    # Add new dummy data to active
    await db.record_verification(9002, "hash_9002", "B")
    assert await db.total_verified() == 2

    # Restore full DB from snapshot
    await db.restore_full_database(backup_path)
    assert db.is_connected
    assert await db.total_verified() == 1  # reverted to 1 verification in snapshot
    rec = await db.get_verification_by_user(9001)
    assert rec is not None
    await db.close()


@pytest.mark.asyncio
async def test_database_campus_and_level_columns_and_details(tmp_path):
    db_file = str(tmp_path / "campus_level_test.db")
    db = Database(db_file)
    await db.connect()
    try:
        user_id = 77701
        await db.record_verification(user_id, "hash_77701", "WM", campus_code="W", level_code="D")

        details = await db.get_verification_details(user_id)
        assert details is not None
        assert details["faculty_code"] == "WM"
        assert details["campus_code"] == "W"
        assert details["level_code"] == "D"
        assert details["is_alumni"] is False

        # Non-existent user
        assert await db.get_verification_details(999999) is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_database_backfill_legacy_verifications(tmp_path):
    db_file = str(tmp_path / "backfill_test.db")

    # 1. Manually create legacy database with NULL campus_code
    async with aiosqlite.connect(db_file) as conn:
        await conn.execute(
            """CREATE TABLE verifications (
                discord_user_id INTEGER PRIMARY KEY,
                student_id_hash TEXT UNIQUE NOT NULL,
                faculty_code TEXT NOT NULL,
                verified_at TEXT NOT NULL
            );"""
        )
        await conn.execute(
            "INSERT INTO verifications VALUES (101, 'hash101', 'M', '2024-01-01 10:00:00');"
        )
        await conn.execute(
            "INSERT INTO verifications VALUES (102, 'hash102', 'B', '2024-01-02 11:00:00');"
        )
        await conn.commit()

    # 2. Connect Database (triggers automatic migration and backfill)
    db = Database(db_file)
    await db.connect()
    try:
        # Check that legacy rows were backfilled with 'W' (KL Main Campus)
        det101 = await db.get_verification_details(101)
        assert det101 is not None
        assert det101["campus_code"] == "W"

        det102 = await db.get_verification_details(102)
        assert det102 is not None
        assert det102["campus_code"] == "W"

        # Test manual update_verification_details
        res = await db.update_verification_details(101, campus_code="P", level_code="R")
        assert res is True
        updated101 = await db.get_verification_details(101)
        assert updated101["campus_code"] == "P"
        assert updated101["level_code"] == "R"

        # Update non-existent returns False
        assert await db.update_verification_details(99999, campus_code="A") is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_database_pending_mass_actions(tmp_path):
    db_file = str(tmp_path / "mass_actions_test.db")
    db = Database(db_file)
    await db.connect()
    try:
        guild_id = 998877
        action_id = "MREV-TEST1"
        user_ids = [1001, 1002, 1003, 1004, 1005]
        reason = "Test email policy revocation"

        # 1. Create pending mass action
        created = await db.create_pending_mass_action(
            action_id=action_id,
            guild_id=guild_id,
            action_type="EMAIL_POLICY_REVOCATION",
            user_ids=user_ids,
            reason=reason,
        )
        assert created is True

        # 2. Duplicate action_id should return False
        dup = await db.create_pending_mass_action(
            action_id=action_id,
            guild_id=guild_id,
            action_type="EMAIL_POLICY_REVOCATION",
            user_ids=user_ids,
            reason=reason,
        )
        assert dup is False

        # 3. Retrieve pending action by ID
        action = await db.get_pending_mass_action(action_id)
        assert action is not None
        assert action["action_id"] == action_id
        assert action["guild_id"] == guild_id
        assert action["action_type"] == "EMAIL_POLICY_REVOCATION"
        assert action["user_ids"] == user_ids
        assert action["status"] == "PENDING"
        assert action["reason"] == reason

        # 4. Check active pending action for guild
        active = await db.get_active_pending_mass_action_for_guild(guild_id, "EMAIL_POLICY_REVOCATION")
        assert active is not None
        assert active["action_id"] == action_id

        # 5. List pending actions
        actions = await db.list_pending_mass_actions(guild_id=guild_id, status="PENDING")
        assert len(actions) == 1
        assert actions[0]["action_id"] == action_id

        # 6. Update status to APPROVED
        updated = await db.update_pending_mass_action_status(action_id, "APPROVED", decided_by_id=55555)
        assert updated is True

        action_after = await db.get_pending_mass_action(action_id)
        assert action_after["status"] == "APPROVED"
        assert action_after["decided_by_id"] == 55555
        assert action_after["decided_at"] is not None

        # No active pending action left
        assert await db.get_active_pending_mass_action_for_guild(guild_id, "EMAIL_POLICY_REVOCATION") is None

        # Updating non-existent action
        assert await db.update_pending_mass_action_status("MREV-NONEXISTENT", "APPROVED") is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_database_programme_code(tmp_path):
    db_file = str(tmp_path / "programme_code_test.db")
    db = Database(db_file)
    await db.connect()
    try:
        # Record with programme_code
        await db.record_verification(
            discord_user_id=123456,
            student_id_hash="hash_123456",
            faculty_code="M",
            campus_code="W",
            level_code="R",
            programme_code="WMR",
        )
        details = await db.get_verification_details(123456)
        assert details is not None
        assert details["programme_code"] == "WMR"
        assert details["campus_code"] == "W"
        assert details["faculty_code"] == "M"

        # Update programme_code
        await db.update_verification_details(123456, programme_code="WMD")
        details2 = await db.get_verification_details(123456)
        assert details2["programme_code"] == "WMD"

        # Test backfill
        await db.record_verification(
            discord_user_id=999999,
            student_id_hash="hash_999999",
            faculty_code="A",
            campus_code="W",
            level_code="D",
            programme_code=None,
        )
        backfilled = await db.backfill_legacy_verifications()
        assert backfilled >= 1
        details3 = await db.get_verification_details(999999)
        assert details3["programme_code"] == "WAD"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_database_bounced_emails(tmp_path):
    db_file = str(tmp_path / "bounced_emails_test.db")
    db = Database(db_file)
    await db.connect()
    try:
        e_hash = "hash_bounce_1"
        assert await db.is_email_bounced(e_hash) is False

        # Record bounced email
        await db.record_bounced_email(
            email_hash=e_hash,
            email_encrypted="enc_bounce_1",
            bounce_code=550,
            bounce_reason="Mailbox not found",
        )
        assert await db.is_email_bounced(e_hash) is True

        # Get details
        info = await db.get_bounced_email(e_hash)
        assert info is not None
        assert info["bounce_code"] == 550
        assert info["bounce_reason"] == "Mailbox not found"

        # List
        bounced_list = await db.list_bounced_emails()
        assert len(bounced_list) == 1
        assert bounced_list[0]["email_hash"] == e_hash

        # Remove
        removed = await db.remove_bounced_email(e_hash)
        assert removed is True
        assert await db.is_email_bounced(e_hash) is False
    finally:
        await db.close()




