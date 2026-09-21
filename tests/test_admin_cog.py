from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from discord import app_commands

from tarveri.cogs.admin_cog import AdminCog, MassRevocationApprovalView, is_admin_or_has_role
from tarveri.cogs.admin_dashboard import (
    AdminDashboardView,
    AlumniRevokeModal,
    GuestRoleModal,
    UnverifyModal,
    _extract_user_id,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import VerificationService


def test_is_admin_or_has_role():
    admin_role_name = "TARVeri Admin"

    # User without guild
    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = None
    assert is_admin_or_has_role(interaction, admin_role_name) is False

    # Member with administrator permission
    interaction.guild = MagicMock(spec=discord.Guild)
    member_admin = MagicMock(spec=discord.Member)
    member_admin.guild_permissions.administrator = True
    member_admin.roles = []
    interaction.user = member_admin
    assert is_admin_or_has_role(interaction, admin_role_name) is True

    # Member with admin role
    member_role = MagicMock(spec=discord.Member)
    member_role.guild_permissions.administrator = False
    role = MagicMock(spec=discord.Role)
    role.name = "TARVeri Admin"
    member_role.roles = [role]
    interaction.user = member_role
    assert is_admin_or_has_role(interaction, admin_role_name) is True

    # Regular member without admin permission or role
    member_regular = MagicMock(spec=discord.Member)
    member_regular.guild_permissions.administrator = False
    other_role = MagicMock(spec=discord.Role)
    other_role.name = "Member"
    member_regular.roles = [other_role]
    interaction.user = member_regular
    assert is_admin_or_has_role(interaction, admin_role_name) is False


def test_extract_user_id():
    assert _extract_user_id("123456789012345678") == 123456789012345678
    assert _extract_user_id("<@123456789012345678>") == 123456789012345678
    assert _extract_user_id("<@!123456789012345678>") == 123456789012345678
    assert _extract_user_id("invalid") is None


@pytest.mark.asyncio
async def test_set_channel_and_set_role(tmp_path):
    db_path = str(tmp_path / "admin_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    guild.name = "My Server"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.__str__.return_value = "Admin#0001"

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 98765
    channel.name = "welcome"
    channel.mention = "<#98765>"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Set welcome channel
    await cog.set_channel.callback(cog, interaction, channel_type="welcome", channel=channel)
    interaction.followup.send.assert_called_once()
    assert "<#98765>" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[0] == 98765

    # 2. Reset welcome channel
    interaction.followup.send.reset_mock()
    await cog.set_channel.callback(cog, interaction, channel_type="welcome", channel=None)
    assert "auto-detect" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[0] is None

    # 3. Set help channel
    help_channel = MagicMock(spec=discord.TextChannel)
    help_channel.id = 54321
    help_channel.name = "help"
    help_channel.mention = "<#54321>"

    interaction.followup.send.reset_mock()
    await cog.set_channel.callback(cog, interaction, channel_type="help", channel=help_channel)
    assert "<#54321>" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[1] == 54321

    # 4. Set guest role
    interaction.followup.send.reset_mock()
    await cog.set_role.callback(cog, interaction, role_type="guest", role_name="Guest (Approved)")
    assert "Guest (Approved)" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[2] == "Guest (Approved)"

    # 5. Set review channel
    review_channel = MagicMock(spec=discord.TextChannel)
    review_channel.id = 776655
    review_channel.name = "review-tickets"
    review_channel.mention = "<#776655>"

    interaction.followup.send.reset_mock()
    await cog.set_channel.callback(cog, interaction, channel_type="review", channel=review_channel)
    assert "<#776655>" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[3] == 776655

    # 6. Set custom admin role
    admin_custom_role = MagicMock(spec=discord.Role)
    admin_custom_role.name = "Review Moderators"
    admin_custom_role.mention = "<@&334455>"

    interaction.followup.send.reset_mock()
    await cog.set_role.callback(cog, interaction, role_type="admin", role=admin_custom_role)
    assert "<@&334455>" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[4] == "Review Moderators"

    # 7. Reset custom admin role
    interaction.followup.send.reset_mock()
    await cog.set_role.callback(cog, interaction, role_type="admin", role=None)
    assert "auto-detect" in interaction.followup.send.call_args[0][0]
    settings = await db.get_guild_settings(12345)
    assert settings[4] is None

    await db.close()


@pytest.mark.asyncio
async def test_sync_commands_slash(tmp_path):
    db_path = str(tmp_path / "sync_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    bot.tree = MagicMock()
    bot.tree.sync = AsyncMock(return_value=[MagicMock(), MagicMock()])
    bot.tree.clear_commands = MagicMock()
    bot.tree.copy_global_to = MagicMock()
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.name = "Test Guild"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.__str__.return_value = "Admin#0001"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Clean duplicates sync (global)
    await cog.sync_commands.callback(cog, interaction, clean_duplicates=True, guild_only=False)
    interaction.response.defer.assert_called_once()
    bot.tree.clear_commands.assert_called_once_with(guild=guild)
    bot.tree.sync.assert_called()
    assert "Successfully synced 2 command(s)" in interaction.followup.send.call_args[0][0]

    # 2. Guild-only sync
    interaction.response.defer.reset_mock()
    interaction.followup.send.reset_mock()
    bot.tree.sync.reset_mock()
    await cog.sync_commands.callback(cog, interaction, clean_duplicates=False, guild_only=True)
    bot.tree.copy_global_to.assert_called_once_with(guild=guild)
    assert "server 'Test Guild'" in interaction.followup.send.call_args[0][0]

    await db.close()


@pytest.mark.asyncio
async def test_admin_commands_permission_denied(tmp_path):
    db = Database(str(tmp_path / "perm_test.db"))
    await db.connect()
    cog = AdminCog(MagicMock(), db, MagicMock(), MagicMock(), admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    regular_user = MagicMock(spec=discord.Member)
    regular_user.guild_permissions.administrator = False
    regular_user.roles = []

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = regular_user
    interaction.response.send_message = AsyncMock()

    # stats
    await cog.stats.callback(cog, interaction)
    interaction.response.send_message.assert_called_once()
    assert "do not have permission" in interaction.response.send_message.call_args[0][0]

    # unverify
    interaction.response.send_message.reset_mock()
    await cog.unverify.callback(cog, interaction, user=MagicMock())
    interaction.response.send_message.assert_called_once()
    assert "do not have permission" in interaction.response.send_message.call_args[0][0]

    # audit
    interaction.response.send_message.reset_mock()
    await cog.audit.callback(cog, interaction)
    interaction.response.send_message.assert_called_once()
    assert "do not have permission" in interaction.response.send_message.call_args[0][0]

    # backup
    interaction.response.send_message.reset_mock()
    await cog.backup.callback(cog, interaction)
    interaction.response.send_message.assert_called_once()
    assert "do not have permission" in interaction.response.send_message.call_args[0][0]

    await db.close()


@pytest.mark.asyncio
async def test_admin_unverify_lifecycle(tmp_path):
    db = Database(str(tmp_path / "unverify_test.db"))
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    service.get_mutual_guilds_for_user = AsyncMock(return_value=[])
    service.get_or_fetch_member = AsyncMock(return_value=None)
    service._match_faculty_role_in_list = MagicMock(return_value=None)
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.name = "Campus Guild"
    bot_top = MagicMock(spec=discord.Role)
    bot_top.position = 10
    bot_member = MagicMock(spec=discord.Member)
    bot_member.guild_permissions.manage_roles = True
    bot_member.top_role = bot_top
    guild.me = bot_member

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.__str__.return_value = "Admin#0001"

    target_user = MagicMock(spec=discord.User)
    target_user.id = 777111
    target_user.mention = "<@777111>"
    target_user.__str__.return_value = "Student#7771"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Unverify on non-verified user
    await cog.unverify.callback(cog, interaction, user=target_user)
    interaction.followup.send.assert_called_once()
    assert "is not verified" in interaction.followup.send.call_args[0][0]

    # 2. Record verification then unverify
    await db.record_verification(target_user.id, "hash777", "M")
    member = MagicMock(spec=discord.Member)
    faculty_role = MagicMock(spec=discord.Role)
    faculty_role.name = "FOCS"
    faculty_role.position = 5
    member.roles = [faculty_role]
    member.remove_roles = AsyncMock()

    service.get_mutual_guilds_for_user = AsyncMock(return_value=[guild])
    service.get_or_fetch_member = AsyncMock(return_value=member)
    service._match_faculty_role_in_list = MagicMock(return_value=faculty_role)

    interaction.followup.send.reset_mock()
    await cog.unverify.callback(cog, interaction, user=target_user, reason="Graduated")
    interaction.followup.send.assert_called_once()
    assert "Successfully unverified" in interaction.followup.send.call_args[0][0]
    assert "Campus Guild (FOCS)" in interaction.followup.send.call_args[0][0]

    # Verification should be deleted from DB
    assert await db.get_verification_by_user(target_user.id) is None
    # Rate limiter was reset
    rate_limiter.reset.assert_called_with(target_user.id)

    await db.close()


@pytest.mark.asyncio
async def test_admin_stats_and_audit(tmp_path):
    db = Database(str(tmp_path / "stats_audit_test.db"))
    await db.connect()

    bot = MagicMock()
    bot.guilds = [MagicMock()]
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1122
    guild.name = "Audit Test Guild"
    guild.get_channel.return_value = None

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Stats with 0 records
    await cog.stats.callback(cog, interaction)
    interaction.followup.send.assert_called_once()
    stats_embed = interaction.followup.send.call_args[1]["embed"]
    assert stats_embed.title == "📊 TARVeri — Verification Statistics"

    # 2. Add verifications & audit logs
    await db.record_verification(101, "hash101", "M")
    await db.record_verification(102, "hash102", "B")
    await db.log("INFO", "TEST_EVENT", "Audit test 1", guild=guild, user_id=101)

    # 3. Query audit
    interaction.followup.send.reset_mock()
    await cog.audit.callback(cog, interaction, limit=5, event_type="TEST_EVENT")
    interaction.followup.send.assert_called_once()
    audit_embed = interaction.followup.send.call_args[1]["embed"]
    assert "Audit Log Entries" in audit_embed.title
    assert len(audit_embed.fields) == 1

    # 4. Query audit with non-matching filter
    interaction.followup.send.reset_mock()
    await cog.audit.callback(cog, interaction, limit=5, event_type="NON_EXISTENT")
    interaction.followup.send.assert_called_once()
    assert "No audit log records found" in interaction.followup.send.call_args[0][0]

    # 5. Query guest tickets
    await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=3001,
        channel_id=4001,
        reason="Attending workshop",
    )
    interaction.followup.send.reset_mock()
    await cog.tickets.callback(cog, interaction, status=None, limit=5)
    interaction.followup.send.assert_called_once()
    gt_embed = interaction.followup.send.call_args[1]["embed"]
    assert "Guest Review Tickets" in gt_embed.title
    assert len(gt_embed.fields) == 1
    assert "Ticket #A0001" in gt_embed.fields[0].name

    await db.close()


@pytest.mark.asyncio
async def test_admin_diagnose_command(tmp_path):
    db = Database(str(tmp_path / "diagnose_test.db"))
    await db.connect()

    service = MagicMock()
    service.restore_src_roles = AsyncMock(return_value={"created": 2, "existing": 6, "failed": 0})
    service.reconcile_duplicate_roles = AsyncMock(
        return_value={"checked_categories": 1, "migrated_members": 2, "deleted_roles": 1, "failed": 0, "details": []}
    )
    service.diagnose_guild_permissions.return_value = ["⚠️ Role hierarchy conflict: Role FOCS is higher than bot role."]
    service.reconcile_verified_members = AsyncMock(return_value={"checked": 5, "restored": 2, "failed": 0})
    service.reconcile_alumni_members = AsyncMock(return_value={"checked": 0, "restored": 0, "failed": 0})

    cog = AdminCog(MagicMock(), db, service, MagicMock(), admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 8877
    guild.name = "Diagnose Guild"

    welcome_ch = MagicMock(spec=discord.TextChannel)
    welcome_ch.id = 1111
    welcome_ch.mention = "<#1111>"

    await db.set_guild_welcome_channel(guild.id, 1111)
    await db.set_guild_help_channel(guild.id, 2222)
    await db.set_guild_review_channel(guild.id, 3333)

    guild.get_channel.side_effect = lambda cid: welcome_ch if cid == 1111 else None

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await cog.diagnose.callback(cog, interaction)

    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "Server Health & Diagnostics" in embed.title
    assert len(embed.fields) >= 4
    assert any("Duplicate Role Cleanup" in f.name for f in embed.fields)
    assert any("SRC Roles Self-Healing" in f.name for f in embed.fields)

    settings = await db.get_guild_settings(guild.id)
    assert settings[0] == 1111
    assert settings[1] is None
    assert settings[3] is None

    await db.close()


@pytest.mark.asyncio
async def test_admin_backup_command_actions(tmp_path):
    db_path = str(tmp_path / "admin_backup_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    cog = AdminCog(bot, db, service, MagicMock(), admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 887799
    guild.name = "Backup Admin Guild"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Action: create backup
    await cog.backup.callback(cog, interaction, action="create")
    interaction.followup.send.assert_called_once()
    assert "Database backup created successfully" in interaction.followup.send.call_args[0][0]

    # 2. Action: list backups
    interaction.followup.send.reset_mock()
    await cog.backup.callback(cog, interaction, action="list")
    interaction.followup.send.assert_called_once()

    # 3. Action: restore_settings
    interaction.followup.send.reset_mock()
    await cog.backup.callback(cog, interaction, action="restore_settings")
    interaction.followup.send.assert_called_once()

    await db.close()


@pytest.mark.asyncio
async def test_admin_dashboard_launcher_and_components(tmp_path):
    db_path = str(tmp_path / "dashboard_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    bot.guilds = []
    service = MagicMock(spec=VerificationService)
    service.restore_src_roles = AsyncMock(return_value={"created": 0})
    service.reconcile_duplicate_roles = AsyncMock(return_value={"deleted_roles": 0, "migrated_members": 0, "failed": 0})
    service.diagnose_guild_permissions = MagicMock(return_value=[])
    service.reconcile_verified_members = AsyncMock(return_value={"checked": 0, "restored": 0})
    service.reconcile_alumni_members = AsyncMock(return_value={"checked": 0, "restored": 0})

    rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
    update_checker = MagicMock()
    update_checker.check_for_updates = AsyncMock(return_value=(False, 0, "hash111", "hash222", "main"))
    cog = AdminCog(
        bot,
        db,
        service,
        rate_limiter,
        admin_role_name="TARVeri Admin",
        update_checker=update_checker,
    )

    guild = MagicMock(spec=discord.Guild)
    guild.id = 554433
    guild.name = "Dashboard Server"
    guild.get_channel.return_value = None

    admin_user = MagicMock(spec=discord.Member)
    admin_user.id = 998811
    admin_user.guild_permissions.administrator = True

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Test /admin dashboard launcher
    await cog.dashboard.callback(cog, interaction)
    interaction.response.defer.assert_called_once_with(ephemeral=True)
    interaction.followup.send.assert_called_once()
    kwargs = interaction.followup.send.call_args[1]
    assert "embed" in kwargs
    assert isinstance(kwargs["view"], AdminDashboardView)

    view: AdminDashboardView = kwargs["view"]

    # 2. Test category switching
    for cat in ["overview", "config", "diagnose", "moderation", "tickets", "backup", "logs", "panel", "updates"]:
        view.current_category = cat
        embed = await view.build_current_embed(guild)
        assert embed.title is not None

    # 3. Test Modals
    unverify_modal = UnverifyModal(cog, view)
    unverify_modal.user_input._value = "123456789012345678"
    unverify_modal.reason_input._value = "Test unverify"
    assert unverify_modal.title == "❌ Unverify Student"

    alumni_modal = AlumniRevokeModal(cog, view)
    alumni_modal.user_input._value = "123456789012345678"
    alumni_modal.reason_input._value = "Test revoke"
    assert alumni_modal.title == "🎓 Revoke Alumni Status"

    guest_role_modal = GuestRoleModal(cog, view)
    guest_role_modal.role_name_input._value = "Guest (Approved)"
    assert guest_role_modal.title == "⚙️ Configure Guest Role Name"

    await db.close()


@pytest.mark.asyncio
async def test_admin_backfill_roles_command(tmp_path):
    db_path = str(tmp_path / "admin_backfill_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    service.backfill_branch_roles = AsyncMock(
        return_value={
            "guilds_scanned": 1,
            "members_checked": 5,
            "roles_assigned": 3,
            "db_migrated": 2,
            "failed": 0,
        }
    )
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.__str__.return_value = "Admin#0001"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    campus_choice = app_commands.Choice(name="Penang Branch", value="P")
    level_choice = app_commands.Choice(name="Degree", value="R")
    await cog.backfill_roles.callback(
        cog, interaction, default_campus=campus_choice, default_level=level_choice, all_servers=False
    )

    service.backfill_branch_roles.assert_called_once_with(
        guild=guild, default_campus_code="P", default_level_code="R"
    )
    interaction.followup.send.assert_called_once()
    kwargs = interaction.followup.send.call_args[1]
    assert "embed" in kwargs
    assert "Role Backfill" in kwargs["embed"].title
    assert "Penang Branch" in kwargs["embed"].footer.text
    assert "Degree" in kwargs["embed"].footer.text

    await db.close()


@pytest.mark.asyncio
async def test_admin_close_ticket_in_thread(tmp_path):
    db_path = str(tmp_path / "admin_close_ticket_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    guest_service = MagicMock()
    guest_service.close_guest_ticket_manually = AsyncMock(
        return_value=(True, "🔒 Guest review ticket #0001 manually closed.")
    )
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin", guest_service=guest_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    thread = MagicMock(spec=discord.Thread)
    thread.id = 999111
    thread.send = AsyncMock()
    thread.edit = AsyncMock()
    guild.get_thread.return_value = thread

    # Create open guest ticket in DB
    await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=111222,
        channel_id=thread.id,
        reason="Attending guest lecture",
    )

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.mention = "<@999>"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.channel = thread
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await cog.close_ticket.callback(cog, interaction, reason="Resolved inquiry")

    guest_service.close_guest_ticket_manually.assert_called_once()
    thread.send.assert_called_once()
    assert "Ticket manually closed" in thread.send.call_args[0][0]
    thread.edit.assert_called_once_with(locked=True, archived=True, reason=f"TARVeri: Ticket closed by {admin_user}")
    interaction.followup.send.assert_called_once()

    await db.close()


@pytest.mark.asyncio
async def test_admin_close_ticket_with_ticket_number(tmp_path):
    db_path = str(tmp_path / "admin_close_ticket_seq_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    guest_service = MagicMock()
    guest_service.close_guest_ticket_manually = AsyncMock(
        return_value=(True, "🔒 Guest review ticket #A0042 manually closed.")
    )
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin", guest_service=guest_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 54321

    # Create ticket with seq 42
    await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=333444,
        channel_id=888777,
        ticket_seq=42,
        reason="Visiting researcher",
    )

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Close using alphanumeric code "A0042"
    await cog.close_ticket.callback(cog, interaction, reason="Duplicate request", ticket="A0042")

    guest_service.close_guest_ticket_manually.assert_called_once()
    interaction.followup.send.assert_called_once()

    await db.close()


@pytest.mark.asyncio
async def test_admin_close_ticket_invalid_ticket(tmp_path):
    db_path = str(tmp_path / "admin_close_ticket_inv_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    guest_service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin", guest_service=guest_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 54321

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.channel = MagicMock(spec=discord.TextChannel)
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Close using non-existent ticket code
    await cog.close_ticket.callback(cog, interaction, reason="Spam", ticket="Z9999")
    interaction.followup.send.assert_called_once()
    assert "No guest review ticket found" in interaction.followup.send.call_args[0][0]

    await db.close()


@pytest.mark.asyncio
async def test_admin_close_ticket_already_left_server_archives_unarchived_thread(tmp_path):
    db_path = str(tmp_path / "admin_close_ticket_left_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    guest_service = MagicMock()
    guest_service._cleanup_channel_overwrites = AsyncMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin", guest_service=guest_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 54321
    thread = MagicMock(spec=discord.Thread)
    thread.id = 888777
    thread.archived = False
    thread.locked = False
    thread.send = AsyncMock()
    thread.edit = AsyncMock()
    guild.get_thread.return_value = thread

    # Create ticket already in LEFT_SERVER status
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=333444,
        channel_id=888777,
        ticket_seq=2,
        reason="Danial Wong Kai Ze (MMU)",
    )
    await db.close_guest_ticket(ticket_id, status="LEFT_SERVER", close_reason="Member left the server")

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.mention = "<@999>"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.channel = thread
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Run /admin close_ticket inside the thread
    await cog.close_ticket.callback(cog, interaction)

    thread.send.assert_called_once()
    assert "Ticket thread archived" in thread.send.call_args[0][0]
    thread.edit.assert_called_once_with(
        locked=True, archived=True, reason=f"TARVeri: Archived by {admin_user} (status: LEFT_SERVER)"
    )
    interaction.followup.send.assert_called_once()
    assert "Cleaned up and archived the thread" in interaction.followup.send.call_args[0][0]

    await db.close()


@pytest.mark.asyncio
async def test_admin_email_verification_command(tmp_path):
    db_path = str(tmp_path / "admin_email_cmd_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 556677
    guild.name = "CS Society"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.send_message = AsyncMock()

    # Opt-in to email verification
    await cog.email_verification.callback(cog, interaction, enabled=True)
    assert await db.is_guild_email_verification_enabled(guild.id) is True
    interaction.response.send_message.assert_called_once()
    assert "MANDATORY (Opted In)" in interaction.response.send_message.call_args[0][0]

    # Opt-out of email verification
    interaction.response.send_message.reset_mock()
    await cog.email_verification.callback(cog, interaction, enabled=False)
    assert await db.is_guild_email_verification_enabled(guild.id) is False
    interaction.response.send_message.assert_called_once()
    assert "OPTIONAL (Opted Out)" in interaction.response.send_message.call_args[0][0]

    await db.close()


@pytest.mark.asyncio
async def test_admin_dashboard_toggle_email_verification(tmp_path):
    db_path = str(tmp_path / "admin_dashboard_toggle_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    bot.guilds = []
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 889900
    guild.name = "TARUMT Cyber Club"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.id = 112233

    view = AdminDashboardView(cog, admin_user, initial_category="config")

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response.defer = AsyncMock()
    interaction.edit_original_response = AsyncMock()

    # 1. Toggle from False -> True
    await view._on_toggle_email_verification_clicked(interaction)
    assert await db.is_guild_email_verification_enabled(guild.id) is True
    interaction.edit_original_response.assert_called_once()
    embed = interaction.edit_original_response.call_args[1]["embed"]
    assert "MANDATORY" in embed.description

    # 2. Toggle from True -> False
    interaction.edit_original_response.reset_mock()
    await view._on_toggle_email_verification_clicked(interaction)
    assert await db.is_guild_email_verification_enabled(guild.id) is False
    interaction.edit_original_response.assert_called_once()
    embed = interaction.edit_original_response.call_args[1]["embed"]
    assert "OPTIONAL" in embed.description

    # 3. Toggle enforcement from False -> True
    interaction.edit_original_response.reset_mock()
    await view._on_toggle_email_enforcement_clicked(interaction)
    assert await db.is_guild_email_enforcement_enabled(guild.id) is True
    interaction.edit_original_response.assert_called_once()
    embed = interaction.edit_original_response.call_args[1]["embed"]
    assert "ENFORCED" in embed.description

    # 4. Toggle enforcement from True -> False
    interaction.edit_original_response.reset_mock()
    await view._on_toggle_email_enforcement_clicked(interaction)
    assert await db.is_guild_email_enforcement_enabled(guild.id) is False
    interaction.edit_original_response.assert_called_once()
    embed = interaction.edit_original_response.call_args[1]["embed"]
    assert "DISABLED" in embed.description

    await db.close()


@pytest.mark.asyncio
async def test_admin_email_enforcement_slash(tmp_path):
    db_path = str(tmp_path / "enforce_slash_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 99887766
    guild.name = "Enforce Slash Guild"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.__str__.return_value = "Admin#0001"
    admin_user.id = 554433

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()

    # 1. Enable enforcement
    await cog.email_enforcement.callback(cog, interaction, enabled=True)
    assert await db.is_guild_email_enforcement_enabled(guild.id) is True
    interaction.response.send_message.assert_called_once()
    assert "ENFORCED" in interaction.response.send_message.call_args[0][0]

    # 2. Disable enforcement
    interaction.response.send_message.reset_mock()
    await cog.email_enforcement.callback(cog, interaction, enabled=False)
    assert await db.is_guild_email_enforcement_enabled(guild.id) is False
    interaction.response.send_message.assert_called_once()
    assert "DISABLED" in interaction.response.send_message.call_args[0][0]

    await db.close()


@pytest.mark.asyncio
async def test_mass_revocation_approval_view_interactions(tmp_path):
    db_path = str(tmp_path / "view_test.db")
    db = Database(db_path)
    await db.connect()

    service = MagicMock()
    service.db = db
    service.execute_approved_mass_revocation = AsyncMock(return_value=(True, "Executed successfully"))
    service.reject_mass_revocation = AsyncMock(return_value=(True, "Rejected successfully"))

    guild = MagicMock(spec=discord.Guild)
    guild.id = 11223344
    guild.name = "View Guild"

    # Non-admin user
    non_admin = MagicMock(spec=discord.Member)
    non_admin.guild_permissions.administrator = False
    non_admin.guild_permissions.manage_guild = False
    non_admin.id = 1111

    # Admin user
    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.id = 2222
    admin_user.mention = "<@2222>"

    # 1. Staged action in DB
    action_id = "MREV-VIEW1"
    await db.create_pending_mass_action(
        action_id=action_id,
        guild_id=guild.id,
        action_type="EMAIL_POLICY_REVOCATION",
        user_ids=[101, 102, 103, 104, 105],
        reason="View Test",
    )

    view = MassRevocationApprovalView(service=service)

    # 2. Non-admin clicks Approve -> Rejected with permission error
    inter_non_admin = MagicMock(spec=discord.Interaction)
    inter_non_admin.guild = guild
    inter_non_admin.user = non_admin
    inter_non_admin.response.send_message = AsyncMock()

    await view.children[0].callback(inter_non_admin)
    inter_non_admin.response.send_message.assert_called_once()
    assert "Only administrators" in inter_non_admin.response.send_message.call_args[0][0]
    service.execute_approved_mass_revocation.assert_not_called()

    # 3. Admin clicks Approve -> Executed
    inter_admin = MagicMock(spec=discord.Interaction)
    inter_admin.guild = guild
    inter_admin.user = admin_user
    inter_admin.response.defer = AsyncMock()
    inter_admin.followup.send = AsyncMock()
    inter_admin.message = MagicMock()
    inter_admin.message.edit = AsyncMock()

    await view.children[0].callback(inter_admin)
    service.execute_approved_mass_revocation.assert_called_once_with(
        guild, action_id, admin=admin_user
    )
    inter_admin.followup.send.assert_called_once_with("Executed successfully", ephemeral=True)
    inter_admin.message.edit.assert_called_once()

    # Mark action 1 as approved in DB to simulate service execution
    await db.update_pending_mass_action_status(action_id, "APPROVED", decided_by_id=admin_user.id)

    # 4. Test Reject flow
    action_id_2 = "MREV-VIEW2"
    await db.create_pending_mass_action(
        action_id=action_id_2,
        guild_id=guild.id,
        action_type="EMAIL_POLICY_REVOCATION",
        user_ids=[201, 202, 203, 204, 205],
        reason="View Test Reject",
    )

    inter_admin_rej = MagicMock(spec=discord.Interaction)
    inter_admin_rej.guild = guild
    inter_admin_rej.user = admin_user
    inter_admin_rej.response.defer = AsyncMock()
    inter_admin_rej.followup.send = AsyncMock()
    inter_admin_rej.message = MagicMock()
    inter_admin_rej.message.edit = AsyncMock()

    await view.children[1].callback(inter_admin_rej)
    service.reject_mass_revocation.assert_called_once_with(
        guild, action_id_2, admin=admin_user
    )
    inter_admin_rej.followup.send.assert_called_once_with("Rejected successfully", ephemeral=True)

    await db.close()


@pytest.mark.asyncio
async def test_admin_mass_revocation_slash(tmp_path):
    db_path = str(tmp_path / "mass_slash_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = MagicMock()
    service.db = db
    service.execute_approved_mass_revocation = AsyncMock(return_value=(True, "Approved via slash"))
    service.reject_mass_revocation = AsyncMock(return_value=(True, "Rejected via slash"))
    rate_limiter = MagicMock()
    cog = AdminCog(bot, db, service, rate_limiter, admin_role_name="TARVeri Admin")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 55667788
    guild.name = "Mass Slash Guild"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True
    admin_user.__str__.return_value = "Admin#0001"
    admin_user.id = 998811

    # Stage an action
    action_id = "MREV-SLASH1"
    await db.create_pending_mass_action(
        action_id=action_id,
        guild_id=guild.id,
        action_type="EMAIL_POLICY_REVOCATION",
        user_ids=[301, 302, 303, 304, 305],
        reason="Slash Command Test",
    )

    # 1. List actions
    inter_list = MagicMock(spec=discord.Interaction)
    inter_list.guild = guild
    inter_list.user = admin_user
    inter_list.response.defer = AsyncMock()
    inter_list.followup.send = AsyncMock()

    await cog.mass_revocation.callback(cog, inter_list, action="list")
    inter_list.followup.send.assert_called_once()
    embed = inter_list.followup.send.call_args[1]["embed"]
    assert "Mass Actions Queue" in embed.title
    assert action_id in embed.fields[0].name

    # 2. Approve action
    inter_app = MagicMock(spec=discord.Interaction)
    inter_app.guild = guild
    inter_app.user = admin_user
    inter_app.response.defer = AsyncMock()
    inter_app.followup.send = AsyncMock()

    await cog.mass_revocation.callback(cog, inter_app, action="approve", action_id=action_id)
    service.execute_approved_mass_revocation.assert_called_once_with(
        guild, action_id, admin=admin_user
    )
    inter_app.followup.send.assert_called_once_with("Approved via slash", ephemeral=True)

    # 3. Reject action
    inter_rej = MagicMock(spec=discord.Interaction)
    inter_rej.guild = guild
    inter_rej.user = admin_user
    inter_rej.response.defer = AsyncMock()
    inter_rej.followup.send = AsyncMock()

    await cog.mass_revocation.callback(cog, inter_rej, action="reject", action_id=action_id)
    service.reject_mass_revocation.assert_called_once_with(
        guild, action_id, admin=admin_user
    )
    inter_rej.followup.send.assert_called_once_with("Rejected via slash", ephemeral=True)

    await db.close()









