"""
Tests for TARVeri Interactive Minecraft-style Console CLI Service.
"""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tarveri.config import Settings
from tarveri.database import Database
from tarveri.services.console_service import ConsoleService


@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.user = MagicMock()
    bot.user.id = 123456789012345678
    bot.user.__str__.return_value = "TARVeri#0001"
    bot.latency = 0.042
    bot.guilds = []
    bot.close = AsyncMock()
    return bot


@pytest.fixture
def mock_service():
    service = MagicMock()
    service.perform_verification = AsyncMock(return_value="✅ Verified successfully!")
    service.unverify_member = AsyncMock(return_value={"success": True, "guilds_updated": 2})
    return service


@pytest.mark.asyncio
async def test_console_service_help_command(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    console = ConsoleService(mock_bot, db, mock_service, settings)

    captured_out = io.StringIO()
    with patch("sys.stdout", captured_out):
        await console.handle_command("help")

    output = captured_out.getvalue()
    assert "TARVeri Console Commands" in output
    assert "status" in output
    assert "verify" in output
    assert "unverify" in output
    await db.close()


@pytest.mark.asyncio
async def test_console_service_status_command(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    console = ConsoleService(mock_bot, db, mock_service, settings)

    captured_out = io.StringIO()
    with patch("sys.stdout", captured_out):
        await console.handle_command("status")

    output = captured_out.getvalue()
    assert "Bot User: TARVeri#0001" in output
    assert "WebSocket Ping: 42.0 ms" in output
    await db.close()


@pytest.mark.asyncio
async def test_console_service_verify_and_lookup_commands(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    # Record a test verification directly
    await db.record_verification(
        discord_user_id=111222333444555666,
        student_id_hash="hash123",
        faculty_code="FOCS",
    )

    console = ConsoleService(mock_bot, db, mock_service, settings)

    # 1. Lookup by discord ID
    captured_out = io.StringIO()
    with patch("sys.stdout", captured_out):
        await console.handle_command("lookup 111222333444555666")
    output = captured_out.getvalue()
    assert "111222333444555666" in output
    assert "FOCS" in output

    # 2. Verify command
    mock_bot.get_user.return_value = MagicMock(id=999888777666555444)
    captured_out = io.StringIO()
    with patch("sys.stdout", captured_out):
        await console.handle_command("verify 23WMD09867 999888777666555444")
    output = captured_out.getvalue()
    assert "Verified successfully!" in output

    # 3. Unverify command
    captured_out = io.StringIO()
    with patch("sys.stdout", captured_out):
        await console.handle_command("unverify 999888777666555444 testing unverify")
    output = captured_out.getvalue()
    assert "unverified cleanly" in output

    await db.close()


@pytest.mark.asyncio
async def test_console_service_backup_command(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    console = ConsoleService(mock_bot, db, mock_service, settings)

    captured_out = io.StringIO()
    with patch("sys.stdout", captured_out):
        await console.handle_command("backup")

    output = captured_out.getvalue()
    assert "Database backup saved to" in output
    await db.close()


@pytest.mark.asyncio
async def test_console_service_stop_command(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    console = ConsoleService(mock_bot, db, mock_service, settings)
    await console.handle_command("stop")
    assert mock_bot.close.called
    await db.close()
