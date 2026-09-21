from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.cogs.verification_cog import VerificationCog
from tarveri.config import Settings
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import VerificationService


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.command_prefix = "!"
    bot.get_prefix = AsyncMock(return_value=["!"])
    bot.guilds = []
    return bot


@pytest.fixture
def mock_service():
    return MagicMock(spec=VerificationService)


@pytest.fixture
def mock_rate_limiter():
    return RateLimiter()


@pytest.mark.asyncio
async def test_is_help_channel_with_configured_id(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_test.db"))
    await db.connect()
    settings = Settings(
        bot_token="token",
        id_hash_secret="secret",
        help_channel_id=998877,
    )
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter, settings=settings)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345

    # Channel matching configured ID
    ch_match = MagicMock(spec=discord.TextChannel)
    ch_match.id = 998877
    ch_match.guild = guild
    assert await cog.is_help_channel(ch_match) is True

    # Channel with different ID and non-help name
    ch_other = MagicMock(spec=discord.TextChannel)
    ch_other.id = 12345
    ch_other.name = "general"
    ch_other.guild = guild
    assert await cog.is_help_channel(ch_other) is False

    await db.close()


@pytest.mark.asyncio
async def test_is_help_channel_per_guild_db_override(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_override_test.db"))
    await db.connect()
    guild_id = 777888
    # Save per-guild help channel 55555
    await db.set_guild_help_channel(guild_id, 55555)

    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id

    ch_per_guild = MagicMock(spec=discord.TextChannel)
    ch_per_guild.id = 55555
    ch_per_guild.name = "custom-ask"
    ch_per_guild.guild = guild
    assert await cog.is_help_channel(ch_per_guild) is True

    ch_other = MagicMock(spec=discord.TextChannel)
    ch_other.id = 99999
    ch_other.name = "help"
    ch_other.guild = guild
    assert await cog.is_help_channel(ch_other) is False

    await db.close()


@pytest.mark.asyncio
async def test_is_help_channel_autodetect_keyword_and_permissions(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_test2.db"))
    await db.connect()
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 23456
    default_role = MagicMock(spec=discord.Role)
    guild.default_role = default_role

    # Channel named "help" where @everyone can send messages
    ch_help = MagicMock(spec=discord.TextChannel)
    ch_help.id = 101
    ch_help.name = "verification-help"
    ch_help.guild = guild
    perms_allowed = MagicMock()
    perms_allowed.view_channel = True
    perms_allowed.send_messages = True
    ch_help.permissions_for.return_value = perms_allowed

    assert await cog.is_help_channel(ch_help) is True

    # Channel named "help" but @everyone cannot send messages (read-only announcements)
    ch_locked = MagicMock(spec=discord.TextChannel)
    ch_locked.id = 102
    ch_locked.name = "help-desk"
    ch_locked.guild = guild
    perms_locked = MagicMock()
    perms_locked.view_channel = True
    perms_locked.send_messages = False
    ch_locked.permissions_for.return_value = perms_locked

    assert await cog.is_help_channel(ch_locked) is False

    await db.close()


@pytest.mark.asyncio
async def test_get_welcome_or_verify_channel_priority(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_test3.db"))
    await db.connect()
    guild_id = 888999
    # Set per-guild welcome channel in DB
    await db.set_guild_welcome_channel(guild_id, 333)

    settings = Settings(
        bot_token="token",
        id_hash_secret="secret",
        welcome_channel_id=111,
        help_channel_id=222,
    )
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter, settings=settings)

    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.me = MagicMock()

    ch_guild_configured = MagicMock(spec=discord.TextChannel)
    ch_guild_configured.id = 333
    perms_guild = MagicMock()
    perms_guild.view_channel = True
    perms_guild.send_messages = True
    ch_guild_configured.permissions_for.return_value = perms_guild

    guild.get_channel.side_effect = lambda cid: ch_guild_configured if cid == 333 else None

    # Priority 0: Per-guild database setting overrides global settings
    found = await cog.get_welcome_or_verify_channel(guild)
    assert found == ch_guild_configured

    # Reset per-guild setting: falls back to global welcome_channel_id (111)
    await db.set_guild_welcome_channel(guild_id, None)
    cog.invalidate_guild_cache(guild_id)

    ch_global_welcome = MagicMock(spec=discord.TextChannel)
    ch_global_welcome.id = 111
    ch_global_welcome.permissions_for.return_value = perms_guild
    guild.get_channel.side_effect = lambda cid: ch_global_welcome if cid == 111 else None

    found = await cog.get_welcome_or_verify_channel(guild)
    assert found == ch_global_welcome

    await db.close()


@pytest.mark.asyncio
async def test_is_unverified_member(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_test.db"))
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    member_unverified = MagicMock(spec=discord.Member)
    role_member = MagicMock(spec=discord.Role)
    role_member.name = "Member"
    member_unverified.roles = [role_member]
    assert cog.is_unverified_member(member_unverified) is True

    member_verified = MagicMock(spec=discord.Member)
    role_faculty = MagicMock(spec=discord.Role)
    role_faculty.name = "FOCS"
    member_verified.roles = [role_member, role_faculty]
    assert cog.is_unverified_member(member_verified) is False


@pytest.mark.asyncio
async def test_on_message_help_channel_keyword_alert(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_msg_test.db"))
    await db.connect()
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    guild.name = "Test Guild"

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 456
    channel.name = "help"
    channel.guild = guild
    perms = MagicMock()
    perms.view_channel = True
    perms.send_messages = True
    channel.permissions_for.return_value = perms

    member = MagicMock(spec=discord.Member)
    member.id = 789
    member.bot = False
    member.roles = []
    member.mention = "<@789>"
    member.__str__.return_value = "Student#1234"

    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.channel = channel
    message.author = member
    message.content = "Hello, how to get role?"
    message.reply = AsyncMock()

    # 1. Trigger role tip alert
    await cog.on_message(message)
    message.reply.assert_called_once()
    reply_embed = message.reply.call_args.kwargs.get("embed") or message.reply.call_args[1].get("embed")
    assert reply_embed is not None
    assert "/verify" in reply_embed.description
    assert "<@789>" in reply_embed.description

    # 2. Test cooldown: sending another inquiry within 60s should NOT trigger another reply
    message.reply.reset_mock()
    message.content = "where is my role"
    await cog.on_message(message)
    message.reply.assert_not_called()

    await db.close()


@pytest.mark.asyncio
async def test_on_message_ignores_verified_member(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_verified_test.db"))
    await db.connect()
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    channel = MagicMock(spec=discord.TextChannel)
    channel.name = "help"
    channel.guild = guild
    perms = MagicMock()
    perms.view_channel = True
    perms.send_messages = True
    channel.permissions_for.return_value = perms

    member = MagicMock(spec=discord.Member)
    member.id = 789
    member.bot = False
    faculty_role = MagicMock(spec=discord.Role)
    faculty_role.name = "FAFB"
    member.roles = [faculty_role]

    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.channel = channel
    message.author = member
    message.content = "how to get role?"
    message.reply = AsyncMock()

    await cog.on_message(message)
    message.reply.assert_not_called()

    await db.close()


@pytest.mark.asyncio
async def test_on_member_join_tags_unverified_member(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_join_test.db"))
    await db.connect()
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    guild.name = "TARUMT Campus"
    guild.me = MagicMock()

    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.name = "welcome"
    welcome_channel.send = AsyncMock()
    perms = MagicMock()
    perms.view_channel = True
    perms.send_messages = True
    welcome_channel.permissions_for.return_value = perms

    guild.text_channels = [welcome_channel]
    guild.system_channel = None

    new_member = MagicMock(spec=discord.Member)
    new_member.id = 99999
    new_member.guild = guild
    new_member.mention = "<@99999>"
    new_member.send = AsyncMock()
    new_member.__str__.return_value = "Newbie#0001"

    await cog.on_member_join(new_member)

    # Welcome channel should be sent a tag message
    welcome_channel.send.assert_called_once()
    tag_content = welcome_channel.send.call_args.kwargs.get("content") or welcome_channel.send.call_args[1].get("content", "")
    tag_embed = welcome_channel.send.call_args.kwargs.get("embed") or welcome_channel.send.call_args[1].get("embed")
    assert "<@99999>" in tag_content or (tag_embed and "<@99999>" in tag_embed.description)
    assert tag_embed is not None

    # DM should also be attempted
    new_member.send.assert_called_once()

    await db.close()


@pytest.mark.asyncio
async def test_on_member_join_auto_sync_already_verified(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_join_sync_test.db"))
    await db.connect()

    # Pre-record verification
    user_id = 88888
    await db.record_verification(user_id, "hash_123", "M")

    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 123
    guild.name = "TARUMT Campus"
    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.name = "welcome"
    welcome_channel.send = AsyncMock()
    guild.text_channels = [welcome_channel]

    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild = guild
    member.send = AsyncMock()
    member.__str__.return_value = "Returning#0002"

    mock_sync_result = MagicMock()
    mock_sync_result.verified_in = [(123, "TARUMT Campus", "FOCS, KL Main Campus, Degree")]
    mock_service.assign_role_across_guilds = AsyncMock(return_value=mock_sync_result)

    await cog.on_member_join(member)

    # Should have called role assignment with faculty, campus, and study level roles
    mock_service.assign_role_across_guilds.assert_called_once_with(
        user_id, "FOCS", [guild], campus_role_name="KL Main Campus", level_role_name="Degree", is_email_verified=False
    )
    # Should NOT have sent the new member tag message in welcome channel
    welcome_channel.send.assert_not_called()
    # Member gets confirmation DM
    member.send.assert_called_once()
    await db.close()


@pytest.mark.asyncio
async def test_on_member_join_auto_sync_with_branch_level_and_alumni(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "cog_join_sync_penang_test.db"))
    await db.connect()

    user_id = 99911
    # Pre-record verification with FAFB (B), Penang campus (P), and Diploma (D) and Alumni status
    await db.record_verification(user_id, "hash_penang", "B", campus_code="P", level_code="D")
    await db.record_alumni_claim(user_id, 2024, "Diploma in Business")

    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 456
    guild.name = "TARUMT Penang Campus"
    welcome_channel = MagicMock(spec=discord.TextChannel)
    welcome_channel.name = "welcome"
    welcome_channel.send = AsyncMock()
    guild.text_channels = [welcome_channel]

    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.guild = guild
    member.send = AsyncMock()
    member.__str__.return_value = "Alumni#0001"

    mock_sync_result = MagicMock()
    mock_sync_result.verified_in = [(456, "TARUMT Penang Campus", "FAFB, Penang Branch, Diploma")]
    mock_service.assign_role_across_guilds = AsyncMock(return_value=mock_sync_result)
    mock_service.sync_alumni_role_across_guilds = AsyncMock(return_value=["TARUMT Penang Campus"])

    await cog.on_member_join(member)

    # Should assign FAFB, Penang Branch, and Diploma roles
    mock_service.assign_role_across_guilds.assert_called_once_with(
        user_id, "FAFB", [guild], campus_role_name="Penang Branch", level_role_name="Diploma", is_email_verified=False
    )
    # Should sync alumni role
    mock_service.sync_alumni_role_across_guilds.assert_called_once_with(
        user_id, [guild], reason="TARVeri: Auto-assigned returning alumni role on join"
    )
    # Member gets confirmation DM mentioning roles
    member.send.assert_called_once()
    await db.close()


@pytest.mark.asyncio
async def test_verify_slash_resync_already_verified(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "verify_resync_test.db"))
    await db.connect()

    user_id = 77722
    await db.record_verification(user_id, "hash_resync", "M", campus_code="A", level_code="F")
    await db.record_alumni_claim(user_id, 2023, "Foundation in Computing")

    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 789
    guild.name = "TARUMT Perak Campus"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    mock_service.get_mutual_guilds_for_user = AsyncMock(return_value=[guild])
    mock_sync_result = MagicMock()
    mock_sync_result.verified_in = [(789, "TARUMT Perak Campus", "FOCS, Perak Branch, Foundation")]
    mock_sync_result.already_had_role_in = []
    mock_sync_result.missing_role_in = []
    mock_sync_result.failed_in = []
    mock_service.assign_role_across_guilds = AsyncMock(return_value=mock_sync_result)
    mock_service.format_role_summary.return_value = "✅ Roles updated successfully"
    mock_service.sync_alumni_role_across_guilds = AsyncMock(return_value=["TARUMT Perak Campus"])

    # Invoke /verify with no student_id (resync path)
    await cog.verify_slash.callback(cog, interaction, student_id=None)

    interaction.response.defer.assert_called_once()
    mock_service.assign_role_across_guilds.assert_called_once_with(
        user_id, "FOCS", [guild], campus_role_name="Perak Branch", level_role_name="Foundation"
    )
    mock_service.sync_alumni_role_across_guilds.assert_called_once_with(
        user_id, [guild], reason="TARVeri: Resync alumni role"
    )
    interaction.followup.send.assert_called_once()
    await db.close()



@pytest.mark.asyncio
async def test_verify_slash_direct_argument(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "verify_slash.db"))
    await db.connect()
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = 333000
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    mock_service.perform_verification = AsyncMock(return_value="✅ Verified successfully")

    # Invoking with student_id directly
    await cog.verify_slash.callback(cog, interaction, student_id="23WMD09867")
    interaction.response.defer.assert_called_once()
    mock_service.perform_verification.assert_called_once_with(interaction.user, "23WMD09867", raw_expiry_date=None)
    interaction.followup.send.assert_called_once_with("✅ Verified successfully", ephemeral=True)

    # Invoking with student_id and expiry_date
    mock_service.perform_verification.reset_mock()
    interaction.response.defer.reset_mock()
    interaction.followup.send.reset_mock()
    await cog.verify_slash.callback(cog, interaction, student_id="23WMD09867", expiry_date="10/26")
    mock_service.perform_verification.assert_called_once_with(interaction.user, "23WMD09867", raw_expiry_date="10/26")

    await db.close()


@pytest.mark.asyncio
async def test_channel_self_healing_clears_deleted_help_and_welcome(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "self_healing_cog_channels.db"))
    await db.connect()
    cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 998811
    guild.name = "Healing Cog Guild"
    guild.system_channel = None

    # Configure deleted help channel and deleted welcome channel in DB
    stale_welcome_id = 111999
    stale_help_id = 222999
    await db.set_guild_welcome_channel(guild.id, stale_welcome_id)
    await db.set_guild_help_channel(guild.id, stale_help_id)

    # get_channel returns None for both stale channels
    guild.get_channel.return_value = None

    # Autodetected fallback channel
    fallback_welcome = MagicMock(spec=discord.TextChannel)
    fallback_welcome.name = "welcome-gate"
    fallback_welcome.guild = guild
    perms = MagicMock()
    perms.view_channel = True
    perms.send_messages = True
    fallback_welcome.permissions_for.return_value = perms
    guild.text_channels = [fallback_welcome]
    guild.me = MagicMock()

    # 1. Test get_welcome_or_verify_channel self-heals stale welcome channel
    chosen = await cog.get_welcome_or_verify_channel(guild)
    assert chosen == fallback_welcome

    # Check DB setting for welcome channel was cleared
    settings = await db.get_guild_settings(guild.id)
    assert settings[0] is None

    # 2. Test is_help_channel self-heals stale help channel
    non_matching_ch = MagicMock(spec=discord.TextChannel)
    non_matching_ch.id = 555555
    non_matching_ch.name = "general-chat"
    non_matching_ch.guild = guild

    is_help = await cog.is_help_channel(non_matching_ch)
    assert is_help is False

    # Check DB setting for help channel was cleared
    settings = await db.get_guild_settings(guild.id)
    assert settings[1] is None

    await db.close()


@pytest.mark.asyncio
async def test_on_message_dm_verification(mock_bot, mock_service, mock_rate_limiter, tmp_path):
    db = Database(str(tmp_path / "dm_prefix_test.db"))
    await db.connect()
    try:
        cog = VerificationCog(mock_bot, db, mock_service, mock_rate_limiter)
        mock_service.perform_verification = AsyncMock(return_value="✅ Verified successfully")

        message = MagicMock(spec=discord.Message)
        message.guild = None
        message.author = MagicMock(spec=discord.User)
        message.author.bot = False
        message.author.send = AsyncMock()
        message.content = "23WMD09867"

        await cog.on_message(message)
        mock_service.perform_verification.assert_called_once_with(message.author, "23WMD09867")
        message.author.send.assert_called_once_with("✅ Verified successfully")
    finally:
        await db.close()



