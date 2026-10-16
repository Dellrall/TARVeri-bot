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
    console.is_authenticated = True
    console.authenticated_user = "admin"

    mock_bot.uptime_service = MagicMock()
    mock_bot.uptime_service.current_uptime_str = "1h 23m 45s"

    captured_out = io.StringIO()
    with patch("sys.stdout", captured_out):
        await console.handle_command("status")

    output = captured_out.getvalue()
    assert "Bot User: TARVeri#0001" in output
    assert "WebSocket Ping: 42.0 ms" in output
    assert "Continuous Uptime: 1h 23m 45s" in output
    await db.close()


@pytest.mark.asyncio
async def test_console_service_stats_command(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    await db.record_verification(
        discord_user_id=111222333444555666,
        student_id_hash="hash123",
        faculty_code="FOCS",
    )

    console = ConsoleService(mock_bot, db, mock_service, settings)
    console.is_authenticated = True
    console.authenticated_user = "admin"

    captured_out = io.StringIO()
    with patch("sys.stdout", captured_out):
        await console.handle_command("stats")

    output = captured_out.getvalue()
    assert "Verification Statistics" in output
    assert "Total Verified Records: 1" in output
    assert "Active Students:        1" in output
    assert "FOCS" in output
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
    console.is_authenticated = True
    console.authenticated_user = "admin"

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
    console.is_authenticated = True
    console.authenticated_user = "admin"

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


@pytest.mark.asyncio
async def test_console_service_clear_command(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    console = ConsoleService(mock_bot, db, mock_service, settings)
    mock_stdout = MagicMock()
    mock_stdout.isatty.return_value = True

    with patch("sys.stdout", mock_stdout):
        await console.handle_command("clear")
        mock_stdout.write.assert_called_with("\033[2J\033[H")

    await db.close()


def test_console_service_completer():
    from tarveri.services.console_service import _console_completer

    with patch("readline.get_line_buffer", return_value=""), patch("readline.get_begidx", return_value=0):
        # Empty line: matches top-level commands
        first = _console_completer("", 0)
        assert first in ("help", "status", "guilds", "stats", "lookup", "verify", "unverify", "backup", "say", "broadcast", "eval", "clear", "stop", "exit", "quit")

    with patch("readline.get_line_buffer", return_value="stats "), patch("readline.get_begidx", return_value=6):
        # Subcommand stats options
        assert _console_completer("", 0) == "overview"
        assert _console_completer("", 1) == "faculties"

    with patch("readline.get_line_buffer", return_value="backup "), patch("readline.get_begidx", return_value=7):
        # Subcommand backup options
        assert _console_completer("", 0) == "snapshot"
        assert _console_completer("", 1) == "manual"


def test_console_service_escable_help(mock_bot, mock_service, tmp_path):
    settings = Settings(bot_token="test", id_hash_secret="secret123")
    console = ConsoleService(mock_bot, MagicMock(), mock_service, settings)

    # In non-TTY fallback, outputs help text
    captured = io.StringIO()
    with patch("sys.stdout", captured), patch("sys.stdin.isatty", return_value=False):
        console._show_escable_help()
    assert "TARVeri Console Commands" in captured.getvalue()


@pytest.mark.asyncio
async def test_console_service_locked_gate(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    console = ConsoleService(mock_bot, db, mock_service, settings)
    assert not console.is_authenticated

    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("status")
    assert "[AUTH REQUIRED]" in captured.getvalue()

    await db.close()


@pytest.mark.asyncio
async def test_console_service_setup_admin_and_login_flow(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    console = ConsoleService(mock_bot, db, mock_service, settings)

    # 1. Login before any admin is setup
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("login rootadmin")
    assert "No console administrators have been initialized yet" in captured.getvalue()

    # 2. Setup admin rejected if not a Linux administrator
    with patch("tarveri.services.console_service.is_linux_admin", return_value=False):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("setup-admin rootadmin secretpassword")
        assert "is not a Linux administrator" in captured.getvalue()
        assert not console.is_authenticated

    # 3. Setup admin with Linux admin and correct PAM auth (mocked)
    with (
        patch("tarveri.services.console_service.is_linux_admin", return_value=True),
        patch.object(console, "_pam_authenticate", return_value=True),
    ):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("setup-admin rootadmin secretpassword")
        assert "Root administrator 'rootadmin' successfully initialized" in captured.getvalue()
        assert console.is_authenticated
        assert console.authenticated_user == "rootadmin"

    # 4. Subsequent setup-admin is blocked
    with patch("tarveri.services.console_service.is_linux_admin", return_value=True):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("setup-admin secondadmin secretpassword")
        assert "An administrator account already exists" in captured.getvalue()

    # 5. Lock / Logout
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("lock")
    assert not console.is_authenticated
    assert "Console locked" in captured.getvalue()

    # 6. Login unauthorized user
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("login fakeuser")
    assert "is not an authorized console administrator" in captured.getvalue()

    # 7. Login authorized user with PAM failure -> increments failure count
    with patch.object(console, "_pam_authenticate", return_value=False):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("login rootadmin wrongpass1")
        assert "Attempt 1/3" in captured.getvalue()
        assert not console.is_authenticated

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("login rootadmin wrongpass2")
        assert "Attempt 2/3" in captured.getvalue()

        # 3rd failed attempt triggers lockout
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("login rootadmin wrongpass3")
        assert "Console locked for 15 minutes" in captured.getvalue()
        assert console._locked_until > 0

        # Attempting while locked out
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("login rootadmin wrongpass4")
        assert "[SECURITY LOCKOUT]" in captured.getvalue()

    await db.close()


@pytest.mark.asyncio
async def test_console_service_user_management(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    console = ConsoleService(mock_bot, db, mock_service, settings)
    console.is_authenticated = True
    console.authenticated_user = "admin"
    await db.add_console_admin("admin", role="admin", added_by="bootstrap")

    # 1. User list
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("user list")
    assert "admin" in captured.getvalue()

    # 2. User add rejected if non-existent Linux user
    with patch("pwd.getpwnam", side_effect=KeyError("not found")):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("user add ghostuser")
        assert "does not exist on this Linux system" in captured.getvalue()

    # 3. User add succeeds for valid Linux user
    with patch("pwd.getpwnam", return_value=MagicMock()):
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("user add operator2")
        assert "Added 'operator2' as authorized console administrator" in captured.getvalue()
        assert await db.is_console_admin("operator2")

        # 4. Prevent duplicate add
        captured = io.StringIO()
        with patch("sys.stdout", captured):
            await console.handle_command("user add operator2")
        assert "already an authorized console administrator" in captured.getvalue()

    # 4. User remove
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("user remove operator2")
    assert "Removed 'operator2' from console administrators" in captured.getvalue()
    assert not await db.is_console_admin("operator2")

    # 5. Cannot remove last remaining admin
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("user remove admin")
    assert "At least one console administrator must remain" in captured.getvalue()
    assert await db.is_console_admin("admin")

    await db.close()


@pytest.mark.asyncio
async def test_console_service_view_and_minimize_layout(mock_bot, mock_service, tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings(bot_token="test", id_hash_secret="secret123")

    console = ConsoleService(mock_bot, db, mock_service, settings)
    assert not console._output_minimized
    assert not console._logs_minimized

    # 1. Minimize output
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("minimize output")
    assert console._output_minimized
    assert "Terminal output minimized" in captured.getvalue()

    # 2. View split restores
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("view split")
    assert not console._output_minimized
    assert not console._logs_minimized
    assert "Split view" in captured.getvalue()

    # 3. View logs minimizes output
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("view logs")
    assert console._output_minimized
    assert not console._logs_minimized
    assert "Logs view" in captured.getvalue()

    # 4. View output minimizes logs
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("view output")
    assert not console._output_minimized
    assert console._logs_minimized
    assert "Output view" in captured.getvalue()

    # 5. Restore restores all
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("restore all")
    assert not console._output_minimized
    assert not console._logs_minimized
    assert "restored to Split view" in captured.getvalue()

    # 6. Auto-expansion of output pane when command is executed
    console._output_minimized = True
    captured = io.StringIO()
    with patch("sys.stdout", captured):
        await console.handle_command("help")
    assert not console._output_minimized

    await db.close()
