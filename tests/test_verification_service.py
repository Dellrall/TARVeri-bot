import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

from tarveri.config import hash_student_id
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import RoleSyncResult, VerificationService


@pytest.mark.asyncio
async def test_format_role_summary():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    result = RoleSyncResult(
        verified_in=[("Server A", "FOCS")],
        already_had_role_in=[("Server B", "FOCS")],
        missing_role_in=["Server C"],
        failed_in=["Server D"],
    )

    summary = service.format_role_summary(result)
    assert "Server A" in summary
    assert "FOCS" in summary
    assert "Server B" in summary
    assert "Server C" in summary
    assert "Server D" in summary


@pytest.mark.asyncio
async def test_perform_verification_invalid_format(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "service_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    user = MagicMock()
    user.id = 12345
    user.__str__.return_value = "TestUser#0001"

    response = await service.perform_verification(user, "invalid_id")
    assert "Invalid student ID format" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_duplicate_id(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "duplicate_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    secret = "secret_123"
    service = VerificationService(bot, db, secret, rate_limiter)

    student_id = "23WMD09867"
    hashed = hash_student_id(student_id, secret)

    # First user is already verified with this ID
    await db.record_verification(11111, hashed, "M")

    # Second user tries to use same student ID
    second_user = MagicMock()
    second_user.id = 22222
    second_user.__str__.return_value = "SecondUser#0002"

    response = await service.perform_verification(second_user, student_id)
    assert "already been used to verify a different Discord account" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_already_verified_resync(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Campus Server"
    bot.guilds = [guild]

    db = Database(str(tmp_path / "resync_test.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        secret = "secret_123"
        service = VerificationService(bot, db, secret, rate_limiter)

        student_id = "23WMD09867"
        hashed = hash_student_id(student_id, secret)
        user_id = 33333

        # User already verified
        await db.record_verification(user_id, hashed, "WM", campus_code="W", level_code="D")

        user = MagicMock()
        user.id = user_id
        user.__str__.return_value = "User#3333"

        # Mock member with all existing roles (faculty + campus + study level)
        member = MagicMock(spec=discord.Member)
        role = MagicMock(spec=discord.Role)
        role.name = "FOCS"
        camp_role = MagicMock(spec=discord.Role)
        camp_role.name = "KL Main Campus"
        lvl_role = MagicMock(spec=discord.Role)
        lvl_role.name = "Diploma"
        member.roles = [role, camp_role, lvl_role]
        guild.get_member.return_value = member

        response = await service.perform_verification(user, student_id)
        assert "already had a faculty role" in response or "already verified" in response
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_perform_verification_rate_limited(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "ratelimit_test.db"))
    await db.connect()
    rate_limiter = RateLimiter(max_attempts=1, window_seconds=60)
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 44444
    user.__str__.return_value = "RateLimitedUser#0001"

    # First attempt consumes the 1 allowed attempt
    rate_limiter.record_attempt(user.id)
    assert rate_limiter.is_rate_limited(user.id)

    response = await service.perform_verification(user, "23WMD09867")
    assert "too many verification attempts" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_unknown_faculty_code(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "unknown_faculty_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 55555
    user.__str__.return_value = "User#5555"

    # 'Z' is not a valid faculty code
    response = await service.perform_verification(user, "23WZD09867")
    assert "Student ID does not match any known faculty" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_account_already_verified_academic_transition(tmp_path):
    bot = MagicMock()
    db = Database(str(tmp_path / "different_id_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    secret = "secret_123"
    service = VerificationService(bot, db, secret, rate_limiter)

    user_id = 66666
    old_id = "23WMD09867"
    old_hash = hash_student_id(old_id, secret)
    await db.record_verification(user_id, old_hash, "M", campus_code="W", level_code="D")

    user = MagicMock()
    user.id = user_id
    user.__str__.return_value = "User#6666"

    # User attempts to transition to Degree (23WMR11111)
    response = await service.perform_verification(user, "23WMR11111", raw_expiry_date="10/26")
    assert "Academic Level Progression Successful" in response
    assert "FOCS" in response
    assert "Degree" in response

    # Transitions record exists
    transitions = await db.get_academic_transitions_for_user(user_id)
    assert len(transitions) == 1
    assert transitions[0]["from_id_hash"] == old_hash
    assert transitions[0]["to_level_code"] == "R"

    # Verifications table is updated
    updated = await db.get_verification_details(user_id)
    assert updated["level_code"] == "R"
    assert updated["card_expiry_date"] == "2026-10-31"

    # Now another user (user_id 99999) tries to verify with the same student ID (23WMR11111) -> should fail as duplicate
    user2 = MagicMock()
    user2.id = 99999
    user2.__str__.return_value = "User#9999"
    response2 = await service.perform_verification(user2, "23WMR11111")
    assert "already been used to verify a different Discord account" in response2

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_no_mutual_guilds(tmp_path):
    bot = MagicMock()
    bot.guilds = []
    db = Database(str(tmp_path / "no_guilds_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 77777
    user.__str__.return_value = "User#7777"

    response = await service.perform_verification(user, "23WMD09867")
    assert "couldn't find you in any server" in response

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_role_creation_and_assignment_success(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Campus Alpha"
    guild.roles = []
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = MagicMock()

    fac_role = MagicMock(spec=discord.Role)
    fac_role.name = "FOCS"
    fac_role.position = 10
    fac_role.__ge__.return_value = False

    campus_role = MagicMock(spec=discord.Role)
    campus_role.name = "KL Main Campus"
    campus_role.position = 9
    campus_role.__ge__.return_value = False

    level_role = MagicMock(spec=discord.Role)
    level_role.name = "Diploma"
    level_role.position = 8
    level_role.__ge__.return_value = False

    def _create_role_side_effect(**kwargs):
        name = kwargs.get("name")
        if name == "FOCS":
            return fac_role
        elif name == "KL Main Campus":
            return campus_role
        elif name == "Diploma":
            return level_role
        r = MagicMock(spec=discord.Role)
        r.name = name
        r.__ge__.return_value = False
        return r

    guild.create_role = AsyncMock(side_effect=_create_role_side_effect)

    member = MagicMock(spec=discord.Member)
    member.roles = []
    member.add_roles = AsyncMock()
    guild.get_member.return_value = member

    bot.guilds = [guild]

    db = Database(str(tmp_path / "success_assign_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 88888
    user.__str__.return_value = "User#8888"

    response = await service.perform_verification(user, "23WMD09867")
    assert "You've been given the following role(s)" in response
    assert "Campus Alpha" in response
    assert "FOCS" in response
    assert "KL Main Campus" in response
    assert "Diploma" in response

    assert guild.create_role.call_count == 3
    member.add_roles.assert_called_once_with(fac_role, campus_role, level_role, reason="TARVeri: Student verification role assignment")

    # Verification recorded in DB with campus and level codes
    record = await db.get_verification_by_user(88888)
    assert record is not None
    assert record[1] == "M"

    details = await db.get_verification_details(88888)
    assert details is not None
    assert details["campus_code"] == "W"
    assert details["level_code"] == "D"

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_role_create_missing_manage_roles_permission(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Locked Server"
    guild.roles = []
    guild.me = MagicMock()
    # Bot does NOT have manage_roles permission
    guild.me.guild_permissions.manage_roles = False

    member = MagicMock(spec=discord.Member)
    member.roles = []
    guild.get_member.return_value = member
    bot.guilds = [guild]

    db = Database(str(tmp_path / "noperms_test.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    user = MagicMock()
    user.id = 99999
    user.__str__.return_value = "User#9999"

    response = await service.perform_verification(user, "23WMD09867")
    assert "I couldn't create/find the required role" in response
    assert "Locked Server" in response

    # Should NOT record verification in DB because no roles were assigned
    record = await db.get_verification_by_user(99999)
    assert record is None

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_database_collision_rollback(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 555123
    guild.name = "Rollback Server"

    existing_role = MagicMock(spec=discord.Role)
    existing_role.name = "FOCS"
    existing_role.__ge__.return_value = False
    guild.roles = [existing_role]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = MagicMock()
    guild.create_role = AsyncMock()

    member = MagicMock(spec=discord.Member)
    member.roles = []

    async def _mock_add_roles(*roles, **kwargs):
        for r in roles:
            member.roles.append(r)

    member.add_roles = AsyncMock(side_effect=_mock_add_roles)
    member.remove_roles = AsyncMock()
    guild.get_member.return_value = member
    bot.guilds = [guild]
    bot.get_guild.return_value = guild

    db = Database(str(tmp_path / "collision_test.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        user = MagicMock()
        user.id = 10101
        user.__str__.return_value = "User#10101"

        # Simulate database collision on record_verification by raising IntegrityError
        with patch.object(db, "record_verification", side_effect=sqlite3.IntegrityError("UNIQUE constraint failed")):
            response = await service.perform_verification(user, "23WMD09867")
            assert "Verification failed due to a collision" in response
            # Rollback should remove the assigned role
            assert member.remove_roles.called
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_verified_members_restores_missing_faculty_role(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.name = "Reconcile Guild"

    # Setup FOCS role
    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.position = 10
    guild.roles = [focs_role]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock(spec=discord.Role)
    bot_top_role.name = "TARVeri Bot"
    bot_top_role.position = 50
    guild.me.top_role = bot_top_role

    # Student 55555 is verified in DB as FOCS ("M"), has campus role, but missing faculty role in Discord
    camp_role = MagicMock(spec=discord.Role)
    camp_role.name = "KL Main Campus"
    camp_role.position = 8
    guild.roles = [focs_role, camp_role]

    member = MagicMock(spec=discord.Member)
    member.id = 55555
    member.roles = [camp_role]
    member.add_roles = AsyncMock()

    guild.get_member.side_effect = lambda uid: member if uid == 55555 else None
    bot.guilds = [guild]

    db = Database(str(tmp_path / "reconcile_roles_test.db"))
    await db.connect()
    await db.record_verification(55555, "hash_55555", "M", campus_code="W")

    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    # Run reconciliation
    summary = await service.reconcile_verified_members(guild)
    assert summary["checked"] == 1
    assert summary["restored"] == 1
    assert summary["failed"] == 0

    member.add_roles.assert_awaited_once_with(
        focs_role, reason="TARVeri: Self-healing automatic role restoration for verified student"
    )

    await db.close()


@pytest.mark.asyncio
async def test_backfill_branch_roles_migration(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 888999
    guild.name = "Migration Guild"

    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.position = 10

    penang_role = MagicMock(spec=discord.Role)
    penang_role.name = "Penang Branch"
    penang_role.position = 8

    degree_role = MagicMock(spec=discord.Role)
    degree_role.name = "Degree"
    degree_role.position = 6

    guild.roles = [focs_role, penang_role, degree_role]
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top = MagicMock(spec=discord.Role)
    bot_top.position = 50
    guild.me.top_role = bot_top

    # Member 66666 only has FOCS role currently
    member = MagicMock(spec=discord.Member)
    member.id = 66666
    member.roles = [focs_role]
    member.add_roles = AsyncMock()

    guild.get_member.return_value = member
    bot.guilds = [guild]

    db_path = str(tmp_path / "backfill_srv_test.db")
    db = Database(db_path)
    await db.connect()

    # User verified as Penang Degree student in DB
    await db.record_verification(66666, "hash_66666", "M", campus_code="P", level_code="R")

    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    stats = await service.backfill_branch_roles(guild=guild, default_campus_code="P")
    assert stats["guilds_scanned"] == 1
    assert stats["members_checked"] == 1
    assert stats["roles_assigned"] == 2  # Penang Branch + Degree roles assigned
    assert stats["failed"] == 0

    assert member.add_roles.called
    added_roles = member.add_roles.call_args[0]
    assert penang_role in added_roles
    assert degree_role in added_roles

    await db.close()


def test_diagnose_guild_permissions_hierarchy_and_permissions(tmp_path):
    bot = MagicMock()
    db = MagicMock()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_123", rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.name = "Diagnosis Guild"

    # 1. Missing Manage Roles permission
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = False
    bot_top_role = MagicMock()
    bot_top_role.name = "TARVeri"
    bot_top_role.position = 10
    guild.me.top_role = bot_top_role
    guild.roles = []

    warnings = service.diagnose_guild_permissions(guild)
    assert any("Manage Roles" in w for w in warnings)

    # 2. Hierarchy conflict: Faculty role higher than bot role
    guild.me.guild_permissions.manage_roles = True
    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.position = 20  # Higher than bot (10)
    guild.roles = [focs_role]

    warnings = service.diagnose_guild_permissions(guild)
    assert any("Role hierarchy conflict" in w and "FOCS" in w for w in warnings)

    # 3. Healthy configuration: Bot role higher than all managed roles
    bot_top_role.position = 100
    warnings = service.diagnose_guild_permissions(guild)
    assert warnings == []

    # 4. Duplicate role detection: multiple roles matching same faculty
    extra_focs = MagicMock(spec=discord.Role)
    extra_focs.name = "focs"
    extra_focs.position = 5
    guild.roles = [focs_role, extra_focs]
    warnings = service.diagnose_guild_permissions(guild)
    assert any("Duplicate faculty roles detected" in w and "FOCS" in w for w in warnings)


@pytest.mark.asyncio
async def test_find_faculty_role_multi_tier_matching():
    bot = MagicMock()
    db = MagicMock()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    guild = MagicMock(spec=discord.Guild)

    # 1. Exact match
    r1 = MagicMock(spec=discord.Role, name="FOCS")
    r1.name = "FOCS"
    r1.position = 10
    guild.roles = [r1]
    assert await service.find_faculty_role(guild, "FOCS") == r1

    # 2. Case-insensitive & trimmed match
    r2 = MagicMock(spec=discord.Role, name="focs ")
    r2.name = "focs "
    r2.position = 10
    guild.roles = [r2]
    assert await service.find_faculty_role(guild, "FOCS") == r2

    # 3. Normalized alphanumeric / bracket / emoji match
    r3 = MagicMock(spec=discord.Role, name="[FOCS]")
    r3.name = "[FOCS]"
    r3.position = 10
    guild.roles = [r3]
    assert await service.find_faculty_role(guild, "FOCS") == r3

    # 4. Prefix & word-boundary match
    r4 = MagicMock(spec=discord.Role, name="FOCS - Faculty of Computing")
    r4.name = "FOCS - Faculty of Computing"
    r4.position = 10
    guild.roles = [r4]
    assert await service.find_faculty_role(guild, "FOCS") == r4

    # 5. Full name expansion without acronym (e.g. "Faculty of Computing and Information Technology")
    r5 = MagicMock(spec=discord.Role, name="Faculty of Computing and Information Technology")
    r5.name = "Faculty of Computing and Information Technology"
    r5.position = 10
    guild.roles = [r5]
    assert await service.find_faculty_role(guild, "FOCS") == r5

    # 6. Pre-University Studies for CPUS
    r6 = MagicMock(spec=discord.Role, name="Centre for Pre-University Studies")
    r6.name = "Centre for Pre-University Studies"
    r6.position = 10
    guild.roles = [r6]
    assert await service.find_faculty_role(guild, "CPUS") == r6

    # 7. Engineering & Technology for FOET
    r7 = MagicMock(spec=discord.Role, name="Faculty of Engineering & Technology")
    r7.name = "Faculty of Engineering & Technology"
    r7.position = 10
    guild.roles = [r7]
    assert await service.find_faculty_role(guild, "FOET") == r7


@pytest.mark.asyncio
async def test_find_faculty_role_live_fetch_roles_fallback():
    bot = MagicMock()
    db = MagicMock()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    # Cache is empty
    guild.roles = []

    live_role = MagicMock(spec=discord.Role, name="FOCS")
    live_role.name = "FOCS"
    live_role.position = 15
    guild.fetch_roles = AsyncMock(return_value=[live_role])

    # Should query fetch_roles and return the live role
    found = await service.find_faculty_role(guild, "FOCS")
    assert found == live_role
    guild.fetch_roles.assert_called_once()


@pytest.mark.asyncio
async def test_perform_verification_never_duplicates_existing_fuzzy_or_cached_role(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 998811
    guild.name = "Banana Hub"

    # Server already has existing roles (FOCS, KL Main Campus, Diploma)
    existing_role = MagicMock(spec=discord.Role)
    existing_role.name = "FOCS"
    existing_role.position = 5
    existing_role.__ge__.return_value = False

    existing_campus = MagicMock(spec=discord.Role)
    existing_campus.name = "KL Main Campus"
    existing_campus.position = 4
    existing_campus.__ge__.return_value = False

    existing_level = MagicMock(spec=discord.Role)
    existing_level.name = "Diploma"
    existing_level.position = 3
    existing_level.__ge__.return_value = False

    guild.roles = [existing_role, existing_campus, existing_level]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock()
    bot_top_role.position = 50
    guild.me.top_role = bot_top_role
    guild.create_role = AsyncMock()

    member = MagicMock(spec=discord.Member)
    member.roles = []
    member.add_roles = AsyncMock()
    guild.get_member.return_value = member
    bot.guilds = [guild]
    bot.get_guild.return_value = guild

    db = Database(str(tmp_path / "never_duplicate.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        user = MagicMock()
        user.id = 12345
        user.__str__.return_value = "Student#1234"

        response = await service.perform_verification(user, "23WMD09867")
        assert "You've been given the following role(s)" in response
        assert "FOCS" in response
        assert "KL Main Campus" in response
        assert "Diploma" in response

        # create_role must NEVER be called because the roles already exist
        guild.create_role.assert_not_called()
        # existing roles were assigned to member
        member.add_roles.assert_called_once_with(existing_role, existing_campus, existing_level, reason="TARVeri: Student verification role assignment")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_duplicate_roles_migrates_members_and_deletes_redundant_roles(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 998822
    guild.name = "Dedup Guild"

    # 1. Primary FOCS role (pos: 15, name: "FOCS")
    primary_focs = MagicMock(spec=discord.Role)
    primary_focs.id = 1001
    primary_focs.name = "FOCS"
    primary_focs.position = 15
    primary_focs.managed = False
    primary_focs.is_default.return_value = False
    primary_focs.delete = AsyncMock()

    # 2. Redundant duplicate FOCS role 1 (pos: 2, name: "focs")
    redundant_focs_1 = MagicMock(spec=discord.Role)
    redundant_focs_1.id = 1002
    redundant_focs_1.name = "focs"
    redundant_focs_1.position = 2
    redundant_focs_1.managed = False
    redundant_focs_1.is_default.return_value = False
    redundant_focs_1.delete = AsyncMock()

    # 3. Redundant duplicate FOCS role 2 (pos: 1, name: "[FOCS]")
    redundant_focs_2 = MagicMock(spec=discord.Role)
    redundant_focs_2.id = 1003
    redundant_focs_2.name = "[FOCS]"
    redundant_focs_2.position = 1
    redundant_focs_2.managed = False
    redundant_focs_2.is_default.return_value = False
    redundant_focs_2.delete = AsyncMock()

    # 4. Guest roles
    primary_guest = MagicMock(spec=discord.Role)
    primary_guest.id = 2001
    primary_guest.name = "Guest(Approved)"
    primary_guest.position = 10
    primary_guest.managed = False
    primary_guest.is_default.return_value = False
    primary_guest.delete = AsyncMock()

    redundant_guest = MagicMock(spec=discord.Role)
    redundant_guest.id = 2002
    redundant_guest.name = "Guest"
    redundant_guest.position = 3
    redundant_guest.managed = False
    redundant_guest.is_default.return_value = False
    redundant_guest.delete = AsyncMock()

    # Members setup
    m1 = MagicMock(spec=discord.Member)  # Already has primary FOCS
    m1.roles = [primary_focs]
    m1.add_roles = AsyncMock()
    m1.remove_roles = AsyncMock()

    m2 = MagicMock(spec=discord.Member)  # Has redundant FOCS 1, missing primary
    m2.roles = [redundant_focs_1]
    m2.add_roles = AsyncMock()
    m2.remove_roles = AsyncMock()

    m3 = MagicMock(spec=discord.Member)  # Has redundant FOCS 2 and primary
    m3.roles = [redundant_focs_2, primary_focs]
    m3.add_roles = AsyncMock()
    m3.remove_roles = AsyncMock()

    guest_m = MagicMock(spec=discord.Member)  # Has redundant Guest, missing primary
    guest_m.roles = [redundant_guest]
    guest_m.add_roles = AsyncMock()
    guest_m.remove_roles = AsyncMock()

    primary_focs.members = [m1, m3]
    redundant_focs_1.members = [m2]
    redundant_focs_2.members = [m3]
    primary_guest.members = []
    redundant_guest.members = [guest_m]

    guild.roles = [primary_focs, redundant_focs_1, redundant_focs_2, primary_guest, redundant_guest]

    # Bot perms and top role
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock()
    bot_top_role.position = 50
    guild.me.top_role = bot_top_role

    db = Database(str(tmp_path / "dedup_test.db"))
    await db.connect()
    try:
        # Record redundant roles as created by the bot
        await db.record_bot_created_role(guild.id, 1002, "focs")
        await db.record_bot_created_role(guild.id, 1003, "Faculty of Computing")
        await db.record_bot_created_role(guild.id, 2002, "Guest")

        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        stats = await service.reconcile_duplicate_roles(guild)

        assert stats["deleted_roles"] == 3
        assert stats["migrated_members"] == 2  # m2 and guest_m
        assert stats["failed"] == 0

        # Verify m2 was given primary FOCS and had redundant FOCS removed
        m2.add_roles.assert_called_once()
        assert m2.add_roles.call_args[0][0] == primary_focs
        m2.remove_roles.assert_called_once_with(
            redundant_focs_1, reason="TARVeri Self-Healing: Remove duplicate role 'focs'"
        )

        # Verify guest_m was given primary guest
        guest_m.add_roles.assert_called_once()
        assert guest_m.add_roles.call_args[0][0] == primary_guest

        # Verify all 3 redundant roles were deleted
        redundant_focs_1.delete.assert_called_once()
        redundant_focs_2.delete.assert_called_once()
        redundant_guest.delete.assert_called_once()
        primary_focs.delete.assert_not_called()
        primary_guest.delete.assert_not_called()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_duplicate_roles_preserves_admin_created_roles(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 776655
    guild.name = "Admin Role Preservation Guild"

    primary_focs = MagicMock(spec=discord.Role)
    primary_focs.id = 5001
    primary_focs.name = "FOCS"
    primary_focs.position = 20
    primary_focs.members = []
    primary_focs.managed = False
    primary_focs.is_default.return_value = False
    primary_focs.delete = AsyncMock()

    # Admin created a duplicate role "focs" manually
    admin_created_focs = MagicMock(spec=discord.Role)
    admin_created_focs.id = 5002
    admin_created_focs.name = "focs"
    admin_created_focs.position = 10
    admin_created_focs.members = []
    admin_created_focs.managed = False
    admin_created_focs.is_default.return_value = False
    admin_created_focs.delete = AsyncMock()

    guild.roles = [primary_focs, admin_created_focs]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.guild_permissions.view_audit_log = False
    bot_top_role = MagicMock()
    bot_top_role.position = 50
    guild.me.top_role = bot_top_role

    db = Database(str(tmp_path / "admin_preserve.db"))
    await db.connect()
    try:
        # Neither role was recorded in bot_created_roles
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        stats = await service.reconcile_duplicate_roles(guild)

        # Admin created duplicate role MUST NOT be deleted
        assert stats["deleted_roles"] == 0
        admin_created_focs.delete.assert_not_called()
        primary_focs.delete.assert_not_called()
        assert any("Preserved admin-created role" in d for d in stats["details"])
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_duplicate_roles_handles_hierarchy_and_forbidden_gracefully(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 998833
    guild.name = "Hierarchy Guild"

    primary_focs = MagicMock(spec=discord.Role)
    primary_focs.id = 9001
    primary_focs.name = "FOCS"
    primary_focs.position = 10
    primary_focs.members = []
    primary_focs.managed = False
    primary_focs.is_default.return_value = False
    primary_focs.delete = AsyncMock()

    # Redundant role is above bot's top role, but tracked as bot created
    high_focs = MagicMock(spec=discord.Role)
    high_focs.id = 9002
    high_focs.name = "focs-high"
    high_focs.position = 60
    high_focs.members = []
    high_focs.managed = False
    high_focs.is_default.return_value = False
    high_focs.delete = AsyncMock()

    guild.roles = [high_focs, primary_focs]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock()
    bot_top_role.position = 20  # Below high_focs (60)
    guild.me.top_role = bot_top_role

    db = Database(str(tmp_path / "hierarchy_dedup.db"))
    await db.connect()
    try:
        await db.record_bot_created_role(guild.id, 9002, "focs-high")
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        stats = await service.reconcile_duplicate_roles(guild)

        # High role should NOT be deleted due to hierarchy
        high_focs.delete.assert_not_called()
        primary_focs.delete.assert_not_called()
        assert stats["deleted_roles"] == 0
        assert stats["failed"] >= 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_src_roles_never_matched_or_deleted_as_duplicates(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 112233
    guild.name = "SRC Protection Guild"

    # Regular FOCS role
    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.position = 15
    focs_role.members = []
    focs_role.delete = AsyncMock()

    # FOCS SRC role (MUST NEVER be matched or deleted)
    focs_src_role = MagicMock(spec=discord.Role)
    focs_src_role.name = "FOCS SRC"
    focs_src_role.position = 12
    focs_src_role.members = []
    focs_src_role.delete = AsyncMock()

    # FOET Council role
    foet_council = MagicMock(spec=discord.Role)
    foet_council.name = "FOET Council"
    foet_council.position = 10
    foet_council.members = []
    foet_council.delete = AsyncMock()

    guild.roles = [focs_role, focs_src_role, foet_council]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    bot_top_role = MagicMock()
    bot_top_role.position = 50
    guild.me.top_role = bot_top_role

    db = Database(str(tmp_path / "src_protection.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        # 1. Matching engine must NOT match FOCS SRC to FOCS
        matched = service._match_faculty_role_in_list([focs_src_role], "FOCS")
        assert matched is None

        # 2. Reconcile duplicate roles must NOT delete FOCS SRC or FOET Council
        stats = await service.reconcile_duplicate_roles(guild)
        assert stats["deleted_roles"] == 0
        focs_src_role.delete.assert_not_called()
        foet_council.delete.assert_not_called()
        focs_role.delete.assert_not_called()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_restore_src_roles_creates_all_8_roles(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 554433
    guild.name = "SRC Restore Guild"

    # Server already has FOCS SRC
    existing_focs_src = MagicMock(spec=discord.Role)
    existing_focs_src.name = "FOCS SRC"
    guild.roles = [existing_focs_src]

    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    created_roles = []

    async def mock_create_role(name, colour, mentionable, reason):
        r = MagicMock(spec=discord.Role)
        r.name = name
        created_roles.append(r)
        return r

    guild.create_role = AsyncMock(side_effect=mock_create_role)

    db = Database(str(tmp_path / "src_restore.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_123", rate_limiter)

        stats = await service.restore_src_roles(guild)

        assert stats["created"] == 7  # 8 total - 1 already existing
        assert stats["existing"] == 1
        assert stats["failed"] == 0
        assert guild.create_role.call_count == 7
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_perform_verification_when_student_already_has_target_faculty_role(tmp_path):
    """
    Ensures that when a student manually self-assigned their target faculty role in Discord,
    perform_verification still records their verification in the DB and renders as verified on their card.
    """
    from tarveri.services.card_service import CardService

    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    guild.name = "Pre-assigned Role Guild"
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = MagicMock()

    # Pre-existing faculty role in member.roles
    fac_role = MagicMock(spec=discord.Role)
    fac_role.name = "FOCS"
    fac_role.position = 5
    fac_role.__ge__.return_value = False
    guild.roles = [fac_role]

    camp_role = MagicMock(spec=discord.Role)
    camp_role.name = "KL Main Campus"
    camp_role.position = 4
    camp_role.__ge__.return_value = False

    lvl_role = MagicMock(spec=discord.Role)
    lvl_role.name = "Diploma"
    lvl_role.position = 3
    lvl_role.__ge__.return_value = False

    def _create_role_side_effect(**kwargs):
        name = kwargs.get("name")
        if name == "KL Main Campus":
            return camp_role
        elif name == "Diploma":
            return lvl_role
        r = MagicMock(spec=discord.Role)
        r.name = name
        r.__ge__.return_value = False
        return r

    guild.create_role = AsyncMock(side_effect=_create_role_side_effect)

    member = MagicMock(spec=discord.Member)
    member.id = 55555
    member.display_name = "Alex"
    member.name = "alex_student"
    member.roles = [fac_role]  # already has FOCS manually assigned
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()
    member.guild_permissions.administrator = False
    member.premium_since = None
    member.joined_at = None

    guild.get_member.return_value = member
    bot.guilds = [guild]

    db = Database(str(tmp_path / "manual_assign_test.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_key", rate_limiter)
        card_service = CardService(db)

        # 1. Before verification: card data is UNVERIFIED
        card_before = await card_service.get_user_card_data(guild, member)
        assert card_before["is_student"] is False
        assert "○ UNVERIFIED" in card_before["badges"]

        # 2. Perform verification with valid student ID
        response = await service.perform_verification(member, "23WMD09867")
        assert "KL Main Campus" in response or "officially verified" in response

        # 3. Missing campus & level roles were assigned
        member.add_roles.assert_called_once_with(camp_role, lvl_role, reason="TARVeri: Student verification role assignment")

        # 4. Database MUST record verification
        record = await db.get_verification_by_user(55555)
        assert record is not None
        assert record[1] == "M"

        # 5. After verification: card is VERIFIED
        card_after = await card_service.get_user_card_data(guild, member)
        assert card_after["is_student"] is True
        assert card_after["faculty_name"] == "FOCS"
        assert "✓ VERIFIED" in card_after["badges"]
        assert "✦ FOCS" in card_after["badges"]
        assert card_after["hash_preview"].startswith("TRV-")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_perform_verification_when_student_has_conflicting_faculty_role(tmp_path):
    """
    Ensures that when a student manually self-assigned the WRONG faculty role (e.g. FAFB),
    perform_verification removes FAFB and assigns the correct FOCS role.
    """
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 67890
    guild.name = "Conflicting Role Guild"
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = MagicMock()

    # Wrong faculty role manually self-assigned earlier
    wrong_role = MagicMock(spec=discord.Role)
    wrong_role.name = "FAFB"
    wrong_role.position = 5
    wrong_role.__ge__.return_value = False

    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.position = 5
    focs_role.__ge__.return_value = False

    camp_role = MagicMock(spec=discord.Role)
    camp_role.name = "KL Main Campus"
    camp_role.position = 4
    camp_role.__ge__.return_value = False

    lvl_role = MagicMock(spec=discord.Role)
    lvl_role.name = "Diploma"
    lvl_role.position = 3
    lvl_role.__ge__.return_value = False

    guild.roles = [wrong_role]

    def _create_role_side_effect(**kwargs):
        name = kwargs.get("name")
        if name == "FOCS":
            return focs_role
        elif name == "KL Main Campus":
            return camp_role
        elif name == "Diploma":
            return lvl_role
        r = MagicMock(spec=discord.Role)
        r.name = name
        r.__ge__.return_value = False
        return r

    guild.create_role = AsyncMock(side_effect=_create_role_side_effect)

    member = MagicMock(spec=discord.Member)
    member.id = 77777
    member.roles = [wrong_role]
    member.add_roles = AsyncMock()
    member.remove_roles = AsyncMock()

    guild.get_member.return_value = member
    bot.guilds = [guild]

    db = Database(str(tmp_path / "conflict_assign_test.db"))
    await db.connect()
    try:
        rate_limiter = RateLimiter()
        service = VerificationService(bot, db, "secret_key", rate_limiter)

        response = await service.perform_verification(member, "23WMD09867")
        assert "FOCS" in response

        # Conflicting wrong role removed
        member.remove_roles.assert_called_once_with(wrong_role, reason="TARVeri: Reconcile faculty/campus/level role mismatch")

        # Correct roles added
        member.add_roles.assert_called_once_with(focs_role, camp_role, lvl_role, reason="TARVeri: Student verification role assignment")

        # Recorded in DB
        record = await db.get_verification_by_user(77777)
        assert record is not None
        assert record[1] == "M"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_cpus_faculty_and_foundation_level_domain_isolation():
    """Verifies that CPUS (faculty) and Foundation (study level) roles never match or collide."""
    bot = MagicMock()
    db = MagicMock()
    service = VerificationService(bot, db, "secret_key", RateLimiter())

    # Faculty matcher tests for CPUS
    role_foundation = MagicMock(spec=discord.Role, name="Foundation")
    role_foundation.name = "Foundation"
    role_foundation.position = 10

    role_foundation_studies = MagicMock(spec=discord.Role, name="Foundation Studies")
    role_foundation_studies.name = "Foundation Studies"
    role_foundation_studies.position = 10

    role_cpus = MagicMock(spec=discord.Role, name="CPUS")
    role_cpus.name = "CPUS"
    role_cpus.position = 10

    role_cpus_full = MagicMock(spec=discord.Role, name="Centre for Pre-University Studies")
    role_cpus_full.name = "Centre for Pre-University Studies"
    role_cpus_full.position = 10

    # _match_faculty_role_in_list for CPUS must NOT match Foundation roles
    assert service._match_faculty_role_in_list([role_foundation], "CPUS") is None
    assert service._match_faculty_role_in_list([role_foundation_studies], "CPUS") is None
    assert service._match_faculty_role_in_list([role_cpus], "CPUS") == role_cpus
    assert service._match_faculty_role_in_list([role_cpus_full], "CPUS") == role_cpus_full

    # _match_study_level_role_in_list for Foundation must NOT match CPUS roles
    assert service._match_study_level_role_in_list([role_cpus], "Foundation") is None
    assert service._match_study_level_role_in_list([role_cpus_full], "Foundation") is None
    assert service._match_study_level_role_in_list([role_foundation], "Foundation") == role_foundation
    assert service._match_study_level_role_in_list([role_foundation_studies], "Foundation") == role_foundation_studies


@pytest.mark.asyncio
async def test_reconcile_duplicate_roles_does_not_delete_foundation_when_cpus_exists(tmp_path):
    """Verifies that self-healing deduplication treats CPUS and Foundation as separate categories and never deletes either."""
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    guild.name = "TARUMT Test Server"

    # CPUS role (Faculty)
    cpus_role = MagicMock(spec=discord.Role)
    cpus_role.id = 101
    cpus_role.name = "CPUS"
    cpus_role.position = 10
    cpus_role.managed = False
    cpus_role.is_default.return_value = False
    cpus_role.delete = AsyncMock()
    cpus_role.members = []

    # Foundation role (Study Level)
    foundation_role = MagicMock(spec=discord.Role)
    foundation_role.id = 102
    foundation_role.name = "Foundation"
    foundation_role.position = 8
    foundation_role.managed = False
    foundation_role.is_default.return_value = False
    foundation_role.delete = AsyncMock()
    foundation_role.members = []

    guild.roles = [cpus_role, foundation_role]
    me = MagicMock(spec=discord.Member)
    me.guild_permissions.manage_roles = True
    me.top_role.position = 20
    guild.me = me

    db = Database(str(tmp_path / "dedup_cpus_foundation.db"))
    await db.connect()
    try:
        # Mark both as bot-created roles
        await db.record_bot_created_role(guild.id, cpus_role.id, "CPUS")
        await db.record_bot_created_role(guild.id, foundation_role.id, "Foundation")

        service = VerificationService(bot, db, "secret_key", RateLimiter())
        stats = await service.reconcile_duplicate_roles(guild)

        # Neither role was deleted!
        assert stats["deleted_roles"] == 0
        assert stats["migrated_members"] == 0
        cpus_role.delete.assert_not_called()
        foundation_role.delete.assert_not_called()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_reconcile_verified_members_for_cpus_and_foundation_students(tmp_path):
    """Verifies that self-healing role sync maintains both CPUS faculty and Foundation level roles simultaneously."""
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 88888
    guild.name = "TARUMT Sync Guild"

    cpus_role = MagicMock(spec=discord.Role)
    cpus_role.id = 201
    cpus_role.name = "CPUS"
    cpus_role.position = 10

    focs_role = MagicMock(spec=discord.Role)
    focs_role.id = 202
    focs_role.name = "FOCS"
    focs_role.position = 10

    camp_role = MagicMock(spec=discord.Role)
    camp_role.id = 203
    camp_role.name = "KL Main Campus"
    camp_role.position = 9

    foundation_role = MagicMock(spec=discord.Role)
    foundation_role.id = 204
    foundation_role.name = "Foundation"
    foundation_role.position = 8

    guild.roles = [cpus_role, focs_role, camp_role, foundation_role]
    me = MagicMock(spec=discord.Member)
    me.guild_permissions.manage_roles = True
    me.top_role.position = 20
    guild.me = me

    # Member 1: CPUS + Foundation (25WPF01234)
    m1 = MagicMock(spec=discord.Member)
    m1.id = 11111
    m1.roles = [cpus_role, camp_role, foundation_role]
    m1.add_roles = AsyncMock()
    m1.remove_roles = AsyncMock()

    # Member 2: FOCS + Foundation (25WMF01234)
    m2 = MagicMock(spec=discord.Member)
    m2.id = 22222
    m2.roles = [focs_role, camp_role, foundation_role]
    m2.add_roles = AsyncMock()
    m2.remove_roles = AsyncMock()

    def _get_member(uid):
        if uid == 11111:
            return m1
        elif uid == 22222:
            return m2
        return None

    guild.get_member.side_effect = _get_member
    bot.guilds = [guild]

    db = Database(str(tmp_path / "sync_cpus_foundation.db"))
    await db.connect()
    try:
        # Record member 1: CPUS ('P'), campus 'W', level 'F'
        hash_1 = hash_student_id("25WPF01234", "secret_key")
        await db.record_verification(11111, hash_1, "P", campus_code="W", level_code="F")

        # Record member 2: FOCS ('M'), campus 'W', level 'F'
        hash_2 = hash_student_id("25WMF01234", "secret_key")
        await db.record_verification(22222, hash_2, "M", campus_code="W", level_code="F")

        service = VerificationService(bot, db, "secret_key", RateLimiter())
        summary = await service.reconcile_verified_members(guild)

        assert summary["checked"] == 2
        assert summary["failed"] == 0

        # Neither member had Foundation role removed as a conflicting faculty role!
        m1.remove_roles.assert_not_called()
        m2.remove_roles.assert_not_called()
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unverify_member_cross_server(tmp_path):
    """Unverify member removes faculty, campus, level, and alumni roles across all mutual guilds."""
    db = Database(str(tmp_path / "unverify_cross_server.db"))
    await db.connect()

    bot = MagicMock()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_unverify", rate_limiter)

    user_id = 987654

    # Pre-record student in DB
    await db.record_verification(user_id, "hash_unv", "M", campus_code="W", level_code="R")
    await db.record_alumni_claim(user_id, 2024, "Bachelor of Computer Science")

    # Guild 1
    g1 = MagicMock(spec=discord.Guild)
    g1.id = 1001
    g1.name = "Main Guild"
    bot_m1 = MagicMock(spec=discord.Member)
    bot_m1.guild_permissions.manage_roles = True
    bot_top1 = MagicMock(spec=discord.Role)
    bot_top1.position = 20
    bot_m1.top_role = bot_top1
    g1.me = bot_m1

    focs_r1 = MagicMock(spec=discord.Role)
    focs_r1.name = "FOCS"
    focs_r1.position = 5

    camp_r1 = MagicMock(spec=discord.Role)
    camp_r1.name = "KL Main Campus"
    camp_r1.position = 4

    alumni_r1 = MagicMock(spec=discord.Role)
    alumni_r1.name = "TARUMT Alumni"
    alumni_r1.position = 3

    m_g1 = MagicMock(spec=discord.Member)
    m_g1.id = user_id
    m_g1.roles = [focs_r1, camp_r1, alumni_r1]
    m_g1.remove_roles = AsyncMock()
    g1.get_member.return_value = m_g1

    # Guild 2
    g2 = MagicMock(spec=discord.Guild)
    g2.id = 1002
    g2.name = "Gaming Club Guild"
    bot_m2 = MagicMock(spec=discord.Member)
    bot_m2.guild_permissions.manage_roles = True
    bot_top2 = MagicMock(spec=discord.Role)
    bot_top2.position = 20
    bot_m2.top_role = bot_top2
    g2.me = bot_m2

    focs_r2 = MagicMock(spec=discord.Role)
    focs_r2.name = "FOCS"
    focs_r2.position = 6

    m_g2 = MagicMock(spec=discord.Member)
    m_g2.id = user_id
    m_g2.roles = [focs_r2]
    m_g2.remove_roles = AsyncMock()
    g2.get_member.return_value = m_g2

    bot.guilds = [g1, g2]

    # Perform unverify
    res = await service.unverify_member(user_id=user_id, admin=None, reason="Graduation test")
    assert res["success"] is True
    assert res["guilds_count"] == 2
    assert len(res["roles_removed"]) == 4

    # DB record is gone
    assert await db.get_verification_by_user(user_id) is None
    await db.close()


@pytest.mark.asyncio
async def test_tiered_verification_opt_in_vs_opt_out(tmp_path):
    """
    Tier 1 (Non-email verified):
      - In Opt-Out guild -> auto-assigns roles.
      - In Opt-In guild -> skips and flags requires_email_in.
    Tier 2 (Email verified / High Trust):
      - In Opt-Out guild -> auto-assigns roles.
      - In Opt-In guild -> auto-assigns roles immediately.
    """
    db = Database(str(tmp_path / "tiered_verif.db"))
    await db.connect()

    bot = MagicMock()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_tiered", rate_limiter)

    # Opt-Out Guild (require_email_verification = 0)
    g_opt_out = MagicMock(spec=discord.Guild)
    g_opt_out.id = 2001
    g_opt_out.name = "Opt-Out Guild"
    bot_m1 = MagicMock(spec=discord.Member)
    bot_m1.guild_permissions.manage_roles = True
    bot_top1 = MagicMock(spec=discord.Role)
    bot_top1.position = 20
    bot_m1.top_role = bot_top1
    g_opt_out.me = bot_m1
    r_focs1 = MagicMock(spec=discord.Role)
    r_focs1.name = "FOCS"
    r_focs1.position = 5
    g_opt_out.roles = [r_focs1]

    m1 = MagicMock(spec=discord.Member)
    m1.id = 555001
    m1.roles = []
    m1.add_roles = AsyncMock()
    g_opt_out.get_member.return_value = m1

    # Opt-In Guild (require_email_verification = 1)
    g_opt_in = MagicMock(spec=discord.Guild)
    g_opt_in.id = 2002
    g_opt_in.name = "Opt-In Guild"
    bot_m2 = MagicMock(spec=discord.Member)
    bot_m2.guild_permissions.manage_roles = True
    bot_top2 = MagicMock(spec=discord.Role)
    bot_top2.position = 20
    bot_m2.top_role = bot_top2
    g_opt_in.me = bot_m2
    r_focs2 = MagicMock(spec=discord.Role)
    r_focs2.name = "FOCS"
    r_focs2.position = 5
    g_opt_in.roles = [r_focs2]

    m2 = MagicMock(spec=discord.Member)
    m2.id = 555001
    m2.roles = []
    m2.add_roles = AsyncMock()
    g_opt_in.get_member.return_value = m2

    bot.guilds = [g_opt_out, g_opt_in]

    await db.set_guild_email_verification(g_opt_in.id, enabled=True)
    await db.set_guild_email_verification(g_opt_out.id, enabled=False)

    # 1. Tier 1 student (is_email_verified = False)
    result_tier1 = await service.assign_role_across_guilds(
        user_id=555001,
        role_name="FOCS",
        guilds=[g_opt_out, g_opt_in],
        is_email_verified=False,
    )
    # Opt-Out guild got roles
    assert any(g[0] == g_opt_out.id for g in result_tier1.verified_in)
    # Opt-In guild was blocked and flagged
    assert "Opt-In Guild" in result_tier1.requires_email_in
    assert not any(g[0] == g_opt_in.id for g in result_tier1.verified_in)

    # 2. Tier 2 student (is_email_verified = True)
    result_tier2 = await service.assign_role_across_guilds(
        user_id=555001,
        role_name="FOCS",
        guilds=[g_opt_out, g_opt_in],
        is_email_verified=True,
    )
    # Both guilds got roles assigned immediately
    assert len(result_tier2.requires_email_in) == 0
    assert any(g[0] == g_opt_in.id for g in result_tier2.verified_in)
    await db.close()


@pytest.mark.asyncio
async def test_reconcile_self_healing_email_policy_and_unverified_cleanup(tmp_path):
    """
    Self-healing:
      1. In Opt-In server, strips roles from non-email verified student.
      2. In Opt-In server, restores roles to email-verified student.
      3. Strips stray verified roles from unverified user.
    """
    db = Database(str(tmp_path / "reconcile_policy.db"))
    await db.connect()

    bot = MagicMock()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret_reconcile", rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 3001
    guild.name = "Opt-In Main"
    bot_m = MagicMock(spec=discord.Member)
    bot_m.guild_permissions.manage_roles = True
    bot_top = MagicMock(spec=discord.Role)
    bot_top.position = 25
    bot_m.top_role = bot_top
    guild.me = bot_m

    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.position = 5
    guild.roles = [focs_role]

    await db.set_guild_email_verification(guild.id, enabled=True)

    # User A (Tier 1: no email hash, but currently has FOCS role in Discord)
    user_a = MagicMock(spec=discord.Member)
    user_a.id = 10001
    user_a.roles = [focs_role]
    user_a.remove_roles = AsyncMock()
    user_a.add_roles = AsyncMock()

    # User B (Tier 2: has email hash, missing FOCS role in Discord)
    user_b = MagicMock(spec=discord.Member)
    user_b.id = 10002
    user_b.roles = []
    user_b.remove_roles = AsyncMock()
    user_b.add_roles = AsyncMock()

    # User C (Unverified: not in DB, but has stray FOCS role in Discord)
    user_c = MagicMock(spec=discord.Member)
    user_c.id = 10003
    user_c.bot = False
    user_c.roles = [focs_role]
    user_c.remove_roles = AsyncMock()
    user_c.add_roles = AsyncMock()

    # User D (Tier 1: verified in DB without email, missing FOCS role because it was stripped earlier)
    user_d = MagicMock(spec=discord.Member)
    user_d.id = 10004
    user_d.roles = []
    user_d.remove_roles = AsyncMock()
    user_d.add_roles = AsyncMock()

    guild.members = [user_a, user_b, user_c, user_d]

    def _get_m(uid):
        if uid == 10001:
            return user_a
        elif uid == 10002:
            return user_b
        elif uid == 10003:
            return user_c
        elif uid == 10004:
            return user_d
        return None

    guild.get_member.side_effect = _get_m
    bot.guilds = [guild]

    # Record User A without email
    await db.record_verification(10001, "hash_a", "M", campus_code="W", level_code="R")

    # Record User B with email
    await db.record_verification(
        10002, "hash_b", "M", campus_code="W", level_code="R",
        student_email_encrypted=b"enc", student_email_hash="email_hash_b"
    )

    # Record User D without email (past verified user)
    await db.record_verification(10004, "hash_d", "M", campus_code="W", level_code="R")

    # 1. Enforcement disabled (default): User A's role is PRESERVED, User D's missing role is RESTORED!
    summary_default = await service.reconcile_verified_members(guild)
    user_a.remove_roles.assert_not_called()
    user_b.add_roles.assert_called_once()
    user_d.add_roles.assert_called_once()
    assert summary_default["unauthorized_cleaned"] == 0
    assert summary_default["restored"] >= 2

    # 2. Enforcement enabled: User A & User D have their roles stripped
    user_a.roles = [focs_role]
    user_d.roles = [focs_role]
    await db.set_guild_email_enforcement(guild.id, enabled=True)
    user_b.add_roles.reset_mock()
    user_c.remove_roles.reset_mock()
    summary_enforced = await service.reconcile_verified_members(guild)

    user_a.remove_roles.assert_called_once()
    user_d.remove_roles.assert_called_once()
    assert summary_enforced["unauthorized_cleaned"] == 2

    # User C should have had stray role stripped
    user_c.remove_roles.assert_called_once()
    assert summary_enforced["unverified_cleaned"] == 1

    await db.close()


@pytest.mark.asyncio
async def test_mass_revocation_circuit_breaker_tripped(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 88888
    guild.name = "Mass Action Guild"

    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.id = 101
    guild.roles = [focs_role]

    db = Database(str(tmp_path / "mass_breaker.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    # Enable email verification and email enforcement for this guild
    await db.set_guild_email_verification(guild.id, enabled=True)
    await db.set_guild_email_enforcement(guild.id, enabled=True)

    # Setup 6 members with verified DB status but no institutional email
    # Default threshold is 5, so 6 will trip the circuit breaker!
    members = []
    for i in range(1, 7):
        m = MagicMock(spec=discord.Member)
        m.id = 20000 + i
        m.bot = False
        m.roles = [focs_role]
        m.remove_roles = AsyncMock()
        m.add_roles = AsyncMock()
        members.append(m)
        await db.record_verification(m.id, f"hash_{m.id}", "M", campus_code="W", level_code="R")

    guild.members = members
    guild.get_member.side_effect = lambda uid: next((m for m in members if m.id == uid), None)
    bot.guilds = [guild]

    # Mock stage_mass_revocation to observe circuit breaker interception
    with patch.object(service, "stage_mass_revocation", wraps=service.stage_mass_revocation) as mock_stage:
        summary = await service.reconcile_verified_members(guild)

        # Circuit breaker should have been tripped
        mock_stage.assert_called_once()
        action = await db.get_active_pending_mass_action_for_guild(guild.id, "EMAIL_POLICY_REVOCATION")
        assert action is not None
        action_id = action["action_id"]
        assert action_id.startswith("MREV-")

        # Roles should NOT have been stripped directly due to breaker tripping
        for m in members:
            m.remove_roles.assert_not_called()

        assert summary["unauthorized_cleaned"] == 0

        # Pending action in DB
        assert len(action["user_ids"]) == 6
        assert action["status"] == "PENDING"

    await db.close()


@pytest.mark.asyncio
async def test_mass_revocation_execution_and_rejection(tmp_path):
    bot = MagicMock()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 77777
    guild.name = "Approval Guild"

    focs_role = MagicMock(spec=discord.Role)
    focs_role.name = "FOCS"
    focs_role.id = 202
    guild.roles = [focs_role]

    db = Database(str(tmp_path / "mass_exec.db"))
    await db.connect()
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    # 5 members
    members = []
    for i in range(1, 6):
        m = MagicMock(spec=discord.Member)
        m.id = 30000 + i
        m.bot = False
        m.roles = [focs_role]
        m.remove_roles = AsyncMock()
        members.append(m)

    guild.members = members
    guild.get_member.side_effect = lambda uid: next((m for m in members if m.id == uid), None)
    bot.get_guild.return_value = guild

    # Stage an action
    user_ids = [m.id for m in members]
    action_id = "MREV-EXEC1"
    await db.create_pending_mass_action(
        action_id=action_id,
        guild_id=guild.id,
        action_type="EMAIL_POLICY_REVOCATION",
        user_ids=user_ids,
        reason="Test Execution",
    )

    # 1. Execute approval
    admin_user = MagicMock(spec=discord.Member)
    admin_user.id = 9999
    admin_user.__str__.return_value = "Admin#0001"

    success, msg = await service.execute_approved_mass_revocation(guild, action_id, admin=admin_user)
    assert success is True
    assert "Successfully executed mass revocation" in msg

    for m in members:
        m.remove_roles.assert_called_once_with(focs_role, reason=f"TARVeri: Mass revocation approved by {admin_user} ({action_id})")

    # Check DB status is APPROVED
    action = await db.get_pending_mass_action(action_id)
    assert action["status"] == "APPROVED"
    assert action["decided_by_id"] == 9999

    # 2. Stage another action and test rejection
    action_id_2 = "MREV-REJ1"
    await db.create_pending_mass_action(
        action_id=action_id_2,
        guild_id=guild.id,
        action_type="STRAY_UNVERIFIED_CLEANUP",
        user_ids=user_ids,
        reason="Test Rejection",
    )

    admin_rej = MagicMock(spec=discord.Member)
    admin_rej.id = 8888
    admin_rej.__str__.return_value = "AdminRej#0002"

    rej_success, rej_msg = await service.reject_mass_revocation(guild, action_id_2, admin=admin_rej)
    assert rej_success is True
    assert "has been rejected" in rej_msg

    action_2 = await db.get_pending_mass_action(action_id_2)
    assert action_2["status"] == "REJECTED"
    assert action_2["decided_by_id"] == 8888

    await db.close()









