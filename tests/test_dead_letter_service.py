"""
Unit and integration tests for Dead Letter Queue (DLQ) service and database operations.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.config import Settings
from tarveri.database import Database
from tarveri.services.dead_letter_service import DeadLetterQueueService
from tarveri.services.email_service import EmailService


@pytest.mark.asyncio
async def test_dlq_database_crud(tmp_path):
    db_file = str(tmp_path / "dlq_test.db")
    db = Database(db_file)
    await db.connect()
    try:
        # 1. Enqueue item
        payload = {"to": "student@student.tarc.edu.my", "code": "123456"}
        dlq_id = await db.enqueue_dead_letter(
            queue_type="EMAIL_VERIFICATION",
            payload=payload,
            error_reason="SMTP timeout",
            max_retries=3,
            metadata={"user_id": 1001},
            next_retry_delay_seconds=0,
        )
        assert dlq_id > 0

        # 2. Get by ID
        item = await db.get_dead_letter_by_id(dlq_id)
        assert item is not None
        assert item["queue_type"] == "EMAIL_VERIFICATION"
        assert item["payload"] == payload
        assert item["error_reason"] == "SMTP timeout"
        assert item["retry_count"] == 0
        assert item["status"] == "PENDING"
        assert item["metadata"] == {"user_id": 1001}

        # 3. Get pending
        pending = await db.get_pending_dead_letters(due_only=False)
        assert len(pending) == 1
        assert pending[0]["id"] == dlq_id

        # 4. Mark retried failure (retry 1)
        ok = await db.mark_dead_letter_retried(dlq_id, success=False, error_reason="Connection refused")
        assert ok is True
        item2 = await db.get_dead_letter_by_id(dlq_id)
        assert item2["retry_count"] == 1
        assert item2["status"] == "PENDING"
        assert item2["error_reason"] == "Connection refused"

        # 5. Mark retried failure reaching max_retries -> DEAD
        await db.mark_dead_letter_retried(dlq_id, success=False, error_reason="Still down")
        await db.mark_dead_letter_retried(dlq_id, success=False, error_reason="Permanent failure")
        item3 = await db.get_dead_letter_by_id(dlq_id)
        assert item3["retry_count"] == 3
        assert item3["status"] == "DEAD"

        # 6. Resolve dead letter
        await db.resolve_dead_letter(dlq_id, note="Resolved manually")
        item4 = await db.get_dead_letter_by_id(dlq_id)
        assert item4["status"] == "RESOLVED"

        # 7. Stats
        stats = await db.get_dead_letter_stats()
        assert stats["total"] == 1
        assert stats["resolved"] == 1
        assert stats["dead"] == 0

        # 8. List
        listed = await db.list_dead_letters(queue_type="EMAIL_VERIFICATION")
        assert len(listed) == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dlq_email_retry_flow(tmp_path):
    db_file = str(tmp_path / "dlq_email.db")
    db = Database(db_file)
    await db.connect()
    try:
        settings = Settings(
            bot_token="fake_token",
            id_hash_secret="0123456789abcdef0123456789abcdef",
            email_encryption_key="",
        )
        bot = MagicMock()
        email_svc = MagicMock()
        dlq_svc = DeadLetterQueueService(bot=bot, db=db, settings=settings, email_service=email_svc)

        # 1. Enqueue an expired OTP -> should be marked DEAD without sending
        dlq_id_expired = await dlq_svc.enqueue_email(
            user_id=123,
            student_id="22WMR12345",
            email="expired@student.tarc.edu.my",
            otp_code="999999",
            server_name="TARUMT",
            ttl_minutes=10,
            expires_at=time.time() - 100,  # Expired in past!
            error_reason="SMTP connection dropped",
        )

        res = await dlq_svc.process_queue(force_all=True)
        assert res["dead"] == 1
        assert res["resolved"] == 0

        item_exp = await db.get_dead_letter_by_id(dlq_id_expired)
        assert item_exp["status"] == "DEAD"
        assert "expired" in item_exp["error_reason"].lower()

        # 2. Enqueue an active OTP -> should succeed on retry
        email_svc._send_otp_email_async = AsyncMock(return_value=True)
        email_svc._pending_otps = {}
        email_svc._lock = MagicMock()
        email_svc._lock.__aenter__ = AsyncMock()
        email_svc._lock.__aexit__ = AsyncMock()

        dlq_id_active = await dlq_svc.enqueue_email(
            user_id=456,
            student_id="23WMR67890",
            email="active@student.tarc.edu.my",
            otp_code="888888",
            server_name="TARUMT",
            ttl_minutes=10,
            expires_at=time.time() + 500,  # Still valid!
            error_reason="Transient timeout",
        )

        res2 = await dlq_svc.process_queue(force_all=True)
        assert res2["resolved"] == 1
        email_svc._send_otp_email_async.assert_awaited_once_with(
            to_email="active@student.tarc.edu.my",
            otp_code="888888",
            server_name="TARUMT",
            ttl_minutes=10,
        )

        item_act = await db.get_dead_letter_by_id(dlq_id_active)
        assert item_act["status"] == "RESOLVED"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dlq_outage_alert_flow(tmp_path):
    db_file = str(tmp_path / "dlq_outage.db")
    db = Database(db_file)
    await db.connect()
    try:
        settings = Settings(
            bot_token="fake_token",
            id_hash_secret="0123456789abcdef0123456789abcdef",
            hoster_discord_id=999888,
        )
        bot = MagicMock()
        bot.is_ready = MagicMock(return_value=True)
        hoster_user = AsyncMock(spec=discord.User)
        hoster_user.send = AsyncMock()
        bot.get_user.return_value = hoster_user

        dlq_svc = DeadLetterQueueService(bot=bot, db=db, settings=settings)

        # Enqueue outage alert
        dlq_id = await dlq_svc.enqueue_outage(
            alert_type="OUTAGE_RESOLVED",
            reason="Gateway reconnected after 45.2s.",
            downtime_seconds=45.2,
            started_at="2026-10-16 12:00:00",
            ended_at="2026-10-16 12:00:45",
            hoster_discord_id=999888,
        )

        res = await dlq_svc.process_queue(force_all=True)
        assert res["resolved"] == 1
        hoster_user.send.assert_awaited_once()

        item = await db.get_dead_letter_by_id(dlq_id)
        assert item["status"] == "RESOLVED"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_dlq_exception_alert_flow(tmp_path):
    db_file = str(tmp_path / "dlq_exc.db")
    db = Database(db_file)
    await db.connect()
    try:
        settings = Settings(
            bot_token="fake_token",
            id_hash_secret="0123456789abcdef0123456789abcdef",
            hoster_discord_id=777666,
        )
        bot = MagicMock()
        bot.is_ready = MagicMock(return_value=True)
        hoster_user = AsyncMock(spec=discord.User)
        hoster_user.send = AsyncMock()
        bot.get_user.return_value = hoster_user

        dlq_svc = DeadLetterQueueService(bot=bot, db=db, settings=settings)

        test_exc = RuntimeError("Database locked / unexpected exception")
        dlq_id = await dlq_svc.enqueue_exception(
            exc=test_exc,
            context="Slash command: /verify",
        )

        res = await dlq_svc.process_queue(force_all=True)
        assert res["resolved"] == 1
        hoster_user.send.assert_awaited_once()

        item = await db.get_dead_letter_by_id(dlq_id)
        assert item["status"] == "RESOLVED"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_email_service_enqueues_to_dlq_on_send_failure(tmp_path):
    db_file = str(tmp_path / "email_dlq_int.db")
    db = Database(db_file)
    await db.connect()
    try:
        settings = Settings(
            bot_token="fake_token",
            id_hash_secret="0123456789abcdef0123456789abcdef",
            enable_email_verification=True,
            smtp_host="fake.smtp.test",
            smtp_user="user",
            smtp_password="pw",
            email_encryption_key="",
        )
        dlq_svc = MagicMock()
        dlq_svc.enqueue_email = AsyncMock(return_value=1)

        email_svc = EmailService(settings, db=db, mock_smtp=False, dlq_service=dlq_svc)
        # Mock _send_smtp_async to simulate failed delivery
        email_svc._send_smtp_async = AsyncMock(return_value=False)

        res = await email_svc.generate_and_send_otp(
            user_id=1122,
            student_id="24WMR99999",
            email_address="test@student.tarc.edu.my",
            server_name="TARUMT Discord",
        )

        assert res["success"] is False
        dlq_svc.enqueue_email.assert_awaited_once()
        args, kwargs = dlq_svc.enqueue_email.call_args
        assert kwargs["user_id"] == 1122
        assert kwargs["student_id"] == "24WMR99999"
        assert kwargs["email"] == "test@student.tarc.edu.my"
    finally:
        await db.close()
