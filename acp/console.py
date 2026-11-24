"""
ACP Console - Human Observation & Interaction Terminal
Allows human operators to monitor multi-agent chat activity and inject explicit prompts.
"""

import asyncio
import codecs
from collections import deque
from contextlib import contextmanager, ExitStack
from enum import Enum
import errno
import json
import logging
import os
import select
import socket
import sys
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

import click
from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from websockets.exceptions import WebSocketException
from websockets.legacy.client import connect

from .avatars import render_message_with_avatar, terminal_text
from .hub import ACPHub
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
        self.hub_mode = "client"
        self.attach_notice = ""
        self._viewport = None
        self._feed_row = 3
        self._feed_at_bottom = False
        self._dock_lines = 0
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

    def _header(self, width: int) -> Text:
        cells = max(0, width - 1)
        port = urlsplit(self.uri).port
        left = f"ACP :{port} ({self.hub_mode})"
        with self._ui_lock:
            members = sorted((h, s) for h, s in self.roster_state.items()
                             if h.casefold() != self.handle.casefold() and s != "offline")
        count = len(members)
        prefix = f"Online ({count}): "
        if cells < 50:
            left = ""
        budget = cells - cell_len(left) - (2 if left else 0)
        right = Text(prefix)
        labels = {"starting": ("◌", "START", "cyan"), "busy": ("●", "BUSY", "yellow"),
                  "idle": ("●", "IDLE", "green"), "blocked": ("●", "BLK", "red")}
        for i, (handle, status) in enumerate(members):
            dot, label, color = labels.get(status, ("●", status.upper(), "white"))
            name = terminal_text(handle).replace("\n", " ")
            item = Text.assemble((dot, color), f" {name} {label}")
            separator = " · " if i else ""
            remaining = count - i - 1
            suffix = f" · +{remaining}" if remaining else ""
            if right.cell_len + item.cell_len + cell_len(separator + suffix) > budget:
                right.append(separator + f"+{count - i}")
                break
            right.append(separator)
            right.append(item)
        if not members:
            right.append("○ waiting for agents")
        right.truncate(max(0, budget))
        return Text.assemble(left, " " * max(0, cells - cell_len(left) - right.cell_len), right)

    def render_header_lines(self, width: int) -> list[str]:
        return [self._header(width).plain, "─" * max(0, width - 1)]

    def render_dock_lines(self, width: Optional[int] = None) -> tuple[list[str], int, int]:
        cells = max(0, (width if width is not None else self._out_console.width) - 1)
        label = "─ Draft Input (Enter send · Esc clear · /help manual) "
        separator = _clip_to_cells(label + "─" * cells, cells)
        prompt = _clip_to_cells(terminal_text(self.handle).replace("\n", " ") + "::> ", cells)
        visible, offset = _window_around_cursor(self._input_buffer, self._cursor_pos, cells - cell_len(prompt))
        commands = "/send /post /broadcast /read /roster /remove /prune /help /quit"
        full = "Commands: " + commands.replace(" ", " | ")
        return [separator, prompt + visible, _clip_to_cells(full if cell_len(full) <= cells else commands, cells)], 1, cell_len(prompt) + offset

    def _refresh_header(self):
        with self._ui_lock:
            if self._dock_active and self._viewport and self._viewport[1] >= 8:
                width, _ = self._viewport
                with self._out_console.capture() as capture:
                    self._out_console.print(self._header(width), end="")
                self._write_raw_stdout(f"\x1b7\x1b[1;1H\x1b[2K{capture.get()}\x1b[0m\x1b8")

    def _handle_resize(self):
        width, height = self._out_console.size
        if self._viewport == (width, height):
            return
        previous = self._viewport
        self._viewport = (width, height)
        self._dock_lines = 0
        if height >= 8:
            bottom = height - 3
            self._feed_row = min(self._feed_row, bottom)
            self._feed_at_bottom = self._feed_at_bottom and self._feed_row == bottom
            if previous is None or previous[1] < 8:
                clear = "\x1b[2J"
            else:
                clear = "".join(f"\x1b[{row};1H\x1b[2K"
                                for row in range(previous[1] - 2, min(previous[1], bottom) + 1))
            self._write_raw_stdout(f"{clear}\x1b[3;{bottom}r\x1b[2;1H\x1b[2K" + "─" * (width - 1))
            self._refresh_header()
        elif previous:
            self._write_raw_stdout("\x1b[r\x1b[2J\x1b[H")

    def _clear_dock(self):
        if self._dock_lines and self._viewport[1] < 8:
            self._write_raw_stdout("\r\x1b[1A\x1b[J")
        self._dock_lines = 0

    def _finish_dock(self):
        with self._ui_lock:
            if self._dock_active:
                self._clear_dock()
                self._write_raw_stdout(f"\x1b[r\x1b[0m\x1b[{self._out_console.height};1H\n")
                self._dock_active = False

    def _draw_dock(self):
        self._handle_resize()
        width, height = self._viewport
        lines, _, col = self.render_dock_lines(width)
        if height >= 8:
            dock = "".join(f"\x1b[{row};1H\x1b[2K{line}" for row, line in enumerate(lines, height - 2))
            self._write_raw_stdout(dock + f"\x1b[{height - 1};{col + 1}H")
        else:
            self._write_raw_stdout("\r" + "\r\n".join(lines) + f"\x1b[1A\x1b[{col + 1}G")
        self._dock_lines = len(lines)

    def _redraw_dock(self):
        with self._ui_lock:
            if self._dock_active:
                self._clear_dock()
                self._draw_dock()

    def _emit(self, fn: Callable[[], None]) -> None:
        with self._ui_lock:
            if not self._dock_active:
                fn()
                return
            if self._viewport != tuple(self._out_console.size):
                self._draw_dock()
            width, height = self._viewport
            if height < 8:
                self._clear_dock()
                fn()
                self._draw_dock()
                return
            with self._out_console.capture() as capture:
                fn()
            bottom = height - 3
            parts = [f"\x1b[{self._feed_row};1H"]
            for line in capture.get().splitlines():
                if self._feed_at_bottom:
                    parts.append("\r\n")
                parts.extend(("\x1b[2K", line, "\x1b[0m"))
                if self._feed_row < bottom:
                    self._feed_row += 1
                    parts.append("\r\n")
                else:
                    self._feed_at_bottom = True
            _, _, col = self.render_dock_lines(width)
            parts.append(f"\x1b[{height - 1};{col + 1}H")
            self._write_raw_stdout("".join(parts))

    def print_banner(self) -> None:
        mode = "Embedded" if self.hub_mode == "host" else "Attached"
        self._emit(lambda: self._out_console.print(
            f"ACP Chatroom · Hub: {mode} {self.uri} · /help for manual{self.attach_notice}", markup=False))

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
                    "  [red]/remove @agent[/red] (or [red]/kick[/red])      Disconnect @agent and remove from roster\n"
                    "  [red]/prune[/red]                         Remove all offline/stale entries from roster\n"
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
        self._message(data, "explicit")

    def on_silent_message(self, data: dict) -> None:
        self._message(data, "silent")

    def _print_card(self, message):
        self._out_console.print(render_message_with_avatar(message, self._out_console.width))

    def _message(self, data, mode):
        message = {**data.get("message", {}), "mode": mode}
        if "delivered" in data:
            message["delivered"] = data["delivered"]
        self._emit(lambda: self._print_card(message))

    def on_presence_change(self, data: dict) -> None:
        raw_handle = str(data.get("handle", ""))
        raw_status = str(data.get("status", "")).lower()
        if raw_handle and raw_handle != self.handle:
            with self._ui_lock:
                if raw_status == AgentStatus.OFFLINE.value:
                    for key in [k for k in self.roster_state if k.lower() == raw_handle.lower()]:
                        self.roster_state.pop(key, None)
                else:
                    self.roster_state[raw_handle] = raw_status
        self._refresh_header()

    def on_broadcast(self, data: dict) -> None:
        self._message(data, data["message"]["mode"])

    async def show_roster(self) -> None:
        await self.sync_roster(display_table=True)

    async def sync_roster(self, display_table=False):
        await self.client.get_roster(on_roster=lambda rows: self._display_roster(rows, display_table))

    def _display_roster(self, roster: list[dict], display_table=True) -> None:
        with self._ui_lock:
            self.roster_state = {
                str(r.get("handle", "")): str(r.get("status", "offline")).lower()
                for r in roster
                if r.get("handle") and r.get("handle") != self.handle
            }
        self._refresh_header()
        if not display_table:
            return
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
                self._send(
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
                self._send(
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
                self._send(
                    target="#general",
                    content=rest,
                    mode=MessageMode.URGENT if urgent else MessageMode.BROADCAST,
                    topic="general",
                    sender=self.handle,
                ),
                loop,
            )
        elif raw in ("/remove", "/kick") or raw.startswith(("/remove ", "/kick ")):
            parts = raw.split(maxsplit=1)
            target = parts[1].strip() if len(parts) > 1 else ""
            if not target or not target.lstrip("@"):
                self._emit(lambda: self._out_console.print("[red]Usage: /remove @agent[/red]"))
                return True
            self._schedule(self._do_remove(target), loop)
        elif raw == "/prune":
            self._schedule(self._do_prune(), loop)
        else:
            self._emit(
                lambda: self._out_console.print(
                    "[yellow]Unknown command. Type /help for the command manual.[/yellow]"
                )
            )
        return True

    async def _send(self, **message):
        if self.hub_mode == "disconnected":
            self._emit(lambda: self._out_console.print("Send failed: Hub disconnected."))
            return
        try:
            response = await self.client.send_message(**message)
            if response.get("status") != "ok":
                raise ValueError(response.get("error", "Send failed"))
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"Send failed: {exc}", markup=False))
            return
        mode = message["mode"]
        if mode == MessageMode.BROADCAST or (mode == MessageMode.EXPLICIT and
                (not response.get("delivered") or message["target"].lstrip("@").lower() != self.handle[1:].lower())):
            self._message({"message": message, **response}, mode.value)

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
                    self._finish_dock()
                else:
                    self._redraw_dock()

        if submit_line is not None and submit_line.strip():
            self.handle_command(submit_line, loop=loop)
            if not self._running:
                with self._ui_lock:
                    if self._dock_active:
                        self._finish_dock()

    def _read_tty_keys(self, loop: asyncio.AbstractEventLoop) -> None:
        if os.name == "nt":
            arrow_map = {"K": "LEFT", "M": "RIGHT", "G": "HOME", "O": "END", "S": "DELETE", "H": "UP", "P": "DOWN"}
            decoder = codecs.getincrementaldecoder("utf-16-le")(errors="replace")
            while self._running:
                if not msvcrt.kbhit():
                    time.sleep(0.02)
                    continue
                # getwch() returns a UTF-16 code unit; draw only complete characters.
                unit = msvcrt.getwch().encode("utf-16-le", errors="surrogatepass")
                for ch in decoder.decode(unit):
                    if ch in ("\x00", "\xe0"):
                        mapped = arrow_map.get(msvcrt.getwch())
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
        self.client.on(EventType.EXPLICIT_MESSAGE.value, self.on_explicit_message)
        self.client.on(EventType.SILENT_MESSAGE.value, self.on_silent_message)
        self.client.on(EventType.BROADCAST.value, self.on_broadcast)
        self.client.on(EventType.PRESENCE_CHANGE.value, self.on_presence_change)

        await self.client.connect(initial_status=AgentStatus.IDLE, role="console")
        try:
            await self.sync_roster()
            await self._input_loop()
        finally:
            self._running = False
            await self.client.close()

    async def _input_loop(self):
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
            try:
                if tty_raw:
                    with self._ui_lock:
                        self._dock_active = True
                        self._draw_dock()

                self.print_banner()
                input_thread = threading.Thread(target=user_input_thread, args=(tty_raw,), daemon=True)
                input_thread.start()

                while self._running:
                    await asyncio.sleep(0.3)
                    with self._ui_lock:
                        if self._dock_active and self._viewport != tuple(self._out_console.size):
                            self._redraw_dock()
                        if not self.client.connected and self.hub_mode != "disconnected":
                            self.hub_mode = "disconnected"
                            self._refresh_header()
                            self._emit(lambda: self._out_console.print("Hub disconnected. Use /quit to exit."))
            finally:
                self._running = False
                with self._ui_lock:
                    if self._dock_active:
                        self._finish_dock()
                self._emit(lambda: self._out_console.print("[dim]Console disconnected.[/dim]"))

    async def _do_read(self, topic: str) -> None:
        messages = await self.client.read_board(topic=topic, reader=self.handle, unread_only=False, limit=20)
        def _render() -> None:
            self._out_console.print(
                f"\n[bold cyan]📖 Recent messages in #{escape(topic)} (count: {len(messages)}):[/bold cyan]\n"
            )
            for idx, m in enumerate(messages):
                if idx > 0:
                    self._out_console.print()
                self._print_card(m)
            self._out_console.print()
        self._emit(_render)

    async def _do_remove(self, target: str) -> None:
        try:
            res = await self.client.remove_agent(target)
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]❌ Remove failed: {escape(str(exc))}[/red]"))
            return
        if res.get("status") != "ok":
            err = escape(str(res.get("error", "Remove failed")))
            self._emit(lambda: self._out_console.print(f"[red]❌ {err}[/red]"))
            return
        handle = str(res.get("handle", target))
        with self._ui_lock:
            for key in [k for k in self.roster_state if k.lower() == handle.lower()]:
                self.roster_state.pop(key, None)
        self._refresh_header()
        self._emit(lambda: self._out_console.print(f"[green]✓ Removed {escape(handle)}[/green]"))

    async def _do_prune(self) -> None:
        try:
            res = await self.client.prune_roster()
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]❌ Prune failed: {escape(str(exc))}[/red]"))
            return
        if res.get("status") != "ok":
            err = escape(str(res.get("error", "Prune failed")))
            self._emit(lambda: self._out_console.print(f"[red]❌ {err}[/red]"))
            return
        removed = res.get("removed", [])
        with self._ui_lock:
            removed_lower = {h.lower() for h in removed}
            for key in [k for k in self.roster_state if k.lower() in removed_lower]:
                self.roster_state.pop(key, None)
        self._refresh_header()
        if removed:
            names = escape(", ".join(removed))
            self._emit(lambda: self._out_console.print(f"[green]✓ Pruned {len(removed)} offline agent(s): {names}[/green]"))
        else:
            self._emit(lambda: self._out_console.print("[dim]No offline agents to prune.[/dim]"))


def start_console(uri: str = "ws://127.0.0.1:8765", handle: str = "@user") -> None:
    hc = HumanConsole(uri=uri, handle=handle)
    try:
        asyncio.run(hc.run())
    except KeyboardInterrupt:
        pass


class ProbeResult(Enum):
    ALIVE_HUB = "alive"
    PORT_FREE = "free"
    PORT_OCCUPIED_NON_ACP = "occupied"


async def probe_hub(uri: str, timeout: float = 0.8) -> ProbeResult:
    target = urlsplit(uri)
    port = target.port if target.port is not None else (443 if target.scheme == "wss" else 80)
    loop = asyncio.get_running_loop()

    async def open_socket():
        addresses = await loop.getaddrinfo(target.hostname, port, type=socket.SOCK_STREAM)
        last_error = None
        for family, socktype, proto, _, address in addresses:
            sock = socket.socket(family, socktype, proto)
            sock.setblocking(False)
            try:
                await loop.sock_connect(sock, address)
                return sock
            except OSError as exc:
                last_error = exc
                sock.close()
            except BaseException:
                sock.close()
                raise
        raise last_error or OSError("No addresses found")

    async def ping(sock):
        async with connect(uri, sock=sock, close_timeout=0.05) as ws:
            await ws.send(json.dumps({"action": "ping", "req_id": "probe"}))
            data = json.loads(await ws.recv())
            return isinstance(data, dict) and data.get("event") == "pong"

    # Windows retries refused SYNs before reporting WSAECONNREFUSED. Give TCP
    # that time while keeping the ACP handshake on an occupied port short.
    try:
        sock = await asyncio.wait_for(open_socket(), max(5.0, timeout) if os.name == "nt" else timeout)
    except ConnectionRefusedError:
        return ProbeResult.PORT_FREE
    try:
        if await asyncio.wait_for(ping(sock), timeout):
            return ProbeResult.ALIVE_HUB
    except (asyncio.TimeoutError, WebSocketException, ValueError, EOFError, OSError):
        pass
    finally:
        sock.close()
    return ProbeResult.PORT_OCCUPIED_NON_ACP


@contextmanager
def _silence_embedded_hub_logging():
    records = deque(maxlen=200)
    class Buffer(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    handler = Buffer()
    with ExitStack() as restore:
        for name in ("acp.hub", "websockets", "websockets.server", "websockets.protocol"):
            logger = logging.getLogger(name)
            restore.callback(setattr, logger, "handlers", logger.handlers)
            restore.callback(setattr, logger, "propagate", logger.propagate)
            restore.callback(logger.setLevel, logger.level)
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.INFO)
        yield


class UnifiedConsoleRunner:
    def __init__(self, uri="ws://127.0.0.1:8765", handle="@user", db_path="acp_chatroom.db",
                 auto_hub=True, bind_host=None, bind_port=None, explicit_hub_options=False):
        self.uri = uri
        self.handle = handle
        self.db_path = db_path
        self.auto_hub = auto_hub
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.explicit_hub_options = explicit_hub_options
        self.owns_hub = False
        self.hub = None

    async def run(self):
        target = urlsplit(self.uri)
        host = target.hostname
        if not host or target.scheme not in ("ws", "wss"):
            raise ValueError("Expected a ws:// or wss:// Hub URI")
        port = target.port
        if port is None:
            port = 443 if target.scheme == "wss" else 80
        allowed = self.auto_hub and target.scheme == "ws" and (
            host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::") or self.bind_host == host)
        local = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        address = f"[{local}]" if ":" in local else local
        if host != local:
            self.uri = target._replace(netloc=f"{address}:{port}").geturl()
        state = ProbeResult.PORT_FREE if port == 0 else await probe_hub(self.uri)
        with ExitStack() as resources:
            if state == ProbeResult.PORT_FREE and allowed:
                logs = resources.enter_context(ExitStack())
                logs.enter_context(_silence_embedded_hub_logging())
                hub = ACPHub(host=self.bind_host or host, port=self.bind_port if self.bind_port is not None else port,
                             db_path=self.db_path, defer_roster_reset=True)
                try:
                    await hub.start()
                except OSError as exc:
                    if exc.errno != errno.EADDRINUSE or await probe_hub(self.uri) != ProbeResult.ALIVE_HUB:
                        raise
                    logs.close()
                else:
                    self.hub = hub
                    self.owns_hub = True
                    if port == 0:
                        self.uri = f"ws://{address}:{hub.bound_port}"
            elif state == ProbeResult.PORT_FREE:
                raise ValueError(f"No ACP Hub listening at {self.uri}")
            elif state != ProbeResult.ALIVE_HUB:
                raise ValueError(f"No ACP pong from {self.uri} (occupied by a non-ACP service or timed out)")
            try:
                self.console = HumanConsole(uri=self.uri, handle=self.handle)
                self.console.hub_mode = "host" if self.owns_hub else "client"
                if self.explicit_hub_options and not self.owns_hub:
                    self.console.attach_notice = " · Using the existing Hub and its database"
                await self.console.run()
            finally:
                if self.owns_hub:
                    await self.hub.stop()


def start_unified_console(**options):
    try:
        asyncio.run(UnifiedConsoleRunner(**options).run())
    except KeyboardInterrupt:
        pass
    except (OSError, ValueError, WebSocketException) as exc:
        raise click.ClickException(str(exc)) from exc
