"""
ACP Console - Human Observation & Interaction Terminal
Allows human operators to monitor multi-agent chat activity and inject explicit prompts.
"""

import asyncio
import codecs
from contextlib import contextmanager
import os
import select
import sys
import threading
import time
from typing import Callable, Optional
from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .protocol import MessageMode, AgentStatus, EventType
from .client import ACPClient

if os.name == "nt":
    import ctypes
    import msvcrt
else:
    import termios

console = Console()


@contextmanager
def _raw_tty_mode():
    """Enable VT output and non-echoing key input when attached to an interactive TTY."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        yield False
        return

    if os.name == "nt":
        STD_OUTPUT_HANDLE = -11
        ENABLE_PROCESSED_OUTPUT = 0x0001
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        old_mode = ctypes.c_uint()
        restored = False
        if handle not in (0, -1) and kernel32.GetConsoleMode(handle, ctypes.byref(old_mode)):
            kernel32.SetConsoleMode(handle, old_mode.value | ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING)
            restored = True
        try:
            yield True
        finally:
            if restored:
                kernel32.SetConsoleMode(handle, old_mode.value)
        return

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    new_attrs = termios.tcgetattr(fd)
    new_attrs[3] &= ~(termios.ECHO | termios.ICANON)
    new_attrs[6][termios.VMIN] = 1
    new_attrs[6][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, new_attrs)
    try:
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def _clip_to_cells(text: str, max_cells: int) -> str:
    """Truncate text so its terminal display width never exceeds max_cells."""
    if max_cells <= 0:
        return ""
    total = 0
    out = []
    for ch in text:
        w = max(0, cell_len(ch))
        if total + w > max_cells:
            break
        out.append(ch)
        total += w
    return "".join(out)


def _window_around_cursor(text: str, cursor_pos: int, max_cells: int) -> tuple[str, int]:
    """Return a horizontally scrolled slice of text keeping cursor_pos visible within max_cells."""
    if max_cells <= 0:
        return "", 0
    cursor_pos = max(0, min(cursor_pos, len(text)))
    left_chars = list(text[:cursor_pos])
    left_widths = [max(0, cell_len(c)) for c in left_chars]

    start = cursor_pos
    used = 0
    # Reserve at least 1 cell for cursor/lookahead if characters exist after the cursor.
    left_budget = max_cells if cursor_pos == len(text) else max(1, max_cells - 1)
    while start > 0:
        w = left_widths[start - 1]
        if used + w > left_budget:
            break
        used += w
        start -= 1

    visible_right = _clip_to_cells(text[cursor_pos:], max_cells - used)
    visible = text[start:cursor_pos] + visible_right
    return visible, used


class HumanConsole:
    def __init__(
        self,
        uri: str = "ws://127.0.0.1:8765",
        handle: str = "@user",
        ui_console: Optional[Console] = None,
    ):
        self.uri = uri
        self.handle = handle if handle.startswith("@") else f"@{handle}"
        self.client = ACPClient(uri=self.uri, handle=self.handle)
        self._console = ui_console
        self._running = False
        self._ui_lock = threading.RLock()
        self._input_buffer = ""
        self._cursor_pos = 0
        self._history: list[str] = []
        self._history_index: Optional[int] = None
        self._draft_stash = ""
        self._dock_active = False
        self._dock_lines = 0
        self._dock_cursor_row = 0
        self.roster_state: dict[str, str] = {}

    @property
    def _out_console(self) -> Console:
        return self._console if self._console is not None else console

    def _write_raw_stdout(self, text: str) -> None:
        out = self._out_console.file
        out.write(text)
        out.flush()

    def format_online_agents_bar(self, max_cells: Optional[int] = None) -> str:
        """Build a single-line summary of currently online agents and their statuses."""
        with self._ui_lock:
            online = [
                (handle, status)
                for handle, status in sorted(self.roster_state.items())
                if handle and handle != self.handle and status.lower() != AgentStatus.OFFLINE.value
            ]
        summary = " · ".join(f"{h} [{s.upper()}]" for h, s in online) if online else "none"
        line = f"Online Agents ({len(online)}): {summary}"
        return _clip_to_cells(line, max_cells) if max_cells is not None else line

    def render_dock_lines(self, width: Optional[int] = None) -> tuple[list[str], int, int]:
        """Build the bottom dock (separator, online agents bar, draft input, command bar)."""
        cols = max(20, width if width is not None else self._out_console.width)
        max_cells = cols - 1

        label = "─ Draft Input (Enter send · Esc clear · /help manual) "
        fill = "─" * max(0, max_cells - cell_len(label))
        sep_line = _clip_to_cells(label + fill, max_cells)
        agents_line = self.format_online_agents_bar(max_cells)

        prompt = f"{self.handle}> "
        prompt_cells = cell_len(prompt)
        avail = max(4, max_cells - prompt_cells)
        visible, rel_col = _window_around_cursor(self._input_buffer, self._cursor_pos, avail)
        input_line = prompt + visible
        cursor_col = prompt_cells + rel_col

        cmd_bar_full = (
            "Commands: /send @agent <msg> | /post [#topic] <msg> | "
            "/broadcast <msg> | /read [topic] | /roster | /help | /quit"
        )
        cmd_bar_compact = "Cmds: /send | /post | /broadcast | /read | /roster | /help | /quit"
        if cell_len(cmd_bar_full) <= max_cells:
            cmd_line = cmd_bar_full
        elif cell_len(cmd_bar_compact) <= max_cells:
            cmd_line = cmd_bar_compact
        else:
            cmd_line = _clip_to_cells(cmd_bar_compact, max_cells)

        return [sep_line, agents_line, input_line, cmd_line], 2, cursor_col

    def _clear_dock(self) -> None:
        if not self._dock_lines:
            return
        parts = ["\r"]
        if self._dock_cursor_row > 0:
            parts.append(f"\x1b[{self._dock_cursor_row}A")
        parts.append("\x1b[J")
        self._write_raw_stdout("".join(parts))
        self._dock_lines = 0
        self._dock_cursor_row = 0

    def _draw_dock(self) -> None:
        lines, cursor_row, cursor_col = self.render_dock_lines(self._out_console.width)
        rendered = "\n".join(lines)
        total = len(lines)
        parts = ["\r", rendered]
        up = total - 1 - cursor_row
        if up > 0:
            parts.append(f"\x1b[{up}A")
        parts.append("\r")
        if cursor_col > 0:
            parts.append(f"\x1b[{cursor_col}C")
        self._write_raw_stdout("".join(parts))
        self._dock_lines = total
        self._dock_cursor_row = cursor_row

    def _redraw_dock(self) -> None:
        with self._ui_lock:
            if not self._dock_active:
                return
            self._clear_dock()
            if self._running:
                self._draw_dock()

    def _emit(self, fn: Callable[[], None]) -> None:
        """Erase the active input dock, run fn() cleanly above it, and redraw the intact dock."""
        with self._ui_lock:
            dock_was_active = self._dock_active
            if dock_was_active:
                self._clear_dock()
            fn()
            if dock_was_active and self._running:
                self._draw_dock()

    def print_banner(self) -> None:
        self._emit(
            lambda: self._out_console.print(
                Panel.fit(
                    "[bold cyan]🤖 ACP Multi-Agent Chatroom Console[/bold cyan]\n"
                    "[dim]Observing agent-to-agent communication & Human-in-the-loop steering[/dim]\n\n"
                    "Commands:\n"
                    "  [green]/send @agent <message>[/green]         - Send explicit prompt directly to agent\n"
                    "  [cyan]/post [#topic] <message>[/cyan]       - Post silent message to topic (default #general)\n"
                    "  [magenta]/broadcast [--urgent] <msg>[/magenta]    - Broadcast message to all online agents\n"
                    "  [yellow]/roster[/yellow]                        - List active online agents & status\n"
                    "  [yellow]/read [topic][/yellow]                  - Read recent messages from topic\n"
                    "  [blue]/help[/blue] or [blue]/?[/blue]                    - Show full console manual\n"
                    "  [red]/quit[/red] or [red]/exit[/red]                - Exit console",
                    border_style="cyan",
                )
            )
        )

    def print_help(self) -> None:
        """Display full command manual and keyboard shortcuts above the input dock."""
        self._emit(
            lambda: self._out_console.print(
                Panel.fit(
                    "[bold cyan]📘 ACP Console Manual[/bold cyan]\n\n"
                    "[bold]Commands:[/bold]\n"
                    "  [green]/send @agent <message>[/green]         Send explicit prompt to wake/steer @agent\n"
                    "  [cyan]/post [#topic] <message>[/cyan]       Post passive update to #topic (default #general)\n"
                    "  [magenta]/broadcast [--urgent] <msg>[/magenta]    Broadcast to all online agents (--urgent bypasses idle wait)\n"
                    "  [yellow]/roster[/yellow]                        Refresh active agent roster & status table\n"
                    "  [yellow]/read [topic][/yellow]                  Read recent 20 messages from #topic (default general)\n"
                    "  [blue]/help[/blue], [blue]/?[/blue], [blue]help[/blue]                Show this command reference\n"
                    "  [red]/quit[/red], [red]/exit[/red], [red]:q[/red]             Disconnect and exit console\n\n"
                    "[bold]Input Box Shortcuts:[/bold]\n"
                    "  [dim]Left/Right, Home/End[/dim]           Move cursor inside active draft\n"
                    "  [dim]Up/Down[/dim]                        Browse submitted command history\n"
                    "  [dim]Esc / Ctrl+U[/dim]                   Clear current draft input\n"
                    "  [dim]Ctrl+C[/dim]                         Exit console",
                    border_style="blue",
                )
            )
        )

    def on_explicit_message(self, data: dict) -> None:
        msg = data.get("message", {})
        sender = escape(str(msg.get("sender", "@unknown")))
        target = escape(str(msg.get("target", "@unknown")))
        content = escape(str(msg.get("content", "")))
        time_str = str(msg.get("created_at", ""))[:19].replace("T", " ")

        self._emit(
            lambda: self._out_console.print(
                f"[dim]{time_str}[/dim] [bold magenta]⚡ EXPLICIT[/bold magenta] "
                f"[bold yellow]{sender}[/bold yellow] ➔ [bold green]{target}[/bold green]:\n"
                f"   [white]{content}[/white]"
            )
        )

    def on_silent_message(self, data: dict) -> None:
        msg = data.get("message", {})
        sender = escape(str(msg.get("sender", "@unknown")))
        topic = escape(str(msg.get("topic", "general")))
        content = escape(str(msg.get("content", "")))
        time_str = str(msg.get("created_at", ""))[:19].replace("T", " ")

        self._emit(
            lambda: self._out_console.print(
                f"[dim]{time_str}[/dim] [bold blue]📢 BOARD [#{topic}][/bold blue] "
                f"[bold yellow]{sender}[/bold yellow]:\n"
                f"   [white]{content}[/white]"
            )
        )

    def on_presence_change(self, data: dict) -> None:
        raw_handle = str(data.get("handle", ""))
        raw_status = str(data.get("status", "")).lower()
        if raw_handle and raw_handle != self.handle:
            with self._ui_lock:
                self.roster_state[raw_handle] = raw_status
        handle = escape(raw_handle)
        color = "green" if raw_status == "idle" else ("yellow" if raw_status == "busy" else "red")
        self._emit(
            lambda: self._out_console.print(
                f"[dim]• Presence:[/dim] [bold]{handle}[/bold] is now [{color}]{escape(raw_status.upper())}[/{color}]"
            )
        )

    def on_broadcast(self, data: dict) -> None:
        msg = data["message"]
        self._emit(
            lambda: self._out_console.print(
                f"{msg['mode'].upper()} #{msg['id']} {msg['sender']}: {msg['content']}",
                markup=False,
            )
        )

    async def show_roster(self) -> None:
        roster = await self.client.get_roster()
        with self._ui_lock:
            self.roster_state = {
                str(r.get("handle", "")): str(r.get("status", "offline")).lower()
                for r in roster
                if r.get("handle") and r.get("handle") != self.handle
            }
        table = Table(title="👥 Active Agent Roster", border_style="blue")
        table.add_column("Agent Handle", style="cyan", no_wrap=True)
        table.add_column("Status", style="bold")
        table.add_column("Last Seen (UTC)", style="dim")

        for r in roster:
            status = r.get("status", "offline")
            color = "green" if status == "idle" else ("yellow" if status == "busy" else "red")
            table.add_row(r.get("handle", ""), f"[{color}]{status.upper()}[/{color}]", r.get("last_seen", "")[:19])

        self._emit(lambda: self._out_console.print(table))

    def _schedule(self, coro, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        if loop is not None:
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            asyncio.create_task(coro)

    def handle_command(self, raw: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
        """Process a complete console command line. Returns False when quitting."""
        raw = raw.strip()
        if not raw:
            return True
        if raw in ("/quit", "/exit", ":q"):
            self._running = False
            return False
        if raw in ("/help", "/?", "help"):
            self.print_help()
        elif raw == "/roster":
            self._schedule(self.show_roster(), loop)
        elif raw == "/read" or raw.startswith("/read "):
            parts = raw.split(maxsplit=1)
            topic = parts[1].lstrip("#") if len(parts) > 1 else "general"
            self._schedule(self._do_read(topic or "general"), loop)
        elif raw == "/send" or raw.startswith("/send "):
            # /send @target message
            parts = raw.split(maxsplit=2)
            if len(parts) < 3:
                self._emit(lambda: self._out_console.print("[red]Usage: /send @target <message>[/red]"))
                return True
            target = parts[1]
            msg = parts[2]
            self._schedule(
                self.client.send_message(
                    target=target,
                    content=msg,
                    mode=MessageMode.EXPLICIT,
                    sender=self.handle,
                ),
                loop,
            )
        elif raw == "/post" or raw.startswith("/post "):
            # /post #topic message OR /post message
            parts = raw.split(maxsplit=2)
            if len(parts) < 2:
                self._emit(lambda: self._out_console.print("[red]Usage: /post [#topic] <message>[/red]"))
                return True
            if parts[1].startswith("#"):
                topic = parts[1][1:] or "general"
                msg = parts[2] if len(parts) > 2 else ""
            else:
                topic = "general"
                msg = raw.split(maxsplit=1)[1]
            if not msg:
                self._emit(lambda: self._out_console.print("[red]Usage: /post [#topic] <message>[/red]"))
                return True
            self._schedule(
                self.client.send_message(
                    target=f"#{topic}",
                    content=msg,
                    mode=MessageMode.SILENT,
                    topic=topic,
                    sender=self.handle,
                ),
                loop,
            )
        elif raw == "/broadcast" or raw.startswith("/broadcast "):
            parts = raw.split(maxsplit=1)
            rest = parts[1].strip() if len(parts) > 1 else ""
            urgent = False
            if rest == "--urgent" or rest.startswith("--urgent "):
                urgent = True
                rest = rest[len("--urgent"):].strip()
            if not rest:
                self._emit(lambda: self._out_console.print("[red]Usage: /broadcast [--urgent] <message>[/red]"))
                return True
            self._schedule(
                self.client.send_message(
                    target="#general",
                    content=rest,
                    mode=MessageMode.URGENT if urgent else MessageMode.BROADCAST,
                    topic="general",
                    sender=self.handle,
                ),
                loop,
            )
        else:
            self._emit(
                lambda: self._out_console.print(
                    "[yellow]Unknown command. Type /help for the command manual.[/yellow]"
                )
            )
        return True

    def handle_key(self, key: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Apply one keystroke or named navigation key to the separated draft input buffer."""
        submit_line: Optional[str] = None
        with self._ui_lock:
            if key in ("\r", "\n"):
                submit_line = self._input_buffer
                self._input_buffer = ""
                self._cursor_pos = 0
                self._history_index = None
                self._draft_stash = ""
                if submit_line.strip():
                    self._history.append(submit_line)
                if self._dock_active:
                    self._clear_dock()
                    if submit_line.strip():
                        self._out_console.print(f"[dim]{escape(self.handle)}> {escape(submit_line)}[/dim]")
            elif key == "\x03":
                self._running = False
            elif key == "\x04" and not self._input_buffer:
                self._running = False
            elif key in ("\x1b", "\x15"):
                self._input_buffer = ""
                self._cursor_pos = 0
                self._history_index = None
            elif key in ("\x08", "\x7f"):
                if self._cursor_pos > 0:
                    self._input_buffer = (
                        self._input_buffer[: self._cursor_pos - 1]
                        + self._input_buffer[self._cursor_pos :]
                    )
                    self._cursor_pos -= 1
            elif key == "DELETE":
                if self._cursor_pos < len(self._input_buffer):
                    self._input_buffer = (
                        self._input_buffer[: self._cursor_pos]
                        + self._input_buffer[self._cursor_pos + 1 :]
                    )
            elif key == "LEFT":
                if self._cursor_pos > 0:
                    self._cursor_pos -= 1
            elif key == "RIGHT":
                if self._cursor_pos < len(self._input_buffer):
                    self._cursor_pos += 1
            elif key == "HOME":
                self._cursor_pos = 0
            elif key == "END":
                self._cursor_pos = len(self._input_buffer)
            elif key == "UP":
                if self._history:
                    if self._history_index is None:
                        self._draft_stash = self._input_buffer
                        self._history_index = len(self._history) - 1
                    elif self._history_index > 0:
                        self._history_index -= 1
                    self._input_buffer = self._history[self._history_index]
                    self._cursor_pos = len(self._input_buffer)
            elif key == "DOWN":
                if self._history_index is not None:
                    if self._history_index + 1 < len(self._history):
                        self._history_index += 1
                        self._input_buffer = self._history[self._history_index]
                    else:
                        self._history_index = None
                        self._input_buffer = self._draft_stash
                    self._cursor_pos = len(self._input_buffer)
            elif len(key) == 1 and ord(key) >= 32:
                self._input_buffer = (
                    self._input_buffer[: self._cursor_pos]
                    + key
                    + self._input_buffer[self._cursor_pos :]
                )
                self._cursor_pos += 1

            if self._dock_active:
                if not self._running:
                    self._clear_dock()
                    self._dock_active = False
                else:
                    if submit_line is None:
                        self._clear_dock()
                    self._draw_dock()

        if submit_line is not None and submit_line.strip():
            self.handle_command(submit_line, loop=loop)
            if not self._running:
                with self._ui_lock:
                    if self._dock_active:
                        self._clear_dock()
                        self._dock_active = False

    def _read_tty_keys(self, loop: asyncio.AbstractEventLoop) -> None:
        if os.name == "nt":
            arrow_map = {"K": "LEFT", "M": "RIGHT", "G": "HOME", "O": "END", "S": "DELETE", "H": "UP", "P": "DOWN"}
            while self._running:
                if not msvcrt.kbhit():
                    time.sleep(0.02)
                    continue
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    code = msvcrt.getwch()
                    mapped = arrow_map.get(code)
                    if mapped:
                        self.handle_key(mapped, loop=loop)
                else:
                    self.handle_key(ch, loop=loop)
            return

        fd = sys.stdin.fileno()
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        esc_map = {"D": "LEFT", "C": "RIGHT", "H": "HOME", "F": "END", "A": "UP", "B": "DOWN"}
        while self._running:
            ready, _, _ = select.select([fd], [], [], 0.05)
            if not ready:
                continue
            raw_b = os.read(fd, 1)
            if not raw_b:
                self._running = False
                break
            if raw_b == b"\x1b":
                if select.select([fd], [], [], 0.02)[0]:
                    nxt = os.read(fd, 1)
                    if nxt == b"[" and select.select([fd], [], [], 0.02)[0]:
                        seq = os.read(fd, 1)
                        key_name = esc_map.get(seq.decode("ascii", errors="ignore"))
                        if key_name:
                            self.handle_key(key_name, loop=loop)
                        elif seq == b"3" and select.select([fd], [], [], 0.02)[0]:
                            if os.read(fd, 1) == b"~":
                                self.handle_key("DELETE", loop=loop)
                else:
                    self.handle_key("\x1b", loop=loop)
                continue
            ch = decoder.decode(raw_b)
            if ch:
                self.handle_key(ch, loop=loop)

    async def run(self) -> None:
        self._running = True
        self.print_banner()

        try:
            await self.client.connect(initial_status=AgentStatus.IDLE, role="console")
        except Exception as e:
            self._out_console.print(f"[bold red]❌ Failed to connect to ACP Hub at {self.uri}: {e}[/bold red]")
            return

        self.client.on(EventType.EXPLICIT_MESSAGE.value, self.on_explicit_message)
        self.client.on(EventType.SILENT_MESSAGE.value, self.on_silent_message)
        self.client.on(EventType.BROADCAST.value, self.on_broadcast)
        self.client.on(EventType.PRESENCE_CHANGE.value, self.on_presence_change)

        await self.show_roster()

        loop = asyncio.get_running_loop()

        def user_input_thread(tty_raw: bool):
            try:
                if tty_raw:
                    self._read_tty_keys(loop)
                    return
                while self._running:
                    raw = input()
                    if not self.handle_command(raw, loop=loop):
                        break
            except (EOFError, KeyboardInterrupt):
                self._running = False
            except Exception as e:
                self._emit(lambda: self._out_console.print(f"[red]Error: {e}[/red]"))

        with _raw_tty_mode() as tty_raw:
            if tty_raw:
                with self._ui_lock:
                    self._dock_active = True
                    self._draw_dock()

            input_thread = threading.Thread(target=user_input_thread, args=(tty_raw,), daemon=True)
            input_thread.start()

            try:
                while self._running:
                    await asyncio.sleep(0.3)
            finally:
                self._running = False
                with self._ui_lock:
                    if self._dock_active:
                        self._clear_dock()
                        self._dock_active = False
                await self.client.close()
                self._out_console.print("\n[dim]Console disconnected.[/dim]")

    async def _do_read(self, topic: str) -> None:
        messages = await self.client.read_board(topic=topic, reader=self.handle, unread_only=False, limit=20)
        def _render() -> None:
            self._out_console.print(
                f"\n[bold cyan]📖 Recent messages in #{escape(topic)} (count: {len(messages)}):[/bold cyan]\n"
            )
            for idx, m in enumerate(messages):
                if idx > 0:
                    self._out_console.print()
                t = str(m.get("created_at", ""))[:19].replace("T", " ")
                sender = escape(str(m.get("sender", "@unknown")))
                content = escape(str(m.get("content", "")))
                self._out_console.print(f"  [dim]{t}[/dim] [yellow]{sender}[/yellow]:")
                self._out_console.print(f"    {content}")
            self._out_console.print()
        self._emit(_render)


def start_console(uri: str = "ws://127.0.0.1:8765", handle: str = "@user") -> None:
    hc = HumanConsole(uri=uri, handle=handle)
    try:
        asyncio.run(hc.run())
    except KeyboardInterrupt:
        pass
