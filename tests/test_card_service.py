import io
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from PIL import Image

from tarveri.cogs.card_cog import CardCog
from tarveri.config import get_configured_tz
from tarveri.database import Database
from tarveri.services.card_service import CardService


@pytest.mark.asyncio
async def test_card_data_generation_verified_student(tmp_path):
    db = Database(str(tmp_path / "card_test_student.db"))
    await db.connect()
    try:
        service = CardService(db=db, admin_role_name="TARVeri Admin")

        user_id = 123456789
        await db.record_verification(
            user_id, "abc123hash999", "M", campus_code="W", level_code="D"
        )  # M -> FOCS, W -> KL Main Campus, D -> Diploma

        guild = MagicMock(spec=discord.Guild)
        guild.id = 998877
        guild.name = "TARUMT Main Server"

        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.display_name = "Alex Tan"
        member.name = "alextan_01"
        member.guild_permissions.administrator = False
        member.roles = []
        member.premium_since = None
        member.joined_at = datetime(2024, 5, 10, 12, 0, 0, tzinfo=UTC)

        data = await service.get_user_card_data(guild, member)

        assert data["user_id"] == user_id
        assert data["display_name"] == "Alex Tan"
        assert data["is_verified"] is True
        assert data["is_student"] is True
        assert data["is_guest"] is False
        assert data["faculty_name"] == "FOCS"
        assert "Faculty of Computing" in data["faculty_full"]
        assert "✦ FOCS" in data["badges"]
        assert "✓ VERIFIED" in data["badges"]
        assert data["cohort_str"] == "KL Main Campus • Diploma"
        assert data["hash_preview"].startswith("TRV-ABC1-")
        assert data["joined_at"] == "10 May 2024"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_card_data_generation_branch_campus_and_level_from_roles(tmp_path):
    db = Database(str(tmp_path / "card_test_branch.db"))
    await db.connect()
    try:
        service = CardService(db=db, admin_role_name="TARVeri Admin")

        user_id = 987654321
        # DB only has faculty code without campus/level
        await db.record_verification(user_id, "penang_hash_111", "M")

        guild = MagicMock(spec=discord.Guild)
        guild.id = 112233
        guild.name = "TARUMT Penang Campus"

        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.display_name = "Penang Student"
        member.name = "penang_student"
        member.guild_permissions.administrator = False

        r_focs = MagicMock(spec=discord.Role)
        r_focs.name = "FOCS"
        r_penang = MagicMock(spec=discord.Role)
        r_penang.name = "Penang Branch"
        r_degree = MagicMock(spec=discord.Role)
        r_degree.name = "Degree"
        member.roles = [r_focs, r_penang, r_degree]
        member.premium_since = None
        member.joined_at = datetime(2023, 9, 1, tzinfo=UTC)

        data = await service.get_user_card_data(guild, member)

        assert data["is_student"] is True
        assert data["cohort_str"] == "Penang Branch • Degree"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_card_data_generation_approved_guest(tmp_path):
    db = Database(str(tmp_path / "card_test_guest.db"))
    await db.connect()
    try:
        service = CardService(db=db, admin_role_name="TARVeri Admin")

        user_id = 234567890
        guild = MagicMock(spec=discord.Guild)
        guild.id = 887766
        guild.name = "TARUMT Gaming Hub"

        # Create approved guest ticket
        t_id = await db.create_guest_ticket(
            guild_id=guild.id,
            applicant_id=user_id,
            referrer_id=111222,
            channel_id=333444,
            reason="Hackathon partner",
        )
        await db.close_guest_ticket(t_id, status="APPROVED", close_reason="Approved by staff")

        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.display_name = "Guest User"
        member.name = "guest_01"
        member.guild_permissions.administrator = False
        member.roles = []
        member.premium_since = None
        member.joined_at = datetime(2025, 1, 15, tzinfo=UTC)

        data = await service.get_user_card_data(guild, member)

        assert data["is_verified"] is True
        assert data["is_student"] is False
        assert data["is_guest"] is True
        assert data["faculty_name"] == "Guest(Approved)"
        assert "◈ GUEST" in data["badges"]
        assert "✓ APPROVED" in data["badges"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_card_data_badges_admin_booster_veteran_voucher(tmp_path):
    db = Database(str(tmp_path / "card_test_badges.db"))
    await db.connect()
    try:
        service = CardService(db=db, admin_role_name="TARVeri Admin")

        user_id = 345678901
        await db.record_verification(user_id, "veteran_hash_555", "B")  # B -> FAFB

        # Create 2 used referrals for super voucher badge
        await db.create_referral_code("TAR-AAA111", 776655, user_id, "2099-01-01 00:00:00")
        await db.update_referral_code_status("TAR-AAA111", 776655, "USED", used_by_discord_id=9901)

        await db.create_referral_code("TAR-BBB222", 776655, user_id, "2099-01-01 00:00:00")
        await db.update_referral_code_status("TAR-BBB222", 776655, "USED", used_by_discord_id=9902)

        guild = MagicMock(spec=discord.Guild)
        guild.id = 776655
        guild.name = "TARUMT Official"

        member = MagicMock(spec=discord.Member)
        member.id = user_id
        member.display_name = "Senior Mod"
        member.name = "seniormod"
        member.guild_permissions.administrator = True
        member.roles = []
        member.premium_since = datetime(2025, 2, 1, tzinfo=UTC)
        # Joined 200 days ago
        member.joined_at = datetime.now(get_configured_tz()) - timedelta(days=200)

        data = await service.get_user_card_data(guild, member)

        assert "★ STAFF" in data["badges"]
        assert "▲ BOOSTER" in data["badges"]
        assert "⚡ VETERAN" in data["badges"]
        assert "◈ VOUCHER x2" in data["badges"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_render_card_image_dimensions_and_png_buffer(tmp_path):
    db = Database(str(tmp_path / "card_test_render.db"))
    await db.connect()
    try:
        service = CardService(db=db, admin_role_name="TARVeri Admin")
        await db.record_verification(456789012, "render_hash_888", "M")

        guild = MagicMock(spec=discord.Guild)
        guild.id = 112233
        guild.name = "TARUMT Test Campus"

        member = MagicMock(spec=discord.Member)
        member.id = 456789012
        member.display_name = "Testing Student"
        member.name = "test_student"
        member.guild_permissions.administrator = False
        member.roles = []
        member.premium_since = None
        member.joined_at = datetime(2024, 1, 1, tzinfo=UTC)

        # Create dummy avatar image bytes
        dummy_av = Image.new("RGBA", (128, 128), (255, 100, 100, 255))
        av_buf = io.BytesIO()
        dummy_av.save(av_buf, format="PNG")
        av_bytes = av_buf.getvalue()

        # Render card
        buf = await service.render_card(guild, member, avatar_bytes=av_bytes)
        assert isinstance(buf, io.BytesIO)

        # Verify output is a valid PNG with 920x530 resolution
        img = Image.open(buf)
        assert img.format == "PNG"
        assert img.size == (920, 530)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_card_cog_slash_and_context_menu(tmp_path):
    db = Database(str(tmp_path / "card_cog_test.db"))
    await db.connect()
    try:
        service = CardService(db=db, admin_role_name="TARVeri Admin")
        bot = MagicMock()
        bot.tree = MagicMock()
        bot.tree.add_command = MagicMock()
        bot.tree.remove_command = MagicMock()

        cog = CardCog(bot=bot, db=db, card_service=service)

        guild = MagicMock(spec=discord.Guild)
        guild.id = 555666
        guild.name = "Campus Guild"

        user = MagicMock(spec=discord.Member)
        user.id = 567890123
        user.display_name = "Me Student"
        user.display_avatar.read = AsyncMock(return_value=None)
        user.guild_permissions.administrator = False
        user.roles = []
        user.premium_since = None
        user.joined_at = None

        interaction = MagicMock(spec=discord.Interaction)
        interaction.guild = guild
        interaction.user = user
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()

        # 1. Test /card self (public by default)
        await cog.card.callback(cog, interaction, member=None, hidden=False)
        interaction.response.defer.assert_called_once_with(ephemeral=False, thinking=True)
        interaction.followup.send.assert_called_once()
        call_kwargs = interaction.followup.send.call_args[1]
        assert "embed" in call_kwargs
        assert "file" in call_kwargs
        assert call_kwargs["ephemeral"] is False

        # 2. Test /card with hidden=True
        interaction.response.defer.reset_mock()
        interaction.followup.send.reset_mock()
        await cog.card.callback(cog, interaction, member=None, hidden=True)
        interaction.response.defer.assert_called_once_with(ephemeral=True, thinking=True)
        call_kwargs_hidden = interaction.followup.send.call_args[1]
        assert call_kwargs_hidden["ephemeral"] is True

        # 3. Test context menu (public by default)
        interaction.response.defer.reset_mock()
        interaction.followup.send.reset_mock()
        await cog.view_card_context_menu(interaction, user)
        interaction.response.defer.assert_called_once_with(ephemeral=False, thinking=True)
        interaction.followup.send.assert_called_once()
        call_kwargs_cm = interaction.followup.send.call_args[1]
        assert call_kwargs_cm["ephemeral"] is False
    finally:
        await db.close()
