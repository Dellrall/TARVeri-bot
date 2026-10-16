"""
Interactive Console CLI Service for TARVeri (Minecraft-style Terminal Backend).

Allows server administrators and bot hosters to execute administrative commands,
trigger verifications, inspect members, generate database snapshots, and monitor
system health directly from the terminal or tmux prompt.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
import threading
from typing import TYPE_CHECKING, Any

import discord

from tarveri.config import hash_student_id, parse_student_id

if TYPE_CHECKING:
    from tarveri.bot import TARVeriBot
    from tarveri.config import Settings
    from tarveri.database import Database
    from tarveri.services.verification_service import VerificationService

logger = logging.getLogger("tarveri.console")


class ConsoleService:
    """Provides an interactive CLI prompt on stdin for host administration."""

    def __init__(
        self,
        bot: TARVeriBot,
        db: Database,
        service: VerificationService,
        settings: Settings,
    ) -> None:
        self.bot = bot
        self.db = db
        self.service = service
        self.settings = settings
        self.running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.prompt = "tarveri> "

    def start(self) -> None:
        """Starts the interactive console thread if running in an interactive terminal."""
        if not getattr(self.settings, "enable_console", True):
            logger.info("[Console] Terminal console disabled via configuration.")
            return

        # Check if stdin is an interactive TTY. If running as systemd daemon or piped, disable CLI.
        if sys.stdin is None or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
            logger.info("[Console] Non-interactive environment detected (systemd/pipe). Terminal CLI disabled.")
            return

        self.running = True
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name="tarveri_console_reader",
        )
        self._thread.start()
        print("\n" + "=" * 60)
        print("🎮 TARVeri Minecraft-style Console CLI initialized!")
        print("Type 'help' or '?' for available console commands.")
        print("=" * 60 + "\n")
        self._print_prompt()

    def stop(self) -> None:
        """Flags the console thread to stop."""
        self.running = False

    def _print_prompt(self) -> None:
        try:
            sys.stdout.write(f"\r{self.prompt}")
            sys.stdout.flush()
        except Exception:
            pass

    def _reader_loop(self) -> None:
        """Runs in a daemon thread, reading stdin line by line."""
        while self.running:
            try:
                line = sys.stdin.readline()
                if not line:
                    # Received EOF (Ctrl+D or closed stdin)
                    logger.info("[Console] EOF received on stdin. Terminal CLI stopped.")
                    break

                cleaned = line.strip()
                if not cleaned:
                    self._print_prompt()
                    continue

                if self._loop and self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self.handle_command(cleaned), self._loop
                    )
                    # Wait briefly for execution so output prints nicely before next prompt
                    try:
                        future.result(timeout=30)
                    except Exception as e:
                        print(f"\n[ERROR] Command failed: {e}")
                else:
                    break

                self._print_prompt()
            except Exception as e:
                logger.debug(f"[Console] Error reading stdin: {e}")
                break

    async def handle_command(self, raw_line: str) -> None:
        """Parses and executes a console command line."""
        try:
            parts = shlex.split(raw_line)
        except ValueError as exc:
            print(f"[ERROR] Syntax error parsing command line: {exc}")
            return

        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]

        try:
            if cmd in ("help", "?"):
                self._cmd_help()
            elif cmd == "status":
                await self._cmd_status()
            elif cmd == "guilds":
                await self._cmd_guilds()
            elif cmd == "stats":
                await self._cmd_stats()
            elif cmd == "lookup":
                await self._cmd_lookup(args)
            elif cmd == "verify":
                await self._cmd_verify(args)
            elif cmd == "unverify":
                await self._cmd_unverify(args)
            elif cmd == "backup":
                await self._cmd_backup(args)
            elif cmd == "say":
                await self._cmd_say(args)
            elif cmd == "broadcast":
                await self._cmd_broadcast(args)
            elif cmd in ("stop", "exit", "quit"):
                await self._cmd_stop()
            elif cmd == "eval":
                await self._cmd_eval(args)
            else:
                print(f"[WARN] Unknown command '{cmd}'. Type 'help' for available commands.")
        except Exception as e:
            print(f"[ERROR] Unhandled exception during command '{cmd}': {e}")
            logger.exception("Console command error")

    # ==========================================
    # Command Implementations
    # ==========================================

    def _cmd_help(self) -> None:
        help_text = """
======================= TARVeri Console Commands =======================
  help, ?                         Show this command reference
  status                          View bot latency, uptime, guilds, memory
  guilds                          List all connected Discord servers
  stats                           Show verification metrics and faculty counts
  lookup <id_or_student_id>       Inspect member verification profile
  verify <student_id> <user_id>   Force-verify a user directly
  unverify <user_id> [reason]     Revoke verification and strip roles
  backup [subfolder]              Trigger immediate SQLite snapshot backup
  say <guild_id> <chan_id> <msg>  Send message to a channel as TARVeri
  broadcast <message>             Send announcement to all configured channels
  eval <code>                     Execute Python expression/statement
  stop, exit, quit                Gracefully shut down TARVeri
========================================================================
"""
        print(help_text.strip())

    async def _cmd_status(self) -> None:
        latency_ms = round(self.bot.latency * 1000, 1) if self.bot.latency else 0
        guilds_count = len(self.bot.guilds)
        total_members = sum(g.member_count or 0 for g in self.bot.guilds)
        
        # Memory usage
        mem_mb = 0.0
        try:
            import resource
            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # On Linux ru_maxrss is in KiB
            mem_mb = round(usage / 1024, 2)
        except Exception:
            pass

        uptime_str = "N/A"
        if hasattr(self.bot, "uptime_service") and self.bot.uptime_service:
            sec = int(self.bot.uptime_service.get_uptime_seconds())
            hours, rem = divmod(sec, 3600)
            mins, secs = divmod(rem, 60)
            uptime_str = f"{hours}h {mins}m {secs}s"

        print(f"[STATUS] Bot User: {self.bot.user} (ID: {self.bot.user.id if self.bot.user else 'N/A'})")
        print(f"[STATUS] WebSocket Ping: {latency_ms} ms | Memory: ~{mem_mb} MB")
        print(f"[STATUS] Connected Guilds: {guilds_count} | Total Members: {total_members}")
        print(f"[STATUS] Continuous Uptime: {uptime_str}")
        print(f"[STATUS] Database: {self.settings.db_path} (Connected: {self.db.is_connected})")

    async def _cmd_guilds(self) -> None:
        if not self.bot.guilds:
            print("[INFO] No connected guilds.")
            return

        print(f"\n--- Connected Guilds ({len(self.bot.guilds)}) ---")
        for g in self.bot.guilds:
            owner_tag = str(g.owner) if g.owner else "Unknown"
            print(f"• [{g.id}] {g.name:<30} | Members: {g.member_count:<5} | Owner: {owner_tag}")
        print("------------------------------------------------")

    async def _cmd_stats(self) -> None:
        try:
            total = await self.db.get_total_verifications()
            alumni = await self.db.get_total_alumni()
            active_students = total - alumni
            faculty_counts = await self.db.get_verifications_count_by_faculty()
            
            print(f"\n--- Verification Statistics ---")
            print(f"• Total Verified Records: {total}")
            print(f"• Active Students:        {active_students}")
            print(f"• Verified Alumni:        {alumni}")
            print("• Faculty Distribution:")
            for fac, count in faculty_counts:
                print(f"    - {fac:<6}: {count}")
            print("-------------------------------")
        except Exception as e:
            print(f"[ERROR] Failed to fetch stats: {e}")

    async def _cmd_lookup(self, args: list[str]) -> None:
        if not args:
            print("[WARN] Usage: lookup <discord_user_id | student_id>")
            return

        query = args[0].strip()
        details: dict[str, Any] | None = None
        user_id: int | None = None

        if query.isdigit() and len(query) >= 16:
            user_id = int(query)
            details = await self.db.get_verification_details(user_id)
        else:
            # Assume student ID
            id_hash = hash_student_id(query, self.settings.id_hash_secret)
            res = await self.db.get_verification_by_id_hash(id_hash)
            if res:
                user_id = res[0]
                details = await self.db.get_verification_details(user_id)

        if not details or user_id is None:
            print(f"[INFO] No verification record found for query '{query}'.")
            return

        user_obj = self.bot.get_user(user_id)
        tag = str(user_obj) if user_obj else f"User {user_id}"

        print(f"\n--- Verification Profile: {tag} ---")
        print(f"• Discord User ID:  {user_id}")
        print(f"• Faculty:          {details.get('faculty_code')}")
        print(f"• Campus:           {details.get('campus_code', 'W')}")
        print(f"• Study Level:      {details.get('level_code', 'R')}")
        print(f"• Programme Code:   {details.get('programme_code', 'N/A')}")
        print(f"• Expiry Date:      {details.get('card_expiry_date', 'N/A')}")
        print(f"• Is Alumni:        {details.get('is_alumni', False)}")
        print(f"• Verified At:      {details.get('verified_at')}")
        print("------------------------------------------")

    async def _cmd_verify(self, args: list[str]) -> None:
        if len(args) < 2:
            print("[WARN] Usage: verify <student_id> <discord_user_id>")
            return

        raw_student_id = args[0].strip()
        user_id_raw = args[1].strip()

        if not user_id_raw.isdigit():
            print(f"[ERROR] Invalid Discord user ID '{user_id_raw}'. Must be numeric.")
            return

        user_id = int(user_id_raw)
        parsed = parse_student_id(raw_student_id)
        if not parsed.is_valid:
            print(f"[ERROR] Invalid student ID '{raw_student_id}'. Example format: 23WMD09867")
            return

        user = self.bot.get_user(user_id)
        if not user:
            try:
                user = await self.bot.fetch_user(user_id)
            except Exception:
                user = None

        if not user:
            print(f"[ERROR] Could not resolve Discord user with ID {user_id}.")
            return

        print(f"[INFO] Initiating console verification for {user} with ID {parsed.student_id}...")
        res = await self.service.perform_verification(
            user=user,
            raw_student_id=parsed.student_id,
        )
        print(f"[RESULT] {res}")

    async def _cmd_unverify(self, args: list[str]) -> None:
        if not args:
            print("[WARN] Usage: unverify <discord_user_id> [reason]")
            return

        user_id_raw = args[0].strip()
        if not user_id_raw.isdigit():
            print(f"[ERROR] Invalid Discord user ID '{user_id_raw}'. Must be numeric.")
            return

        user_id = int(user_id_raw)
        reason = " ".join(args[1:]) if len(args) > 1 else "Unverified via host console"

        res = await self.service.unverify_member(
            user_id=user_id,
            admin=self.bot.user,
            current_guild=None,
            reason=reason,
        )
        if res.get("success"):
            print(f"[SUCCESS] User {user_id} unverified cleanly. Roles stripped across {res.get('guilds_updated', 0)} guild(s).")
        else:
            print(f"[WARN] User {user_id} was not verified or could not be unverified.")

    async def _cmd_backup(self, args: list[str]) -> None:
        subfolder = args[0] if args else "daily"
        print(f"[INFO] Creating point-in-time database snapshot in backups/{subfolder}...")
        try:
            path = await self.db.create_backup(subfolder=subfolder)
            print(f"[SUCCESS] Database backup saved to: {path}")
        except Exception as e:
            print(f"[ERROR] Database backup failed: {e}")

    async def _cmd_say(self, args: list[str]) -> None:
        if len(args) < 3:
            print("[WARN] Usage: say <guild_id> <channel_id> <message>")
            return

        g_id_raw, c_id_raw = args[0], args[1]
        if not g_id_raw.isdigit() or not c_id_raw.isdigit():
            print("[ERROR] guild_id and channel_id must be integers.")
            return

        guild = self.bot.get_guild(int(g_id_raw))
        if not guild:
            print(f"[ERROR] Bot is not in guild ID {g_id_raw}.")
            return

        channel = guild.get_channel(int(c_id_raw))
        if not channel or not isinstance(channel, discord.TextChannel):
            print(f"[ERROR] TextChannel {c_id_raw} not found in guild {guild.name}.")
            return

        msg = " ".join(args[2:])
        try:
            await channel.send(msg)
            print(f"[SUCCESS] Message sent to #{channel.name} in {guild.name}.")
        except Exception as e:
            print(f"[ERROR] Failed to send message: {e}")

    async def _cmd_broadcast(self, args: list[str]) -> None:
        if not args:
            print("[WARN] Usage: broadcast <message>")
            return

        msg = " ".join(args)
        sent = 0
        failed = 0
        for guild in self.bot.guilds:
            target_chan = None
            settings = await self.db.get_guild_settings(guild.id)
            if settings and settings.get("welcome_channel_id"):
                target_chan = guild.get_channel(settings["welcome_channel_id"])
            if not target_chan:
                target_chan = guild.system_channel
            if not target_chan:
                for c in guild.text_channels:
                    if c.permissions_for(guild.me).send_messages:
                        target_chan = c
                        break

            if target_chan and isinstance(target_chan, discord.TextChannel):
                try:
                    await target_chan.send(f"📢 **System Broadcast:** {msg}")
                    sent += 1
                except Exception:
                    failed += 1

        print(f"[SUCCESS] Broadcast dispatched to {sent} guild(s). ({failed} failed)")

    async def _cmd_eval(self, args: list[str]) -> None:
        if not args:
            print("[WARN] Usage: eval <expression>")
            return

        code = " ".join(args)
        scope = {
            "bot": self.bot,
            "db": self.db,
            "service": self.service,
            "settings": self.settings,
            "discord": discord,
            "asyncio": asyncio,
        }
        try:
            result = eval(code, scope)
            if asyncio.iscoroutine(result):
                result = await result
            print(f"[EVAL] => {result!r}")
        except Exception as e:
            print(f"[EVAL ERROR] {type(e).__name__}: {e}")

    async def _cmd_stop(self) -> None:
        print("[INFO] Shutting down bot gracefully from console...")
        self.stop()
        await self.bot.close()
