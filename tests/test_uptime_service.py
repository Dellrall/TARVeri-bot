"""
Tests for UptimeService, Downtime Detection, and SLA Performance Metrics.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from tarveri.cogs.admin_cog import AdminCog
from tarveri.cogs.admin_dashboard import AdminDashboardView
from tarveri.config import get_configured_tz
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.uptime_service import (
    UptimeService,
    evaluate_sla_grade,
    format_duration_seconds,
)


def test_format_duration_seconds():
    assert format_duration_seconds(4.2) == "4.2s"
    assert format_duration_seconds(45) == "45s"
    assert format_duration_seconds(125) == "2m 5s"
    assert format_duration_seconds(3665) == "1h 1m 5s"
    assert format_duration_seconds(90065) == "1d 1h 1m"


def test_evaluate_sla_grade():
    assert "Five Nines" in evaluate_sla_grade(99.9995)
    assert "Four Nines" in evaluate_sla_grade(99.991)
    assert "Tier 1" in evaluate_sla_grade(99.95)
    assert "Tier 2" in evaluate_sla_grade(99.5)
    assert "Degraded" in evaluate_sla_grade(96.0)
    assert "Major Outage" in evaluate_sla_grade(92.0)


@pytest.mark.asyncio
async def test_uptime_service_heartbeat_and_startup_clean_restart(tmp_path):
    db_path = str(tmp_path / "uptime_clean.db")
    db = Database(db_path)
    await db.connect()

    # 1. Seed a previous session with a clean shutdown 60s ago
    tz = get_configured_tz()
    now_dt = datetime.now(tz)
    past_started = (now_dt - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    past_shutdown = (now_dt - timedelta(seconds=60)).strftime("%Y-%m-%d %H:%M:%S")

    await db.upsert_uptime_heartbeat(
        session_id="prev_session_1",
        started_at=past_started,
        last_heartbeat_at=past_shutdown,
        clean_shutdown_at=past_shutdown,
    )

    mock_bot = MagicMock()
    service = UptimeService(bot=mock_bot, db=db, heartbeat_interval_seconds=5)

    await service.start()
    assert service.is_running is True

    # Check that a CLEAN_RESTART downtime event was detected and recorded
    recent_events = await db.get_recent_downtime_events(limit=5)
    assert len(recent_events) == 1
    ev = recent_events[0]
    assert ev["downtime_type"] == "CLEAN_RESTART"
    assert 55.0 <= ev["duration_seconds"] <= 65.0

    # Stop service cleanly
    await service.stop()
    assert service.is_running is False

    hb = await db.get_uptime_heartbeat()
    assert hb["clean_shutdown_at"] is not None
    await db.close()


@pytest.mark.asyncio
async def test_uptime_service_unexpected_downtime_detection(tmp_path):
    db_path = str(tmp_path / "uptime_crash.db")
    db = Database(db_path)
    await db.connect()

    # 1. Seed a previous session with NO clean shutdown (simulating unexpected crash/power loss 120s ago)
    tz = get_configured_tz()
    now_dt = datetime.now(tz)
    past_started = (now_dt - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S")
    past_heartbeat = (now_dt - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")

    await db.upsert_uptime_heartbeat(
        session_id="crashed_session_2",
        started_at=past_started,
        last_heartbeat_at=past_heartbeat,
        clean_shutdown_at=None,
    )

    mock_bot = MagicMock()
    service = UptimeService(bot=mock_bot, db=db, heartbeat_interval_seconds=5)

    await service.start()

    recent_events = await db.get_recent_downtime_events(limit=5)
    assert len(recent_events) == 1
    ev = recent_events[0]
    assert ev["downtime_type"] == "UNEXPECTED_DOWNTIME"
    assert 115.0 <= ev["duration_seconds"] <= 125.0

    await service.stop()
    await db.close()


@pytest.mark.asyncio
async def test_uptime_service_sla_metrics_calculation(tmp_path):
    db_path = str(tmp_path / "uptime_sla.db")
    db = Database(db_path)
    await db.connect()

    mock_bot = MagicMock()
    mock_bot.latency = 0.042
    service = UptimeService(bot=mock_bot, db=db)
    await service.start()

    # 1. Record gateway outage
    tz = get_configured_tz()
    now_dt = datetime.now(tz)
    start_dt = now_dt - timedelta(seconds=180)
    end_dt = now_dt - timedelta(seconds=60)

    await service.record_gateway_outage(
        duration_seconds=120.0,
        started_at=start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        ended_at=end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        reason="Network packet drop",
    )

    metrics = await service.get_sla_metrics()
    assert metrics["current_uptime_seconds"] >= 0.0
    assert metrics["session_id"] == service.session_id

    sla_24h = metrics["sla_24h"]
    assert sla_24h["downtime_seconds"] == 120.0
    assert sla_24h["incidents"] == 1
    # 24h total seconds = 86400; (86400 - 120) / 86400 * 100 = 99.861%
    assert 99.85 <= sla_24h["sla_percent"] <= 99.87

    sla_7d = metrics["sla_7d"]
    assert 99.97 <= sla_7d["sla_percent"] <= 100.0

    assert len(metrics["recent_incidents"]) == 1

    await service.stop()
    await db.close()


@pytest.mark.asyncio
async def test_admin_dashboard_uptime_embed_rendering(tmp_path):
    db_path = str(tmp_path / "uptime_dashboard.db")
    db = Database(db_path)
    await db.connect()

    mock_bot = MagicMock()
    mock_bot.latency = 0.035
    mock_bot.guilds = []

    service = UptimeService(bot=mock_bot, db=db)
    await service.start()
    mock_bot.uptime_service = service

    cog = AdminCog(
        bot=mock_bot,
        db=db,
        service=MagicMock(),
        rate_limiter=RateLimiter(),
        admin_role_name="TARVeri Admin",
        uptime_service=service,
    )

    admin_user = MagicMock(spec=discord.Member)
    dashboard_view = AdminDashboardView(cog=cog, admin_user=admin_user, initial_category="uptime")

    mock_guild = MagicMock(spec=discord.Guild)
    mock_guild.id = 123456
    mock_guild.name = "TARUMT Test Server"

    uptime_embed = await dashboard_view.build_uptime_embed(mock_guild)
    assert "TARVeri System Uptime & SLA Performance" in uptime_embed.title
    assert any("Active Session Health" in f.name for f in uptime_embed.fields)
    assert any("SLA Availability Scores" in f.name for f in uptime_embed.fields)

    overview_embed = await dashboard_view.build_overview_embed(mock_guild)
    assert any("Uptime & 24h SLA" in f.name for f in overview_embed.fields)

    # Test refresh button callback
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response.is_done.return_value = False
    interaction.guild = mock_guild
    interaction.response.defer = AsyncMock()
    interaction.response.edit_message = AsyncMock()

    await dashboard_view._on_refresh_uptime_clicked(interaction)
    interaction.response.edit_message.assert_called_once()

    await service.stop()
    await db.close()


@pytest.mark.asyncio
async def test_admin_uptime_slash_command(tmp_path):
    db_path = str(tmp_path / "uptime_slash.db")
    db = Database(db_path)
    await db.connect()

    mock_bot = MagicMock()
    mock_bot.latency = 0.020
    mock_bot.guilds = []

    service = UptimeService(bot=mock_bot, db=db)
    await service.start()

    cog = AdminCog(
        bot=mock_bot,
        db=db,
        service=MagicMock(),
        rate_limiter=RateLimiter(),
        admin_role_name="TARVeri Admin",
        uptime_service=service,
    )

    interaction = MagicMock(spec=discord.Interaction)
    interaction.guild = MagicMock(spec=discord.Guild)
    interaction.guild.id = 999
    interaction.guild.name = "TARUMT Hub"
    interaction.user = MagicMock(spec=discord.Member)
    interaction.user.guild_permissions.administrator = True
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()

    await cog.uptime.callback(cog, interaction)
    interaction.response.defer.assert_called_once_with(ephemeral=True)
    interaction.followup.send.assert_called_once()
    called_embed = interaction.followup.send.call_args[1]["embed"]
    assert "TARVeri System Uptime & SLA Performance" in called_embed.title

    await service.stop()
    await db.close()
