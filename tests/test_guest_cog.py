from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.cogs.guest_cog import (
    GuestCog,
    GuestReviewThreadView,
    build_review_embed,
)
from tarveri.database import Database
from tarveri.services.guest_service import GuestService
from tarveri.services.verification_service import VerificationService


@pytest.mark.asyncio
async def test_guest_cog_referral_commands(tmp_path):
    db_path = str(tmp_path / "cog_guest.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    guest_service = GuestService(bot, db, admin_role_name="TARVeri Admin")
    verification_service = MagicMock(spec=VerificationService)

    cog = GuestCog(bot, db, guest_service, verification_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    guild.name = "My Guild"

    # 1. Unverified student tries to generate referral code
    unverified_user = MagicMock(spec=discord.Member)
    unverified_user.id = 1001
    unverified_user.roles = []

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = unverified_user
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await cog.referral_generate.callback(cog, interaction, ttl_hours=48)
    interaction.response.send_message.assert_called_once()
    assert "Only verified TARUMT students" in interaction.response.send_message.call_args[0][0]

    # 2. Verified student generates referral code
    await db.record_verification(1001, "hash1001", "M")
    interaction.response.send_message.reset_mock()

    await cog.referral_generate.callback(cog, interaction, ttl_hours=48)
    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "TAR-" in embed.description

    # 3. Referral list
    interaction.followup.send.reset_mock()
    await cog.referral_list.callback(cog, interaction)
    interaction.followup.send.assert_called_once()
    list_embed = interaction.followup.send.call_args[1]["embed"]
    assert len(list_embed.fields) == 1
    assert "TAR-" in list_embed.fields[0].name

    await db.close()


@pytest.mark.asyncio
async def test_send_gateway_panel(tmp_path):
    db_path = str(tmp_path / "gateway_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    guest_service = GuestService(bot, db, admin_role_name="TARVeri Admin")
    verification_service = MagicMock(spec=VerificationService)
    cog = GuestCog(bot, db, guest_service, verification_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 111222

    admin_user = MagicMock(spec=discord.Member)
    admin_user.guild_permissions.administrator = True

    channel = MagicMock(spec=discord.TextChannel)
    channel.mention = "<#333444>"
    channel.send = AsyncMock()

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = admin_user
    interaction.channel = channel
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await cog.send_gateway_panel.callback(cog, interaction, channel=channel)
    channel.send.assert_called_once()
    interaction.followup.send.assert_called_once()
    assert "Verification gateway panel posted" in interaction.followup.send.call_args[0][0]

    await db.close()


@pytest.mark.asyncio
async def test_guest_review_thread_double_verification(tmp_path):
    db_path = str(tmp_path / "double_verif_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    guest_service = GuestService(bot, db, admin_role_name="TARVeri Admin")

    view = GuestReviewThreadView(guest_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 555
    guild.name = "Double Guild"
    guild.me = MagicMock()
    guild.me.guild_permissions.manage_roles = True
    guild.me.top_role = MagicMock()

    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest(Approved)"
    guild.roles = [guest_role]

    admin_user = MagicMock(spec=discord.Member)
    admin_user.id = 9999
    admin_user.guild_permissions.administrator = True
    admin_user.roles = []
    admin_user.mention = "<@9999>"

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 8888
    applicant.top_role = MagicMock()
    applicant.top_role.__lt__.return_value = True
    applicant.add_roles = AsyncMock()
    applicant.send = AsyncMock()
    guild.get_member.return_value = applicant

    # Create referral ticket without vouch_note yet
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=applicant.id,
        referrer_id=7777,
        channel_id=4444,
        referral_code="TAR-DOUBLE",
    )

    channel = MagicMock(spec=discord.Thread)
    channel.id = 4444
    channel.send = AsyncMock()
    channel.edit = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.channel = channel
    interaction.user = admin_user
    interaction.message = message
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Admin tries to approve BEFORE voucher has confirmed
    await view.approve_btn.callback(interaction)
    interaction.response.send_message.assert_called_once()
    assert "Double Verification Required" in interaction.response.send_message.call_args[0][0]

    # 2. Voucher submits vouch
    await db.update_guest_ticket_vouch(ticket_id, "Confirmed classmate")
    interaction.response.send_message.reset_mock()

    # 3. Admin approves AFTER voucher has confirmed -> Success!
    await view.approve_btn.callback(interaction)
    interaction.followup.send.assert_called_once()
    applicant.add_roles.assert_called_once()

    ticket_after = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_after["status"] == "APPROVED"
    assert ticket_after["closed_by_admin_id"] == admin_user.id
    assert ticket_after["close_reason"] == "Approved by admin"

    await db.close()


@pytest.mark.asyncio
async def test_guest_review_thread_permissions(tmp_path):
    db_path = str(tmp_path / "review_perm_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    guest_service = GuestService(bot, db, admin_role_name="TARVeri Admin")
    view = GuestReviewThreadView(guest_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 666
    guild.name = "Perm Guild"

    regular_member = MagicMock(spec=discord.Member)
    regular_member.id = 1111
    regular_member.guild_permissions.administrator = False
    regular_member.roles = []

    channel = MagicMock(spec=discord.Thread)
    channel.id = 5555

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.channel = channel
    interaction.user = regular_member
    interaction.response.send_message = AsyncMock()

    # 1. Non-admin tries to approve
    await view.approve_btn.callback(interaction)
    interaction.response.send_message.assert_called_once()
    assert "Only server administrators" in interaction.response.send_message.call_args[0][0]

    # 2. Non-admin tries to reject
    interaction.response.send_message.reset_mock()
    await view.reject_btn.callback(interaction)
    interaction.response.send_message.assert_called_once()
    assert "Only server administrators" in interaction.response.send_message.call_args[0][0]

    # 3. Non-referrer / Non-admin tries to vouch
    await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=2222,
        referrer_id=3333,  # Referrer is 3333, caller is 1111
        channel_id=channel.id,
    )
    interaction.response.send_message.reset_mock()
    await view.vouch_btn.callback(interaction)
    interaction.response.send_message.assert_called_once()
    assert "Only the referring student" in interaction.response.send_message.call_args[0][0]

    await db.close()


def test_build_review_embed_alphanumeric_sequence():
    guild = MagicMock(spec=discord.Guild)
    applicant = MagicMock(spec=discord.Member)
    applicant.mention = "<@12345>"
    applicant.id = 12345

    ticket_1 = {
        "applicant_id": 12345,
        "ticket_seq": 1,
        "status": "OPEN",
    }
    embed_1 = build_review_embed(ticket_1, guild, applicant)
    assert embed_1.title == "📋 Guest Review Ticket #A0001"

    ticket_series = {
        "applicant_id": 12345,
        "ticket_seq": 10000,
        "status": "APPROVED",
    }
    embed_series = build_review_embed(ticket_series, guild, applicant)
    assert embed_series.title == "📋 Guest Review Ticket #B0001"


def test_build_review_embed_closed_status():
    guild = MagicMock(spec=discord.Guild)
    applicant = MagicMock(spec=discord.Member)
    applicant.mention = "<@12345>"
    applicant.id = 12345

    # Referral ticket closed
    ticket_ref = {
        "applicant_id": 12345,
        "referrer_id": 6789,
        "referral_code": "TAR-XYZ123",
        "ticket_seq": 5,
        "status": "CLOSED",
        "closed_by_admin_id": 9999,
        "close_reason": "Admin dismissal / spam check",
    }
    embed_ref = build_review_embed(ticket_ref, guild, applicant)
    assert embed_ref.title == "📋 Guest Review Ticket #A0005"
    assert embed_ref.color == discord.Color.dark_grey()
    decision_field = [f for f in embed_ref.fields if "Admin Decision" in f.name][0]
    assert "Closed / Dismissed" in decision_field.value
    assert "<@9999>" in decision_field.value
    assert "No kicking or role assigned" in decision_field.value

    # Application ticket closed
    ticket_app = {
        "applicant_id": 12345,
        "reason": "Speaker at conference",
        "ticket_seq": 6,
        "status": "CLOSED",
        "closed_by_admin_id": 9999,
        "close_reason": "Inquiries resolved",
    }
    embed_app = build_review_embed(ticket_app, guild, applicant)
    verdict_field = [f for f in embed_app.fields if "Staff Verdict" in f.name][0]
    assert "Closed / Dismissed" in verdict_field.value
    assert "Inquiries resolved" in verdict_field.value


@pytest.mark.asyncio
async def test_guest_review_thread_close_button_flow(tmp_path):
    from tarveri.cogs.guest_cog import CloseTicketModal

    db_path = str(tmp_path / "close_btn_test.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    guest_service = GuestService(bot, db, admin_role_name="TARVeri Admin")
    view = GuestReviewThreadView(guest_service)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 5566
    guild.name = "Close Btn Guild"

    admin_user = MagicMock(spec=discord.Member)
    admin_user.id = 9988
    admin_user.guild_permissions.administrator = True
    admin_user.roles = []
    admin_user.mention = "<@9988>"

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 7766
    applicant.mention = "<@7766>"
    applicant.kick = AsyncMock()
    applicant.add_roles = AsyncMock()
    guild.get_member.return_value = applicant

    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=applicant.id,
        channel_id=3322,
        reason="Testing close button",
        ticket_seq=12,
    )

    channel = MagicMock(spec=discord.Thread)
    channel.id = 3322
    channel.send = AsyncMock()
    channel.edit = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.edit = AsyncMock()

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.channel = channel
    interaction.user = admin_user
    interaction.message = message
    interaction.response.send_modal = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Admin clicks Close Ticket button -> Opens modal
    await view.close_btn.callback(interaction)
    interaction.response.send_modal.assert_called_once()
    modal = interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal, CloseTicketModal)

    # 2. Submit modal with custom closure reason
    modal.reason._value = "Duplicate ticket / Reconsidering later"
    await modal.on_submit(interaction)

    # Verify followup sent and thread archived
    interaction.followup.send.assert_called_once()
    channel.send.assert_called_once()
    assert "Ticket manually closed" in channel.send.call_args[0][0]
    assert "Duplicate ticket / Reconsidering later" in channel.send.call_args[0][0]
    channel.edit.assert_awaited_once_with(locked=True, archived=True)

    # Verify applicant was NOT kicked or given role
    applicant.kick.assert_not_called()
    applicant.add_roles.assert_not_called()

    # Verify DB status
    ticket_after = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket_after["status"] == "CLOSED"
    assert ticket_after["closed_by_admin_id"] == admin_user.id
    assert ticket_after["close_reason"] == "Duplicate ticket / Reconsidering later"

    await db.close()
