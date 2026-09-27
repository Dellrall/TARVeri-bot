"""
Network and Power Outage Detection and Graceful Shutdown Watchdog Service.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import TYPE_CHECKING

from tarveri.config import now_formatted

if TYPE_CHECKING:
    from discord.ext import commands

    from tarveri.database import Database

logger = logging.getLogger("tarveri")

DEFAULT_PROBE_TARGETS: tuple[tuple[str, int], ...] = (
    ("1.1.1.1", 53),  # Cloudflare DNS (Raw IP - no DNS dependency)
    ("8.8.8.8", 53),  # Google DNS (Raw IP - no DNS dependency)
    ("discord.com", 443),  # Discord HTTPS API
)


class OutageService:
    """
    Monitors network connectivity, Discord gateway disconnections, and power outage signals.
    Applies intelligent debouncing to avoid alarming logs on normal sub-second gateway resumes.
    If a continuous network/gateway outage exceeds `timeout_seconds` (default: 300s / 5 minutes),
    it automatically initiates an emergency graceful shutdown to safeguard database integrity
    and cleanly checkpoint SQLite WAL files.
    """

    def __init__(
        self,
        bot: commands.Bot,
        db: Database,
        timeout_seconds: int = 300,
        probe_interval: int = 15,
        alert_grace_seconds: int = 20,
        probe_targets: tuple[tuple[str, int], ...] | None = None,
    ):
        self.bot = bot
        self.db = db
        self.timeout_seconds = max(10, timeout_seconds)
        self.probe_interval = max(1, probe_interval)
        self.alert_grace_seconds = max(0, alert_grace_seconds)
        self.probe_targets = probe_targets or DEFAULT_PROBE_TARGETS

        self._task: asyncio.Task[None] | None = None
        self._disconnected_at: float | None = None
        self._disconnect_walltime: str | None = None
        self._alert_logged: bool = False
        self._last_progress_log_time: float = 0.0
        self._last_probe_success: bool = True
        self._last_probe_latency_ms: float | None = None
        self._last_probe_time: float | None = None
        self._shutdown_triggered: bool = False
        self._shutdown_reason: str | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def is_outage_active(self) -> bool:
        return self._disconnected_at is not None

    @property
    def alert_logged(self) -> bool:
        return self._alert_logged

    @property
    def disconnected_at(self) -> float | None:
        return self._disconnected_at

    @property
    def disconnect_duration(self) -> float:
        if self._disconnected_at is None:
            return 0.0
        return time.monotonic() - self._disconnected_at

    @property
    def last_probe_success(self) -> bool:
        return self._last_probe_success

    @property
    def last_probe_latency_ms(self) -> float | None:
        return self._last_probe_latency_ms

    async def check_connectivity(self, timeout: float = 2.0) -> tuple[bool, float | None]:
        """
        Probes internet connectivity by attempting fast socket handshakes
        to raw DNS IPs and Discord endpoints. Returns (is_reachable, latency_ms).
        """
        t0 = time.monotonic()
        for host, port in self.probe_targets:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=timeout,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except (OSError, asyncio.CancelledError) as exc:
                    logger.debug("Socket wait_closed ignored in network probe: %s", exc)
                latency_ms = (time.monotonic() - t0) * 1000.0
                self._last_probe_success = True
                self._last_probe_latency_ms = latency_ms
                self._last_probe_time = time.monotonic()
                return True, latency_ms
            except (TimeoutError, OSError):
                continue
            except Exception as e:
                logger.debug(f"Connectivity probe to {host}:{port} failed: {e}")
                continue

        self._last_probe_success = False
        self._last_probe_latency_ms = None
        self._last_probe_time = time.monotonic()
        return False, None

    def on_disconnect(self) -> None:
        """Invoked when Discord gateway connection drops."""
        if self._disconnected_at is None:
            self._disconnected_at = time.monotonic()
            self._disconnect_walltime = now_formatted()
            self._alert_logged = False
            self._last_progress_log_time = time.monotonic()
            logger.debug(
                f"Discord gateway connection dropped at {self._disconnect_walltime}. Watchdog monitoring started."
            )

    def on_reconnect(self) -> None:
        """Invoked when Discord gateway reconnects or resumes."""
        if self._disconnected_at is not None:
            downtime = time.monotonic() - self._disconnected_at
            if self._alert_logged:
                logger.info(
                    f"✅ [OutageWatchdog] Gateway reconnected after {downtime:.1f}s. "
                    f"Emergency graceful shutdown countdown cancelled."
                )
            else:
                logger.debug(f"Discord gateway reconnected/resumed after {downtime:.2f}s (normal blip).")

            if downtime >= 5.0 and hasattr(self.bot, "uptime_service"):
                uptime_svc = getattr(self.bot, "uptime_service", None)
                if uptime_svc and hasattr(uptime_svc, "record_gateway_outage"):
                    started_str = self._disconnect_walltime or now_formatted()
                    ended_str = now_formatted()
                    coro = uptime_svc.record_gateway_outage(
                        duration_seconds=downtime,
                        started_at=started_str,
                        ended_at=ended_str,
                        reason="Discord gateway disconnection / network outage",
                    )
                    if inspect.isawaitable(coro):
                        asyncio.create_task(coro, name="tarveri_record_gateway_outage")

            self._disconnected_at = None
            self._disconnect_walltime = None
            self._alert_logged = False
            self._shutdown_triggered = False

    def on_power_signal(self, sig_name: str) -> None:
        """Invoked when an OS power outage / failure signal (e.g. SIGPWR) is received."""
        logger.critical(
            f"⚡ [OutageWatchdog] OS Power Outage Signal '{sig_name}' received! "
            f"Initiating immediate emergency graceful shutdown to flush SQLite WAL and preserve state."
        )
        self._shutdown_reason = f"Power outage signal ({sig_name})"
        self._shutdown_triggered = True

    async def _watchdog_loop(self) -> None:
        """Main periodic watchdog loop checking connection and outage timeout."""
        logger.info(
            f"Outage watchdog started: {self.timeout_seconds}s timeout, {self.probe_interval}s interval, {self.alert_grace_seconds}s debounce."
        )
        try:
            while not self.bot.is_closed():
                await asyncio.sleep(self.probe_interval)

                is_disconnected = False
                if self._disconnected_at is not None:
                    is_disconnected = True
                elif hasattr(self.bot, "is_ready") and not self.bot.is_ready():
                    if self._disconnected_at is None and getattr(self.bot, "_is_ready_logged", False):
                        self.on_disconnect()
                        is_disconnected = True

                if is_disconnected and self._disconnected_at is not None:
                    elapsed = time.monotonic() - self._disconnected_at
                    remaining = max(0.0, self.timeout_seconds - elapsed)

                    reachable, latency = await self.check_connectivity(timeout=2.0)
                    probe_desc = f"Internet: {'Reachable (' + str(round(latency, 1)) + 'ms)' if reachable else 'Unreachable / Offline'}"

                    # Trigger outage warning if internet is offline OR if disconnect has persisted past alert_grace_seconds
                    should_alert = (not reachable) or (elapsed >= self.alert_grace_seconds)

                    if should_alert and not self._alert_logged:
                        self._alert_logged = True
                        net_status = (
                            "Internet Unreachable / Offline (Local Network or Power Loss)"
                            if not reachable
                            else f"Internet Online ({latency:.1f}ms) - Discord Gateway Reconnecting"
                        )
                        logger.warning(
                            f"⚠️ [OutageWatchdog] Outage detected at {self._disconnect_walltime} ({net_status}). "
                            f"Starting {remaining:.0f}s grace countdown before emergency graceful shutdown..."
                        )

                    if elapsed >= self.timeout_seconds:
                        self._shutdown_triggered = True
                        self._shutdown_reason = (
                            f"Network outage exceeded {self.timeout_seconds}s limit ({elapsed:.1f}s total downtime)"
                        )
                        logger.critical(
                            f"🛑 [OutageWatchdog] Outage persisted for {elapsed:.1f}s (limit: {self.timeout_seconds}s). "
                            f"{probe_desc}. Executing emergency graceful shutdown..."
                        )
                        try:
                            if self.db.is_connected:
                                await self.db.log(
                                    "CRITICAL",
                                    "OUTAGE_SHUTDOWN",
                                    f"Emergency graceful shutdown triggered after {elapsed:.1f}s continuous outage. {probe_desc}.",
                                )
                        except Exception as exc:
                            logger.warning("Failed logging emergency outage shutdown to db: %s", exc, exc_info=True)

                        asyncio.create_task(self.bot.close(), name="tarveri_outage_graceful_shutdown")
                        break
                    elif self._alert_logged:
                        # Log periodic progress notice every 60 seconds once alert has been raised
                        now = time.monotonic()
                        if now - self._last_progress_log_time >= 60.0:
                            self._last_progress_log_time = now
                            logger.warning(
                                f"⏳ [OutageWatchdog] Outage in progress: {elapsed:.0f}s elapsed, {remaining:.0f}s remaining "
                                f"until emergency shutdown. {probe_desc}."
                            )
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Unexpected error in outage watchdog loop: {e}", exc_info=True)

    def start(self) -> None:
        """Starts the outage watchdog background task if not already running."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watchdog_loop(), name="tarveri_outage_watchdog")

    def stop(self) -> None:
        """Stops and cancels the outage watchdog background task."""
        if self._task and not self._task.done():
            self._task.cancel()
            self._task = None
