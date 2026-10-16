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
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from prompt_toolkit.application import Application
from prompt_toolkit.completion import NestedCompleter
from prompt_toolkit.filters import has_completions
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import Float, FloatContainer, HSplit
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import HorizontalLine, TextArea

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
    """Provides an interactive Split-Screen TUI for TARVeri with upper logs and compact bottom prompt."""

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
        self.is_active = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self.app: Application[None] | None = None
        self.log_area: TextArea | None = None
        self.input_box: TextArea | None = None
        self.prompt = "tarveri> "

    def append_log(self, text: str) -> None:
        """Appends log text to the top pane and scrolls to bottom."""
        if not self.is_active or not self.log_area or not self.app:
            return

        def _update() -> None:
            if self.log_area and self.app:
                self.log_area.text += f"{text}\n"
                self.log_area.buffer.cursor_position = len(self.log_area.text)
                self.app.invalidate()

        if self._loop and self._loop.is_running():
            try:
                curr = asyncio.get_running_loop()
                if curr is self._loop:
                    _update()
                else:
                    self._loop.call_soon_threadsafe(_update)
            except RuntimeError:
                self._loop.call_soon_threadsafe(_update)

    def write_output(self, text: str) -> None:
        """Outputs text cleanly to log_area if TUI is active, else sys.stdout."""
        if self.is_active:
            for line in str(text).splitlines():
                self.append_log(line)
        else:
            for line in str(text).splitlines():
                sys.stdout.write(f"{line}\n")
            sys.stdout.flush()

    def start(self) -> None:
        """Starts the interactive Split-Screen TUI console if running in an interactive terminal."""
        if not getattr(self.settings, "enable_console", True):
            logger.info("[Console] Terminal console disabled via configuration.")
            return

        # Check if stdin is an interactive TTY. If running as systemd daemon or piped, disable CLI.
        if sys.stdin is None or not hasattr(sys.stdin, "isatty") or not sys.stdin.isatty():
            logger.info(
                "[Console] Non-interactive environment detected (systemd/pipe). Terminal CLI disabled."
            )
            return

        self.running = True
        self.is_active = True
        self._loop = asyncio.get_running_loop()
        ConsoleAwareStreamHandler._console_instance = self

        history_file = Path("logs/.tarveri_history")
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_file))

        completer = NestedCompleter.from_nested_dict({
            "help": None,
            "?": None,
            "status": None,
            "guilds": None,
            "stats": {"overview": None, "faculties": None},
            "lookup": None,
            "verify": None,
            "unverify": None,
            "backup": {"snapshot": None, "manual": None},
            "say": None,
            "broadcast": None,
            "eval": None,
            "clear": None,
            "stop": None,
            "exit": None,
            "quit": None,
        })

        initial_text = "\n".join(ConsoleAwareStreamHandler._log_buffer)
        if initial_text:
            initial_text += "\n"
        initial_text += "🎮 TARVeri Console initialized. Type 'help' for commands | 'exit' to quit.\n"

        self.log_area = TextArea(
            text=initial_text,
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
            focusable=False,
        )
        self.log_area.buffer.cursor_position = len(self.log_area.text)

        style = Style.from_dict({
            "text-area.prompt": "ansicyan bold",
            "completion-menu": "bg:#1e1e2e #cdd6f4",
            "completion-menu.completion": "bg:#1e1e2e #cdd6f4",
            "completion-menu.completion.current": "bg:#45475a #89b4fa bold",
            "scrollbar.background": "bg:#11111b",
            "scrollbar.button": "bg:#585b70",
        })

        self.input_box = TextArea(
            height=1,
            prompt="tarveri> ",
            multiline=False,
            wrap_lines=False,
            history=history,
            completer=completer,
            accept_handler=self._on_accept,
        )

        kb = KeyBindings()

        @kb.add("pageup")
        def _scroll_up(event):
            if self.log_area:
                self.log_area.buffer.cursor_up(count=10)

        @kb.add("pagedown")
        def _scroll_down(event):
            if self.log_area:
                self.log_area.buffer.cursor_down(count=10)

        @kb.add("tab")
        def _on_tab(event):
            b = self.input_box.buffer if self.input_box else event.app.current_buffer
            if b.complete_state:
                b.complete_next()
            else:
                b.start_completion(select_first=False)

        @kb.add("s-tab")
        def _on_s_tab(event):
            b = self.input_box.buffer if self.input_box else event.app.current_buffer
            if b.complete_state:
                b.complete_previous()

        @kb.add("down", filter=has_completions)
        def _on_down(event):
            b = self.input_box.buffer if self.input_box else event.app.current_buffer
            b.complete_next()

        @kb.add("up", filter=has_completions)
        def _on_up(event):
            b = self.input_box.buffer if self.input_box else event.app.current_buffer
            b.complete_previous()

        @kb.add("escape", filter=has_completions)
        def _on_escape(event):
            b = self.input_box.buffer if self.input_box else event.app.current_buffer
            b.cancel_completion()

        @kb.add("c-c")
        def _on_ctrl_c(event):
            if self.input_box and self.input_box.text:
                self.input_box.text = ""
            else:
                self.append_log("[CONSOLE] Type 'exit' or 'stop' to shut down the bot.")

        @kb.add("c-d")
        def _on_ctrl_d(event):
            event.app.exit()
            if self._loop and self._loop.is_running():
                asyncio.create_task(self._cmd_stop())

        body = HSplit([
            self.log_area,
            HorizontalLine(),
            self.input_box,
        ])

        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    content=CompletionsMenu(max_height=16),
                )
            ],
        )

        self.app = Application(
            layout=Layout(root, focused_element=self.input_box),
            key_bindings=kb,
            style=style,
            full_screen=True,
            mouse_support=True,
        )

        self._task = asyncio.create_task(self._run_app())

    async def _run_app(self) -> None:
        try:
            if self.app:
                await self.app.run_async()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Console TUI error: %s", exc)
        finally:
            self.is_active = False
            self.running = False
            ConsoleAwareStreamHandler._console_instance = None

    def _on_accept(self, buff) -> bool:
        cmd = buff.text.strip()
        buff.text = ""
        if cmd:
            logger.info("[CMD] Executed: %s", cmd)
            self.append_log(f"tarveri> {cmd}")
            if self._loop and self._loop.is_running():
                asyncio.create_task(self.handle_command(cmd))
        return False

    def stop(self) -> None:
        """Stops the interactive split-screen TUI console."""
        self.running = False
        self.is_active = False
        ConsoleAwareStreamHandler._console_instance = None
        if self.app and self.app.is_running:
            self.app.exit()
        if self._task and not self._task.done():
            self._task.cancel()

    def _show_escable_help(self) -> None:
        self._cmd_help()

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
        if self.is_active and self.log_area:
            self.log_area.text = ""
            if self.app:
                self.app.invalidate()
        elif sys.stdout is not None and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
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
            total = await self.db.total_verified()
            alumni = await self.db.count_alumni()
            active_students = total - alumni
            faculty_counts = await self.db.counts_by_faculty()
            last_24h = await self.db.verified_in_last(24)

            lines = [
                "\n--- Verification Statistics ---",
                f"• Total Verified Records: {total}",
                f"• Active Students:        {active_students}",
                f"• Verified Alumni:        {alumni}",
                f"• Verified (Last 24h):    {last_24h}",
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
        try:
            await asyncio.wait_for(self.bot.close(), timeout=5.0)
        except Exception as e:
            logger.warning("Error during bot shutdown: %s", e)
        finally:
            if self._loop and self._loop.is_running() and "pytest" not in sys.modules:
                self._loop.call_soon(sys.exit, 0)
