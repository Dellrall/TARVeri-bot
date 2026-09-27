"""
Tests for Student Academic Lifecycle transitions, UI modals, views, and card expiry extensions.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.cogs.verification_cog import (
    ExpiryAnomalyConfirmView,
    ExtendExpiryModal,
    FurtherStudyTransitionModal,
    ReEnterExpiryModal,
    StudentDropoutConfirmModal,
    StudentLifecycleResolutionView,
    VerificationCog,
    VerificationModal,
)
from tarveri.config import (
    format_card_expiry_display,
    hash_student_id,
    is_expiry_date_anomalous,
    parse_card_expiry_date,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.verification_service import VerificationService


def test_date_parsing_and_formatting():
    # MM/YY and MM/YYYY
    assert parse_card_expiry_date("10/26") == "2026-10-31"
    assert parse_card_expiry_date("10/2026") == "2026-10-31"
    assert parse_card_expiry_date("02/24") == "2024-02-29"  # 2024 is leap year
    assert parse_card_expiry_date("02/25") == "2025-02-28"

    # DD/MM/YYYY, DD-MM-YYYY, DD.MM.YYYY
    assert parse_card_expiry_date("06/07/2026") == "2026-07-06"
    assert parse_card_expiry_date("31/10/2026") == "2026-10-31"
    assert parse_card_expiry_date("15-08-2027") == "2027-08-15"
    assert parse_card_expiry_date("01.12.2025") == "2025-12-01"
    assert parse_card_expiry_date("06/07/26") == "2026-07-06"

    # YYYY-MM-DD and YYYY-MM
    assert parse_card_expiry_date("2026-10-15") == "2026-10-15"
    assert parse_card_expiry_date("2026-10") == "2026-10-31"

    # Month Names
    assert parse_card_expiry_date("OCT 2026") == "2026-10-31"
    assert parse_card_expiry_date("15 OCTOBER 2026") == "2026-10-15"
    assert parse_card_expiry_date("15 OCT 26") == "2026-10-15"

    # 2-part historical (e.g. 06/07 parsed as June 2007)
    assert parse_card_expiry_date("06/07") == "2007-06-30"

    # Invalid / None
    assert parse_card_expiry_date("invalid") is None
    assert parse_card_expiry_date("99/99") is None
    assert parse_card_expiry_date(None) is None

    # Display formatting
    assert format_card_expiry_display("2026-10-31") == "10/26"
    assert format_card_expiry_display("2024-02-29") == "02/24"
    assert format_card_expiry_display("2007-06-30") == "06/07"
    assert format_card_expiry_display(None) is None


def test_is_expiry_date_anomalous():
    # 06/07 parsed as 2007-06-30 for an intake in 2024 (17 years before intake)
    anom, reason = is_expiry_date_anomalous("2007-06-30", student_id="24WMD09867")
    assert anom is True
    assert "before your intake year (2024)" in reason

    # Past threshold check relative to dynamic current year
    anom2, reason2 = is_expiry_date_anomalous("2007-06-30")
    assert anom2 is True
    assert "years in the past" in reason2

    # Future threshold (> 8 years)
    anom3, reason3 = is_expiry_date_anomalous("2040-10-31", student_id="24WMD09867")
    assert anom3 is True
    assert "after your intake year (2024)" in reason3

    # Normal valid date for 24WMD09867 (Degree/Diploma intake 2024, expiry 2026 or 2027)
    anom4, reason4 = is_expiry_date_anomalous("2026-10-31", student_id="24WMD09867")
    assert anom4 is False
    assert reason4 is None

    anom5, reason5 = is_expiry_date_anomalous("2026-07-06", student_id="24WMD09867")
    assert anom5 is False
    assert reason5 is None

    # Edge cases
    assert is_expiry_date_anomalous(None) == (False, None)
    assert is_expiry_date_anomalous("invalid-format") == (False, None)


@pytest.mark.asyncio
async def test_extend_expiry_modal(tmp_path):
    db_path = str(tmp_path / "extend_modal.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = VerificationService(bot, db, "secret", RateLimiter())

    user_id = 123456
    id_hash = hash_student_id("23WMD09867", "secret")
    await db.record_verification(user_id, id_hash, "M", campus_code="W", level_code="D", card_expiry_date="2024-10-31")

    modal = ExtendExpiryModal(db, service)
    modal.expiry_date._value = "05/27"
    modal.note._value = "Extended internship semester"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.guild = MagicMock()
    interaction.guild.name = "Test Guild"
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.response.defer.assert_called_once()
    interaction.followup.send.assert_called_once()
    reply_text = interaction.followup.send.call_args[0][0]
    assert "Student Card Validity Extended" in reply_text
    assert "05/27" in reply_text

    # Check database
    details = await db.get_verification_details(user_id)
    assert details["card_expiry_date"] == "2027-05-31"
    assert details["lifecycle_prompt_status"] == "extended"

    await db.close()


@pytest.mark.asyncio
async def test_further_study_transition_modal(tmp_path):
    db_path = str(tmp_path / "further_study_modal.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    bot.guilds = []
    service = VerificationService(bot, db, "secret", RateLimiter())

    user_id = 998877
    old_hash = hash_student_id("22WMD00001", "secret")
    await db.record_verification(user_id, old_hash, "M", campus_code="W", level_code="D")

    modal = FurtherStudyTransitionModal(service)
    modal.student_id._value = "24WMR55555"
    modal.card_expiry._value = "10/28"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.guild = None
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.response.defer.assert_called_once()
    interaction.followup.send.assert_called_once()
    reply_text = interaction.followup.send.call_args[0][0]
    assert "Academic Level Progression Successful" in reply_text

    details = await db.get_verification_details(user_id)
    assert details["level_code"] == "R"
    assert details["card_expiry_date"] == "2028-10-31"

    transitions = await db.get_academic_transitions_for_user(user_id)
    assert len(transitions) == 1
    assert transitions[0]["from_level_code"] == "D"
    assert transitions[0]["to_level_code"] == "R"

    await db.close()


@pytest.mark.asyncio
async def test_student_lifecycle_resolution_view_buttons():
    service = MagicMock(spec=VerificationService)
    db = MagicMock(spec=Database)
    view = StudentLifecycleResolutionView(service, db)

    # Test Graduated Button Callback
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response.send_modal = AsyncMock()
    await view.children[0].callback(interaction)
    interaction.response.send_modal.assert_called_once()

    # Test Further Study Button Callback
    interaction.response.send_modal.reset_mock()
    await view.children[1].callback(interaction)
    interaction.response.send_modal.assert_called_once()

    # Test Extend Button Callback
    interaction.response.send_modal.reset_mock()
    await view.children[2].callback(interaction)
    interaction.response.send_modal.assert_called_once()

    # Test Dropout Button Callback
    interaction.response.send_modal.reset_mock()
    await view.children[3].callback(interaction)
    interaction.response.send_modal.assert_called_once()


@pytest.mark.asyncio
async def test_on_message_expired_student_prompt(tmp_path):
    db_path = str(tmp_path / "on_msg_expired.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = VerificationService(bot, db, "secret", RateLimiter())
    cog = VerificationCog(bot, db, service, RateLimiter())

    user_id = 776655
    id_hash = hash_student_id("21WMR11111", "secret")
    await db.record_verification(user_id, id_hash, "M", campus_code="W", level_code="R", card_expiry_date="2024-05-31")

    guild = MagicMock(spec=discord.Guild)
    guild.id = 111222
    guild.name = "Active Guild"

    channel = MagicMock(spec=discord.TextChannel)
    channel.name = "general"
    channel.guild = guild

    author = MagicMock(spec=discord.Member)
    author.id = user_id
    author.bot = False
    author.mention = f"<@{user_id}>"
    author.send = AsyncMock()

    message = MagicMock(spec=discord.Message)
    message.guild = guild
    message.channel = channel
    message.author = author
    message.content = "Hello everyone!"

    await cog.on_message(message)

    author.send.assert_called_once()
    call_kwargs = author.send.call_args[1]
    assert "embed" in call_kwargs
    assert "view" in call_kwargs
    embed = call_kwargs["embed"]
    assert "TARUMT Student Card Expiry" in embed.title

    # Second message from the same author should be throttled by in-memory cooldown
    author.send.reset_mock()
    await cog.on_message(message)
    author.send.assert_not_called()

    await db.close()


def test_estimate_student_card_expiry():
    from tarveri.config import estimate_student_card_expiry

    # Foundation: 1 yr -> May 31
    assert estimate_student_card_expiry("24WMF12345") == "2025-05-31"
    # Diploma: 2 yrs -> Oct 31
    assert estimate_student_card_expiry("23WMD09867") == "2025-10-31"
    # Degree: 3 yrs -> Oct 31
    assert estimate_student_card_expiry("24WMR12345") == "2027-10-31"
    # Postgrad: 2 yrs -> Oct 31
    assert estimate_student_card_expiry("23WMP00001") == "2025-10-31"
    # Invalid / short
    assert estimate_student_card_expiry("123") is None
    assert estimate_student_card_expiry(None) is None


@pytest.mark.asyncio
async def test_perform_verification_omitted_expiry_auto_estimates(tmp_path):
    db_path = str(tmp_path / "auto_estimate.db")
    db = Database(db_path)
    await db.connect()

    guild = MagicMock(spec=discord.Guild)
    guild.id = 112233
    guild.name = "Test Campus"
    guild.roles = []
    guild.get_member = MagicMock(return_value=MagicMock())

    role_focs = MagicMock(spec=discord.Role)
    role_focs.name = "FOCS"
    role_kl = MagicMock(spec=discord.Role)
    role_kl.name = "KL Main Campus"
    role_deg = MagicMock(spec=discord.Role)
    role_deg.name = "Degree"

    guild.create_role = AsyncMock(side_effect=[role_focs, role_kl, role_deg])

    bot = MagicMock()
    bot.guilds = [guild]

    member = MagicMock(spec=discord.Member)
    member.id = 445566
    member.guild = guild
    member.roles = []
    member.add_roles = AsyncMock()
    guild.get_member.return_value = member

    service = VerificationService(bot, db, "secret", RateLimiter())

    # Verify student WITHOUT specifying raw_expiry_date (omitted / None)
    result = await service.perform_verification(
        user=member,
        raw_student_id="24WMR12345",
        raw_expiry_date=None,
    )
    assert "You've been given the following role(s)" in result

    # Check database: card_expiry_date should be automatically set to 2027-10-31
    details = await db.get_verification_details(member.id)
    assert details["card_expiry_date"] == "2027-10-31"
    assert details["level_code"] == "R"
    assert details["campus_code"] == "W"

    await db.close()


@pytest.mark.asyncio
async def test_database_startup_backfills_legacy_card_expiry(tmp_path):
    import aiosqlite

    db_path = str(tmp_path / "legacy_backfill.db")
    # Manually create legacy table with no card_expiry_date populated
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            """
            CREATE TABLE verifications (
                discord_user_id INTEGER PRIMARY KEY,
                student_id_hash TEXT UNIQUE NOT NULL,
                faculty_code TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                campus_code TEXT,
                level_code TEXT,
                is_alumni INTEGER DEFAULT 0
            );
            """
        )
        await conn.execute(
            """
            INSERT INTO verifications (discord_user_id, student_id_hash, faculty_code, verified_at, campus_code, level_code, is_alumni)
            VALUES (101, 'hash1', 'M', '2023-06-15 10:00:00', 'W', 'D', 0),
                   (102, 'hash2', 'A', '2024-03-01 12:00:00', 'W', 'R', 0),
                   (103, 'hash3', 'M', '2022-01-01 00:00:00', 'W', 'R', 1);
            """
        )
        await conn.commit()

    # Now open with Database class, which runs connect() migrations and backfill
    db = Database(db_path)
    await db.connect()

    # User 101 (Diploma verified in 2023): should be backfilled to 2025-10-31
    d1 = await db.get_verification_details(101)
    assert d1["card_expiry_date"] == "2025-10-31"

    # User 102 (Degree verified in 2024): should be backfilled to 2027-10-31
    d2 = await db.get_verification_details(102)
    assert d2["card_expiry_date"] == "2027-10-31"

    # User 103 (Alumni): should remain None since is_alumni = 1
    d3 = await db.get_verification_details(103)
    assert d3["card_expiry_date"] is None

    await db.close()


@pytest.mark.asyncio
async def test_perform_verification_invalid_expiry_validation():
    bot = MagicMock()
    db = MagicMock(spec=Database)
    service = VerificationService(bot, db, "secret", RateLimiter())

    member = MagicMock(spec=discord.Member)
    member.id = 12345

    # Passing an invalid expiry format string
    res = await service.perform_verification(
        user=member,
        raw_student_id="24WMR12345",
        raw_expiry_date="invalid_date",
    )
    assert "Invalid student card expiry date format" in res
    assert "MM/YY" in res


@pytest.mark.asyncio
async def test_student_verification_modal_in_guest_cog():
    from tarveri.cogs.guest_cog import StudentVerificationModal

    service = MagicMock(spec=VerificationService)
    service.perform_verification = AsyncMock(return_value="✅ Verified!")

    modal = StudentVerificationModal(service)
    modal.student_id._value = "23WMD09867"
    modal.card_expiry._value = "10/26"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.response.defer.assert_called_once()
    service.perform_verification.assert_called_once_with(interaction.user, "23WMD09867", raw_expiry_date="10/26")
    interaction.followup.send.assert_called_once()


@pytest.mark.asyncio
async def test_expired_intake_triggers_alumni_notice(tmp_path):
    db_path = str(tmp_path / "expired_intake.db")
    db = Database(db_path)
    await db.connect()

    guild = MagicMock(spec=discord.Guild)
    guild.id = 778899
    guild.name = "TARUMT Main"
    guild.roles = []
    guild.get_member = MagicMock(return_value=MagicMock())

    role_focs = MagicMock(spec=discord.Role)
    role_focs.name = "FOCS"
    role_kl = MagicMock(spec=discord.Role)
    role_kl.name = "KL Main Campus"
    role_deg = MagicMock(spec=discord.Role)
    role_deg.name = "Degree"
    guild.roles = [role_focs, role_kl, role_deg]
    guild.create_role = AsyncMock(return_value=role_focs)

    bot = MagicMock()
    bot.guilds = [guild]

    member = MagicMock(spec=discord.Member)
    member.id = 112233
    member.guild = guild
    member.roles = []
    member.add_roles = AsyncMock()
    guild.get_member = MagicMock(side_effect=lambda uid: member if uid == 112233 else member2)

    service = VerificationService(bot, db, "secret", RateLimiter())

    # Verify with an older intake ID (e.g. 20WMR12345: Degree intake 2020 -> Expiry 2023-10-31)
    response = await service.perform_verification(
        user=member,
        raw_student_id="20WMR12345",
    )

    assert "You've been given the following role(s)" in response
    assert "Alumni / Academic Status Notice" in response
    assert "/graduate" in response
    assert "10/23" in response

    # Now verify with a future / active student ID (e.g. 25WMR12345: Degree intake 2025 -> Expiry 2028-10-31)
    member2 = MagicMock(spec=discord.Member)
    member2.id = 445566
    member2.guild = guild
    member2.roles = []
    member2.add_roles = AsyncMock()
    guild.get_member.return_value = member2

    response2 = await service.perform_verification(
        user=member2,
        raw_student_id="25WMR12345",
    )
    assert "You've been given the following role(s)" in response2
    assert "Alumni / Academic Status Notice" not in response2

    await db.close()


@pytest.mark.asyncio
async def test_verification_modal_attaches_lifecycle_view_for_expired_intake(tmp_path):
    db_path = str(tmp_path / "modal_lifecycle.db")
    db = Database(db_path)
    await db.connect()

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1111
    guild.name = "TARUMT Main"
    guild.roles = []
    guild.get_member = MagicMock(return_value=MagicMock())

    r1 = MagicMock(spec=discord.Role)
    r1.name = "FOCS"
    r2 = MagicMock(spec=discord.Role)
    r2.name = "KL Main Campus"
    r3 = MagicMock(spec=discord.Role)
    r3.name = "Degree"
    guild.create_role = AsyncMock(side_effect=[r1, r2, r3])

    bot = MagicMock()
    bot.guilds = [guild]

    member = MagicMock(spec=discord.Member)
    member.id = 887766
    member.guild = guild
    member.roles = []
    member.add_roles = AsyncMock()
    guild.get_member.return_value = member

    service = VerificationService(bot, db, "secret", RateLimiter())

    modal = VerificationModal(service)
    modal.student_id._value = "19WMR99999"  # 2019 intake -> Expired in 2022

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = member
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.followup.send.assert_called_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("view") is not None
    assert isinstance(kwargs["view"], StudentLifecycleResolutionView)

    await db.close()


@pytest.mark.asyncio
async def test_verification_modal_anomalous_expiry_triggers_confirm_view(tmp_path):
    db_path = str(tmp_path / "anomaly_test.db")
    db = Database(db_path)
    await db.connect()

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1111
    guild.name = "TARUMT Main"
    guild.roles = []

    bot = MagicMock()
    bot.guilds = [guild]

    member = MagicMock(spec=discord.Member)
    member.id = 123001
    member.guild = guild
    member.roles = []
    guild.get_member.return_value = member

    service = VerificationService(bot, db, "secret", RateLimiter())

    # User enters 24WMD09867 (intake 2024) with '06/07' (which maps to 2007-06-30, 17 years before intake)
    modal = VerificationModal(service)
    modal.student_id._value = "24WMD09867"
    modal.card_expiry._value = "06/07"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = member
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    # Should NOT immediately verify or grant role; should send ExpiryAnomalyConfirmView
    interaction.followup.send.assert_called_once()
    kwargs = interaction.followup.send.call_args[1]
    assert kwargs.get("embed") is not None
    assert "Please Confirm Student Card Expiry Date" in kwargs["embed"].title
    assert "06/07" in kwargs["embed"].description
    assert isinstance(kwargs.get("view"), ExpiryAnomalyConfirmView)

    # 1. Test clicking "Confirm This Date" on ExpiryAnomalyConfirmView
    confirm_view = kwargs["view"]
    r1 = MagicMock(spec=discord.Role)
    r1.name = "FOCS"
    r2 = MagicMock(spec=discord.Role)
    r2.name = "KL Main Campus"
    r3 = MagicMock(spec=discord.Role)
    r3.name = "Diploma"
    guild.create_role = AsyncMock(side_effect=[r1, r2, r3])
    member.add_roles = AsyncMock()

    confirm_interaction = MagicMock(spec=discord.Interaction)
    confirm_interaction.user = member
    confirm_interaction.response.defer = AsyncMock()
    confirm_interaction.followup.send = AsyncMock()

    # Children 0 is Confirm This Date
    await confirm_view.children[0].callback(confirm_interaction)

    confirm_interaction.followup.send.assert_called_once()
    confirm_kwargs = confirm_interaction.followup.send.call_args[1]
    # Because 2007 is in the past, it should attach the lifecycle resolution view
    assert "Recorded as `06/07`" in confirm_interaction.followup.send.call_args[0][0]
    assert isinstance(confirm_kwargs.get("view"), StudentLifecycleResolutionView)

    # Check database stored 2007-06-30
    details = await db.get_verification_details(member.id)
    assert details["card_expiry_date"] == "2007-06-30"

    await db.close()


@pytest.mark.asyncio
async def test_expiry_anomaly_auto_calculate_and_reenter_flow(tmp_path):
    db_path = str(tmp_path / "auto_calc_test.db")
    db = Database(db_path)
    await db.connect()

    guild = MagicMock(spec=discord.Guild)
    guild.id = 2222
    guild.name = "TARUMT Main"
    guild.roles = []

    r1 = MagicMock(spec=discord.Role)
    r1.name = "FOCS"
    r2 = MagicMock(spec=discord.Role)
    r2.name = "KL Main Campus"
    r3 = MagicMock(spec=discord.Role)
    r3.name = "Diploma"
    guild.create_role = AsyncMock(side_effect=[r1, r2, r3, r1, r2, r3])

    bot = MagicMock()
    bot.guilds = [guild]

    member1 = MagicMock(spec=discord.Member)
    member1.id = 123002
    member1.guild = guild
    member1.roles = []
    member1.add_roles = AsyncMock()

    member2 = MagicMock(spec=discord.Member)
    member2.id = 123003
    member2.guild = guild
    member2.roles = []
    member2.add_roles = AsyncMock()

    guild.get_member = MagicMock(side_effect=lambda uid: member1 if uid == member1.id else member2)

    service = VerificationService(bot, db, "secret", RateLimiter())

    # User 1 chooses "Auto-Calculate for Me" (children[2])
    view1 = ExpiryAnomalyConfirmView(
        service=service,
        db=db,
        student_id="24WMD09867",
        raw_expiry_input="06/07",
        parsed_iso_date="2007-06-30",
        anomaly_reason="Year is in past",
    )
    interaction1 = MagicMock(spec=discord.Interaction)
    interaction1.user = member1
    interaction1.response.defer = AsyncMock()
    interaction1.followup.send = AsyncMock()

    await view1.children[2].callback(interaction1)
    details1 = await db.get_verification_details(member1.id)
    # Diploma (2 years) from 2024 -> 2026-10-31
    assert details1["card_expiry_date"] == "2026-10-31"

    # User 2 clicks "Re-enter Expiry Date" (children[1]) -> opens ReEnterExpiryModal
    view2 = ExpiryAnomalyConfirmView(
        service=service,
        db=db,
        student_id="24WMD09868",
        raw_expiry_input="06/07",
        parsed_iso_date="2007-06-30",
        anomaly_reason="Year is in past",
    )
    interaction2 = MagicMock(spec=discord.Interaction)
    interaction2.response.send_modal = AsyncMock()
    await view2.children[1].callback(interaction2)
    interaction2.response.send_modal.assert_called_once()
    modal_opened = interaction2.response.send_modal.call_args[0][0]
    assert isinstance(modal_opened, ReEnterExpiryModal)

    # User 2 submits ReEnterExpiryModal with corrected DD/MM/YYYY date "06/07/2026"
    modal_opened.card_expiry._value = "06/07/2026"
    submit_interaction = MagicMock(spec=discord.Interaction)
    submit_interaction.user = member2
    submit_interaction.response.defer = AsyncMock()
    submit_interaction.followup.send = AsyncMock()

    await modal_opened.on_submit(submit_interaction)
    details2 = await db.get_verification_details(member2.id)
    assert details2["card_expiry_date"] == "2026-07-06"

    await db.close()


@pytest.mark.asyncio
async def test_verify_slash_command_with_anomalous_expiry(tmp_path):
    db_path = str(tmp_path / "slash_anomaly.db")
    db = Database(db_path)
    await db.connect()

    guild = MagicMock(spec=discord.Guild)
    guild.id = 3333
    guild.name = "TARUMT Main"
    guild.roles = []

    bot = MagicMock()
    bot.guilds = [guild]
    service = VerificationService(bot, db, "secret", RateLimiter())
    cog = VerificationCog(bot, db, service, RateLimiter())

    member = MagicMock(spec=discord.Member)
    member.id = 123004
    member.guild = guild
    member.roles = []
    guild.get_member.return_value = member

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = member
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Call /verify with student_id and anomalous expiry_date "06/07"
    await cog.verify_slash.callback(
        cog,
        interaction=interaction,
        student_id="24WMD11111",
        expiry_date="06/07",
    )

    interaction.followup.send.assert_called_once()
    kwargs = interaction.followup.send.call_args[1]
    assert isinstance(kwargs.get("view"), ExpiryAnomalyConfirmView)

    await db.close()


@pytest.mark.asyncio
async def test_guest_cog_student_verification_modal_anomalous_expiry(tmp_path):
    db_path = str(tmp_path / "guest_anomaly.db")
    db = Database(db_path)
    await db.connect()

    from tarveri.cogs.guest_cog import StudentVerificationModal

    guild = MagicMock(spec=discord.Guild)
    guild.id = 4444
    guild.name = "TARUMT Main"
    guild.roles = []

    bot = MagicMock()
    bot.guilds = [guild]
    service = VerificationService(bot, db, "secret", RateLimiter())

    member = MagicMock(spec=discord.Member)
    member.id = 123005
    member.guild = guild
    member.roles = []
    guild.get_member.return_value = member

    modal = StudentVerificationModal(service)
    modal.student_id._value = "24WMD22222"
    modal.card_expiry._value = "06/07"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = member
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.followup.send.assert_called_once()
    kwargs = interaction.followup.send.call_args[1]
    assert isinstance(kwargs.get("view"), ExpiryAnomalyConfirmView)

    await db.close()


@pytest.mark.asyncio
async def test_student_dropout_confirm_modal_validation(tmp_path):
    db_path = str(tmp_path / "dropout_validation.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = VerificationService(bot, db, "secret", RateLimiter())

    user_id = 998800
    await db.record_verification(user_id, "hash_drop", "M", campus_code="W", level_code="R")

    modal = StudentDropoutConfirmModal(service, db)

    # 1. Invalid confirmation phrase should be rejected
    modal.confirmation._value = "I am quitting"
    interaction = MagicMock(spec=discord.Interaction)
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    interaction.user = user
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    interaction.followup.send.assert_called_once()
    msg = interaction.followup.send.call_args[0][0]
    assert "Confirmation phrase did not match" in msg
    # DB record should still exist
    assert await db.get_verification_by_user(user_id) is not None

    # 2. Valid confirmation phrase "Yes, I am dropping out." succeeds
    interaction.followup.send.reset_mock()
    modal.confirmation._value = "Yes, I am dropping out."
    modal.reason._value = "Transferred to overseas uni"

    await modal.on_submit(interaction)

    interaction.followup.send.assert_called_once()
    success_msg = interaction.followup.send.call_args[0][0]
    assert "Student verification updated to Guest" in success_msg
    # DB record should be deleted
    assert await db.get_verification_by_user(user_id) is None

    await db.close()


@pytest.mark.asyncio
async def test_process_student_dropout_service_lifecycle(tmp_path):
    db_path = str(tmp_path / "dropout_service.db")
    db = Database(db_path)
    await db.connect()

    guild = MagicMock(spec=discord.Guild)
    guild.id = 1234
    guild.name = "TARUMT Main Campus"

    fac_role = MagicMock(spec=discord.Role)
    fac_role.name = "FOCS"
    fac_role.position = 5
    camp_role = MagicMock(spec=discord.Role)
    camp_role.name = "KL Main Campus"
    camp_role.position = 4
    lvl_role = MagicMock(spec=discord.Role)
    lvl_role.name = "Degree"
    lvl_role.position = 3

    bot = MagicMock()
    bot.guilds = [guild]
    rate_limiter = RateLimiter()
    service = VerificationService(bot, db, "secret", rate_limiter)

    user_id = 554433
    await db.record_verification(user_id, "hash_drop_2", "M", campus_code="W", level_code="R")

    guest_role = MagicMock(spec=discord.Role)
    guest_role.name = "Guest(Approved)"
    guest_role.position = 2
    guild.create_role = AsyncMock(return_value=guest_role)

    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.roles = [fac_role, camp_role, lvl_role]
    member.remove_roles = AsyncMock()
    member.add_roles = AsyncMock()
    guild.get_member.return_value = member

    me = MagicMock()
    me.guild_permissions.manage_roles = True
    me.top_role.position = 10
    guild.me = me

    # Process dropout
    result = await service.process_student_dropout(member, reason="Gap year")
    assert result["success"] is True
    assert result["faculty_name"] == "FOCS"
    assert result["campus_name"] == "KL Main Campus"
    assert result["level_name"] == "Degree"

    # Verify student roles removal called
    assert member.remove_roles.call_count >= 1

    # Verify Guest(Approved) role was added
    assert member.add_roles.call_count >= 1
    assert member.add_roles.call_args[0][0] == guest_role

    # Verify DB record deleted
    assert await db.get_verification_by_user(user_id) is None

    # Test unverfied user calling dropout returns error
    err_res = await service.process_student_dropout(member)
    assert err_res["success"] is False

    await db.close()


@pytest.mark.asyncio
async def test_dropout_slash_command(tmp_path):
    db_path = str(tmp_path / "dropout_slash.db")
    db = Database(db_path)
    await db.connect()

    bot = MagicMock()
    service = VerificationService(bot, db, "secret", RateLimiter())
    cog = VerificationCog(bot, db, service, RateLimiter())

    interaction = MagicMock(spec=discord.Interaction)
    user = MagicMock()
    user.id = 887766
    interaction.user = user
    interaction.response.send_message = AsyncMock()
    interaction.response.send_modal = AsyncMock()

    # 1. Unverified user running /dropout
    await cog.dropout_slash.callback(cog, interaction)
    interaction.response.send_message.assert_called_once()
    assert "You do not have an active student verification" in interaction.response.send_message.call_args[0][0]

    # 2. Verified user running /dropout opens modal
    await db.record_verification(user.id, "hash_drop_3", "M")
    interaction.response.send_message.reset_mock()
    interaction.response.send_modal.reset_mock()

    await cog.dropout_slash.callback(cog, interaction)
    interaction.response.send_modal.assert_called_once()
    modal_arg = interaction.response.send_modal.call_args[0][0]
    assert isinstance(modal_arg, StudentDropoutConfirmModal)

    await db.close()
