"""
Interactive Console CLI Service for TARVeri (Minecraft-style Terminal Backend).

Allows server administrators and bot hosters to execute administrative commands,
trigger verifications, inspect members, generate database snapshots, and monitor
system health directly from the terminal or tmux prompt with full scrollback support.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import shlex
import socket
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from prompt_toolkit.application import Application
from prompt_toolkit.completion import DynamicCompleter, NestedCompleter
from prompt_toolkit.filters import Condition, has_completions
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    FloatContainer,
    HSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame, TextArea

try:
    import pam
except ImportError:
    pam = None

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
    "login",
    "setup-admin",
    "lock",
    "logout",
    "user",
    "view",
    "minimize",
    "restore",
    "clear",
    "stop",
    "exit",
    "quit",
]

AUTH_LOCKED_COMMANDS = [
    "help",
    "?",
    "login",
    "setup-admin",
    "view",
    "minimize",
    "restore",
    "clear",
    "stop",
    "exit",
    "quit",
]

SUBCOMMAND_OPTIONS: dict[str, list[str]] = {
    "stats": ["overview", "faculties"],
    "backup": ["snapshot", "manual"],
    "user": ["list", "add", "remove"],
    "view": ["split", "logs", "output"],
    "minimize": ["output", "logs"],
    "restore": ["all", "output", "logs"],
    "clear": ["output", "logs", "all"],
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
    "  login <username>                Authenticate via Linux PAM to unlock console",
    "  setup-admin <username>          Initialize the first console administrator",
    "  lock, logout                    Lock console session",
    "  user <list|add|remove> [name]   Manage authorized console administrators",
    "  view [split|logs|output]        Switch UI layout view (F2/F3 shortcuts)",
    "  minimize [output|logs]          Minimize terminal output or logs pane",
    "  restore [all|output|logs]       Restore minimized panes to split view",
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
    "  clear [output|logs|all]         Clear terminal screen or logs",
    "  stop, exit, quit                Gracefully shut down TARVeri",
    "  • Hotkeys: F2/Ctrl+O (Toggle Output), F3 (Toggle Logs), PgUp/PgDn (Scroll)",
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


def is_linux_admin(username: str) -> bool:
    """Checks if a username is an administrator (root or member of sudo/wheel) on the Linux system."""
    try:
        import grp
        import pwd

        pw = pwd.getpwnam(username)
        if pw.pw_uid == 0:
            return True

        admin_groups = {"sudo", "wheel", "admin", "root"}
        user_groups = set()
        for g in grp.getgrall():
            if username in g.gr_mem or g.gr_gid == pw.pw_gid:
                user_groups.add(g.gr_name)

        if user_groups & admin_groups:
            return True

        if username == getpass.getuser():
            return True

        return False
    except (KeyError, ImportError, Exception):
        return False


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
        self.output_area: TextArea | None = None
        self.input_box: TextArea | None = None

        # Linux PAM Authentication & Console Security State
        self.is_authenticated: bool = False
        self.authenticated_user: str | None = None
        self._is_password_mode: bool = False
        self._pending_auth_user: str | None = None
        self._pending_auth_action: str = "login"
        self._failed_attempts: int = 0
        self._locked_until: float = 0.0

        # UI Minimization state
        self._output_minimized: bool = False
        self._logs_minimized: bool = False

    def _get_prompt(self) -> str:
        if self._is_password_mode:
            return f"Password for {self._pending_auth_user}: "
        if self.is_authenticated:
            return f"tarveri [{self.authenticated_user}]> "
        return "tarveri [LOCKED]> "

    def _get_completer(self) -> NestedCompleter | None:
        if self._is_password_mode:
            return None
        if not self.is_authenticated:
            return NestedCompleter.from_nested_dict({
                "help": None,
                "?": None,
                "login": None,
                "setup-admin": None,
                "view": {"split": None, "logs": None, "output": None},
                "minimize": {"output": None, "logs": None},
                "restore": {"all": None, "output": None, "logs": None},
                "clear": {"output": None, "logs": None, "all": None},
                "stop": None,
                "exit": None,
                "quit": None,
            })
        return NestedCompleter.from_nested_dict({
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
            "login": None,
            "setup-admin": None,
            "lock": None,
            "logout": None,
            "user": {"list": None, "add": None, "remove": None},
            "view": {"split": None, "logs": None, "output": None},
            "minimize": {"output": None, "logs": None},
            "restore": {"all": None, "output": None, "logs": None},
            "clear": {"output": None, "logs": None, "all": None},
            "stop": None,
            "exit": None,
            "quit": None,
        })

    def append_log(self, text: str) -> None:
        """Appends log text to the top system logs pane and scrolls to bottom."""
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

    def append_output(self, text: str) -> None:
        """Appends output text to the command output pane and scrolls to bottom."""
        if not self.is_active or not self.output_area or not self.app:
            return

        def _update() -> None:
            if self.output_area and self.app:
                self.output_area.text += f"{text}\n"
                self.output_area.buffer.cursor_position = len(self.output_area.text)
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
        """Outputs text cleanly to output_area if TUI is active, else sys.stdout."""
        if self.is_active:
            for line in str(text).splitlines():
                self.append_output(line)
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

        initial_logs = "\n".join(ConsoleAwareStreamHandler._log_buffer)
        if initial_logs:
            initial_logs += "\n"

        self.log_area = TextArea(
            text=initial_logs,
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
            focusable=False,
        )
        self.log_area.buffer.cursor_position = len(self.log_area.text)

        initial_output = (
            "🎮 TARVeri Console initialized.\n"
            "🔒 Console is locked. Type 'login <username>' to authenticate "
            "(or 'setup-admin <username>' if first time).\n"
        )
        self.output_area = TextArea(
            text=initial_output,
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
            focusable=False,
        )
        self.output_area.buffer.cursor_position = len(self.output_area.text)

        style = Style.from_dict({
            "text-area.prompt": "ansicyan bold",
            "frame.label": "ansigreen bold",
            "frame.border": "#45475a",
            "minimized-bar": "bg:#1e1e2e #89b4fa bold",
            "minimized-bar.logs": "bg:#181825 #a6adc8",
            "completion-menu": "bg:#1e1e2e #cdd6f4",
            "completion-menu.completion": "bg:#1e1e2e #cdd6f4",
            "completion-menu.completion.current": "bg:#45475a #89b4fa bold",
            "scrollbar.background": "bg:#11111b",
            "scrollbar.button": "bg:#585b70",
        })

        self.input_box = TextArea(
            height=1,
            prompt=lambda: self._get_prompt(),
            password=Condition(lambda: self._is_password_mode),
            multiline=False,
            wrap_lines=False,
            history=history,
            completer=DynamicCompleter(self._get_completer),
            accept_handler=self._on_accept,
        )

        kb = KeyBindings()

        @kb.add("f2")
        def _toggle_output_kb(event):
            self._toggle_output_minimized()

        @kb.add("c-o")
        def _toggle_output_ctrl_o(event):
            self._toggle_output_minimized()

        @kb.add("f3")
        def _toggle_logs_kb(event):
            self._toggle_logs_minimized()

        @kb.add("pageup")
        def _scroll_up(event):
            if not self._output_minimized and self.output_area:
                self.output_area.buffer.cursor_up(count=10)
            elif self.log_area:
                self.log_area.buffer.cursor_up(count=10)

        @kb.add("pagedown")
        def _scroll_down(event):
            if not self._output_minimized and self.output_area:
                self.output_area.buffer.cursor_down(count=10)
            elif self.log_area:
                self.log_area.buffer.cursor_down(count=10)

        @kb.add("c-up")
        def _scroll_logs_up(event):
            if self.log_area:
                self.log_area.buffer.cursor_up(count=10)

        @kb.add("c-down")
        def _scroll_logs_down(event):
            if self.log_area:
                self.log_area.buffer.cursor_down(count=10)

        @kb.add("s-pageup")
        def _scroll_logs_sup(event):
            if self.log_area:
                self.log_area.buffer.cursor_up(count=10)

        @kb.add("s-pagedown")
        def _scroll_logs_sdown(event):
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

        last_ctrl_c = 0.0

        @kb.add("c-c")
        def _on_ctrl_c(event):
            nonlocal last_ctrl_c
            if self._is_password_mode:
                self._is_password_mode = False
                self._pending_auth_user = None
                self.write_output("[AUTH CANCELLED] Password entry cancelled.")
                if self.app:
                    self.app.invalidate()
                return

            now = time.monotonic()
            if (not self.input_box or not self.input_box.text) or (now - last_ctrl_c < 2.0):
                self.write_output("[INFO] Ctrl+C received. Shutting down bot gracefully...")
                event.app.exit()
                if self._loop and self._loop.is_running():
                    asyncio.create_task(self._cmd_stop())
                return

            last_ctrl_c = now
            if self.input_box:
                self.input_box.text = ""
            self.write_output("[CONSOLE] Press Ctrl+C again (or type 'stop' / 'exit') to shut down.")

        @kb.add("c-d")
        def _on_ctrl_d(event):
            event.app.exit()
            if self._loop and self._loop.is_running():
                asyncio.create_task(self._cmd_stop())

        logs_frame = ConditionalContainer(
            Frame(
                self.log_area,
                title="📡 System Logs (Ctrl+Up/Down to scroll • F3 to minimize)",
                height=Dimension(weight=80, min=6),
            ),
            filter=Condition(lambda: not self._logs_minimized),
        )
        logs_bar = ConditionalContainer(
            Window(
                content=FormattedTextControl(
                    " 📡 System Logs: [MINIMIZED] — Press F3 or type 'restore logs' to expand"
                ),
                height=1,
                style="class:minimized-bar.logs",
            ),
            filter=Condition(lambda: self._logs_minimized),
        )

        output_frame = ConditionalContainer(
            Frame(
                self.output_area,
                title="💻 Terminal Output (PgUp/PgDn to scroll • F2 to minimize)",
                height=Dimension(weight=15, min=4),
            ),
            filter=Condition(lambda: not self._output_minimized),
        )
        output_bar = ConditionalContainer(
            Window(
                content=FormattedTextControl(
                    " 💻 Terminal Output: [MINIMIZED] — Press F2/Ctrl+O or type 'restore' to expand"
                ),
                height=1,
                style="class:minimized-bar",
            ),
            filter=Condition(lambda: self._output_minimized),
        )

        body = HSplit([
            logs_frame,
            logs_bar,
            output_frame,
            output_bar,
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
        text = buff.text
        buff.text = ""

        if self._is_password_mode:
            self._is_password_mode = False
            if not text:
                self.write_output("[AUTH CANCELLED] Password entry cancelled.")
                self._pending_auth_user = None
                if self.app:
                    self.app.invalidate()
                return False
            self.write_output(f"{self._get_prompt()}********")
            if self._loop and self._loop.is_running():
                asyncio.create_task(self._handle_password_input(text))
            return False

        cmd = text.strip()
        if cmd:
            logger.info("[CMD] Executed: %s", cmd)
            self.write_output(f"{self._get_prompt()}{cmd}")
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

    def _toggle_output_minimized(self) -> None:
        self._output_minimized = not self._output_minimized
        if self.app:
            self.app.invalidate()

    def _toggle_logs_minimized(self) -> None:
        self._logs_minimized = not self._logs_minimized
        if self.app:
            self.app.invalidate()

    def _cmd_view(self, args: list[str]) -> None:
        if not args:
            self.write_output(
                f"[VIEW] Current layout: output_minimized={self._output_minimized}, "
                f"logs_minimized={self._logs_minimized}. Options: view <split|logs|output>"
            )
            return
        target = args[0].lower()
        if target in ("logs", "log"):
            self._output_minimized = True
            self._logs_minimized = False
            self.write_output("📺 Layout switched to Logs view (Terminal output minimized). Press F2 to restore.")
        elif target in ("output", "out"):
            self._logs_minimized = True
            self._output_minimized = False
            self.write_output("📺 Layout switched to Output view (System logs minimized). Press F3 to restore.")
        elif target in ("split", "both", "all"):
            self._output_minimized = False
            self._logs_minimized = False
            self.write_output("📺 Layout switched to Split view (both panes visible).")
        else:
            self.write_output(f"[WARN] Unknown view mode '{target}'. Options: split, logs, output")
        if self.app:
            self.app.invalidate()

    def _cmd_minimize(self, args: list[str]) -> None:
        target = args[0].lower() if args else "output"
        if target in ("logs", "log"):
            self._logs_minimized = True
            self.write_output("📺 System logs minimized. Press F3 to restore.")
        else:
            self._output_minimized = True
            self.write_output("📺 Terminal output minimized. Press F2 or Ctrl+O to restore.")
        if self.app:
            self.app.invalidate()

    def _cmd_restore(self, args: list[str]) -> None:
        target = args[0].lower() if args else "all"
        if target in ("logs", "log"):
            self._logs_minimized = False
            self.write_output("📺 System logs restored.")
        elif target in ("output", "out"):
            self._output_minimized = False
            self.write_output("📺 Terminal output restored.")
        else:
            self._output_minimized = False
            self._logs_minimized = False
            self.write_output("📺 All panes restored to Split view.")
        if self.app:
            self.app.invalidate()

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

        # Auto-expand output pane if user executes any command other than minimize/view/clear
        if cmd not in ("view", "minimize", "clear") and self._output_minimized:
            self._output_minimized = False
            if self.app:
                self.app.invalidate()

        try:
            # Commands unconditionally accessible when locked
            if cmd in ("help", "?"):
                self._cmd_help()
                return
            elif cmd == "clear":
                self._cmd_clear(args)
                return
            elif cmd == "view":
                self._cmd_view(args)
                return
            elif cmd == "minimize":
                self._cmd_minimize(args)
                return
            elif cmd == "restore":
                self._cmd_restore(args)
                return
            elif cmd in ("stop", "exit", "quit"):
                await self._cmd_stop()
                return
            elif cmd == "setup-admin":
                await self._cmd_setup_admin(args)
                return
            elif cmd == "login":
                await self._cmd_login(args)
                return

            # Strict Authentication Gate - No bypass allowed
            if not self.is_authenticated:
                self.write_output(
                    "🔒 [AUTH REQUIRED] Console is locked. Run 'login <username>' to authenticate "
                    "(or 'setup-admin <username>' if initializing for the first time)."
                )
                return

            # Authenticated commands
            if cmd in ("lock", "logout"):
                self._cmd_lock()
            elif cmd == "user":
                await self._cmd_user(args)
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
        status_line = (
            f"  Status: 🔓 AUTHENTICATED ({self.authenticated_user})"
            if self.is_authenticated
            else "  Status: 🔒 LOCKED (Run 'login <username>' to authenticate)"
        )
        banner = [
            HELP_LINES[0],
            status_line,
            "------------------------------------------------------------------------",
        ]
        self.write_output("\n".join(banner + HELP_LINES[1:]))

    def _pam_authenticate(self, username: str, password: str) -> bool:
        if pam is None:
            logger.error("python-pam library not installed; authentication failing.")
            return False
        try:
            p = pam.pam()
            return bool(p.authenticate(username, password, service="login"))
        except Exception as exc:
            logger.warning("PAM authentication exception for %s: %s", username, exc)
            return False

    async def _is_authorized_admin(self, username: str) -> bool:
        """Determines if a user has authority to access the console."""
        # 1. Explicitly authorized in database
        if await self.db.is_console_admin(username):
            return True
        # 2. Linux system administrator (sudo/wheel/root or bot process owner)
        if is_linux_admin(username):
            return True
        return False

    async def _handle_password_input(self, password: str) -> None:
        username = self._pending_auth_user
        action = self._pending_auth_action
        self._pending_auth_user = None
        self._pending_auth_action = "login"

        if not username:
            return

        now = time.monotonic()
        if now < self._locked_until:
            rem = int(self._locked_until - now)
            self.write_output(
                f"🚨 [SECURITY LOCKOUT] Console is temporarily locked due to repeated failed attempts. "
                f"Try again in {rem}s."
            )
            return

        success = await asyncio.to_thread(self._pam_authenticate, username, password)

        if success:
            self._failed_attempts = 0
            self.is_authenticated = True
            self.authenticated_user = username

            if action == "setup_admin":
                await self.db.add_console_admin(username, role="admin", added_by="bootstrap")
                self.write_output(f"🎉 [SECURITY] Root administrator '{username}' successfully initialized!")
                await self._dispatch_security_alert(
                    title="🛡️ Console Admin Initialized",
                    description=f"First console administrator **{username}** was initialized via bootstrap.",
                    color=0x2ECC71,
                )
            else:
                if not await self.db.is_console_admin(username):
                    await self.db.add_console_admin(username, role="admin", added_by="linux_pam")

            self.write_output(f"🔓 [AUTH SUCCESS] Welcome, {username}! Console unlocked. Type 'help' for commands.")
            if self.app:
                self.app.invalidate()

            await self._dispatch_security_alert(
                title="🔐 Console Login Successful",
                description=f"Administrator **{username}** authenticated via Linux PAM and unlocked backend terminal session.",
                color=0x57F287,
            )
        else:
            self._failed_attempts += 1
            self.write_output(f"❌ [AUTH FAILED] Invalid credentials for user '{username}'. Attempt {self._failed_attempts}/3.")
            if self._failed_attempts >= 3:
                self._locked_until = time.monotonic() + 900  # 15 minutes
                self.write_output("🚨 [SECURITY LOCKOUT] 3 failed authentication attempts detected. Console locked for 15 minutes.")
                await self._dispatch_security_alert(
                    title="🚨 Console Brute-Force Lockout",
                    description=f"**3 failed authentication attempts** detected for user **{username}**.\nConsole locked for 15 minutes.",
                    color=0xED4245,
                )

    async def _cmd_setup_admin(self, args: list[str]) -> None:
        username = args[0].strip() if args else getpass.getuser()

        has_admin = await self.db.has_any_console_admin()
        if has_admin:
            self.write_output(
                "🔒 [AUTH ERROR] An administrator account already exists. "
                "Please login with an authorized account or ask an administrator to add you."
            )
            return

        if not is_linux_admin(username):
            self.write_output(
                f"❌ [AUTH ERROR] User '{username}' is not a Linux administrator (must belong to sudo/wheel group or root)."
            )
            return

        now = time.monotonic()
        if now < self._locked_until:
            rem = int(self._locked_until - now)
            self.write_output(f"🚨 [SECURITY LOCKOUT] Console is locked. Try again in {rem}s.")
            return

        self._pending_auth_user = username
        self._pending_auth_action = "setup_admin"

        if len(args) >= 2:
            await self._handle_password_input(args[1])
            return

        self._is_password_mode = True
        self.write_output(f"🔑 Initializing root admin '{username}'. Please enter Linux PAM password:")
        if self.app:
            self.app.invalidate()

    async def _cmd_login(self, args: list[str]) -> None:
        if self.is_authenticated:
            self.write_output(
                f"[INFO] Already logged in as '{self.authenticated_user}'. Run 'lock' or 'logout' to switch users."
            )
            return

        username = args[0].strip() if args else getpass.getuser()

        now = time.monotonic()
        if now < self._locked_until:
            rem = int(self._locked_until - now)
            self.write_output(f"🚨 [SECURITY LOCKOUT] Console is locked. Try again in {rem}s.")
            return

        has_admin = await self.db.has_any_console_admin()
        if not has_admin:
            self.write_output("🔒 [AUTH REQUIRED] No console administrators have been initialized yet.")
            self.write_output(f"Run 'setup-admin {username}' to bootstrap this account as root administrator.")
            return

        if not await self._is_authorized_admin(username):
            self.write_output(f"❌ [AUTH ERROR] User '{username}' is not an authorized console administrator.")
            return

        self._pending_auth_user = username
        self._pending_auth_action = "login"

        if len(args) >= 2:
            await self._handle_password_input(args[1])
            return

        self._is_password_mode = True
        self.write_output(f"🔑 Enter Linux PAM password for '{username}':")
        if self.app:
            self.app.invalidate()

    def _cmd_lock(self) -> None:
        if not self.is_authenticated:
            self.write_output("[INFO] Console is already locked.")
            return
        user = self.authenticated_user
        self.is_authenticated = False
        self.authenticated_user = None
        self._is_password_mode = False
        self._pending_auth_user = None
        self.write_output(f"🔒 [CONSOLE] User '{user}' logged out. Console locked.")
        if self.app:
            self.app.invalidate()

    async def _cmd_user(self, args: list[str]) -> None:
        if not args:
            self.write_output("[WARN] Usage: user <list|add|remove> [username]")
            return

        sub = args[0].lower()
        if sub == "list":
            admins = await self.db.get_console_admins()
            if not admins:
                self.write_output("No console administrators registered.")
                return
            self.write_output("================ Authorized Console Admins ================")
            for a in admins:
                added_by = a.get("added_by") or "system"
                added_at = a.get("added_at") or "unknown"
                self.write_output(f" • {a['username']} (Role: {a.get('role', 'admin')}, Added by: {added_by}, Date: {added_at})")
            self.write_output("===========================================================")
        elif sub == "add":
            if len(args) < 2:
                self.write_output("[WARN] Usage: user add <username>")
                return
            target = args[1].strip()
            # Verify target exists as a valid Linux user
            try:
                import pwd
                pwd.getpwnam(target)
            except (KeyError, ImportError):
                self.write_output(f"❌ [ERROR] User '{target}' does not exist on this Linux system.")
                return

            if await self.db.is_console_admin(target):
                self.write_output(f"[WARN] User '{target}' is already an authorized console administrator.")
                return
            await self.db.add_console_admin(target, role="admin", added_by=self.authenticated_user)
            self.write_output(f"✅ Added '{target}' as authorized console administrator.")
            await self._dispatch_security_alert(
                title="🛡️ Console Admin Added",
                description=f"Administrator **{self.authenticated_user}** added **{target}** as console admin.",
                color=0x3498DB,
            )
        elif sub in ("remove", "delete", "del"):
            if len(args) < 2:
                self.write_output("[WARN] Usage: user remove <username>")
                return
            target = args[1].strip()
            admins = await self.db.get_console_admins()
            if len(admins) <= 1 and any(a["username"] == target for a in admins):
                self.write_output(f"❌ Cannot remove '{target}': At least one console administrator must remain.")
                return
            removed = await self.db.remove_console_admin(target)
            if removed:
                self.write_output(f"✅ Removed '{target}' from console administrators.")
                await self._dispatch_security_alert(
                    title="🛡️ Console Admin Removed",
                    description=f"Administrator **{self.authenticated_user}** removed **{target}** from console admins.",
                    color=0xE67E22,
                )
            else:
                self.write_output(f"[WARN] User '{target}' was not found in console administrators.")
        else:
            self.write_output(f"[WARN] Unknown subcommand 'user {sub}'. Available: list, add, remove")

    async def _dispatch_security_alert(self, title: str, description: str, color: int) -> None:
        try:
            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=discord.utils.utcnow(),
            )
            host_info = f"{getpass.getuser()}@{socket.gethostname()}"
            embed.add_field(name="Host", value=f"`{host_info}`", inline=True)
            bot_tag = str(self.bot.user) if self.bot.user else "TARVeri"
            embed.set_footer(text=f"TARVeri Console Security • {bot_tag}")

            # Send to hoster DM if configured
            hoster_id = getattr(self.settings, "hoster_discord_id", None)
            if hoster_id:
                hoster = self.bot.get_user(hoster_id)
                if not hoster:
                    try:
                        hoster = await self.bot.fetch_user(hoster_id)
                    except Exception:
                        hoster = None
                if hoster:
                    try:
                        await hoster.send(embed=embed)
                    except Exception as exc:
                        logger.debug("Could not send DM to hoster: %s", exc)

            # Send to #tarveri-log channel in guilds (identical to intercepted security actions)
            for guild in self.bot.guilds:
                sent = False

                # 1. Primary: Use VerificationService's send_admin_security_alert (which targets or provisions #tarveri-log)
                verif_service = self.service or getattr(self.bot, "service", None)
                if verif_service and hasattr(verif_service, "send_admin_security_alert"):
                    try:
                        sent = await verif_service.send_admin_security_alert(guild, embed)
                    except Exception as exc:
                        logger.debug("Failed sending security alert via send_admin_security_alert in %s: %s", guild.id, exc)

                # 2. Secondary: Search for existing #tarveri-log channel directly
                if not sent:
                    target_names = {"tarveri-log", "tarveri_log", "tarveri-logs", "tarveri_logs"}
                    text_channels = getattr(guild, "text_channels", []) or []
                    target_chan = next(
                        (ch for ch in text_channels if getattr(ch, "name", "").lower() in target_names),
                        None,
                    )
                    if target_chan and isinstance(target_chan, discord.TextChannel):
                        bot_member = getattr(guild, "me", None)
                        perms = target_chan.permissions_for(bot_member) if bot_member else None
                        if perms and perms.send_messages and perms.embed_links:
                            try:
                                await target_chan.send(embed=embed)
                                sent = True
                            except Exception as exc:
                                logger.debug("Failed sending security alert to %s: %s", target_chan.id, exc)

                # 3. Tertiary: Fallback to review_channel_id if configured (restricted admin review channel)
                # NOTE: Never fall back to welcome_channel_id or system_channel to prevent leaking security logs to public chat
                if not sent:
                    try:
                        g_settings = await self.db.get_guild_settings(guild.id)
                        review_chan_id = None
                        if isinstance(g_settings, (tuple, list)) and len(g_settings) > 3:
                            review_chan_id = g_settings[3]
                        elif isinstance(g_settings, dict):
                            review_chan_id = g_settings.get("review_channel_id")

                        if review_chan_id:
                            target_chan = guild.get_channel(review_chan_id)
                            if target_chan and isinstance(target_chan, discord.TextChannel):
                                bot_member = getattr(guild, "me", None)
                                perms = target_chan.permissions_for(bot_member) if bot_member else None
                                if perms and perms.send_messages and perms.embed_links:
                                    await target_chan.send(embed=embed)
                    except Exception as exc:
                        logger.debug("Review channel fallback lookup error: %s", exc)
        except Exception as exc:
            logger.debug("Security alert dispatch error: %s", exc)

    def _cmd_clear(self, args: list[str] | None = None) -> None:
        target = args[0].lower() if args else "output"
        if self.is_active:
            if target in ("all", "both"):
                if self.log_area:
                    self.log_area.text = ""
                if self.output_area:
                    self.output_area.text = ""
            elif target in ("logs", "log"):
                if self.log_area:
                    self.log_area.text = ""
            else:
                if self.output_area:
                    self.output_area.text = ""
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
            try:
                ups = self.bot.uptime_service
                if hasattr(ups, "current_uptime_str") and ups.current_uptime_str:
                    uptime_str = ups.current_uptime_str
                elif hasattr(ups, "current_uptime_seconds"):
                    sec = int(ups.current_uptime_seconds)
                    hours, rem = divmod(sec, 3600)
                    mins, secs = divmod(rem, 60)
                    uptime_str = f"{hours}h {mins}m {secs}s"
                elif hasattr(ups, "get_uptime_seconds"):
                    sec = int(ups.get_uptime_seconds())
                    hours, rem = divmod(sec, 3600)
                    mins, secs = divmod(rem, 60)
                    uptime_str = f"{hours}h {mins}m {secs}s"
            except Exception as exc:
                logger.debug("Failed computing uptime string: %s", exc)

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
