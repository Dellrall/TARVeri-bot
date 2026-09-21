from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.cogs.admin_cog import AdminCog
from tarveri.config import hash_email, hash_student_id
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService


@pytest.fixture
async def in_memory_db(tmp_path):
    db_file = str(tmp_path / "test_tarveri_blacklist.db")
    db = Database(db_file)
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def secret_key():
    return "test_secret_key_12345"


@pytest.mark.asyncio
async def test_database_blacklist_crud(in_memory_db, secret_key):
    guild_id_1 = 111111111111111111
    guild_id_2 = 222222222222222222
    user_id = 999888777
    raw_student_id = "23WMD01234"
    id_hash = hash_student_id(raw_student_id, secret_key)
    raw_email = "student1@student.tarc.edu.my"
    email_hash = hash_email(raw_email, secret_key)

    # 1. Add targets to guild 1
    await in_memory_db.add_to_blacklist(
        guild_id=guild_id_1,
        target_type="USER",
        target_value=str(user_id),
        display_mask=f"User {user_id}",
        reason="Toxic behavior",
        blacklisted_by=123,
    )
    await in_memory_db.add_to_blacklist(
        guild_id=guild_id_1,
        target_type="STUDENT_ID",
        target_value=id_hash,
        display_mask="23***34",
        reason="Academic dishonesty",
        blacklisted_by=123,
    )
    await in_memory_db.add_to_blacklist(
        guild_id=guild_id_1,
        target_type="EMAIL",
        target_value=email_hash,
        display_mask="s***@student...",
        reason="Compromised email",
        blacklisted_by=123,
    )

    # 2. Verify is_blacklisted in Guild 1
    is_bl, reason = await in_memory_db.is_blacklisted(guild_id_1, user_id=user_id)
    assert is_bl is True
    assert reason == "Toxic behavior"

    is_bl_id, reason_id = await in_memory_db.is_blacklisted(guild_id_1, student_id_hash=id_hash)
    assert is_bl_id is True
    assert reason_id == "Academic dishonesty"

    is_bl_em, reason_em = await in_memory_db.is_blacklisted(guild_id_1, email_hash=email_hash)
    assert is_bl_em is True
    assert reason_em == "Compromised email"

    # Combined check
    is_bl_comb, _ = await in_memory_db.is_blacklisted(guild_id_1, user_id=user_id, student_id_hash=id_hash)
    assert is_bl_comb is True

    # 3. Isolation: Guild 2 should NOT be blacklisted
    is_bl_g2, _ = await in_memory_db.is_blacklisted(guild_id_2, user_id=user_id)
    assert is_bl_g2 is False

    is_bl_g2_id, _ = await in_memory_db.is_blacklisted(guild_id_2, student_id_hash=id_hash)
    assert is_bl_g2_id is False

    # 4. Count and list
    assert await in_memory_db.count_guild_blacklist(guild_id_1) == 3
    assert await in_memory_db.count_guild_blacklist(guild_id_1, target_type="USER") == 1
    assert await in_memory_db.count_guild_blacklist(guild_id_2) == 0

    records = await in_memory_db.get_guild_blacklist(guild_id_1)
    assert len(records) == 3

    user_records = await in_memory_db.get_guild_blacklist(guild_id_1, target_type="USER")
    assert len(user_records) == 1
    assert user_records[0]["target_value"] == str(user_id)

    # 5. Remove target
    assert await in_memory_db.remove_from_blacklist(guild_id_1, "USER", str(user_id)) is True
    is_bl_after, _ = await in_memory_db.is_blacklisted(guild_id_1, user_id=user_id)
    assert is_bl_after is False
    assert await in_memory_db.count_guild_blacklist(guild_id_1) == 2

    # 6. Clear blacklist
    cleared = await in_memory_db.clear_guild_blacklist(guild_id_1)
    assert cleared == 2
    assert await in_memory_db.count_guild_blacklist(guild_id_1) == 0


@pytest.mark.asyncio
async def test_verification_service_blacklist_flow(in_memory_db, secret_key):
    bot = MagicMock(spec=discord.Client)
    rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
    service = VerificationService(bot=bot, db=in_memory_db, secret=secret_key, rate_limiter=rate_limiter)

    guild_1 = MagicMock(spec=discord.Guild)
    guild_1.id = 111111111111111111
    guild_1.name = "TARUMT Main Hub"
    guild_1.roles = []
    guild_1.me = MagicMock(spec=discord.Member)
    guild_1.me.top_role = MagicMock(spec=discord.Role, position=100)
    guild_1.me.guild_permissions = MagicMock(manage_roles=True)

    guild_2 = MagicMock(spec=discord.Guild)
    guild_2.id = 222222222222222222
    guild_2.name = "TARUMT Club Guild"
    guild_2.roles = []
    guild_2.me = MagicMock(spec=discord.Member)
    guild_2.me.top_role = MagicMock(spec=discord.Role, position=100)
    guild_2.me.guild_permissions = MagicMock(manage_roles=True)

    bot.guilds = [guild_1, guild_2]

    # Target user
    user = MagicMock(spec=discord.Member)
    user.id = 123456789
    user.name = "TestStudent"
    user.display_name = "Test Student"
    user.guild = guild_1

    focs_role = MagicMock(spec=discord.Role)
    focs_role.position = 10
    focs_role.name = "Faculty of Computing and Information Technology"
    user.roles = [focs_role]
    user.remove_roles = AsyncMock()

    guild_1.get_member = MagicMock(return_value=user)
    guild_2.get_member = MagicMock(return_value=user)

    admin = MagicMock(spec=discord.Member)
    admin.id = 999999
    admin.name = "SuperAdmin"

    # 1. Blacklist user via service
    success, msg = await service.blacklist_target(
        guild=guild_1,
        target_type="USER",
        raw_value=str(user.id),
        reason="Repeated rule violations",
        admin=admin,
    )
    assert success is True
    assert "Successfully added [USER]" in msg
    assert user.remove_roles.called

    # 2. Attempt verification in Guild 1 - should be blocked by blacklist
    response = await service.perform_verification(
        user=user,
        raw_student_id="23WMD09867",
        raw_email="test@student.tarc.edu.my",
    )
    assert "⛔ You are blacklisted from verifying in **TARUMT Main Hub**" in response

    # 3. Attempt verification with Guild 2 context - should succeed in Guild 2, but skip Guild 1
    user.guild = guild_2
    user.roles = []
    user.add_roles = AsyncMock()

    response_g2 = await service.perform_verification(
        user=user,
        raw_student_id="23WMD09867",
        raw_email="test@student.tarc.edu.my",
    )
    assert "You've been given the following role(s)" in response_g2
    assert "TARUMT Club Guild" in response_g2
    # Should NOT have verified roles in Guild 1
    assert "TARUMT Main Hub" not in response_g2

    # 4. Self-healing reconciliation: If user somehow obtains roles in Guild 1, reconcile should strip them
    user.roles = [focs_role]
    user.remove_roles.reset_mock()
    summary = await service.reconcile_verified_members(guild_1)
    assert summary["unauthorized_cleaned"] >= 1
    assert user.remove_roles.called


@pytest.mark.asyncio
async def test_guest_service_blacklist_enforcement(in_memory_db, secret_key):
    bot = MagicMock(spec=discord.Client)
    rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
    guest_service = GuestService(bot=bot, db=in_memory_db, rate_limiter=rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 555555555555555555
    guild.name = "Guest Test Guild"
    guild.roles = []

    blacklisted_user = MagicMock(spec=discord.Member)
    blacklisted_user.id = 777888999
    blacklisted_user.guild = guild
    blacklisted_user.roles = []

    # Blacklist the user
    await in_memory_db.add_to_blacklist(
        guild_id=guild.id,
        target_type="USER",
        target_value=str(blacklisted_user.id),
        reason="Spamming guest applications",
    )

    # 1. Blacklisted user trying to create a referral code
    ok_ref, ref_msg = await guest_service.create_referral_code(guild.id, blacklisted_user)
    assert ok_ref is False
    assert "⛔ You are blacklisted" in ref_msg

    # 2. Blacklisted user trying to open guest review ticket
    ok_tkt, tkt_msg, _ = await guest_service.open_guest_review_ticket(
        guild=guild,
        applicant=blacklisted_user,
        reason="Trying to get in",
    )
    assert ok_tkt is False
    assert "⛔ You are blacklisted from requesting guest access" in tkt_msg


@pytest.mark.asyncio
async def test_admin_cog_blacklist_commands(in_memory_db, secret_key):
    bot = MagicMock(spec=discord.Client)
    rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
    service = VerificationService(bot=bot, db=in_memory_db, secret=secret_key, rate_limiter=rate_limiter)
    cog = AdminCog(
        bot=bot,
        db=in_memory_db,
        service=service,
        rate_limiter=rate_limiter,
        admin_role_name="TARVeri Admin",
    )

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123456789012345678
    guild.name = "Admin Test Server"

    admin_member = MagicMock(spec=discord.Member)
    admin_member.id = 111111111
    admin_member.guild_permissions.administrator = True
    admin_member.roles = []

    target_user = MagicMock(spec=discord.User)
    target_user.id = 222222222
    target_user.name = "BadUser"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_member
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    # 1. /admin blacklist user
    await cog.blacklist_user.callback(cog, interaction, user=target_user, reason="Raid attempt")
    interaction.followup.send.assert_called_once()
    assert "Successfully added [USER]" in interaction.followup.send.call_args[0][0]

    # 2. /admin blacklist student_id
    interaction.followup.send.reset_mock()
    await cog.blacklist_student_id.callback(cog, interaction, student_id="23WMD00001", reason="Forged card")
    interaction.followup.send.assert_called_once()
    assert "Successfully added [STUDENT_ID]" in interaction.followup.send.call_args[0][0]

    # 3. /admin blacklist email
    interaction.followup.send.reset_mock()
    await cog.blacklist_email.callback(cog, interaction, email="bad@student.tarc.edu.my", reason="Compromised account")
    interaction.followup.send.assert_called_once()
    assert "Successfully added [EMAIL]" in interaction.followup.send.call_args[0][0]

    # 4. /admin blacklist list
    interaction.followup.send.reset_mock()
    await cog.blacklist_list.callback(cog, interaction, target_type="ALL", page=1)
    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "Guild Blacklist" in embed.title
    assert len(embed.fields) == 3

    # 5. /admin blacklist remove
    interaction.followup.send.reset_mock()
    await cog.blacklist_remove.callback(cog, interaction, target_type="USER", target=str(target_user.id))
    interaction.followup.send.assert_called_once()
    assert "Successfully removed [USER]" in interaction.followup.send.call_args[0][0]

    # 6. /admin blacklist clear
    interaction.followup.send.reset_mock()
    await cog.blacklist_clear.callback(cog, interaction)
    interaction.followup.send.assert_called_once()
    assert "Cleared **2** blacklist entries" in interaction.followup.send.call_args[0][0]


@pytest.mark.asyncio
async def test_tarveri_log_channel_creation_and_alerts(in_memory_db, secret_key):
    bot = MagicMock(spec=discord.Client)
    rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
    service = VerificationService(bot=bot, db=in_memory_db, secret=secret_key, rate_limiter=rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 888888888888888888
    guild.name = "Alerts Test Guild"
    guild.default_role = MagicMock(spec=discord.Role)
    guild.text_channels = []

    admin_role = MagicMock(spec=discord.Role)
    admin_role.permissions = MagicMock(administrator=True, manage_guild=False)
    guild.roles = [guild.default_role, admin_role]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.name = "tarveri-log"
    log_channel.send = AsyncMock()

    guild.me = MagicMock(spec=discord.Member)
    guild.me.guild_permissions = MagicMock(administrator=True, manage_channels=True)
    guild.create_text_channel = AsyncMock(return_value=log_channel)

    # 1. First get_or_create should auto-create the channel
    ch = await service.get_or_create_tarveri_log_channel(guild)
    assert ch == log_channel
    guild.create_text_channel.assert_called_once()
    args, kwargs = guild.create_text_channel.call_args
    assert kwargs["name"] == "tarveri-log"
    overwrites = kwargs["overwrites"]
    # Check default_role overwrites hide channel
    assert overwrites[guild.default_role].view_channel is False
    # Check admin role overwrites allow viewing
    assert overwrites[admin_role].view_channel is True

    # 2. Subsequent lookup should find existing channel in text_channels
    guild.text_channels = [log_channel]
    log_channel.permissions_for = MagicMock(return_value=MagicMock(view_channel=True, send_messages=True))
    guild.create_text_channel.reset_mock()
    ch_existing = await service.get_or_create_tarveri_log_channel(guild)
    assert ch_existing == log_channel
    guild.create_text_channel.assert_not_called()

    # 3. Test send_admin_security_alert
    alert_embed = discord.Embed(title="Test Alert", description="Security alert description")
    sent = await service.send_admin_security_alert(guild, alert_embed)
    assert sent is True
    log_channel.send.assert_called_with(embed=alert_embed)


@pytest.mark.asyncio
async def test_blacklist_member_join_alert(in_memory_db, secret_key):
    from tarveri.cogs.verification_cog import VerificationCog

    bot = MagicMock(spec=discord.Client)
    rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
    service = VerificationService(bot=bot, db=in_memory_db, secret=secret_key, rate_limiter=rate_limiter)
    cog = VerificationCog(bot=bot, db=in_memory_db, service=service, rate_limiter=rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 999111222333444555
    guild.name = "Join Alert Guild"
    guild.default_role = MagicMock(spec=discord.Role)
    guild.roles = [guild.default_role]

    log_channel = MagicMock(spec=discord.TextChannel)
    log_channel.name = "tarveri-log"
    log_channel.send = AsyncMock()
    log_channel.permissions_for = MagicMock(return_value=MagicMock(view_channel=True, send_messages=True))
    guild.text_channels = [log_channel]
    guild.me = MagicMock(spec=discord.Member)
    guild.me.guild_permissions = MagicMock(administrator=True)

    member = MagicMock(spec=discord.Member)
    member.id = 777111222
    member.name = "BlacklistedJoiner"
    member.mention = "<@777111222>"
    member.guild = guild
    member.roles = []

    # Blacklist member
    await in_memory_db.add_to_blacklist(
        guild_id=guild.id,
        target_type="USER",
        target_value=str(member.id),
        display_mask=f"User {member.id}",
        reason="Server raider",
    )

    # Member joins
    await cog.on_member_join(member)

    # Check that alert embed was sent to #tarveri-log
    log_channel.send.assert_called_once()
    embed = log_channel.send.call_args[1]["embed"]
    assert "Blacklisted User Joined" in embed.title
    assert "Server raider" in [f.value for f in embed.fields if f.name == "📝 Reason"][0]

