"""
Dead Letter Queue (DLQ) service for TARVeri.
Captures, persists, retries, and audits failed asynchronous operations including:
- Student verification emails (SMTP timeouts, relay failures)
- Network and gateway outage notifications (debounced DMs, recovery alerts)
- Unhandled command/event exception alerts (hoster alerts)
- Guild administrator security alerts (#tarveri-log failures)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
import traceback
from datetime import datetime
from typing import TYPE_CHECKING, Any

import discord

from tarveri.config import Settings, mask_email, now_formatted
from tarveri.database import Database

if TYPE_CHECKING:
    from tarveri.services.email_service import EmailService

logger = logging.getLogger("tarveri")


class DeadLetterQueueService:
    """
    Manages the Dead Letter Queue:
    - Persists failed emails, outage alerts, exception alerts, and security notifications to SQLite.
    - Runs a background worker that attempts exponential-backoff retries.
    - Gracefully handles expired OTPs without sending stale codes.
    - Flushes queued outage alerts immediately once Discord connectivity is restored.
    """

    def __init__(
        self,
        bot: discord.Client,
        db: Database,
        settings: Settings,
        email_service: EmailService | None = None,
    ) -> None:
        self.bot = bot
        self.db = db
        self.settings = settings
        self.email_service = email_service
        self.retry_interval = max(10, getattr(settings, "dlq_retry_interval_seconds", 60))
        self.max_retries = max(1, getattr(settings, "dlq_max_retries", 5))

        self._task: asyncio.Task[None] | None = None
        self._wake_event = asyncio.Event()
        self._is_running = False
        self._last_purge_time: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._is_running

    def start(self) -> None:
        """Starts the background DLQ worker task."""
        if not getattr(self.settings, "enable_dead_letter_queue", True):
            logger.info("Dead Letter Queue is disabled via configuration.")
            return

        if self._task is None or self._task.done():
            self._is_running = True
            self._task = asyncio.create_task(self._worker_loop(), name="tarveri_dlq_worker")
            logger.info(
                "Dead Letter Queue (DLQ) worker started (interval: %ss, max_retries: %s).",
                self.retry_interval,
                self.max_retries,
            )

    def stop(self) -> None:
        """Stops the background DLQ worker task."""
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("Dead Letter Queue (DLQ) worker stopped.")

    def trigger_processing(self) -> None:
        """Wakes up the worker immediately (e.g. after network reconnection or on-demand)."""
        self._wake_event.set()

    # =========================================================================
    # 📥 Enqueue Helpers
    # =========================================================================

    async def enqueue_email(
        self,
        user_id: int,
        student_id: str,
        email: str,
        otp_code: str,
        server_name: str,
        ttl_minutes: int,
        expires_at: float,
        error_reason: str,
        card_expiry_date: str | None = None,
        guild_id: int | None = None,
    ) -> int:
        """Enqueues a failed verification email for retry."""
        clean_email = email.strip().lower()

        # Encrypt email if encryption key is present, preserving privacy in the DLQ table
        stored_email = clean_email
        is_encrypted = False
        if self.email_service and hasattr(self.email_service, "encrypt_student_email"):
            try:
                res = self.email_service.encrypt_student_email(clean_email)
                if isinstance(res, str):
                    stored_email = res
                    is_encrypted = True
            except Exception as e:
                logger.debug("Could not encrypt DLQ email: %s", e)

        payload = {
            "user_id": user_id,
            "student_id": student_id,
            "email": stored_email,
            "is_encrypted": is_encrypted,
            "otp_code": otp_code,
            "server_name": server_name,
            "ttl_minutes": ttl_minutes,
            "expires_at": expires_at,
            "card_expiry_date": card_expiry_date,
        }
        metadata = {
            "guild_id": guild_id,
            "user_id": user_id,
            "email_masked": mask_email(clean_email),
        }

        dlq_id = await self.db.enqueue_dead_letter(
            queue_type="EMAIL_VERIFICATION",
            payload=payload,
            error_reason=error_reason,
            max_retries=self.max_retries,
            metadata=metadata,
            next_retry_delay_seconds=30,  # fast first retry
        )
        logger.info(
            "Enqueued failed verification email to DLQ #%s for user %s (%s). Reason: %s",
            dlq_id,
            user_id,
            mask_email(clean_email),
            error_reason,
        )
        return dlq_id

    async def enqueue_outage(
        self,
        alert_type: str,
        reason: str,
        downtime_seconds: float = 0.0,
        started_at: str | None = None,
        ended_at: str | None = None,
        probe_desc: str | None = None,
        hoster_discord_id: int | None = None,
        guild_id: int | None = None,
    ) -> int:
        """Enqueues an outage alert for delivery once network/gateway recovers."""
        payload = {
            "alert_type": alert_type,
            "reason": reason,
            "downtime_seconds": downtime_seconds,
            "started_at": started_at or now_formatted(),
            "ended_at": ended_at,
            "probe_desc": probe_desc,
            "hoster_discord_id": hoster_discord_id,
        }
        metadata = {
            "guild_id": guild_id,
            "alert_type": alert_type,
        }
        dlq_id = await self.db.enqueue_dead_letter(
            queue_type="OUTAGE_ALERT",
            payload=payload,
            error_reason=reason,
            max_retries=self.max_retries,
            metadata=metadata,
            next_retry_delay_seconds=15,
        )
        logger.info("Enqueued outage alert to DLQ #%s (type: %s).", dlq_id, alert_type)
        return dlq_id

    async def enqueue_exception(
        self,
        exc: Exception,
        context: str | None = None,
        guild_id: int | None = None,
        user_id: int | None = None,
    ) -> int:
        """Enqueues an unhandled exception for hoster/admin notification."""
        exc_type = type(exc).__name__
        exc_msg = str(exc)
        tb_lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        tb_str = "".join(tb_lines)[-1800:]  # keep within Discord embed limits

        payload = {
            "exception_type": exc_type,
            "message": exc_msg,
            "traceback": tb_str,
            "context": context or "Unknown context",
            "occurred_at": now_formatted(),
            "hoster_discord_id": getattr(self.settings, "hoster_discord_id", None),
        }
        metadata = {
            "guild_id": guild_id,
            "user_id": user_id,
            "exception_type": exc_type,
        }
        dlq_id = await self.db.enqueue_dead_letter(
            queue_type="EXCEPTION_ALERT",
            payload=payload,
            error_reason=f"{exc_type}: {exc_msg}",
            max_retries=self.max_retries,
            metadata=metadata,
            next_retry_delay_seconds=30,
        )
        logger.warning("Enqueued unhandled exception to DLQ #%s (%s: %s).", dlq_id, exc_type, exc_msg)
        return dlq_id

    async def enqueue_security_alert(
        self,
        guild_id: int,
        embed_data: dict[str, Any],
        error_reason: str,
    ) -> int:
        """Enqueues a failed guild security alert embed for redelivery."""
        payload = {
            "guild_id": guild_id,
            "embed": embed_data,
        }
        metadata = {
            "guild_id": guild_id,
        }
        dlq_id = await self.db.enqueue_dead_letter(
            queue_type="SECURITY_ALERT",
            payload=payload,
            error_reason=error_reason,
            max_retries=self.max_retries,
            metadata=metadata,
            next_retry_delay_seconds=60,
        )
        logger.info("Enqueued security alert to DLQ #%s for guild %s.", dlq_id, guild_id)
        return dlq_id

    # =========================================================================
    # ⚙️ Worker Loop & Queue Processor
    # =========================================================================

    async def _worker_loop(self) -> None:
        """Periodic background retry loop."""
        while self._is_running:
            try:
                # Wait for next interval or wake trigger
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=float(self.retry_interval))
                    self._wake_event.clear()
                except TimeoutError:
                    pass

                if not self._is_running:
                    break

                await self.process_queue()

                # Daily automatic purge of old resolved/dead records
                now = time.monotonic()
                if now - self._last_purge_time >= 86400:
                    self._last_purge_time = now
                    try:
                        purge_days = getattr(self.settings, "dlq_purge_days", 30)
                        purged = await self.db.purge_dead_letters(days=purge_days)
                        if purged > 0:
                            logger.info(
                                "DLQ maintenance: purged %s expired records older than %s days.", purged, purge_days
                            )
                    except Exception as purge_err:
                        logger.debug("DLQ purge error: %s", purge_err)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Unexpected error in DLQ worker loop: %s", e, exc_info=True)
                await asyncio.sleep(5)

    async def process_queue(self, queue_type: str | None = None, force_all: bool = False) -> dict[str, int]:
        """
        Processes pending and due items from the dead letter queue.
        Returns a summary dict of processed items: {'processed': N, 'resolved': N, 'failed': N, 'dead': N}.
        """
        stats = {"processed": 0, "resolved": 0, "failed": 0, "dead": 0}
        if not self.db.is_connected:
            return stats

        pending_items = await self.db.get_pending_dead_letters(queue_type=queue_type, limit=25, due_only=not force_all)
        if not pending_items:
            return stats

        for item in pending_items:
            stats["processed"] += 1
            item_id = item["id"]
            q_type = item["queue_type"]
            payload = item["payload"] if isinstance(item["payload"], dict) else {}

            try:
                if q_type == "EMAIL_VERIFICATION":
                    res, reason = await self._process_email_item(item, payload)
                elif q_type == "OUTAGE_ALERT":
                    res, reason = await self._process_outage_item(item, payload)
                elif q_type == "EXCEPTION_ALERT":
                    res, reason = await self._process_exception_item(item, payload)
                elif q_type == "SECURITY_ALERT":
                    res, reason = await self._process_security_item(item, payload)
                else:
                    logger.warning("Unknown DLQ queue_type '%s' for item #%s", q_type, item_id)
                    res, reason = False, f"Unknown queue_type: {q_type}"

                if res:
                    await self.db.mark_dead_letter_retried(item_id, success=True, error_reason=reason or "Delivered")
                    stats["resolved"] += 1
                    logger.info("DLQ #%s (%s) successfully resolved: %s", item_id, q_type, reason)
                else:
                    new_count = item["retry_count"] + 1
                    if new_count >= item["max_retries"] or "expired" in str(reason).lower():
                        await self.db.mark_dead_letter_dead(item_id, error_reason=reason or "Max retries exceeded")
                        stats["dead"] += 1
                        logger.warning("DLQ #%s (%s) marked DEAD: %s", item_id, q_type, reason)
                    else:
                        await self.db.mark_dead_letter_retried(item_id, success=False, error_reason=reason)
                        stats["failed"] += 1
            except Exception as exc:
                logger.error("Error processing DLQ item #%s: %s", item_id, exc, exc_info=True)
                await self.db.mark_dead_letter_retried(item_id, success=False, error_reason=str(exc))
                stats["failed"] += 1

        return stats

    # =========================================================================
    # ✉️ Email Verification Processor
    # =========================================================================

    async def _process_email_item(self, item: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str]:
        """Attempts redelivery of a failed verification email."""
        if not self.email_service:
            return False, "Email service unavailable"

        expires_at = payload.get("expires_at", 0)
        # Check if the verification OTP has expired
        if time.time() > expires_at:
            return False, "Verification OTP code expired before delivery could succeed"

        to_email = payload.get("email", "")
        if payload.get("is_encrypted") and hasattr(self.email_service, "decrypt_student_email"):
            try:
                to_email = self.email_service.decrypt_student_email(to_email)
            except Exception as e:
                return False, f"Could not decrypt email from DLQ: {e}"

        otp_code = payload.get("otp_code", "")
        server_name = payload.get("server_name", "TARUMT Discord")
        ttl_minutes = payload.get("ttl_minutes", 10)
        user_id = payload.get("user_id")

        # Attempt sending email via email_service
        send_ok = False
        if hasattr(self.email_service, "_send_otp_email_async"):
            send_ok = await self.email_service._send_otp_email_async(
                to_email=to_email,
                otp_code=otp_code,
                server_name=server_name,
                ttl_minutes=ttl_minutes,
            )

        if send_ok:
            # If user does not currently have a pending OTP, restore transient state so code is valid
            if user_id and hasattr(self.email_service, "_pending_otps"):
                from tarveri.services.email_service import PendingOtp

                async with self.email_service._lock:
                    existing = self.email_service._pending_otps.get(user_id)
                    if not existing or existing.otp_code == otp_code:
                        now_m = time.monotonic()
                        rem_ttl = max(60.0, expires_at - time.time())
                        self.email_service._pending_otps[user_id] = PendingOtp(
                            user_id=user_id,
                            student_id=payload.get("student_id", ""),
                            email=to_email,
                            otp_code=otp_code,
                            card_expiry_date=payload.get("card_expiry_date"),
                            created_at=now_m,
                            expires_at=now_m + rem_ttl,
                            attempts_left=getattr(self.settings, "email_otp_max_attempts", 3),
                            last_sent_at=now_m,
                            server_name=server_name,
                        )
            return True, f"Delivered to {mask_email(to_email)}"

        last_bounce = getattr(self.email_service, "_last_bounce_info", None)
        return False, last_bounce or "SMTP transmission failed"

    # =========================================================================
    # ⚡ Outage Alert Processor
    # =========================================================================

    async def _process_outage_item(self, item: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str]:
        """Dispatches queued outage notifications once Discord gateway is reachable."""
        if not getattr(self.bot, "is_ready", lambda: False)():
            return False, "Bot is not ready or gateway is disconnected"

        alert_type = payload.get("alert_type", "OUTAGE")
        reason = payload.get("reason", "Network/gateway outage")
        downtime_sec = payload.get("downtime_seconds", 0.0)
        started_at = payload.get("started_at", "Unknown")
        ended_at = payload.get("ended_at")
        probe_desc = payload.get("probe_desc")

        if alert_type == "OUTAGE_RESOLVED":
            color = discord.Color.green()
            title = "🟢 TARVeri Outage Resolved — Service Restored"
            status_desc = f"Gateway connection and network connectivity have been restored.\n**Downtime Duration:** `{downtime_sec:.1f}s`"
        elif alert_type == "EMERGENCY_SHUTDOWN":
            color = discord.Color.red()
            title = "🛑 TARVeri Emergency Graceful Shutdown"
            status_desc = f"Continuous outage exceeded threshold. Clean shutdown initiated.\n**Total Downtime:** `{downtime_sec:.1f}s`"
        else:
            color = discord.Color.orange()
            title = "⚠️ TARVeri Network Outage Detected"
            status_desc = f"A connectivity interruption was detected.\n**Started At:** `{started_at}`"

        embed = discord.Embed(title=title, description=status_desc, color=color, timestamp=datetime.now())
        embed.add_field(name="📝 Details / Reason", value=reason, inline=False)
        if probe_desc:
            embed.add_field(name="🔍 Network Probe", value=f"`{probe_desc}`", inline=True)
        if started_at:
            embed.add_field(name="⏰ Outage Started", value=f"`{started_at}`", inline=True)
        if ended_at:
            embed.add_field(name="✅ Outage Resolved", value=f"`{ended_at}`", inline=True)
        embed.set_footer(text="TARVeri Outage Watchdog • Dead Letter Queue")

        delivered = False
        notes: list[str] = []

        # 1. Send DM to hoster
        hoster_id = payload.get("hoster_discord_id") or getattr(self.settings, "hoster_discord_id", None)
        if hoster_id:
            try:
                hoster_user = self.bot.get_user(hoster_id) or await self.bot.fetch_user(hoster_id)
                if hoster_user:
                    await hoster_user.send(embed=embed)
                    delivered = True
                    notes.append(f"DM to hoster ({hoster_id})")
            except Exception as e:
                logger.debug("Failed sending outage alert DM to hoster %s: %s", hoster_id, e)

        # 2. Also send to guild admin log channel if guild_id provided
        meta = item.get("metadata") or {}
        guild_id = meta.get("guild_id")
        if guild_id and hasattr(self.bot, "get_guild"):
            guild = self.bot.get_guild(guild_id)
            if guild:
                ch = discord.utils.get(guild.text_channels, name="tarveri-log")
                if ch:
                    try:
                        await ch.send(embed=embed)
                        delivered = True
                        notes.append(f"Channel #{ch.name}")
                    except Exception as e:
                        logger.debug("Failed sending outage alert to #tarveri-log in %s: %s", guild.name, e)

        if delivered:
            return True, f"Delivered via {', '.join(notes)}"
        return False, "Could not reach hoster or guild alert channels"

    # =========================================================================
    # 🐛 Exception Alert Processor
    # =========================================================================

    async def _process_exception_item(self, item: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str]:
        """Dispatches unhandled exception reports to hoster."""
        if not getattr(self.bot, "is_ready", lambda: False)():
            return False, "Bot is not ready"

        exc_type = payload.get("exception_type", "Exception")
        msg = payload.get("message", "Unknown error")
        tb = payload.get("traceback", "")
        context = payload.get("context", "Unknown")
        occurred_at = payload.get("occurred_at", "Unknown")

        embed = discord.Embed(
            title=f"🚨 TARVeri Exception Alert: `{exc_type}`",
            description=f"**Context:** `{context}`\n**Message:** {msg}",
            color=discord.Color.red(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="⏰ Occurred At", value=f"`{occurred_at}`", inline=True)
        if tb:
            embed.add_field(
                name="📋 Traceback (Truncated)",
                value=f"```python\n{tb[-950:]}\n```",
                inline=False,
            )
        embed.set_footer(text="TARVeri System Guard • Dead Letter Queue")

        hoster_id = payload.get("hoster_discord_id") or getattr(self.settings, "hoster_discord_id", None)
        if not hoster_id:
            return True, "No hoster_discord_id configured; exception logged in DLQ"

        try:
            hoster_user = self.bot.get_user(hoster_id) or await self.bot.fetch_user(hoster_id)
            if hoster_user:
                await hoster_user.send(embed=embed)
                return True, f"Delivered exception alert DM to hoster ({hoster_id})"
        except Exception as e:
            return False, f"Failed sending DM to hoster: {e}"

        return False, "Hoster user not found"

    # =========================================================================
    # 🛡️ Security Alert Processor
    # =========================================================================

    async def _process_security_item(self, item: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, str]:
        """Re-attempts dispatching security alert embed to guild #tarveri-log."""
        if not getattr(self.bot, "is_ready", lambda: False)():
            return False, "Bot is not ready"

        guild_id = payload.get("guild_id")
        embed_dict = payload.get("embed")
        if not guild_id or not embed_dict:
            return False, "Missing guild_id or embed payload"

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return False, f"Guild {guild_id} not available"

        embed = discord.Embed.from_dict(embed_dict)
        ch = discord.utils.get(guild.text_channels, name="tarveri-log")
        if not ch:
            # Try to auto-create or find via verification service
            return False, "Security log channel #tarveri-log not found in guild"

        try:
            res = ch.send(embed=embed)
            if inspect.isawaitable(res):
                await res
            return True, f"Delivered security alert to #{ch.name} in {guild.name}"
        except Exception as e:
            return False, f"Failed posting to #{ch.name}: {e}"

    # =========================================================================
    # 🩺 Health & Diagnostic Reporting
    # =========================================================================

    async def get_health_status(self) -> dict[str, Any]:
        """Returns DLQ health summary for diagnostics and dashboard embeds."""
        stats = await self.db.get_dead_letter_stats()
        dead_count = stats.get("dead", 0)
        pending_count = stats.get("pending", 0)

        is_healthy = dead_count == 0
        warning = None
        if dead_count > 0:
            warning = f"⚠️ {dead_count} unresolvable item(s) in Dead Letter Queue (inspect with `/admin dlq list status:DEAD`)."
        elif pending_count > 10:
            warning = f"⏳ {pending_count} pending items in Dead Letter Queue awaiting retry."

        return {
            "healthy": is_healthy,
            "warning": warning,
            "stats": stats,
            "worker_running": self._is_running,
        }
