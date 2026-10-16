"""
Comprehensive test suite for edge cases:
- Full-width unicode and IME normalization for student IDs, expiry dates, OTP codes, and referral codes
- Zero-width spaces and formatting resilience
- Modal early deferral and Discord interaction timeout prevention
- InteractionChannel thread resolution and idempotent closing/archiving
- Guest approval role failure rollback resilience
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.config import (
    parse_card_expiry_date,
    parse_student_id,
    validate_student_id,
)
from tarveri.database import Database
from tarveri.services.email_service import EmailService, PendingOtp
from tarveri.services.guest_service import GuestService
from tarveri.views.guest_views import _resolve_review_thread


def test_parse_student_id_full_width_and_zero_width():
    """Verify that full-width IME characters and zero-width spaces are properly sanitized."""
    # Full-width: ２４ＷＭＤ０１２３４ -> 24WMD01234
    res = parse_student_id("２４ＷＭＤ０１２３４")
    assert res.is_valid is True
    assert res.student_id == "24WMD01234"
    assert res.faculty_code == "M"
    assert res.campus_code == "W"
    assert res.level_code == "D"
    assert res.intake_year == "24"
    assert res.sequence == "01234"

    # Embedded zero-width spaces, non-breaking spaces, and dashes
    res_zw = parse_student_id("\u200b24\xa0-\ufeffWMD\u200c-\u200d01234")
    assert res_zw.is_valid is True
    assert res_zw.student_id == "24WMD01234"

    # Backwards compatibility wrapper
    is_valid, norm_id, fac_code, fac_role = validate_student_id("２４ＷＭＤ０１２３４")
    assert is_valid is True
    assert norm_id == "24WMD01234"
    assert fac_code == "M"
    assert fac_role == "FOCS"


def test_parse_card_expiry_date_edge_cases():
    """Verify full-width dates, zero-width characters, and edge-case delimiters in parse_card_expiry_date."""
    # Full-width slashes and digits: １０／２６ -> 2026-10-31
    assert parse_card_expiry_date("１０／２６") == "2026-10-31"

    # Full-width full ISO: ２０２６－１０－３１ -> 2026-10-31
    assert parse_card_expiry_date("２０２６－１０－３１") == "2026-10-31"

    # Full-width DD/MM/YYYY: １５／０８／２０２６ -> 2026-08-15
    assert parse_card_expiry_date("１５／０８／２０２６") == "2026-08-15"

    # Zero-width spaces in date
    assert parse_card_expiry_date("\u200b10\ufeff/\xa026") == "2026-10-31"

    # None and empty strings
    assert parse_card_expiry_date(None) is None
    assert parse_card_expiry_date("") is None
    assert parse_card_expiry_date("   \u200b  ") is None


@pytest.mark.asyncio
async def test_email_service_verify_otp_edge_cases():
    """Verify OTP code parsing handles spaces, hyphens, and full-width IME characters."""
    settings = MagicMock()
    settings.email_otp_ttl_seconds = 300
    settings.email_otp_max_attempts = 3
    email_service = EmailService(settings)

    user_id = 998877
    now = time.monotonic()
    email_service._pending_otps[user_id] = PendingOtp(
        user_id=user_id,
        student_id="24WMD01234",
        email="student@student.tarc.edu.my",
        server_name="Test Guild",
        card_expiry_date="2026-10-31",
        otp_code="123456",
        expires_at=now + 300,
        attempts_left=3,
        created_at=now,
        last_sent_at=now,
    )

    # 1. Spaced OTP: '123 456'
    res = await email_service.verify_otp(user_id, "123 456")
    assert res["success"] is True
    assert res["pending"].otp_code == "123456"

    # Re-insert for full-width test
    now = time.monotonic()
    email_service._pending_otps[user_id] = PendingOtp(
        user_id=user_id,
        student_id="24WMD01234",
        email="student@student.tarc.edu.my",
        server_name="Test Guild",
        card_expiry_date="2026-10-31",
        otp_code="654321",
        expires_at=now + 300,
        attempts_left=3,
        created_at=now,
        last_sent_at=now,
    )

    # 2. Full-width IME digits: '６５４３２１'
    res_fw = await email_service.verify_otp(user_id, "６５４３２１")
    assert res_fw["success"] is True
    assert res_fw["pending"].otp_code == "654321"

    # Re-insert for hyphenated + zero-width test
    now = time.monotonic()
    email_service._pending_otps[user_id] = PendingOtp(
        user_id=user_id,
        student_id="24WMD01234",
        email="student@student.tarc.edu.my",
        server_name="Test Guild",
        card_expiry_date="2026-10-31",
        otp_code="987654",
        expires_at=now + 300,
        attempts_left=3,
        created_at=now,
        last_sent_at=now,
    )

    # 3. Hyphenated with zero-width characters: '\u200b987-654\ufeff'
    res_hy = await email_service.verify_otp(user_id, "\u200b987-654\ufeff")
    assert res_hy["success"] is True
    assert res_hy["pending"].otp_code == "987654"


@pytest.mark.asyncio
async def test_resolve_review_thread_fallback(tmp_path):
    """Verify that _resolve_review_thread resolves threads even when interaction.channel is not discord.Thread."""
    db = Database(str(tmp_path / "edge_thread.db"))
    await db.connect()
    bot = MagicMock(spec=discord.Client)
    guest_service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    mock_thread = MagicMock(spec=discord.Thread)
    mock_thread.id = 555666
    guild.get_thread = MagicMock(return_value=mock_thread)

    # Scenario A: interaction.channel is an actual discord.Thread
    inter_thread = MagicMock(spec=discord.Interaction)
    inter_thread.guild = guild
    inter_thread.channel = mock_thread
    resolved_a = await _resolve_review_thread(guest_service, inter_thread, {"channel_id": 555666})
    assert resolved_a is mock_thread

    # Scenario B: interaction.channel is an InteractionChannel mock (not isinstance(..., discord.Thread))
    # but has id=555666
    inter_channel = MagicMock(spec=discord.Interaction)
    inter_channel.guild = guild
    # Create an object that is NOT discord.Thread
    partial_channel = MagicMock()
    partial_channel.id = 555666
    inter_channel.channel = partial_channel

    resolved_b = await _resolve_review_thread(guest_service, inter_channel, {"channel_id": 555666})
    assert resolved_b is mock_thread

    # Scenario C: interaction.channel is None, falls back to ticket['channel_id']
    inter_none = MagicMock(spec=discord.Interaction)
    inter_none.guild = guild
    inter_none.channel = None
    resolved_c = await _resolve_review_thread(guest_service, inter_none, {"channel_id": 555666})
    assert resolved_c is mock_thread


@pytest.mark.asyncio
async def test_approve_guest_application_role_failure_rollback(tmp_path):
    """Verify that if add_roles raises HTTPException, the ticket status rolls back to OPEN."""
    db = Database(str(tmp_path / "edge_rollback.db"))
    await db.connect()
    bot = MagicMock(spec=discord.Client)
    guest_service = GuestService(bot, db)

    guild = MagicMock(spec=discord.Guild)
    guild.id = 999
    guild.name = "Test Server"
    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest"
    guild.roles = [guest_role]

    applicant = MagicMock(spec=discord.Member)
    applicant.id = 1111
    # Simulate role hierarchy conflict (HTTPException 403)
    response_mock = MagicMock()
    response_mock.status = 403
    applicant.add_roles = AsyncMock(
        side_effect=discord.HTTPException(response=response_mock, message="Missing Permissions")
    )
    guild.get_member = MagicMock(return_value=applicant)

    admin = MagicMock(spec=discord.Member)
    admin.id = 8888
    admin.mention = "<@8888>"

    # Create ticket in DB
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=applicant.id,
        channel_id=7777,
        reason="Testing error rollback",
    )
    ticket = await db.get_guest_ticket_by_id(ticket_id)
    assert ticket["status"] == "OPEN"

    # Attempt approval - role assignment should fail and trigger rollback
    success, msg = await guest_service.approve_guest_application(ticket, guild, admin)
    assert success is False
    assert "Failed to assign guest role" in msg

    # Verify ticket was rolled back to OPEN in DB
    updated = await db.get_guest_ticket_by_id(ticket_id)
    assert updated["status"] == "OPEN"


@pytest.mark.asyncio
async def test_verification_modal_immediate_deferral():
    """Verify that VerificationModal and StudentVerificationModal defer immediately on submit."""
    from tarveri.services.verification_service import VerificationService
    from tarveri.views.guest_views import StudentVerificationModal
    from tarveri.views.verification_views import VerificationModal

    mock_verif_service = MagicMock(spec=VerificationService)
    mock_verif_service.perform_verification = AsyncMock(return_value="Verified")
    mock_verif_service.db = None

    # Test VerificationModal
    modal = VerificationModal(service=mock_verif_service)
    modal.student_id._value = "24WMD01234"
    modal.card_expiry._value = "10/26"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 12345
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.id = 999
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)
    # Ensure defer was called immediately with ephemeral=True
    assert interaction.response.defer.called
    interaction.response.defer.assert_called_with(ephemeral=True, thinking=True)

    # Test StudentVerificationModal
    guest_modal = StudentVerificationModal(verification_service=mock_verif_service)
    guest_modal.student_id._value = "24WMD01234"
    guest_modal.card_expiry._value = "10/26"

    inter_guest = MagicMock(spec=discord.Interaction)
    inter_guest.user = MagicMock(spec=discord.Member)
    inter_guest.user.id = 12345
    inter_guest.guild = MagicMock(spec=discord.Guild)
    inter_guest.guild.id = 999
    inter_guest.response = MagicMock()
    inter_guest.response.defer = AsyncMock()
    inter_guest.followup = MagicMock()
    inter_guest.followup.send = AsyncMock()

    await guest_modal.on_submit(inter_guest)
    assert inter_guest.response.defer.called
    inter_guest.response.defer.assert_called_with(ephemeral=True, thinking=True)


@pytest.mark.asyncio
async def test_referral_entry_modal_full_width_normalization():
    """Verify that ReferralEntryModal normalizes full-width and spaced referral codes."""
    from tarveri.views.guest_views import ReferralEntryModal

    mock_guest_service = MagicMock(spec=GuestService)
    mock_guest_service.open_guest_review_ticket = AsyncMock(return_value=(False, "Invalid code", None))

    modal = ReferralEntryModal(guest_service=mock_guest_service)
    modal.code._value = " ＴＡＲ－８Ｘ２Ｋ９Ｐ "

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 12345
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.id = 999
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)
    # Check that open_guest_review_ticket received the normalized uppercase ASCII code
    mock_guest_service.open_guest_review_ticket.assert_called_once()
    _, kwargs = mock_guest_service.open_guest_review_ticket.call_args
    assert kwargs["referral_code"] == "TAR-8X2K9P"


@pytest.mark.asyncio
async def test_approve_btn_already_approved_archives_thread(tmp_path):
    """Verify that if a ticket was already marked APPROVED in DB, approve_btn still closes/archives the thread."""
    from tarveri.views.guest_views import GuestReviewThreadView

    db = Database(str(tmp_path / "edge_approve_btn.db"))
    await db.connect()
    bot = MagicMock(spec=discord.Client)
    guest_service = GuestService(bot, db)
    guest_service.safe_close_and_archive_thread = AsyncMock(return_value=(True, True))

    guild = MagicMock(spec=discord.Guild)
    guild.id = 888
    guild.name = "Test Server"
    thread = MagicMock(spec=discord.Thread)
    thread.id = 4444
    thread.archived = False
    thread.send = AsyncMock()
    guild.get_thread = MagicMock(return_value=thread)

    # Ticket already approved in DB
    ticket_id = await db.create_guest_ticket(
        guild_id=guild.id,
        applicant_id=3333,
        channel_id=thread.id,
        reason="Already approved test",
    )
    await db.close_guest_ticket(ticket_id, status="APPROVED", close_reason="First approval")

    view = GuestReviewThreadView(guest_service=guest_service)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 9999
    interaction.guild = guild
    interaction.channel = thread
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    # Call approve button callback directly
    await view.approve_btn.callback(interaction)

    # safe_close_and_archive_thread should be called to ensure the thread isn't left hanging open
    assert guest_service.safe_close_and_archive_thread.called
