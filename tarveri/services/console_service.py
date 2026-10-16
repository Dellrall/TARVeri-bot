"""
Interactive Console CLI Service for TARVeri (Minecraft-style Terminal Backend).

Allows server administrators and bot hosters to execute administrative commands,
trigger verifications, inspect members, generate database snapshots, and monitor
system health directly from the terminal or tmux prompt with full scrollback support.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord

from tarveri.config import ConsoleAwareStreamHandler, hash_student_id, parse_student_id

if TYPE_CHECKING:
    from tarveri.bot import TARVeriBot
    from tarveri.config import Settings
    from tarveri.database import Database
    from tarveri.services.verification_service import VerificationService

logger = logging.getLogger("tarveri.console")


AVAILABLE_COMMANDS = [
    "help",
    "status",
    "guilds",
    "stats",
    "lookup",
    "verify",
    "unverify",
    "backup",
    "say",
    "broadcast",
    "eval",
    "clear",
    "stop",
    "exit",
    "quit",
]

SUBCOMMAND_OPTIONS: dict[str, list[str]] = {
    "stats": ["overview", "faculties"],
    "backup": ["snapshot", "manual"],
}

FACULTY_CODES = [
    "FOCS",
    "FAFB",
    "FOET",
    "FOAS",
    "FOBE",
    "FCCI",
    "FSSH",
    "CPUS",
]

HELP_LINES = [
    "======================= TARVeri Console Commands =======================",
    "  help, ?                         Show this interactive help reference",
    "  status                          View bot latency, uptime, guilds, memory",
    "  guilds                          List all connected Discord servers",
    "  stats [overview|faculties]      Show verification metrics and faculty counts",
    "  lookup <id_or_student_id>       Inspect member verification profile",
    "  verify <student_id> <user_id>   Force-verify a user directly",
    "  unverify <user_id> [reason]     Revoke verification and strip roles",
    "  backup [subfolder]              Trigger immediate SQLite snapshot backup",
    "  say <guild_id> <chan_id> <msg>  Send message to a channel as TARVeri",
    "  broadcast <message>             Send announcement to all configured channels",
    "  eval <code>                     Execute Python expression/statement",
    "  clear                           Clear terminal screen",
    "  stop, exit, quit                Gracefully shut down TARVeri",
    "========================================================================",
]


def _console_completer(text: str, state: int) -> str | None:
    try:
        import readline

        buf = readline.get_line_buffer()
        begidx = readline.get_begidx()
        text_before = buf[:begidx].strip()
        try:
            words = shlex.split(text_before)
        except ValueError:
            words = text_before.split()
    except Exception:
        words = []

    matches: list[str] = []
    if not words:
        matches = [c for c in AVAILABLE_COMMANDS if c.startswith(text.lower())]
    elif len(words) == 1 and words[0] in SUBCOMMAND_OPTIONS:
        matches = [opt for opt in SUBCOMMAND_OPTIONS[words[0]] if opt.startswith(text.lower())]
    elif words[0] == "verify" and len(words) == 2:
        matches = [f for f in FACULTY_CODES if f.startswith(text.upper())]

    if state < len(matches):
        return matches[state]
    return None


class ConsoleService:
    """Provides an interactive CLI prompt for TARVeri without disrupting terminal scrollback."""

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

    def write_output(self, text: str) -> None:
        """Outputs text cleanly without redisplaying prompt mid-command."""
        stream = sys.stdout
        if stream is None or not hasattr(stream, "isatty") or not stream.isatty():
            print(text)
            return

        for line in str(text).splitlines():
            stream.write(f"{line}\n")
        stream.flush()

    def start(self) -> None:
        """Starts the interactive console thread if running in an interactive terminal."""
        if not getattr(self.settings, "enable_console", True):
            logger.info("[Console] Terminal console disabled via configuration.")
            return

        # Check if stdin is an interactive TTY. If running as systemd daemon or piped, disable CLI.
        if sys.stdin is None or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
            logger.info(
                "[Console] Non-interactive environment detected (systemd/pipe). Terminal CLI disabled."
            )
            return

        # Reset any lingering scroll margins from prior runs
        if sys.stdout is not None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            sys.stdout.write("\033[r")
            sys.stdout.flush()

        # Configure tab-completion and GNU readline history
        try:
            import readline

            readline.set_completer(_console_completer)
            readline.set_completer_delims(" \t\n")
            readline.parse_and_bind("tab: complete")
            try:
                readline.set_auto_history(True)
            except Exception as exc:
                logger.debug("Readline auto history skipped: %s", exc)

            history_file = Path("logs/.tarveri_history")
            if history_file.exists():
                try:
                    readline.read_history_file(str(history_file))
                except Exception as exc:
                    logger.debug("Readline history read skipped: %s", exc)
        except Exception as exc:
            logger.debug("Readline binding skipped: %s", exc)

        self.running = True
        self._loop = asyncio.get_running_loop()

        self._thread = threading.Thread(
            target=self._reader_loop,
            daemon=True,
            name="tarveri_console_reader",
        )
        self._thread.start()
        self.write_output("🎮 TARVeri Console initialized. Type 'help' for commands.")

    def stop(self) -> None:
        """Flags the console thread to stop and restores normal terminal state."""
        self.running = False
        ConsoleAwareStreamHandler._prompt_active = False
        try:
            import readline

            history_file = Path("logs/.tarveri_history")
            history_file.parent.mkdir(parents=True, exist_ok=True)
            readline.set_history_length(1000)
            readline.write_history_file(str(history_file))
        except Exception as exc:
            logger.debug("Readline history write skipped: %s", exc)
        if sys.stdout is not None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            sys.stdout.write("\033[r\n")
            sys.stdout.flush()

    def _show_escable_help(self) -> None:
        """Displays help menu in an alternate screen buffer dismissable with ESC, 'q', or Enter."""
        if not (
            sys.stdin
            and hasattr(sys.stdin, "isatty")
            and sys.stdin.isatty()
            and sys.stdout
            and hasattr(sys.stdout, "isatty")
            and sys.stdout.isatty()
        ):
            self._cmd_help()
            return

        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            # Enter alternate screen buffer, clear screen, hide cursor
            sys.stdout.write("\033[?1049h\033[H\033[2J\033[?25l")
            content = "\n".join(HELP_LINES)
            sys.stdout.write(
                content + "\n\n  \033[90m[ Press ESC, 'q', or Enter to return to console ]\033[0m\n"
            )
            sys.stdout.flush()

            tty.setcbreak(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("\x1b", "q", "Q", "\r", "\n", " "):
                    break
        except Exception as exc:
            logger.debug("Escable help notice: %s", exc)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception as exc:
                logger.debug("Terminal settings restore skipped: %s", exc)
            # Show cursor, restore primary screen buffer
            sys.stdout.write("\033[?25h\033[?1049l")
            sys.stdout.flush()

    def _reader_loop(self) -> None:
        """Runs in a daemon thread, reading stdin line by line with GNU readline support."""
        while self.running:
            try:
                # Render divider bar above bottom prompt if interactive TTY
                if sys.stdout is not None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
                    cols, _ = shutil.get_terminal_size()
                    sys.stdout.write(f"\033[36m{'─' * cols}\033[0m\n")
                    sys.stdout.flush()

                ConsoleAwareStreamHandler._prompt_active = True
                try:
                    line = input(self.prompt)
                finally:
                    ConsoleAwareStreamHandler._prompt_active = False

                cleaned = line.strip()
                if not cleaned:
                    continue

                # Save to readline history so Up / Down arrow keys can scroll through history
                try:
                    import readline

                    last_item = readline.get_history_item(
                        readline.get_current_history_length()
                    )
                    if last_item != cleaned:
                        readline.add_history(cleaned)
                    history_file = Path("logs/.tarveri_history")
                    history_file.parent.mkdir(parents=True, exist_ok=True)
                    readline.set_history_length(1000)
                    readline.write_history_file(str(history_file))
                except Exception as exc:
                    logger.debug("History save notice: %s", exc)

                # Record user CMD input with timestamp into bot file logs
                logger.info("[CMD] Executed: %s", cleaned)

                # Escable help menu: rendered in alternate screen buffer so it never clutters logs or scrollback
                if cleaned.lower() in ("help", "?"):
                    self._show_escable_help()
                    continue

                if self._loop and self._loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self.handle_command(cleaned), self._loop
                    )
                    try:
                        future.result(timeout=30)
                    except Exception as e:
                        self.write_output(f"[ERROR] Command failed: {e}")
                else:
                    break
            except (EOFError, KeyboardInterrupt):
                logger.info("[Console] EOF/Interrupt received. Terminal CLI stopped.")
                break
            except Exception as e:
                logger.debug("Error reading stdin: %s", e)
                break

    async def handle_command(self, raw_line: str) -> None:
        """Parses and executes a console command line."""
        try:
            parts = shlex.split(raw_line)
        except ValueError as exc:
            self.write_output(f"[ERROR] Syntax error parsing command line: {exc}")
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
            elif cmd == "clear":
                self._cmd_clear()
            elif cmd in ("stop", "exit", "quit"):
                await self._cmd_stop()
            elif cmd == "eval":
                await self._cmd_eval(args)
            else:
                self.write_output(f"[WARN] Unknown command '{cmd}'. Type 'help' for commands.")
        except Exception as e:
            self.write_output(f"[ERROR] Unhandled exception during command '{cmd}': {e}")
            logger.exception("Console command error")

    # ==========================================
    # Command Implementations
    # ==========================================

    def _cmd_help(self) -> None:
        self.write_output("\n".join(HELP_LINES))

    def _cmd_clear(self) -> None:
        if sys.stdout is not None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()

    async def _cmd_status(self) -> None:
        latency_ms = round(self.bot.latency * 1000, 1) if self.bot.latency else 0
        guilds_count = len(self.bot.guilds)
        total_members = sum(g.member_count or 0 for g in self.bot.guilds)

        mem_mb = 0.0
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            mem_mb = round(usage / 1024, 2)
        except Exception as exc:
            logger.debug("Resource usage retrieval skipped: %s", exc)

        uptime_str = "N/A"
        if hasattr(self.bot, "uptime_service") and self.bot.uptime_service:
            sec = int(self.bot.uptime_service.get_uptime_seconds())
            hours, rem = divmod(sec, 3600)
            mins, secs = divmod(rem, 60)
            uptime_str = f"{hours}h {mins}m {secs}s"

        usr_tag = str(self.bot.user) if self.bot.user else "N/A"
        usr_id = self.bot.user.id if self.bot.user else "N/A"
        out = (
            f"[STATUS] Bot User: {usr_tag} (ID: {usr_id})\n"
            f"[STATUS] WebSocket Ping: {latency_ms} ms | Memory: ~{mem_mb} MB\n"
            f"[STATUS] Connected Guilds: {guilds_count} | Total Members: {total_members}\n"
            f"[STATUS] Continuous Uptime: {uptime_str}\n"
            f"[STATUS] Database: {self.settings.db_path} (Connected: {self.db.is_connected})"
        )
        self.write_output(out)

    async def _cmd_guilds(self) -> None:
        if not self.bot.guilds:
            self.write_output("[INFO] No connected guilds.")
            return

        lines = [f"\n--- Connected Guilds ({len(self.bot.guilds)}) ---"]
        for g in self.bot.guilds:
            owner_tag = str(g.owner) if g.owner else "Unknown"
            lines.append(f"• [{g.id}] {g.name:<30} | Members: {g.member_count:<5} | Owner: {owner_tag}")
        lines.append("------------------------------------------------")
        self.write_output("\n".join(lines))

    async def _cmd_stats(self) -> None:
        try:
            total = await self.db.get_total_verifications()
            alumni = await self.db.get_total_alumni()
            active_students = total - alumni
            faculty_counts = await self.db.get_verifications_count_by_faculty()

            lines = [
                "\n--- Verification Statistics ---",
                f"• Total Verified Records: {total}",
                f"• Active Students:        {active_students}",
                f"• Verified Alumni:        {alumni}",
                "• Faculty Distribution:",
            ]
            for fac, count in faculty_counts:
                lines.append(f"    - {fac:<6}: {count}")
            lines.append("-------------------------------")
            self.write_output("\n".join(lines))
        except Exception as e:
            self.write_output(f"[ERROR] Failed to fetch stats: {e}")

    async def _cmd_lookup(self, args: list[str]) -> None:
        if not args:
            self.write_output("[WARN] Usage: lookup <discord_user_id | student_id>")
            return

        query = args[0].strip()
        details: dict[str, Any] | None = None
        user_id: int | None = None

        if query.isdigit() and len(query) >= 16:
            user_id = int(query)
            details = await self.db.get_verification_details(user_id)
        else:
            id_hash = hash_student_id(query, self.settings.id_hash_secret)
            res = await self.db.get_verification_by_id_hash(id_hash)
            if res:
                user_id = res[0]
                details = await self.db.get_verification_details(user_id)

        if not details or user_id is None:
            self.write_output(f"[INFO] No verification record found for query '{query}'.")
            return

        user_obj = self.bot.get_user(user_id)
        tag = str(user_obj) if user_obj else f"User {user_id}"

        lines = [
            f"\n--- Verification Profile: {tag} ---",
            f"• Discord User ID:  {user_id}",
            f"• Faculty:          {details.get('faculty_code')}",
            f"• Campus:           {details.get('campus_code', 'W')}",
            f"• Study Level:      {details.get('level_code', 'R')}",
            f"• Programme Code:   {details.get('programme_code', 'N/A')}",
            f"• Expiry Date:      {details.get('card_expiry_date', 'N/A')}",
            f"• Is Alumni:        {details.get('is_alumni', False)}",
            f"• Verified At:      {details.get('verified_at')}",
            "------------------------------------------",
        ]
        self.write_output("\n".join(lines))

    async def _cmd_verify(self, args: list[str]) -> None:
        if len(args) < 2:
            self.write_output("[WARN] Usage: verify <student_id> <discord_user_id>")
            return

        raw_student_id = args[0].strip()
        user_id_raw = args[1].strip()

        if not user_id_raw.isdigit():
            self.write_output(f"[ERROR] Invalid Discord user ID '{user_id_raw}'. Must be numeric.")
            return

        user_id = int(user_id_raw)
        parsed = parse_student_id(raw_student_id)
        if not parsed.is_valid:
            self.write_output(f"[ERROR] Invalid student ID '{raw_student_id}'. Format: 23WMD09867")
            return

        user = self.bot.get_user(user_id)
        if not user:
            try:
                user = await self.bot.fetch_user(user_id)
            except Exception:
                user = None

        if not user:
            self.write_output(f"[ERROR] Could not resolve Discord user with ID {user_id}.")
            return

        self.write_output(f"[INFO] Verifying {user} with Student ID {parsed.student_id}...")
        res = await self.service.perform_verification(
            user=user,
            raw_student_id=parsed.student_id,
        )
        self.write_output(f"[RESULT] {res}")

    async def _cmd_unverify(self, args: list[str]) -> None:
        if not args:
            self.write_output("[WARN] Usage: unverify <discord_user_id> [reason]")
            return

        user_id_raw = args[0].strip()
        if not user_id_raw.isdigit():
            self.write_output(f"[ERROR] Invalid Discord user ID '{user_id_raw}'. Must be numeric.")
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
            g_count = res.get("guilds_updated", 0)
            self.write_output(f"[SUCCESS] User {user_id} unverified cleanly. Roles stripped across {g_count} guild(s).")
        else:
            self.write_output(f"[WARN] User {user_id} was not verified or could not be unverified.")

    async def _cmd_backup(self, args: list[str]) -> None:
        subfolder = args[0] if args else "daily"
        self.write_output(f"[INFO] Creating point-in-time database snapshot in backups/{subfolder}...")
        try:
            path = await self.db.create_backup(subfolder=subfolder)
            self.write_output(f"[SUCCESS] Database backup saved to: {path}")
        except Exception as e:
            self.write_output(f"[ERROR] Database backup failed: {e}")

    async def _cmd_say(self, args: list[str]) -> None:
        if len(args) < 3:
            self.write_output("[WARN] Usage: say <guild_id> <channel_id> <message>")
            return

        g_id_raw, c_id_raw = args[0], args[1]
        if not g_id_raw.isdigit() or not c_id_raw.isdigit():
            self.write_output("[ERROR] guild_id and channel_id must be integers.")
            return

        guild = self.bot.get_guild(int(g_id_raw))
        if not guild:
            self.write_output(f"[ERROR] Bot is not in guild ID {g_id_raw}.")
            return

        channel = guild.get_channel(int(c_id_raw))
        if not channel or not isinstance(channel, discord.TextChannel):
            self.write_output(f"[ERROR] TextChannel {c_id_raw} not found in guild {guild.name}.")
            return

        msg = " ".join(args[2:])
        try:
            await channel.send(msg)
            self.write_output(f"[SUCCESS] Message sent to #{channel.name} in {guild.name}.")
        except Exception as e:
            self.write_output(f"[ERROR] Failed to send message: {e}")

    async def _cmd_broadcast(self, args: list[str]) -> None:
        if not args:
            self.write_output("[WARN] Usage: broadcast <message>")
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

        self.write_output(f"[SUCCESS] Broadcast dispatched to {sent} guild(s). ({failed} failed)")

    async def _cmd_eval(self, args: list[str]) -> None:
        if not args:
            self.write_output("[WARN] Usage: eval <expression>")
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
            self.write_output(f"[EVAL] => {result!r}")
        except Exception as e:
            self.write_output(f"[EVAL ERROR] {type(e).__name__}: {e}")

    async def _cmd_stop(self) -> None:
        self.write_output("[INFO] Shutting down bot gracefully from console...")
        self.stop()
        await self.bot.close()
