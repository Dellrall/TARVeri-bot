"""
Unit and integration tests for EmailService, AES-256 email encryption, OTP lifecycle, and database persistence.
"""

from __future__ import annotations

import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest
from cryptography.fernet import Fernet

from tarveri.cogs.verification_cog import (
    AlumniEmailConfirmationView,
    StudentOtpModal,
    VerificationModal,
)
from tarveri.config import (
    Settings,
    decrypt_email,
    encrypt_email,
    hash_email,
    hash_student_id,
    is_valid_student_email,
    mask_email,
)
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.email_service import EmailService
from tarveri.services.verification_service import VerificationService


def test_email_crypto_helpers():
    key = Fernet.generate_key().decode()
    email = "23WMD09867@student.tarc.edu.my"

    encrypted = encrypt_email(email, key)
    assert encrypted != email
    decrypted = decrypt_email(encrypted, key)
    assert decrypted == email.lower()

    # Masking test
    masked = mask_email(email)
    assert masked.endswith("@student.tarc.edu.my")
    assert "***" in masked

    # Blind index hash
    secret = "test-secret-pepper"
    h1 = hash_email(email, secret)
    h2 = hash_email("  23WMD09867@STUDENT.TARC.EDU.MY  ", secret)
    assert h1 == h2

    # Validation
    assert is_valid_student_email("23wmd09867@student.tarc.edu.my") is True
    assert is_valid_student_email("staff@tarc.edu.my") is True
    assert is_valid_student_email("imposter@gmail.com") is False
    assert is_valid_student_email("invalid-email") is False


@pytest.mark.asyncio
async def test_email_service_otp_lifecycle():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        email_otp_ttl_seconds=300,
        email_otp_max_attempts=3,
        email_otp_resend_cooldown_seconds=5,
    )
    svc = EmailService(settings, mock_smtp=True)

    user_id = 998877
    student_id = "24WMD01234"
    email = "24wmd01234@student.tarc.edu.my"

    # 1. Generate & send OTP
    res = await svc.generate_and_send_otp(user_id, student_id, email, server_name="TARUMT Main")
    assert res["success"] is True
    assert len(svc.sent_emails) == 1
    sent_otp = svc.sent_emails[0]["otp"]
    assert len(sent_otp) == 6

    # 2. Resend cooldown test
    res_cooldown = await svc.generate_and_send_otp(user_id, student_id, email, server_name="TARUMT Main")
    assert res_cooldown["success"] is False
    assert "Please wait" in res_cooldown["error"]

    # 3. Invalid OTP attempt
    bad_res = await svc.verify_otp(user_id, "000000")
    assert bad_res["success"] is False
    assert "Incorrect verification code" in bad_res["error"]

    # 4. Valid OTP attempt
    good_res = await svc.verify_otp(user_id, sent_otp)
    assert good_res["success"] is True
    assert good_res["pending"].student_id == student_id
    assert good_res["pending"].email == email

    # 5. Verify cannot reuse used OTP
    reuse_res = await svc.verify_otp(user_id, sent_otp)
    assert reuse_res["success"] is False


@pytest.mark.asyncio
async def test_email_service_otp_max_attempts():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        email_otp_max_attempts=2,
    )
    svc = EmailService(settings, mock_smtp=True)
    user_id = 112233

    await svc.generate_and_send_otp(user_id, "24WMD11111", "24wmd11111@student.tarc.edu.my", server_name="Server")

    # Attempt 1 failed
    res1 = await svc.verify_otp(user_id, "999999")
    assert res1["success"] is False
    assert "1 attempt remaining" in res1["error"]

    # Attempt 2 failed (should invalidate)
    res2 = await svc.verify_otp(user_id, "999999")
    assert res2["success"] is False
    assert "invalidated" in res2["error"].lower()

    # Attempt 3: No active code
    res3 = await svc.verify_otp(user_id, "999999")
    assert res3["success"] is False
    assert "No pending verification code" in res3["error"]


@pytest.mark.asyncio
async def test_database_email_storage_and_duplicate_detection(tmp_path):
    db_path = str(tmp_path / "email_test.db")
    db = Database(db_path)
    await db.connect()

    key = Fernet.generate_key().decode()
    secret = "secret-pepper"
    email = "24wmd00001@student.tarc.edu.my"
    encrypted = encrypt_email(email, key)
    email_hash = hash_email(email, secret)

    await db.record_verification(
        discord_user_id=1001,
        student_id_hash="hash-001",
        faculty_code="FOCS",
        campus_code="W",
        level_code="R",
        card_expiry_date="2026-10-31",
        student_email_encrypted=encrypted,
        student_email_hash=email_hash,
    )

    details = await db.get_verification_details(1001)
    assert details is not None
    assert details["student_email_encrypted"] == encrypted
    assert details["student_email_hash"] == email_hash
    assert decrypt_email(details["student_email_encrypted"], key) == email

    # Lookup by email hash
    match = await db.get_verification_by_email_hash(email_hash)
    assert match is not None
    assert match[0] == 1001

    await db.close()


@pytest.mark.asyncio
async def test_verification_service_with_email_duplicate_guard(tmp_path):
    db_path = str(tmp_path / "veri_email.db")
    db = Database(db_path)
    await db.connect()

    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
    )
    email_svc = EmailService(settings, mock_smtp=True)
    rate_limiter = RateLimiter()
    bot = MagicMock()
    service = VerificationService(
        bot=bot,
        db=db,
        secret="fake_secret",
        rate_limiter=rate_limiter,
        settings=settings,
        email_service=email_svc,
    )

    user1 = MagicMock(spec=discord.Member)
    user1.id = 5001
    user1.name = "Student1"
    user1.mention = "<@5001>"
    user1.roles = []
    guild1 = MagicMock(spec=discord.Guild)
    guild1.id = 100
    guild1.name = "TARUMT Hub"
    guild1.roles = []
    user1.guild = guild1

    # First user verifies with email
    with patch.object(service, "get_mutual_guilds_for_user", AsyncMock(return_value=[guild1])):
        with patch.object(service, "assign_role_across_guilds") as mock_assign:
            mock_assign.return_value = MagicMock(verified_in=[(100, "TARUMT Hub", "FOCS")], already_had_role_in=[], missing_role_in=[], failed_in=[])
            res1 = await service.perform_verification(
                user1,
                "23WMD01111",
                raw_email="23wmd01111@student.tarc.edu.my",
            )
            assert "FOCS" in res1

    # Second user tries to verify with the same student email
    user2 = MagicMock(spec=discord.Member)
    user2.id = 5002
    user2.name = "Student2"
    user2.mention = "<@5002>"
    user2.roles = []
    user2.guild = guild1

    res2 = await service.perform_verification(
        user2,
        "23WMD02222",
        raw_email="23wmd01111@student.tarc.edu.my",
    )
    assert "already been used" in res2

    await db.close()


@pytest.mark.asyncio
async def test_student_otp_modal_submission(tmp_path):
    db_path = str(tmp_path / "modal_email.db")
    db = Database(db_path)
    await db.connect()

    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
    )
    email_svc = EmailService(settings, mock_smtp=True)
    rate_limiter = RateLimiter()
    bot = MagicMock()
    service = VerificationService(
        bot=bot,
        db=db,
        secret="fake_secret",
        rate_limiter=rate_limiter,
        settings=settings,
        email_service=email_svc,
    )

    user_id = 7001
    user = MagicMock(spec=discord.Member)
    user.id = user_id
    user.mention = "<@7001>"
    user.roles = []
    guild = MagicMock(spec=discord.Guild)
    guild.id = 200
    guild.name = "TARUMT Campus"
    guild.roles = []
    user.guild = guild

    # Generate OTP
    res_otp = await email_svc.generate_and_send_otp(user_id, "24WMD08888", "24wmd08888@student.tarc.edu.my", server_name="TARUMT Campus")
    assert res_otp["success"] is True
    otp = email_svc.sent_emails[0]["otp"]

    modal = StudentOtpModal(service=service, email_service=email_svc)
    modal.otp_code._value = otp

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user
    interaction.guild = guild
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    with patch.object(service, "get_mutual_guilds_for_user", AsyncMock(return_value=[guild])):
        with patch.object(service, "assign_role_across_guilds") as mock_assign:
            mock_assign.return_value = MagicMock(verified_in=[(200, "TARUMT Campus", "FOCS")], already_had_role_in=[], missing_role_in=[], failed_in=[])
            await modal.on_submit(interaction)

    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "Institutional Email & Student Verified" in embed.title

    # Verify DB record has encrypted email
    details = await db.get_verification_details(user_id)
    assert details is not None
    assert decrypt_email(details["student_email_encrypted"], key) == "24wmd08888@student.tarc.edu.my"

    await db.close()


@pytest.mark.asyncio
async def test_email_service_smtp_fallback_on_primary_failure():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        smtp_host="mail.smtp2go.com",
        smtp_port=587,
        smtp_user="smtp2go_user",
        smtp_password="smtp2go_password",
        smtp_fallback_host="mail.direct-domain.com",
        smtp_fallback_port=587,
        smtp_fallback_user="verify@direct-domain.com",
        smtp_fallback_password="mailbox_password",
    )
    svc = EmailService(settings, mock_smtp=False)

    async def mock_endpoint(to_email, otp_code, server_name, ttl_minutes, host, port, user, password, from_email, from_name, use_tls, relay_label="SMTP"):
        if host == "mail.smtp2go.com":
            # Simulate SMTP2GO limit / quota exhausted error
            return False, "550 5.7.1 Daily message sending limit exceeded on SMTP2GO relay"
        if host == "mail.direct-domain.com":
            # Fallback direct SMTP succeeds
            return True, None
        return False, "Unknown host"

    with patch.object(svc, "_send_to_smtp_endpoint", side_effect=mock_endpoint) as mock_send:
        success = await svc._send_smtp_async(
            to_email="24wmd12345@student.tarc.edu.my",
            otp_code="123456",
            server_name="Test Server",
            ttl_minutes=10,
        )
        assert success is True
        assert mock_send.call_count == 2
        # First call was primary
        assert mock_send.call_args_list[0].kwargs["host"] == "mail.smtp2go.com"
        # Second call was fallback
        assert mock_send.call_args_list[1].kwargs["host"] == "mail.direct-domain.com"


@pytest.mark.asyncio
async def test_email_service_circuit_breaker_trips_to_instant_fallback():
    """Verifies circuit breaker trips after fail_max errors and immediately bypasses primary relay."""
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        smtp_host="mail.smtp2go.com",
        smtp_fallback_host="mail.direct-domain.com",
        circuit_breaker_fail_max=2,
        circuit_breaker_reset_timeout=60,
    )
    svc = EmailService(settings, mock_smtp=False)

    async def mock_endpoint(to_email, otp_code, server_name, ttl_minutes, host, port, user, password, from_email, from_name, use_tls, relay_label="SMTP"):
        if host == "mail.smtp2go.com":
            return False, "500 Server Error"
        if host == "mail.direct-domain.com":
            return True, None
        return False, "Unknown host"

    with patch.object(svc, "_send_to_smtp_endpoint", side_effect=mock_endpoint) as mock_send:
        # Request 1: Primary fails -> Fallback succeeds (1 failure recorded)
        r1 = await svc._send_smtp_async("u1@student.tarc.edu.my", "111111", "Server", 10)
        assert r1 is True
        assert svc._primary_breaker.current_state == "closed"
        assert svc._primary_breaker.fail_count == 1

        # Request 2: Primary fails -> Fallback succeeds (2 failures -> Breaker trips OPEN!)
        r2 = await svc._send_smtp_async("u2@student.tarc.edu.my", "222222", "Server", 10)
        assert r2 is True
        assert svc._primary_breaker.current_state == "open"

        # Request 3: Circuit breaker is OPEN -> Primary is COMPLETELY BYPASSED with 0 network calls
        mock_send.reset_mock()
        r3 = await svc._send_smtp_async("u3@student.tarc.edu.my", "333333", "Server", 10)
        assert r3 is True
        assert mock_send.call_count == 1  # Only called fallback!
        assert mock_send.call_args_list[0].kwargs["host"] == "mail.direct-domain.com"


@pytest.mark.asyncio
async def test_email_service_smtp_both_fail():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        smtp_host="mail.smtp2go.com",
        smtp_fallback_host="mail.direct-domain.com",
    )
    svc = EmailService(settings, mock_smtp=False)

    with patch.object(svc, "_send_to_smtp_endpoint", AsyncMock(return_value=(False, "Connection timeout"))):
        success = await svc._send_smtp_async(
            to_email="24wmd12345@student.tarc.edu.my",
            otp_code="123456",
            server_name="Test Server",
            ttl_minutes=10,
        )
        assert success is False


@pytest.mark.asyncio
async def test_email_service_primary_fails_without_fallback():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        smtp_host="mail.smtp2go.com",
        smtp_fallback_host="",  # No fallback configured
    )
    svc = EmailService(settings, mock_smtp=False)

    with patch.object(svc, "_send_to_smtp_endpoint", AsyncMock(return_value=(False, "550 Limit Exceeded"))):
        success = await svc._send_smtp_async(
            to_email="24wmd12345@student.tarc.edu.my",
            otp_code="123456",
            server_name="Test Server",
            ttl_minutes=10,
        )
        assert success is False


@pytest.mark.asyncio
async def test_verification_modal_guild_opt_in_and_opt_out(tmp_path):
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
    )
    db = Database(str(tmp_path / "guild_opt_in.db"))
    await db.connect()

    bot = MagicMock()
    service = VerificationService(bot, db, "secret", RateLimiter(), email_service=EmailService(settings, mock_smtp=True))
    email_service = service.email_service

    guild_id = 999888
    guild = MagicMock(spec=discord.Guild)
    guild.id = guild_id
    guild.name = "Opt In Guild"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.id = 777111
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    # Case 1: Guild is OPTED-OUT (default) -> email is optional, omitting email succeeds directly
    modal_opt_out = VerificationModal(service, email_service, require_email=False)
    assert modal_opt_out.student_email.required is False
    modal_opt_out.student_id._value = "24WMD05555"
    modal_opt_out.student_email._value = ""

    with patch.object(service, "perform_verification", return_value="✅ Verified!") as mock_verify:
        await modal_opt_out.on_submit(interaction)
        interaction.followup.send.assert_called_once_with("✅ Verified!", ephemeral=True)
        mock_verify.assert_called_once_with(interaction.user, "24WMD05555", raw_expiry_date=None)

    # Case 1b: Guild is OPTED-OUT, user provides email -> verifies directly without OTP, storing email encrypted
    modal_opt_out.student_email._value = "24wmd05555@student.tarc.edu.my"
    interaction.followup.send.reset_mock()
    with patch.object(service, "perform_verification", return_value="✅ Verified with stored email!") as mock_verify:
        await modal_opt_out.on_submit(interaction)
        interaction.followup.send.assert_called_once_with("✅ Verified with stored email!", ephemeral=True)
        mock_verify.assert_called_once_with(
            interaction.user, "24WMD05555", raw_expiry_date=None, raw_email="24wmd05555@student.tarc.edu.my"
        )
        assert email_service.get_pending_otp(interaction.user.id) is None  # No OTP generated/sent

    # Case 2: Guild OPTS-IN -> require_email = True, omitting email is blocked
    await db.set_guild_email_verification(guild_id, True)
    assert await db.is_guild_email_verification_enabled(guild_id) is True

    modal_opt_in = VerificationModal(service, email_service, require_email=True)
    assert modal_opt_in.student_email.required is True
    modal_opt_in.student_id._value = "24WMD05555"
    modal_opt_in.student_email._value = ""

    interaction.followup.send.reset_mock()
    await modal_opt_in.on_submit(interaction)
    interaction.followup.send.assert_called_once()
    assert "Institutional student email is required" in interaction.followup.send.call_args[0][0]

    # Case 3: Guild is OPTED-IN and valid email is provided -> OTP dispatched
    modal_opt_in.student_email._value = "24wmd05555@student.tarc.edu.my"
    interaction.followup.send.reset_mock()
    await modal_opt_in.on_submit(interaction)
    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert "Verification Code Sent!" in embed.title
    assert "<t:" in embed.description
    assert ":R>" in embed.description

    # Case 4: Feature flag email_restrict_smtp_usage = False (unrestricted mode)
    # Even if guild is OPTED-OUT, entering an email triggers OTP dispatch
    await db.set_guild_email_verification(guild_id, False)
    assert await db.is_guild_email_verification_enabled(guild_id) is False

    settings_unrestricted = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        email_restrict_smtp_usage=False,
    )
    email_service_unrestricted = EmailService(settings_unrestricted, mock_smtp=True)
    modal_unrestricted = VerificationModal(service, email_service_unrestricted, require_email=False)
    modal_unrestricted.student_id._value = "24WMD05555"
    modal_unrestricted.student_email._value = "24wmd05555@student.tarc.edu.my"

    interaction.followup.send.reset_mock()
    await modal_unrestricted.on_submit(interaction)
    interaction.followup.send.assert_called_once()
    embed_unrestricted = interaction.followup.send.call_args[1]["embed"]
    assert "Verification Code Sent!" in embed_unrestricted.title

    await db.close()


@pytest.mark.asyncio
async def test_otp_slash_command_flow(tmp_path):
    from tarveri.cogs.verification_cog import VerificationCog

    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
    )
    db = Database(str(tmp_path / "otp_slash.db"))
    await db.connect()

    bot = MagicMock()
    email_svc = EmailService(settings, mock_smtp=True)
    service = VerificationService(bot, db, "secret", RateLimiter(), email_service=email_svc)
    cog = VerificationCog(bot, db, service, RateLimiter(), email_service=email_svc)

    user_id = 998811
    guild = MagicMock(spec=discord.Guild)
    guild.id = 12345
    guild.name = "Test Guild"

    member = MagicMock(spec=discord.Member)
    member.id = user_id
    member.mention = f"<@{user_id}>"

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = guild
    interaction.user = member
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send = AsyncMock()

    # 1. Running /otp without any pending OTP on an opt-out server
    await cog.otp_slash.callback(cog, interaction, code="123456")
    interaction.response.send_message.assert_called_once()
    assert "This server has not mandated email verification" in interaction.response.send_message.call_args[0][0]

    # 2. Generate OTP for user
    await email_svc.generate_and_send_otp(user_id, "24WMD07777", "24wmd07777@student.tarc.edu.my", server_name="Test Guild")
    otp_code = email_svc.sent_emails[0]["otp"]

    # 3. Invalid OTP submission
    interaction.response.send_message.reset_mock()
    interaction.followup.send.reset_mock()
    await cog.otp_slash.callback(cog, interaction, code="000000")
    interaction.followup.send.assert_called_once()
    assert "Incorrect verification code" in interaction.followup.send.call_args[0][0]

    # 4. Valid OTP submission
    with patch.object(service, "perform_verification", return_value="✅ Verified successfully"):
        interaction.followup.send.reset_mock()
        await cog.otp_slash.callback(cog, interaction, code=otp_code)
        interaction.followup.send.assert_called_once()
        embed = interaction.followup.send.call_args[1]["embed"]
        assert "Institutional Email & Student Verified!" in embed.title

    await db.close()


@pytest.mark.asyncio
async def test_email_service_edge_cases():
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        email_otp_resend_cooldown_seconds=60,
    )
    svc = EmailService(settings, mock_smtp=True)

    # 1. Hyphenated and abbreviated TARUMT email formats
    assert is_valid_student_email("yaplz-wm23@student.tarc.edu.my") is True
    assert is_valid_student_email("tan.kh-pk24@student.tarc.edu.my") is True
    assert is_valid_student_email("lee_ck-jh22@student.tarc.edu.my") is True
    assert is_valid_student_email("admin@tarc.edu.my") is True
    assert is_valid_student_email("invalid@gmail.com") is False

    user_id = 445566
    # 2. Generate initial OTP
    res1 = await svc.generate_and_send_otp(
        user_id, "23WMD01111", "yaplz-wm23@student.tarc.edu.my", server_name="TARUMT KL"
    )
    assert res1["success"] is True
    assert len(svc.sent_emails) == 1

    # 3. Resending to the EXACT SAME email within cooldown -> blocked by cooldown
    res_same = await svc.generate_and_send_otp(
        user_id, "23WMD01111", "yaplz-wm23@student.tarc.edu.my", server_name="TARUMT KL"
    )
    assert res_same["success"] is False
    assert "Please wait" in res_same["error"]

    # 4. Correcting email address (e.g. intake typo 23 -> 24) -> bypasses cooldown immediately!
    res_corrected = await svc.generate_and_send_otp(
        user_id, "23WMD01111", "yaplz-wm24@student.tarc.edu.my", server_name="TARUMT KL"
    )
    assert res_corrected["success"] is True
    assert len(svc.sent_emails) == 2
    assert svc.sent_emails[1]["to"] == "yaplz-wm24@student.tarc.edu.my"

    # 5. Lazy expired OTP pruning
    # Manually expire the pending OTP
    pending = svc.get_pending_otp(user_id)
    assert pending is not None
    pending.expires_at = time.monotonic() - 10

    # Calling get_pending_otp automatically prunes the expired entry
    assert svc.get_pending_otp(user_id) is None
    assert user_id not in svc._pending_otps


@pytest.mark.asyncio
async def test_validate_preflight_for_otp(tmp_path):
    db_path = str(tmp_path / "preflight_test.db")
    db = Database(db_path)
    await db.connect()

    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret_12345",
        enable_email_verification=True,
        email_encryption_key=key,
    )
    email_svc = EmailService(settings, mock_smtp=True)
    rate_limiter = RateLimiter(max_attempts=3, window_seconds=60)
    bot = MagicMock()
    service = VerificationService(
        bot=bot,
        db=db,
        secret="fake_secret_12345",
        rate_limiter=rate_limiter,
        settings=settings,
        email_service=email_svc,
    )

    # 1. Valid inputs pass preflight
    ok, err = await service.validate_preflight_for_otp(
        user_id=1001,
        raw_student_id="24WMD01234",
        raw_email="yaplz-wm24@student.tarc.edu.my",
    )
    assert ok is True
    assert err is None

    # 2. Invalid student ID format fails preflight
    ok, err = await service.validate_preflight_for_otp(
        user_id=1001,
        raw_student_id="invalid_id",
        raw_email="yaplz-wm24@student.tarc.edu.my",
    )
    assert ok is False
    assert "Invalid student ID format" in err

    # 3. Unknown faculty code fails preflight
    ok, err = await service.validate_preflight_for_otp(
        user_id=1001,
        raw_student_id="24WXD01234",
        raw_email="yaplz-wm24@student.tarc.edu.my",
    )
    assert ok is False
    assert "does not match any known faculty" in err

    # 4. Invalid email domain fails preflight
    ok, err = await service.validate_preflight_for_otp(
        user_id=1001,
        raw_student_id="24WMD01234",
        raw_email="student@gmail.com",
    )
    assert ok is False
    assert "Invalid student email address" in err

    # 5. Seed existing verified user (ID: 9999, student_id: 24WMD09999, email: existing-wm24@student.tarc.edu.my)
    id_hash = hash_student_id("24WMD09999", "fake_secret_12345")
    email_hash = hash_email("existing-wm24@student.tarc.edu.my", "fake_secret_12345")
    await db.record_verification(
        discord_user_id=9999,
        student_id_hash=id_hash,
        faculty_code="M",
        student_email_hash=email_hash,
    )

    # 6. Duplicate student ID attempt from a different user fails preflight
    ok, err = await service.validate_preflight_for_otp(
        user_id=1002,
        raw_student_id="24WMD09999",
        raw_email="new-wm24@student.tarc.edu.my",
    )
    assert ok is False
    assert "student ID has already been used" in err

    # 7. Duplicate email attempt from a different user fails preflight
    ok, err = await service.validate_preflight_for_otp(
        user_id=1002,
        raw_student_id="24WMD05555",
        raw_email="existing-wm24@student.tarc.edu.my",
    )
    assert ok is False
    assert "student email has already been used" in err

    # 8. Same user re-verifying own ID/email passes preflight
    ok, err = await service.validate_preflight_for_otp(
        user_id=9999,
        raw_student_id="24WMD09999",
        raw_email="existing-wm24@student.tarc.edu.my",
    )
    assert ok is True
    assert err is None

    # 9. Rate limiting fails preflight
    rate_limiter.record_attempt(1003)
    rate_limiter.record_attempt(1003)
    rate_limiter.record_attempt(1003)
    ok, err = await service.validate_preflight_for_otp(
        user_id=1003,
        raw_student_id="24WMD01111",
        raw_email="student-wm24@student.tarc.edu.my",
    )
    assert ok is False
    assert "too many verification attempts" in err

    await db.close()


@pytest.mark.asyncio
async def test_alumni_email_confirmation_flow(tmp_path):
    db_path = str(tmp_path / "alumni_confirm_test.db")
    db = Database(db_path)
    await db.connect()

    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret_12345",
        enable_email_verification=True,
        email_encryption_key=key,
    )
    email_svc = EmailService(settings, mock_smtp=True)
    rate_limiter = RateLimiter(max_attempts=5, window_seconds=60)
    bot = MagicMock()
    service = VerificationService(
        bot=bot,
        db=db,
        secret="fake_secret_12345",
        rate_limiter=rate_limiter,
        settings=settings,
        email_service=email_svc,
    )

    user = MagicMock(spec=discord.Member)
    user.id = 8888
    user.mention = "<@8888>"
    user.roles = []
    guild = MagicMock(spec=discord.Guild)
    guild.id = 300
    guild.name = "TARUMT Alumni Hub"
    guild.roles = []
    user.guild = guild

    # Past cohort ID (6 years ago dynamically, e.g. Diploma duration 2 years -> expired 4 years ago)
    now = datetime.now()
    past_yy = str((now.year - 6) % 100).zfill(2)
    old_student_id = f"{past_yy}WMD01234"
    old_email = f"alumni-wm{past_yy}@student.tarc.edu.my"

    # Test modal submission with past cohort ID
    modal = VerificationModal(service=service, email_service=email_svc, require_email=True)
    modal.student_id._value = old_student_id
    modal.student_email._value = old_email
    modal.card_expiry._value = ""

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = user
    interaction.guild = guild
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    await modal.on_submit(interaction)

    # 1. Ensure 0 emails were dispatched to SMTP (alumni gate intercepted it)
    assert len(email_svc.sent_emails) == 0

    # 2. Ensure AlumniEmailConfirmationView was rendered
    interaction.followup.send.assert_called_once()
    kwargs = interaction.followup.send.call_args[1]
    embed = kwargs["embed"]
    view = kwargs["view"]

    assert "Graduated Student Cohort Detected" in embed.title
    assert isinstance(view, AlumniEmailConfirmationView)

    # 3. Test clicking "Verify as Graduated Alumni" button
    confirm_interaction = MagicMock(spec=discord.Interaction)
    confirm_interaction.user = user
    confirm_interaction.guild = guild
    confirm_interaction.response = MagicMock()
    confirm_interaction.response.defer = AsyncMock()
    confirm_interaction.followup = MagicMock()
    confirm_interaction.followup.send = AsyncMock()

    with patch.object(service, "get_mutual_guilds_for_user", AsyncMock(return_value=[guild])):
        with patch.object(service, "assign_role_across_guilds") as mock_assign:
            mock_assign.return_value = MagicMock(
                verified_in=[(300, "TARUMT Alumni Hub", "FOCS")],
                already_had_role_in=[],
                missing_role_in=[],
                failed_in=[],
            )
            await view.children[0].callback(confirm_interaction)

    # 4. User is verified in database with email saved encrypted, and 0 OTP emails sent!
    assert len(email_svc.sent_emails) == 0
    await db.close()


@pytest.mark.asyncio
async def test_email_bounce_detection(tmp_path):
    key = Fernet.generate_key().decode()
    settings = Settings(
        bot_token="fake_token",
        id_hash_secret="fake_secret",
        enable_email_verification=True,
        email_encryption_key=key,
        smtp_host="mail.smtp2go.com",
        smtp_user="test_smtp_user",
        smtp_password="test_smtp_pass",
        smtp_fallback_host="",
    )
    db = Database(str(tmp_path / "email_bounce_test.db"))
    await db.connect()
    try:
        svc = EmailService(settings, db=db, mock_smtp=False)

        # 1. Simulate SMTP bounce response (550 User unknown)
        bounce_error = "550 5.1.1 <invalid_user@student.tarc.edu.my>: Recipient address rejected: User unknown in virtual mailbox table"
        with patch.object(svc, "_send_to_smtp_endpoint", AsyncMock(return_value=(False, bounce_error))):
            res = await svc.generate_and_send_otp(
                user_id=12345,
                student_id="24WMR12345",
                email_address="invalid_user@student.tarc.edu.my",
                server_name="Test Campus",
            )
            assert res["success"] is False
            assert "rejected by mail server" in res["error"]

            # Verify it was recorded in bounced_emails DB table
            e_hash = svc.hash_student_email("invalid_user@student.tarc.edu.my")
            assert await db.is_email_bounced(e_hash) is True

        # 2. Subsequent request to the same bounced email is blocked immediately before contacting SMTP
        with patch.object(svc, "_send_to_smtp_endpoint") as mock_endpoint:
            res2 = await svc.generate_and_send_otp(
                user_id=12345,
                student_id="24WMR12345",
                email_address="invalid_user@student.tarc.edu.my",
                server_name="Test Campus",
            )
            assert res2["success"] is False
            assert "previously rejected/bounced" in res2["error"]
            # 0 network/SMTP calls made
            assert mock_endpoint.call_count == 0
    finally:
        await db.close()






