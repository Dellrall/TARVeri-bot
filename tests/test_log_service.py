import logging
import os
import tarfile
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.cogs.admin_cog import AdminCog
from tarveri.config import DailyRotatingFileHandler, Settings
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.log_service import (
    LogRotationService,
    archive_old_logs,
    get_10day_period,
    get_archive_filename,
    list_daily_logs,
    list_log_archives,
    parse_log_date,
)
from tarveri.services.verification_service import VerificationService


def test_get_10day_period_decade_grouping():
    # Part 1: Days 1 to 10
    tag, s, e = get_10day_period(date(2026, 9, 1))
    assert tag == "2026-09-01_to_2026-09-10"
    assert s == "2026-09-01"
    assert e == "2026-09-10"

    tag, s, e = get_10day_period(date(2026, 9, 10))
    assert tag == "2026-09-01_to_2026-09-10"

    # Part 2: Days 11 to 20
    tag, s, e = get_10day_period(date(2026, 9, 11))
    assert tag == "2026-09-11_to_2026-09-20"
    assert s == "2026-09-11"
    assert e == "2026-09-20"

    tag, s, e = get_10day_period(date(2026, 9, 20))
    assert tag == "2026-09-11_to_2026-09-20"

    # Part 3: Days 21 to 30 (30-day month)
    tag, s, e = get_10day_period(date(2026, 9, 21))
    assert tag == "2026-09-21_to_2026-09-30"
    assert s == "2026-09-21"
    assert e == "2026-09-30"

    tag, s, e = get_10day_period(date(2026, 9, 30))
    assert tag == "2026-09-21_to_2026-09-30"

    # Part 3: 31-day month (August)
    tag, s, e = get_10day_period(date(2026, 8, 31))
    assert tag == "2026-08-21_to_2026-08-31"

    # Part 3: February leap year (2024)
    tag, s, e = get_10day_period(date(2024, 2, 29))
    assert tag == "2024-02-21_to_2024-02-29"

    # Part 3: February non-leap year (2025)
    tag, s, e = get_10day_period(date(2025, 2, 28))
    assert tag == "2025-02-21_to_2025-02-28"


def test_get_archive_filename():
    filename = get_archive_filename("2026-09-01_to_2026-09-10")
    assert filename == "tarveri-logs-2026-09-01_to_2026-09-10.tar.gz"


def test_parse_log_date(tmp_path):
    assert parse_log_date("tarveri-2026-09-10.log") == date(2026, 9, 10)
    assert parse_log_date("app-2025-12-31.log") == date(2025, 12, 31)

    # Fallback to mtime if filename has no date
    mock_file = tmp_path / "custom.log"
    mock_file.write_text("sample content")
    parsed = parse_log_date("custom.log", file_path=str(mock_file))
    assert parsed is not None
    assert isinstance(parsed, date)


def test_daily_rotating_file_handler(tmp_path):
    logs_dir = str(tmp_path / "logs")
    handler = DailyRotatingFileHandler(logs_dir=logs_dir, prefix="testbot", tz_name="Asia/Kuala_Lumpur")

    logger = logging.getLogger("test_daily_rotator")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    # 1. Log record for 2026-09-10
    record1 = logging.LogRecord(
        name="test", level=logging.INFO, pathname="test.py", lineno=1, msg="First daily message", args=(), exc_info=None
    )
    # Fixed timestamp: 2026-09-10 12:00:00 UTC+8 (1789012800)
    record1.created = 1789012800.0
    logger.handle(record1)

    log_path1 = os.path.join(logs_dir, "testbot-2026-09-10.log")
    assert os.path.exists(log_path1)
    with open(log_path1, encoding="utf-8") as f:
        content = f.read()
    assert "First daily message" in content

    # 2. Log record for next day 2026-09-11
    record2 = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=2,
        msg="Second daily message on next day",
        args=(),
        exc_info=None,
    )
    # Next day timestamp: 2026-09-11 12:00:00 UTC+8 (1789099200)
    record2.created = 1789099200.0
    logger.handle(record2)

    log_path2 = os.path.join(logs_dir, "testbot-2026-09-11.log")
    assert os.path.exists(log_path2)
    with open(log_path2, encoding="utf-8") as f:
        content2 = f.read()
    assert "Second daily message on next day" in content2

    handler.close()
    logger.removeHandler(handler)


def test_archive_old_logs_groups_by_10th_days_and_compresses_tar_gz(tmp_path):
    logs_dir = str(tmp_path / "logs")
    os.makedirs(logs_dir, exist_ok=True)

    # Create logs older than 10 days relative to 2026-09-10
    # Group 1: 2026-08-01 to 2026-08-10 (2 files)
    (tmp_path / "logs" / "tarveri-2026-08-01.log").write_text("Log from Aug 1")
    (tmp_path / "logs" / "tarveri-2026-08-05.log").write_text("Log from Aug 5")

    # Group 2: 2026-08-11 to 2026-08-20 (1 file)
    (tmp_path / "logs" / "tarveri-2026-08-15.log").write_text("Log from Aug 15")

    # Group 3: 2026-08-21 to 2026-08-31 (1 file)
    (tmp_path / "logs" / "tarveri-2026-08-25.log").write_text("Log from Aug 25")

    # Recent log (5 days old): 2026-09-05 -> MUST NOT be archived
    (tmp_path / "logs" / "tarveri-2026-09-05.log").write_text("Recent log from Sep 5")

    # Active log: 2026-09-10 -> MUST NOT be archived
    (tmp_path / "logs" / "tarveri-2026-09-10.log").write_text("Today active log")

    ref_date = date(2026, 9, 10)
    results = archive_old_logs(
        logs_dir=logs_dir,
        older_than_days=10,
        reference_date=ref_date,
    )

    assert len(results) == 3

    # Check that .tar.gz archives were created in logs/archives
    archives_dir = os.path.join(logs_dir, "archives")
    ar1 = os.path.join(archives_dir, "tarveri-logs-2026-08-01_to_2026-08-10.tar.gz")
    ar2 = os.path.join(archives_dir, "tarveri-logs-2026-08-11_to_2026-08-20.tar.gz")
    ar3 = os.path.join(archives_dir, "tarveri-logs-2026-08-21_to_2026-08-31.tar.gz")

    assert os.path.isfile(ar1)
    assert os.path.isfile(ar2)
    assert os.path.isfile(ar3)

    # Verify archive 1 contents
    with tarfile.open(ar1, "r:gz") as tar:
        members = set(tar.getnames())
        assert "tarveri-2026-08-01.log" in members
        assert "tarveri-2026-08-05.log" in members

    # Verify original archived log files were safely removed
    assert not os.path.exists(tmp_path / "logs" / "tarveri-2026-08-01.log")
    assert not os.path.exists(tmp_path / "logs" / "tarveri-2026-08-05.log")
    assert not os.path.exists(tmp_path / "logs" / "tarveri-2026-08-15.log")
    assert not os.path.exists(tmp_path / "logs" / "tarveri-2026-08-25.log")

    # Verify recent logs remain untouched
    assert os.path.exists(tmp_path / "logs" / "tarveri-2026-09-05.log")
    assert os.path.exists(tmp_path / "logs" / "tarveri-2026-09-10.log")


def test_archive_old_logs_merges_into_existing_archive(tmp_path):
    logs_dir = str(tmp_path / "logs")
    archives_dir = str(tmp_path / "logs" / "archives")
    os.makedirs(archives_dir, exist_ok=True)

    # Create an existing archive with one file
    existing_ar = os.path.join(archives_dir, "tarveri-logs-2026-08-01_to_2026-08-10.tar.gz")
    temp_old_log = tmp_path / "tarveri-2026-08-01.log"
    temp_old_log.write_text("Existing archived log")

    with tarfile.open(existing_ar, "w:gz") as tar:
        tar.add(str(temp_old_log), arcname="tarveri-2026-08-01.log")

    # Now add another log for that same period in logs_dir
    (tmp_path / "logs" / "tarveri-2026-08-08.log").write_text("Newly found old log")

    ref_date = date(2026, 9, 10)
    results = archive_old_logs(
        logs_dir=logs_dir,
        older_than_days=10,
        reference_date=ref_date,
    )

    assert len(results) == 1
    with tarfile.open(existing_ar, "r:gz") as tar:
        members = set(tar.getnames())
        assert "tarveri-2026-08-01.log" in members
        assert "tarveri-2026-08-08.log" in members

    # Uncompressed file removed
    assert not os.path.exists(tmp_path / "logs" / "tarveri-2026-08-08.log")


def test_list_daily_logs_and_archives(tmp_path):
    logs_dir = str(tmp_path / "logs")
    os.makedirs(logs_dir, exist_ok=True)

    (tmp_path / "logs" / "tarveri-2026-09-09.log").write_text("line1\nline2")
    (tmp_path / "logs" / "tarveri-2026-09-10.log").write_text("line1\nline2\nline3")

    daily = list_daily_logs(logs_dir)
    assert len(daily) == 2
    assert daily[0]["filename"] == "tarveri-2026-09-10.log"
    assert daily[0]["lines"] == 3

    # Create dummy archive
    archives_dir = os.path.join(logs_dir, "archives")
    os.makedirs(archives_dir, exist_ok=True)
    ar_path = os.path.join(archives_dir, "tarveri-logs-2026-08-01_to_2026-08-10.tar.gz")
    with tarfile.open(ar_path, "w:gz") as tar:
        tar.add(str(tmp_path / "logs" / "tarveri-2026-09-09.log"), arcname="tarveri-2026-08-01.log")

    archives = list_log_archives(logs_dir)
    assert len(archives) == 1
    assert archives[0]["filename"] == "tarveri-logs-2026-08-01_to_2026-08-10.tar.gz"
    assert archives[0]["file_count"] == 1


@pytest.mark.asyncio
async def test_log_rotation_service_lifecycle(tmp_path):
    logs_dir = str(tmp_path / "logs")
    os.makedirs(logs_dir, exist_ok=True)
    (tmp_path / "logs" / "tarveri-2026-08-01.log").write_text("Old log")

    service = LogRotationService(logs_dir=logs_dir, older_than_days=10)
    assert not service.is_running

    service.start()
    assert service.is_running

    # Trigger rotation manually
    results = service.trigger_rotation(reference_date=date(2026, 9, 10))
    assert len(results) >= 1

    service.stop()
    assert not service.is_running


@pytest.mark.asyncio
async def test_admin_logs_command(tmp_path):
    bot = MagicMock()
    bot.settings = Settings(
        bot_token="tok",
        id_hash_secret="sec",
        logs_dir=str(tmp_path / "logs"),
        timezone_name="Asia/Kuala_Lumpur",
    )
    os.makedirs(bot.settings.logs_dir, exist_ok=True)
    (tmp_path / "logs" / "tarveri-2026-09-10.log").write_text("Log line 1\nLog line 2")

    db = Database(str(tmp_path / "admin_logs.db"))
    await db.connect()
    service = VerificationService(bot, db, "sec", RateLimiter(5, 60))
    rotator = LogRotationService(logs_dir=bot.settings.logs_dir, older_than_days=10)

    cog = AdminCog(bot, db, service, RateLimiter(5, 60), "TARVeri Admin", log_rotator=rotator)

    interaction = MagicMock(spec=discord.Interaction)
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.guild_permissions.administrator = True
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.roles = []
    interaction.user.roles = []
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()

    # 1. Action: list
    await cog.logs.callback(cog, interaction, action="list")
    interaction.followup.send.assert_called_once()
    embed = interaction.followup.send.call_args[1]["embed"]
    assert any("Active Daily Logs" in f.name for f in embed.fields)

    # 2. Action: recent
    interaction.followup.send.reset_mock()
    await cog.logs.callback(cog, interaction, action="recent")
    interaction.followup.send.assert_called_once()
    call_text = interaction.followup.send.call_args[0][0]
    assert "Latest Logs" in call_text
    assert "Log line 1" in call_text

    # 3. Action: archive
    interaction.followup.send.reset_mock()
    await cog.logs.callback(cog, interaction, action="archive")
    interaction.followup.send.assert_called_once()

    await db.close()


def test_settings_log_rotator_env_parsing(monkeypatch):
    monkeypatch.setenv("TARVERI_BOT_TOKEN", "token")
    monkeypatch.setenv("TARVERI_ID_HASH_SECRET", "secret")
    monkeypatch.setenv("TARVERI_LOGS_DIR", "custom_logs")
    monkeypatch.setenv("TARVERI_LOG_ARCHIVE_DAYS", "15")
    monkeypatch.setenv("TARVERI_ENABLE_LOG_ROTATOR", "true")

    s = Settings.from_env()
    assert s.logs_dir == "custom_logs"
    assert s.log_archive_days == 15
    assert s.enable_log_rotator is True

    monkeypatch.setenv("TARVERI_ENABLE_LOG_ROTATOR", "false")
    s2 = Settings.from_env()
    assert s2.enable_log_rotator is False
