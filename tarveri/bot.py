"""
Bot class, lifecycle orchestration, and main runner.
"""

from __future__ import annotations

import asyncio
import logging
import signal

import discord
from discord.ext import commands

from tarveri.cogs.admin_cog import AdminCog, MassRevocationApprovalView
from tarveri.cogs.card_cog import CardCog
from tarveri.cogs.guest_cog import (
    GuestCog,
    GuestReviewThreadView,
    VerificationGatewayView,
)
from tarveri.cogs.verification_cog import (
    StudentLifecycleResolutionView,
    VerificationCog,
)
from tarveri.config import Settings, setup_logger
from tarveri.database import Database
from tarveri.rate_limiter import RateLimiter
from tarveri.services.card_service import CardService
from tarveri.services.email_service import EmailService
from tarveri.services.graduation_watchdog_service import GraduationWatchdogService
from tarveri.services.guest_service import GuestService
from tarveri.services.log_service import LogRotationService
from tarveri.services.outage_service import OutageService
from tarveri.services.storage_guard_service import StorageGuardService
from tarveri.services.update_checker import UpdateCheckerService
from tarveri.services.verification_service import VerificationService

logger = logging.getLogger("tarveri")


class TARVeriBot(commands.Bot):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.db = Database(settings.db_path)
        self.rate_limiter = RateLimiter(
            max_attempts=settings.rate_limit_max_attempts,
            window_seconds=settings.rate_limit_window_seconds,
        )
        self.email_service = EmailService(
            settings=settings,
        )
        self.service = VerificationService(
            bot=self,
            db=self.db,
            secret=settings.id_hash_secret,
            rate_limiter=self.rate_limiter,
            settings=settings,
            email_service=self.email_service,
        )
        self.guest_service = GuestService(
            bot=self,
            db=self.db,
            admin_role_name=settings.admin_role_name,
            rate_limiter=self.rate_limiter,
        )
        self.card_service = CardService(
            db=self.db,
            admin_role_name=settings.admin_role_name,
        )
        self.graduation_watchdog = (
            GraduationWatchdogService(
                bot=self,
                db=self.db,
                verification_service=self.service,
                interval_hours=settings.graduation_check_interval_hours,
                prompt_cooldown_days=settings.graduation_prompt_cooldown_days,
            )
            if settings.enable_graduation_watchdog
            else None
        )
        self.update_checker = (
            UpdateCheckerService(
                bot=self,
                db=self.db,
                hoster_discord_id=settings.hoster_discord_id,
                interval_hours=settings.update_check_interval_hours,
                update_stream=settings.update_stream,
            )
            if settings.enable_update_checker
            else None
        )
        self.outage_service = (
            OutageService(
                bot=self,
                db=self.db,
                timeout_seconds=settings.outage_timeout_seconds,
                probe_interval=settings.outage_probe_interval_seconds,
                alert_grace_seconds=settings.outage_alert_grace_seconds,
            )
            if settings.enable_outage_watchdog
            else None
        )
        self.log_rotator = (
            LogRotationService(
                logs_dir=settings.logs_dir,
                older_than_days=settings.log_archive_days,
                tz_name=settings.timezone_name,
            )
            if settings.enable_log_rotator
            else None
        )
        self.storage_guard = (
            StorageGuardService(
                db=self.db,
                settings=settings,
                bot=self,
            )
            if settings.enable_storage_guard
            else None
        )
        self._is_ready_logged = False
        self._cmd_sync_task: asyncio.Task[None] | None = None

    async def _sync_commands_background(self) -> None:
        """Asynchronously syncs application commands without blocking gateway connection."""
        try:
            synced = await self.tree.sync()
            logger.info(f"Command tree synced successfully ({len(synced)} commands).")
        except Exception as e:
            logger.warning(f"Background application command sync encountered a non-fatal error: {e}")

    async def setup_hook(self) -> None:
        """Initializes database and registers cogs and persistent views during bot startup."""
        await self.db.connect()

        # Add cogs
        await self.add_cog(
            VerificationCog(
                bot=self,
                db=self.db,
                service=self.service,
                rate_limiter=self.rate_limiter,
                settings=self.settings,
                guest_service=self.guest_service,
                email_service=self.email_service,
            )
        )
        await self.add_cog(
            AdminCog(
                bot=self,
                db=self.db,
                service=self.service,
                rate_limiter=self.rate_limiter,
                admin_role_name=self.settings.admin_role_name,
                update_checker=self.update_checker,
                log_rotator=self.log_rotator,
                guest_service=self.guest_service,
            )
        )
        await self.add_cog(
            GuestCog(
                bot=self,
                db=self.db,
                guest_service=self.guest_service,
                verification_service=self.service,
            )
        )
        await self.add_cog(
            CardCog(
                bot=self,
                db=self.db,
                card_service=self.card_service,
                verification_service=self.service,
            )
        )

        # Register persistent views so buttons work across bot reboots
        self.add_view(VerificationGatewayView(self.service, self.guest_service))
        self.add_view(GuestReviewThreadView(self.guest_service))
        self.add_view(StudentLifecycleResolutionView(self.service, self.db))
        self.add_view(MassRevocationApprovalView(self.service))

        # Launch non-blocking background command sync so bot connects to gateway immediately
        self._cmd_sync_task = asyncio.create_task(
            self._sync_commands_background(), name="tarveri_cmd_sync"
        )
        logger.info("Database connected, cogs loaded, and persistent views registered.")

        if self.update_checker:
            self.update_checker.start()

        if self.guest_service:
            self.guest_service.start_escalation_task()

        if self.outage_service:
            self.outage_service.start()

        if self.log_rotator:
            self.log_rotator.start()

        if self.graduation_watchdog:
            self.graduation_watchdog.start()

        if self.storage_guard:
            self.storage_guard.start()

    async def on_disconnect(self) -> None:
        logger.debug("Discord gateway connection lost (disconnect event).")
        if self.outage_service:
            self.outage_service.on_disconnect()

    async def on_resumed(self) -> None:
        logger.debug("Discord gateway session successfully resumed.")
        if self.outage_service:
            self.outage_service.on_reconnect()

    async def on_connect(self) -> None:
        logger.debug("Discord gateway connected.")
        if self.outage_service:
            self.outage_service.on_reconnect()

    async def on_ready(self) -> None:
        if self.outage_service:
            self.outage_service.on_reconnect()

        if self.user and not self._is_ready_logged:
            self._is_ready_logged = True
            total_verified = await self.db.total_verified()
            guild_names = [g.name for g in self.guilds]
            await self.db.log(
                "INFO",
                "STARTUP",
                f"Logged in as {self.user} (ID: {self.user.id}) | Connected to {len(self.guilds)} server(s): {guild_names} | Total verified students: {total_verified}",
            )
            logger.info(
                f"TARVeri ready: Logged in as {self.user} (ID: {self.user.id}) | Servers: {len(self.guilds)}"
            )

            # Run startup diagnostics and self-healing across connected guilds
            async def _startup_self_healing() -> None:
                try:
                    for guild in self.guilds:
                        # 1. Restore faculty SRC roles if missing
                        if self.service:
                            await self.service.restore_src_roles(guild)

                        # 2. Deduplicate faculty and guest roles (migrate members & cleanup redundant roles)
                        if self.service:
                            await self.service.reconcile_duplicate_roles(guild)

                        # 3. Run permission and hierarchy diagnostics
                        if self.service:
                            warnings = self.service.diagnose_guild_permissions(guild)
                            for w in warnings:
                                logger.warning(f"[{guild.name}] Diagnostic Warning: {w}")
                                await self.db.log(
                                    "WARNING", "HIERARCHY_DIAGNOSTIC", f"[{guild.name}] {w}", guild=guild
                                )

                        # 4. Reconcile verified member roles and graduated alumni roles
                        if self.service:
                            await self.service.reconcile_verified_members(guild)
                            await self.service.reconcile_alumni_members(guild)

                    # 5. Reconcile guest tickets and downtime events
                    if self.guest_service:
                        await self.guest_service.reconcile_downtime_state()
                except Exception as e:
                    logger.error(f"Error during startup self-healing: {e}", exc_info=True)

            asyncio.create_task(_startup_self_healing(), name="tarveri_startup_self_healing")

    async def close(self) -> None:
        """Gracefully tears down the bot, logs shutdown, and flushes SQLite WAL."""
        logger.info("Initiating graceful shutdown...")

        if self._cmd_sync_task and not self._cmd_sync_task.done():
            self._cmd_sync_task.cancel()

        if self.outage_service:
            self.outage_service.stop()

        if self.storage_guard:
            self.storage_guard.stop()

        if self.log_rotator:
            self.log_rotator.stop()

        if self.update_checker:
            self.update_checker.stop()

        if self.guest_service:
            self.guest_service.stop_escalation_task()

        if self.graduation_watchdog:
            self.graduation_watchdog.stop()

        try:
            if self.db.is_connected:
                await self.db.log("INFO", "SHUTDOWN", "TARVeri is shutting down gracefully.")
        except Exception as e:
            logger.warning(f"Could not log shutdown to DB: {e}")

        try:
            await self.db.close()
            logger.info("Database connection closed cleanly with WAL checkpoint.")
        except Exception as e:
            logger.error(f"Error while closing database: {e}")

        await super().close()


async def run_bot(settings: Settings | None = None) -> None:
    """Entry point for running the bot with OS signal handlers."""
    if settings is None:
        settings = Settings.from_env()

    setup_logger(
        log_file=settings.log_file,
        max_bytes=settings.log_max_bytes,
        backup_count=settings.log_backup_count,
        tz_name=settings.timezone_name,
        logs_dir=settings.logs_dir,
    )

    if settings.sentry_dsn:
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=settings.sentry_dsn,
                traces_sample_rate=0.1,
                profiles_sample_rate=0.1,
            )
            logger.info("🚨 Sentry real-time crash and error telemetry initialized.")
        except Exception as e:
            logger.warning(f"Failed to initialize Sentry SDK: {e}")

    bot = TARVeriBot(settings)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_signal(sig: int) -> None:
        if not stop_event.is_set():
            stop_event.set()
            try:
                sig_name = signal.Signals(sig).name
            except (ValueError, AttributeError):
                sig_name = str(sig)

            if sig_name == "SIGPWR":
                logger.critical("⚡ Power failure / outage signal (SIGPWR) received. Flushing SQLite WAL and shutting down gracefully...")
                if bot.outage_service:
                    bot.outage_service.on_power_signal(sig_name)
            else:
                logger.info(f"Signal {sig_name} received. Closing TARVeri gracefully...")
            asyncio.create_task(bot.close())

    signals_to_handle = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        signals_to_handle.append(signal.SIGHUP)
    if hasattr(signal, "SIGPWR"):
        signals_to_handle.append(signal.SIGPWR)

    for sig in signals_to_handle:
        try:
            loop.add_signal_handler(sig, lambda s=sig: handle_signal(s))
        except (NotImplementedError, RuntimeError) as exc:
            logger.debug("Signal handler registration skipped for %s: %s", sig, exc)

    async with bot:
        await bot.start(settings.bot_token)
