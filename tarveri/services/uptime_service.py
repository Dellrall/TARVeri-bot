"""
Uptime, Downtime Detection, and SLA Performance Tracking Service.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from tarveri.config import get_configured_tz, now_formatted
from tarveri.utils import parse_db_timestamp

if TYPE_CHECKING:
    import discord

    from tarveri.database import Database

logger = logging.getLogger("tarveri")


def format_duration_seconds(seconds: float) -> str:
    """Formats a duration in seconds into a clean, human-readable string."""
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{int(seconds)}s"

    total_seconds = int(seconds)
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    rem_seconds = total_seconds % 60

    parts: list[str] = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if rem_seconds > 0 and days == 0:
        parts.append(f"{rem_seconds}s")

    return " ".join(parts) if parts else "0s"


def evaluate_sla_grade(sla_percent: float) -> str:
    """Classifies an SLA availability score into standard operational tiers."""
    if sla_percent >= 99.999:
        return "🌟 Five Nines (99.999%)"
    if sla_percent >= 99.99:
        return "🟢 Four Nines (99.99%)"
    if sla_percent >= 99.9:
        return "🟢 Tier 1 (High Availability - 99.9%)"
    if sla_percent >= 99.0:
        return "🟡 Tier 2 (Standard - 99.0%)"
    if sla_percent >= 95.0:
        return "🟠 Degraded Performance (<99.0%)"
    return "🔴 Major Outage (<95.0%)"


class UptimeService:
    """
    Service tracking continuous bot session uptime, persistent heartbeats,
    unplanned/planned downtime detection across restarts, and SLA percentage calculations.
    """

    def __init__(
        self,
        bot: discord.Client,
        db: Database,
        heartbeat_interval_seconds: int = 15,
        min_downtime_record_seconds: float = 5.0,
    ):
        self.bot = bot
        self.db = db
        self.heartbeat_interval_seconds = max(5, heartbeat_interval_seconds)
        self.min_downtime_record_seconds = max(1.0, min_downtime_record_seconds)

        self.session_id: str = uuid.uuid4().hex[:12]
        self.started_at: datetime = datetime.now(get_configured_tz())
        self.started_at_monotonic: float = time.monotonic()
        self._task: asyncio.Task[None] | None = None
        self._is_running: bool = False

    @property
    def is_running(self) -> bool:
        return self._is_running and self._task is not None and not self._task.done()

    @property
    def current_uptime_seconds(self) -> float:
        return time.monotonic() - self.started_at_monotonic

    @property
    def current_uptime_str(self) -> str:
        return format_duration_seconds(self.current_uptime_seconds)

    async def start(self) -> None:
        """
        Initializes the uptime tracker:
        1. Evaluates prior session heartbeats to detect and record downtime gap.
        2. Upserts new session heartbeat in database.
        3. Spawns periodic heartbeat loop.
        """
        if self._is_running:
            return

        self._is_running = True
        self.started_at = datetime.now(get_configured_tz())
        self.started_at_monotonic = time.monotonic()

        await self._evaluate_startup_downtime()

        # Record new session heartbeat
        now_ts = now_formatted()
        try:
            await self.db.upsert_uptime_heartbeat(
                session_id=self.session_id,
                started_at=now_ts,
                last_heartbeat_at=now_ts,
                system_version="TARVeri 2.0",
                clean_shutdown_at=None,
            )
        except Exception as e:
            logger.warning(f"Could not record startup heartbeat in database: {e}")

        self._task = asyncio.create_task(self._heartbeat_loop(), name="tarveri_uptime_heartbeat")
        logger.info(
            f"⏱️ Uptime service initialized (Session ID: {self.session_id}, Heartbeat: {self.heartbeat_interval_seconds}s)."
        )

    async def stop(self) -> None:
        """Gracefully halts heartbeat task and records clean shutdown timestamp."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None

        now_ts = now_formatted()
        try:
            if self.db.is_connected:
                await self.db.record_clean_shutdown(clean_shutdown_at=now_ts)
        except Exception as e:
            logger.debug(f"Could not record clean shutdown timestamp in database: {e}")

        logger.info(f"⏱️ Uptime service stopped cleanly at {now_ts}.")

    async def _evaluate_startup_downtime(self) -> None:
        """Checks previous session heartbeat to detect if a downtime period occurred before this startup."""
        try:
            prev = await self.db.get_uptime_heartbeat()
            if not prev:
                return

            last_hb_str = prev.get("last_heartbeat_at")
            clean_sd_str = prev.get("clean_shutdown_at")

            downtime_start_dt = None
            downtime_reason = "Clean service restart / update"
            downtime_type = "CLEAN_RESTART"

            if clean_sd_str:
                downtime_start_dt = parse_db_timestamp(clean_sd_str)
            elif last_hb_str:
                downtime_start_dt = parse_db_timestamp(last_hb_str)
                downtime_reason = "Unscheduled process termination / crash / power outage"
                downtime_type = "UNEXPECTED_DOWNTIME"

            if downtime_start_dt:
                now_dt = datetime.now(get_configured_tz())
                # Normalize timezone
                if downtime_start_dt.tzinfo is None:
                    downtime_start_dt = downtime_start_dt.replace(tzinfo=get_configured_tz())

                duration_seconds = (now_dt - downtime_start_dt).total_seconds()
                if duration_seconds >= self.min_downtime_record_seconds:
                    downtime_start_str = downtime_start_dt.strftime("%Y-%m-%d %H:%M:%S")
                    downtime_end_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")

                    await self.db.record_downtime_event(
                        downtime_type=downtime_type,
                        started_at=downtime_start_str,
                        ended_at=downtime_end_str,
                        duration_seconds=duration_seconds,
                        reason=f"{downtime_reason} ({format_duration_seconds(duration_seconds)})",
                    )
                    logger.info(
                        f"📉 [Downtime Detected] Previous downtime period recorded: {format_duration_seconds(duration_seconds)} "
                        f"({downtime_type}: from {downtime_start_str} to {downtime_end_str})"
                    )
        except Exception as e:
            logger.warning(f"Error evaluating startup downtime: {e}", exc_info=True)

    async def record_gateway_outage(
        self,
        duration_seconds: float,
        started_at: str,
        ended_at: str,
        reason: str = "Discord Gateway Reconnection",
    ) -> None:
        """Records a recovered gateway / network outage incident."""
        if duration_seconds < self.min_downtime_record_seconds:
            return

        try:
            await self.db.record_downtime_event(
                downtime_type="GATEWAY_OUTAGE",
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration_seconds,
                reason=reason,
            )
            logger.info(
                f"📉 [Gateway Outage Recorded] Duration: {format_duration_seconds(duration_seconds)} ({started_at} - {ended_at})"
            )
        except Exception as e:
            logger.warning(f"Failed to record gateway outage event in database: {e}")

    async def _heartbeat_loop(self) -> None:
        """Periodic background task updating the database heartbeat timestamp."""
        while self._is_running:
            try:
                await asyncio.sleep(self.heartbeat_interval_seconds)
                if self.db.is_connected:
                    await self.db.update_uptime_heartbeat_timestamp(now_formatted())
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Heartbeat update failed: {e}")

    async def get_sla_metrics(self) -> dict[str, Any]:
        """
        Calculates comprehensive SLA percentage scores, downtime totals, and incident history
        over 24-hour, 7-day, and 30-day operational windows.
        """
        tz = get_configured_tz()
        now_dt = datetime.now(tz)
        now_iso = now_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Windows
        w24_dt = now_dt - timedelta(hours=24)
        w7d_dt = now_dt - timedelta(days=7)
        w30d_dt = now_dt - timedelta(days=30)

        w30d_iso = w30d_dt.strftime("%Y-%m-%d %H:%M:%S")

        # Query all downtime events in the last 30 days
        events = await self.db.get_downtime_events_in_range(w30d_iso, now_iso)
        recent_events = await self.db.get_recent_downtime_events(limit=8)

        def _compute_window_downtime(window_start_dt: datetime) -> tuple[float, int]:
            total_dt_sec = 0.0
            incident_count = 0
            for ev in events:
                ev_start = parse_db_timestamp(ev["started_at"], tz=tz)
                ev_end = parse_db_timestamp(ev["ended_at"], tz=tz)
                if not ev_start or not ev_end:
                    continue

                overlap_start = max(window_start_dt, ev_start)
                overlap_end = min(now_dt, ev_end)

                if overlap_end > overlap_start:
                    overlap_sec = (overlap_end - overlap_start).total_seconds()
                    total_dt_sec += overlap_sec
                    incident_count += 1

            return total_dt_sec, incident_count

        dt_24h, count_24h = _compute_window_downtime(w24_dt)
        dt_7d, count_7d = _compute_window_downtime(w7d_dt)
        dt_30d, count_30d = _compute_window_downtime(w30d_dt)

        # Total available window seconds
        total_24h_sec = 24 * 3600.0
        total_7d_sec = 7 * 86400.0
        total_30d_sec = 30 * 86400.0

        def _calc_sla(downtime_sec: float, total_sec: float) -> float:
            avail_sec = max(0.0, total_sec - downtime_sec)
            return round((avail_sec / total_sec) * 100.0, 3)

        sla_24h = _calc_sla(dt_24h, total_24h_sec)
        sla_7d = _calc_sla(dt_7d, total_7d_sec)
        sla_30d = _calc_sla(dt_30d, total_30d_sec)

        return {
            "current_uptime_seconds": self.current_uptime_seconds,
            "current_uptime_str": self.current_uptime_str,
            "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "started_at_timestamp": int(self.started_at.timestamp()),
            "session_id": self.session_id,
            "sla_24h": {
                "sla_percent": sla_24h,
                "downtime_seconds": dt_24h,
                "downtime_str": format_duration_seconds(dt_24h),
                "incidents": count_24h,
                "grade": evaluate_sla_grade(sla_24h),
            },
            "sla_7d": {
                "sla_percent": sla_7d,
                "downtime_seconds": dt_7d,
                "downtime_str": format_duration_seconds(dt_7d),
                "incidents": count_7d,
                "grade": evaluate_sla_grade(sla_7d),
            },
            "sla_30d": {
                "sla_percent": sla_30d,
                "downtime_seconds": dt_30d,
                "downtime_str": format_duration_seconds(dt_30d),
                "incidents": count_30d,
                "grade": evaluate_sla_grade(sla_30d),
            },
            "recent_incidents": recent_events,
        }
