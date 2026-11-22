"""
ACP Console - Human observation terminal with a fixed header, DECSTBM feed, and input dock.
"""

import asyncio
import codecs
from collections import deque
from contextlib import contextmanager
from enum import Enum
import errno
import io
import json
import logging
import os
import select
import shutil
import socket
import sys
import threading
import time
import uuid
from typing import Callable, Optional
from urllib.parse import urlparse

from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
import websockets

from .avatars import render_message_with_avatar
from .client import ACPClient
from .hub import ACPHub
from .protocol import AgentStatus, EventType, MessageMode

if os.name == "nt":
    import ctypes
    import msvcrt
else:
    import termios

console = Console()

HEADER_LINES = 2
DOCK_LINES = 3
MIN_ROWS = 8

_STATUS_MARKS = {
    "idle": ("●", "IDLE"),
    "busy": ("●", "BUSY"),
    "starting": ("◌", "START"),
    "blocked": ("●", "BLK"),
}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}
_CHAT_COMMANDS = {"/send", "/post", "/broadcast"}


class ProbeStatus(str, Enum):
    ALIVE_HUB = "alive_hub"
    PORT_FREE = "port_free"
    PORT_OCCUPIED_NON_ACP = "port_occupied_non_acp"


class HubConnectionError(RuntimeError):
    def __init__(self, message: str, *, reason: str):
        super().__init__(message)
        self.reason = reason


class _RingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.buffer: deque[str] = deque(maxlen=200)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass


@contextmanager
def _silence_embedded_hub_logging():
    """Keep embedded hub and websocket logs out of the terminal until the TUI exits."""
    names = ("acp.hub", "websockets", "websockets.server", "websockets.client", "websockets.protocol")
    saved = []
    for name in names:
        target = logging.getLogger(name)
        saved.append((target, target.handlers, target.level, target.propagate))
        target.handlers = [_RingHandler()]
        target.setLevel(logging.DEBUG)
        target.propagate = False
    try:
        yield
    finally:
        for target, handlers, level, propagate in saved:
            target.handlers = handlers
            target.setLevel(level)
            target.propagate = propagate


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
        width = cell_len(ch)
        if width > 0 and total + width > max_cells:
            break
        out.append(ch)
        total += max(0, width)
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
    left_budget = max_cells if cursor_pos == len(text) else max(1, max_cells - 1)
    while start > 0:
        width = left_widths[start - 1]
        if used + width > left_budget:
            break
        used += width
        start -= 1

    visible_right = _clip_to_cells(text[cursor_pos:], max_cells - used)
    visible = text[start:cursor_pos] + visible_right
    return visible, used


def _format_stamp(value: object) -> str:
    text = str(value or "")
    if "T" in text:
        text = text.replace("T", " ")
    return text[:19]


def _is_chat_command(raw: str) -> bool:
    parts = raw.strip().split(maxsplit=1)
    return bool(parts) and parts[0].lower() in _CHAT_COMMANDS


def _is_local_host(host: str) -> bool:
    return host.strip("[]").lower() in _LOCAL_HOSTS


def split_ws_uri(uri: str) -> tuple[str, int]:
    parsed = urlparse(uri)
    if not parsed.hostname:
        raise ValueError(f"Invalid hub URI: {uri}")
    return parsed.hostname, 8765 if parsed.port is None else int(parsed.port)


def client_connect_uri(bind_host: str, port: int) -> str:
    """Map a bind address to the URI the in-process console client should dial."""
    host = bind_host
    if host in ("0.0.0.0", ""):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        return f"ws://[{host}]:{port}"
    return f"ws://{host}:{port}"


def _connection_refused(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ConnectionRefusedError):
            return True
        if isinstance(current, OSError):
            winerror = getattr(current, "winerror", None)
            if winerror in (10061, 1225) or current.errno in (errno.ECONNREFUSED, 111, 10061):
                return True
        current = current.__cause__ or current.__context__
    text = str(exc).lower()
    return "refused" in text or "10061" in text or "1225" in text


def local_port_available(host: str, port: int) -> bool:
    """True when nothing is listening on host:port. A dropped SYN still counts as free."""
    if port == 0:
        return True
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _addr_in_use(exc: BaseException) -> bool:
    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        if winerror == 10048 or exc.errno in (errno.EADDRINUSE, 98, 10048):
            return True
    return "address already in use" in str(exc).lower()


async def probe_hub(uri: str, timeout: float = 0.8) -> ProbeStatus:
    """Distinguish a live ACP hub, a free port, and a non-ACP listener."""
    try:
        return await asyncio.wait_for(_probe_once(uri), timeout)
    except asyncio.TimeoutError:
        return ProbeStatus.PORT_OCCUPIED_NON_ACP


async def _probe_once(uri: str) -> ProbeStatus:
    try:
        ws = await websockets.connect(uri, open_timeout=0.8, close_timeout=0.2)
    except Exception as exc:
        if _connection_refused(exc):
            return ProbeStatus.PORT_FREE
        return ProbeStatus.PORT_OCCUPIED_NON_ACP
    try:
        req_id = uuid.uuid4().hex[:8]
        await ws.send(json.dumps({"action": "ping", "req_id": req_id}))
        raw = await asyncio.wait_for(ws.recv(), 0.8)
        data = json.loads(raw)
        if data.get("event") == EventType.PONG.value or data.get("event") == "pong":
            return ProbeStatus.ALIVE_HUB
        return ProbeStatus.PORT_OCCUPIED_NON_ACP
    except Exception:
        return ProbeStatus.PORT_OCCUPIED_NON_ACP
    finally:
        try:
            await ws.close()
        except Exception:
            pass


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
        self._layout_started = False
        self._layout_closed = False
        self._decstbm_active = False
        self._auto_resize = False
        self._viewport: Optional[tuple[int, int]] = None
        self._feed_row = 3
        self._feed_at_bottom = False
        self._disconnect_noted = False
        self.roster_state: dict[str, str] = {}
        self.hub_mode = "client"
        self.display_port = self._port_from_uri(uri)
        self.owns_hub = False
        self.attached_notice = False

    @staticmethod
    def _port_from_uri(uri: str) -> int:
        try:
            return split_ws_uri(uri)[1]
        except Exception:
            return 8765

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
                if handle and handle.lower() != self.handle.lower() and status.lower() != AgentStatus.OFFLINE.value
            ]
        summary = " · ".join(f"{h} [{s.upper()}]" for h, s in online) if online else "none"
        line = f"Online Agents ({len(online)}): {summary}"
        return _clip_to_cells(line, max_cells) if max_cells is not None else line

    def _online_entries(self) -> list[tuple[str, str]]:
        with self._ui_lock:
            entries = []
            for handle, status in sorted(self.roster_state.items(), key=lambda item: item[0].lower()):
                if not handle or handle.lower() == self.handle.lower():
                    continue
                if str(status).lower() == AgentStatus.OFFLINE.value:
                    continue
                entries.append((handle, str(status).lower()))
        return entries

    def _online_header_text(self, budget: int) -> str:
        entries = self._online_entries()
        count = len(entries)
        if count == 0:
            return _clip_to_cells("Online (0): ○ waiting for agents", budget)
        prefix = f"Online ({count}): "
        parts = []
        for handle, status in entries:
            symbol, label = _STATUS_MARKS.get(status, ("●", status.upper()[:4]))
            parts.append(f"{symbol} {handle} {label}")

        def joined(shown: list[str], hidden: int) -> str:
            body = " · ".join(shown)
            if hidden <= 0:
                return prefix + body
            suffix = f"+{hidden}"
            return prefix + (f"{body} · {suffix}" if body else suffix)

        shown: list[str] = []
        for part in parts:
            trial = shown + [part]
            if cell_len(joined(trial, count - len(trial))) <= budget:
                shown = trial
            else:
                break
        while shown and cell_len(joined(shown, count - len(shown))) > budget:
            shown.pop()
        text = joined(shown, count - len(shown))
        if cell_len(text) > budget:
            text = _clip_to_cells(prefix.rstrip(), budget)
        return text

    def render_header_lines(self, width: Optional[int] = None) -> list[str]:
        """Fixed two-line header: hub mode on the left, online agents on the right."""
        cols = width if width is not None else (self._viewport[0] if self._viewport else self._out_console.width)
        cols = max(2, int(cols))
        max_cells = cols - 1
        if self.hub_mode == "host":
            left = f"⚡ ACP :{self.display_port} (host)"
        elif self.hub_mode == "disconnected":
            left = f"⚠ ACP :{self.display_port} (disconnected)"
        else:
            left = f"🔗 ACP :{self.display_port} (client)"
        left = _clip_to_cells(left, max_cells)
        gap = 2 if cell_len(left) + 2 < max_cells else (1 if cell_len(left) + 1 < max_cells else 0)
        budget = max(0, max_cells - cell_len(left) - gap)
        right = self._online_header_text(budget)
        if cell_len(right) > budget:
            right = _clip_to_cells(right, budget)
        pad = max(0, max_cells - cell_len(left) - cell_len(right))
        info = _clip_to_cells(left + (" " * pad) + right, max_cells)
        return [info, "─" * max_cells]

    def render_dock_lines(self, width: Optional[int] = None) -> tuple[list[str], int, int]:
        """Bottom dock: separator, `{handle}::>` draft, and the command bar."""
        cols = max(20, width if width is not None else (self._viewport[0] if self._viewport else self._out_console.width))
        max_cells = cols - 1

        label = "─ Draft Input (Enter send · Esc clear · /help manual) "
        fill = "─" * max(0, max_cells - cell_len(label))
        sep_line = _clip_to_cells(label + fill, max_cells)

        prompt = f"{self.handle}::> "
        prompt_cells = cell_len(prompt)
        avail = max(4, max_cells - prompt_cells)
        visible, rel_col = _window_around_cursor(self._input_buffer, self._cursor_pos, avail)
        input_line = _clip_to_cells(prompt + visible, max_cells)
        cursor_col = min(prompt_cells + rel_col, max_cells)

        full = (
            "Commands: /send @agent <msg> | /post [#topic] <msg> | "
            "/broadcast <msg> | /read | /roster | /remove | /prune | /help | /quit"
        )
        medium = "Cmds: /send | /post | /broadcast | /read | /roster | /remove | /prune | /help | /quit"
        tight = "Cmds: /send /post /broadcast /read /roster /remove /prune /help /quit"
        if cell_len(full) <= max_cells:
            cmd_line = full
        elif cell_len(medium) <= max_cells:
            cmd_line = medium
        elif cell_len(tight) <= max_cells:
            cmd_line = tight
        else:
            cmd_line = _clip_to_cells(tight, max_cells)

        return [sep_line, input_line, cmd_line], 1, cursor_col

    def begin_layout(self, cols: int, rows: int) -> None:
        """Install the fixed header, scroll region, and 3-line dock for a known viewport."""
        self._viewport = (max(1, int(cols)), max(1, int(rows)))
        self._dock_active = True
        self._running = True
        self._layout_started = True
        self._layout_closed = False
        self._feed_row = HEADER_LINES + 1
        self._feed_at_bottom = False
        if rows >= MIN_ROWS:
            bottom = rows - DOCK_LINES
            self._decstbm_active = True
            self._write_raw_stdout(f"\x1b[2J\x1b[H\x1b[3;{bottom}r")
            self._paint_header_full()
            self._draw_dock_fixed()
        else:
            self._decstbm_active = False
            self._draw_dock()

    def apply_resize(self, cols: int, rows: int) -> None:
        self._viewport = (max(1, int(cols)), max(1, int(rows)))
        if not self._layout_started:
            return
        if rows >= MIN_ROWS:
            bottom = rows - DOCK_LINES
            self._decstbm_active = True
            self._feed_row = min(max(self._feed_row, HEADER_LINES + 1), bottom)
            self._write_raw_stdout(f"\x1b[3;{bottom}r")
            self._paint_header_full()
            self._draw_dock_fixed()
        else:
            if self._decstbm_active:
                self._write_raw_stdout("\x1b[r")
            self._decstbm_active = False
            self._feed_at_bottom = False
            self._draw_dock()

    def _maybe_resize(self) -> None:
        if not self._auto_resize or not self._layout_started or not self._viewport:
            return
        size = shutil.get_terminal_size(fallback=self._viewport)
        current = (size.columns, size.lines)
        if current != self._viewport:
            self.apply_resize(*current)

    def _paint_header_full(self) -> None:
        cols = self._viewport[0] if self._viewport else self._out_console.width
        info, rule = self.render_header_lines(cols)
        self._write_raw_stdout(f"\x1b[1;1H\x1b[2K{info}\x1b[0m\x1b[2;1H\x1b[2K{rule}\x1b[0m")

    def _refresh_header_row(self) -> None:
        cols = self._viewport[0] if self._viewport else self._out_console.width
        info = self.render_header_lines(cols)[0]
        self._write_raw_stdout(f"\x1b7\x1b[1;1H\x1b[2K{info}\x1b[0m\x1b8")

    def _draw_dock_fixed(self) -> None:
        cols, rows = self._viewport or (self._out_console.width, 24)
        lines, cursor_row, cursor_col = self.render_dock_lines(cols)
        top = rows - len(lines) + 1
        parts = []
        for offset, line in enumerate(lines):
            parts.append(f"\x1b[{top + offset};1H\x1b[2K{line}\x1b[0m")
        parts.append(f"\x1b[{top + cursor_row};{cursor_col + 1}H")
        self._write_raw_stdout("".join(parts))
        self._dock_lines = len(lines)
        self._dock_cursor_row = cursor_row

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
        lines, cursor_row, cursor_col = self.render_dock_lines(
            self._viewport[0] if self._viewport else self._out_console.width
        )
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
            if not self._dock_active or not self._running:
                return
            if self._decstbm_active:
                self._draw_dock_fixed()
            else:
                self._clear_dock()
                self._draw_dock()

    def _shutdown_layout(self) -> None:
        if self._layout_closed:
            self._dock_active = False
            self._decstbm_active = False
            return
        rows = self._viewport[1] if self._viewport else 24
        if self._layout_started:
            self._write_raw_stdout(f"\x1b[r\x1b[0m\x1b[{max(1, rows)};1H\n")
        elif self._dock_lines:
            self._clear_dock()
        self._layout_closed = True
        self._layout_started = False
        self._dock_active = False
        self._decstbm_active = False
        self._dock_lines = 0
        self._dock_cursor_row = 0

    def _capture_lines(self, fn: Callable[[], None]) -> list[str]:
        buf = io.StringIO()
        cols = self._viewport[0] if self._viewport else self._out_console.width
        memory = Console(
            file=buf,
            width=max(20, cols),
            force_terminal=self._out_console.is_terminal,
            color_system=self._out_console.color_system,
            no_color=getattr(self._out_console, "no_color", not self._out_console.is_terminal),
            legacy_windows=False,
        )
        previous = self._console
        self._console = memory
        try:
            fn()
        finally:
            self._console = previous
        return buf.getvalue().splitlines()

    def _write_feed(self, lines: list[str]) -> None:
        if not lines or not self._viewport:
            return
        _cols, rows = self._viewport
        bottom = rows - DOCK_LINES
        if self._feed_at_bottom:
            self._write_raw_stdout(f"\x1b[{bottom};1H")
            for line in lines:
                self._write_raw_stdout(f"\n{line}\x1b[0m")
            return
        row = self._feed_row
        for line in lines:
            if row > bottom:
                self._feed_at_bottom = True
                self._write_raw_stdout(f"\x1b[{bottom};1H\n{line}\x1b[0m")
                row = bottom + 1
            else:
                self._write_raw_stdout(f"\x1b[{row};1H\x1b[2K{line}\x1b[0m")
                row += 1
        if not self._feed_at_bottom:
            self._feed_row = row

    def _emit(self, fn: Callable[[], None]) -> None:
        """Write fn() into the scroll region when DECSTBM is active, else above the dock."""
        with self._ui_lock:
            self._maybe_resize()
            if self._decstbm_active and self._dock_active and self._running:
                lines = self._capture_lines(fn)
                self._write_feed(lines)
                self._draw_dock_fixed()
                return
            dock_was_active = self._dock_active
            if dock_was_active:
                self._clear_dock()
            fn()
            if dock_was_active and self._running:
                self._draw_dock()

    def _print_card(self, sender: str, content: str, mode_badge: str, timestamp: str = "") -> None:
        width = self._viewport[0] if self._viewport else max(self._out_console.width, 54)
        markup = render_message_with_avatar(
            sender,
            content,
            mode_badge=mode_badge,
            timestamp=timestamp,
            max_width=width,
        )
        self._emit(lambda: self._out_console.print(markup))

    def print_banner(self) -> None:
        kind = "Embedded" if self.hub_mode == "host" else "Attached"
        uri = escape(self.uri)
        self._emit(
            lambda: self._out_console.print(
                Panel.fit(
                    f"[bold cyan]ACP Chatroom[/bold cyan] · {kind} Hub {uri} · /help for manual\n"
                    "[dim]Commands: /send /post /broadcast /read /roster /remove /prune /help /quit[/dim]",
                    border_style="cyan",
                )
            )
        )

    def print_help(self) -> None:
        """Display full command manual and keyboard shortcuts above the input dock."""
        self._emit(
            lambda: self._out_console.print(
                Panel.fit(
                    "[bold cyan]ACP Console Manual[/bold cyan]\n\n"
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
        msg = data.get("message", {})
        sender = str(msg.get("sender", "@unknown"))
        target = str(msg.get("target", "@unknown"))
        content = str(msg.get("content", ""))
        self._print_card(sender, content, f"⚡ EXPLICIT ➔ {target}", _format_stamp(msg.get("created_at", "")))

    def on_silent_message(self, data: dict) -> None:
        msg = data.get("message", {})
        sender = str(msg.get("sender", "@unknown"))
        topic = str(msg.get("topic", "general"))
        content = str(msg.get("content", ""))
        self._print_card(sender, content, f"📢 #{topic}", _format_stamp(msg.get("created_at", "")))

    def _remember_presence(self, raw_handle: str, raw_status: str) -> None:
        if not raw_handle or raw_handle.lower() == self.handle.lower():
            return
        with self._ui_lock:
            if raw_status == AgentStatus.OFFLINE.value:
                for key in [k for k in self.roster_state if k.lower() == raw_handle.lower()]:
                    self.roster_state.pop(key, None)
            else:
                for key in [k for k in self.roster_state if k.lower() == raw_handle.lower() and k != raw_handle]:
                    self.roster_state.pop(key, None)
                self.roster_state[raw_handle] = raw_status

    def on_presence_change(self, data: dict) -> None:
        raw_handle = str(data.get("handle", ""))
        raw_status = str(data.get("status", "")).lower()
        self._remember_presence(raw_handle, raw_status)
        if self._dock_active:
            self._refresh_header_row()

    def on_broadcast(self, data: dict) -> None:
        msg = data.get("message", {})
        sender = str(msg.get("sender", "@unknown"))
        content = str(msg.get("content", ""))
        mode = str(msg.get("mode", "broadcast")).lower()
        badge = "🚨 URGENT" if mode == MessageMode.URGENT.value else "📣 BROADCAST"
        self._print_card(sender, content, badge, _format_stamp(msg.get("created_at", "")))

    def mark_disconnected(self) -> None:
        if self._disconnect_noted:
            return
        self._disconnect_noted = True
        self.hub_mode = "disconnected"
        self._emit(lambda: self._out_console.print("[bold yellow]Hub disconnected. The session is no longer connected.[/bold yellow]"))
        if self._dock_active:
            self._refresh_header_row()

    def _bind_events(self) -> None:
        self.client.on(EventType.EXPLICIT_MESSAGE.value, self.on_explicit_message)
        self.client.on(EventType.SILENT_MESSAGE.value, self.on_silent_message)
        self.client.on(EventType.BROADCAST.value, self.on_broadcast)
        self.client.on(EventType.PRESENCE_CHANGE.value, self.on_presence_change)

    async def show_roster(self) -> None:
        await self.sync_roster(display_table=True)

    async def sync_roster(self, display_table: bool = False) -> None:
        await self.client.get_roster(on_roster=lambda roster: self._apply_roster_snapshot(roster, display_table))

    def _apply_roster_snapshot(self, roster: list[dict], display_table: bool = True) -> None:
        with self._ui_lock:
            self.roster_state = {
                str(row.get("handle", "")): str(row.get("status", "offline")).lower()
                for row in roster
                if row.get("handle") and str(row.get("handle")).lower() != self.handle.lower()
            }
        if not display_table:
            if self._dock_active:
                self._refresh_header_row()
            return
        table = Table(title="Active Agent Roster", border_style="blue")
        table.add_column("Agent Handle", style="cyan", no_wrap=True)
        table.add_column("Status", style="bold")
        table.add_column("Last Seen (UTC)", style="dim")
        for row in roster:
            status = row.get("status", "offline")
            color = "green" if status == "idle" else ("yellow" if status == "busy" else "red")
            table.add_row(row.get("handle", ""), f"[{color}]{status.upper()}[/{color}]", str(row.get("last_seen", ""))[:19])
        self._emit(lambda: self._out_console.print(table))

    def _schedule(self, coro, loop: Optional[asyncio.AbstractEventLoop]) -> None:
        if loop is not None:
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            asyncio.create_task(coro)

    def _show_self_card(self, content: str, mode_badge: str, *, queued: bool = False) -> None:
        body = f"{content}\n(offline · queued)" if queued else content
        self._print_card(self.handle, body, mode_badge)

    async def _do_explicit(self, target: str, msg: str) -> None:
        try:
            res = await self.client.send_message(
                target=target, content=msg, mode=MessageMode.EXPLICIT, sender=self.handle,
            )
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]Send failed: {escape(str(exc))}[/red]"))
            return
        if not isinstance(res, dict) or res.get("status") != "ok":
            if isinstance(res, dict):
                err = escape(str(res.get("error", "Send failed")))
                self._emit(lambda: self._out_console.print(f"[red]{err}[/red]"))
            return
        self._show_self_card(msg, f"⚡ EXPLICIT ➔ {target}", queued=not bool(res.get("delivered")))

    async def _do_post(self, topic: str, msg: str) -> None:
        try:
            await self.client.send_message(
                target=f"#{topic}", content=msg, mode=MessageMode.SILENT, topic=topic, sender=self.handle,
            )
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]Post failed: {escape(str(exc))}[/red]"))

    async def _do_broadcast(self, msg: str, urgent: bool) -> None:
        try:
            res = await self.client.send_message(
                target="#general",
                content=msg,
                mode=MessageMode.URGENT if urgent else MessageMode.BROADCAST,
                topic="general",
                sender=self.handle,
            )
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]Broadcast failed: {escape(str(exc))}[/red]"))
            return
        if urgent or not isinstance(res, dict) or res.get("status") != "ok":
            return
        self._show_self_card(msg, "📣 BROADCAST")

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
            parts = raw.split(maxsplit=2)
            if len(parts) < 3:
                self._emit(lambda: self._out_console.print("[red]Usage: /send @target <message>[/red]"))
                return True
            self._schedule(self._do_explicit(parts[1], parts[2]), loop)
        elif raw == "/post" or raw.startswith("/post "):
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
            self._schedule(self._do_post(topic, msg), loop)
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
            self._schedule(self._do_broadcast(rest, urgent), loop)
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

    def handle_key(self, key: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Apply one keystroke or named navigation key to the separated draft input buffer."""
        submit_line: Optional[str] = None
        echo_line: Optional[str] = None
        with self._ui_lock:
            if key in ("\r", "\n"):
                submit_line = self._input_buffer
                self._input_buffer = ""
                self._cursor_pos = 0
                self._history_index = None
                self._draft_stash = ""
                if submit_line.strip():
                    self._history.append(submit_line)
                    if self._dock_active and not _is_chat_command(submit_line):
                        echo_line = submit_line
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

            stopping = not self._running

        if stopping:
            self._shutdown_layout()
        elif echo_line and self._decstbm_active:
            shown = echo_line
            self._emit(lambda: self._out_console.print(f"[dim]{escape(self.handle)}::> {escape(shown)}[/dim]"))
        elif self._dock_active and self._decstbm_active:
            self._draw_dock_fixed()
        elif self._dock_active:
            if submit_line is None:
                self._clear_dock()
            if echo_line:
                shown = echo_line
                self._out_console.print(f"[dim]{escape(self.handle)}::> {escape(shown)}[/dim]")
            if self._running:
                self._draw_dock()

        if submit_line is not None and submit_line.strip():
            if not self.handle_command(submit_line, loop=loop):
                self._shutdown_layout()

    def _read_tty_keys(self, loop: asyncio.AbstractEventLoop) -> None:
        if os.name == "nt":
            arrow_map = {"K": "LEFT", "M": "RIGHT", "G": "HOME", "O": "END", "S": "DELETE", "H": "UP", "P": "DOWN"}
            decoder = codecs.getincrementaldecoder("utf-16-le")(errors="replace")
            while self._running:
                if not msvcrt.kbhit():
                    time.sleep(0.02)
                    continue
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

    async def watch_connection(self) -> None:
        """Poll until an attached hub drops, then switch the header once."""
        self._running = True
        try:
            while self._running:
                await asyncio.sleep(0.05)
                self._maybe_resize()
                if not self.owns_hub and not self.client.connected:
                    self.mark_disconnected()
                    return
        finally:
            self._running = False

    async def run_ui(self, *, already_connected: bool = False) -> None:
        self._running = True
        try:
            if not already_connected:
                try:
                    await self.client.connect(initial_status=AgentStatus.IDLE, role="console")
                except Exception as exc:
                    self._out_console.print(
                        f"[bold red]Failed to connect to ACP Hub at {escape(self.uri)}: {escape(str(exc))}[/bold red]"
                    )
                    return
                self._bind_events()
                await self.sync_roster(display_table=False)

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
                except Exception as exc:
                    self._emit(lambda: self._out_console.print(f"[red]Error: {escape(str(exc))}[/red]"))

            with _raw_tty_mode() as tty_raw:
                if tty_raw:
                    size = shutil.get_terminal_size(fallback=(80, 24))
                    self._auto_resize = True
                    self.begin_layout(size.columns, size.lines)
                self.print_banner()
                if self.attached_notice:
                    self._emit(
                        lambda: self._out_console.print(
                            "[yellow]Attached to an existing Hub. The requested host or database was not used to start a new one.[/yellow]"
                        )
                    )
                input_thread = threading.Thread(target=user_input_thread, args=(tty_raw,), daemon=True)
                input_thread.start()
                try:
                    while self._running:
                        await asyncio.sleep(0.3)
                        self._maybe_resize()
                        if not self.owns_hub and self.client.ws is not None and not self.client.connected:
                            self.mark_disconnected()
                finally:
                    self._running = False
                    self._shutdown_layout()
        finally:
            await self.client.close()

    async def run(self) -> None:
        await self.run_ui(already_connected=False)
        self._out_console.print("\n[dim]Console disconnected.[/dim]")

    async def _do_read(self, topic: str) -> None:
        messages = await self.client.read_board(topic=topic, reader=self.handle, unread_only=False, limit=20)

        def _render() -> None:
            self._out_console.print(
                f"\n[bold cyan]Recent messages in #{escape(topic)} (count: {len(messages)}):[/bold cyan]\n"
            )
            for idx, message in enumerate(messages):
                if idx > 0:
                    self._out_console.print()
                sender = str(message.get("sender", "@unknown"))
                content = str(message.get("content", ""))
                mode = str(message.get("mode", "silent")).lower()
                if mode == MessageMode.EXPLICIT.value:
                    badge = f"⚡ EXPLICIT ➔ {message.get('target', '')}"
                elif mode == MessageMode.URGENT.value:
                    badge = "🚨 URGENT"
                elif mode == MessageMode.BROADCAST.value:
                    badge = "📣 BROADCAST"
                else:
                    badge = f"📢 #{message.get('topic', topic)}"
                self._out_console.print(
                    render_message_with_avatar(
                        sender,
                        content,
                        mode_badge=badge,
                        timestamp=_format_stamp(message.get("created_at", "")),
                        max_width=self._viewport[0] if self._viewport else max(self._out_console.width, 54),
                    )
                )
            self._out_console.print()

        self._emit(_render)

    def _drop_roster(self, handles: list[str]) -> None:
        wanted = {handle.lower() for handle in handles}
        with self._ui_lock:
            for key in [k for k in self.roster_state if k.lower() in wanted]:
                self.roster_state.pop(key, None)
        if self._dock_active:
            self._refresh_header_row()

    async def _do_remove(self, target: str) -> None:
        try:
            res = await self.client.remove_agent(target)
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]Remove failed: {escape(str(exc))}[/red]"))
            return
        if res.get("status") != "ok":
            err = escape(str(res.get("error", "Remove failed")))
            self._emit(lambda: self._out_console.print(f"[red]{err}[/red]"))
            return
        handle = str(res.get("handle", target))
        self._emit(lambda: self._out_console.print(f"[green]Removed {escape(handle)}[/green]"))
        self._drop_roster([handle])

    async def _do_prune(self) -> None:
        try:
            res = await self.client.prune_roster()
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]Prune failed: {escape(str(exc))}[/red]"))
            return
        if res.get("status") != "ok":
            err = escape(str(res.get("error", "Prune failed")))
            self._emit(lambda: self._out_console.print(f"[red]{err}[/red]"))
            return
        removed = list(res.get("removed", []))
        if removed:
            names = escape(", ".join(removed))
            self._emit(lambda: self._out_console.print(f"[green]Pruned {len(removed)} offline agent(s): {names}[/green]"))
        else:
            self._emit(lambda: self._out_console.print("[dim]No offline agents to prune.[/dim]"))
        self._drop_roster(removed)


class UnifiedConsoleRunner:
    """Probe, optionally embed ACPHub, and drive one HumanConsole in the same loop."""

    def __init__(
        self,
        uri: Optional[str] = None,
        handle: str = "@user",
        db_path: str = "acp_chatroom.db",
        auto_hub: bool = True,
        bind_host: Optional[str] = None,
        bind_port: Optional[int] = None,
        explicit_hub_options: bool = False,
        ui_console: Optional[Console] = None,
    ):
        self.uri = uri or "ws://127.0.0.1:8765"
        self.handle = handle if str(handle).startswith("@") else f"@{handle}"
        self.db_path = db_path
        self.auto_hub = auto_hub
        self.bind_host = bind_host
        self.bind_port = bind_port
        self.explicit_hub_options = explicit_hub_options
        self.ui_console = ui_console
        self.owns_hub = False
        self.hub: Optional[ACPHub] = None
        self.console: Optional[HumanConsole] = None
        self.bound_port: Optional[int] = None
        self.client_uri = self.uri
        self.attached_notice = False
        self._log_cm = None
        self._stopped = False

    def _enter_silence(self) -> None:
        if self._log_cm is None:
            self._log_cm = _silence_embedded_hub_logging()
            self._log_cm.__enter__()

    def _exit_silence(self) -> None:
        if self._log_cm is not None:
            self._log_cm.__exit__(None, None, None)
            self._log_cm = None

    async def _embed(self, host: str, port: int) -> None:
        self._enter_silence()
        self.hub = ACPHub(host=host, port=port, db_path=self.db_path)
        try:
            await self.hub.start()
        except OSError as exc:
            failed = self.hub
            self.hub = None
            self._exit_silence()
            if failed is not None:
                try:
                    await failed.stop()
                except Exception:
                    pass
            if _addr_in_use(exc):
                status = await probe_hub(self.uri)
                if status is ProbeStatus.ALIVE_HUB:
                    self.owns_hub = False
                    self.client_uri = self.uri
                    self.bound_port = split_ws_uri(self.uri)[1]
                    if self.explicit_hub_options:
                        self.attached_notice = True
                    return
            raise
        self.owns_hub = True
        self.bound_port = self.hub.bound_port
        self.client_uri = client_connect_uri(host, self.bound_port)

    async def _ensure_hub(self) -> None:
        host, port = split_ws_uri(self.uri)
        bind_host = self.bind_host or host
        bind_port = self.bind_port if self.bind_port is not None else port
        local = _is_local_host(host) and _is_local_host(bind_host)
        if self.auto_hub and local and (bind_port == 0 or port == 0 or local_port_available(bind_host, bind_port)):
            await self._embed(bind_host, 0 if bind_port == 0 or port == 0 else bind_port)
            return
        if not self.auto_hub or not local:
            status = await probe_hub(self.uri)
            if status is not ProbeStatus.ALIVE_HUB:
                raise HubConnectionError(
                    f"ACP Hub at {self.uri} is not reachable ({status.value}). Refusing to bind a local hub.",
                    reason=status.value,
                )
            self.owns_hub = False
            self.client_uri = self.uri
            self.bound_port = port
            return
        status = await probe_hub(self.uri)
        if status is ProbeStatus.ALIVE_HUB:
            self.owns_hub = False
            self.client_uri = self.uri
            self.bound_port = port
            if self.explicit_hub_options:
                self.attached_notice = True
            return
        if status is ProbeStatus.PORT_OCCUPIED_NON_ACP:
            raise HubConnectionError(
                f"Port {self.uri} is occupied by a non-ACP service. Refusing to attach.",
                reason=status.value,
            )
        await self._embed(bind_host, bind_port)

    async def start(self) -> None:
        await self._ensure_hub()
        self.console = HumanConsole(uri=self.client_uri, handle=self.handle, ui_console=self.ui_console)
        self.console.hub_mode = "host" if self.owns_hub else "client"
        self.console.display_port = int(self.bound_port or self.console.display_port)
        self.console.owns_hub = self.owns_hub
        self.console.attached_notice = self.attached_notice
        try:
            await self.console.client.connect(initial_status=AgentStatus.IDLE, role="console")
        except Exception:
            await self.shutdown()
            raise
        self.console._bind_events()
        await self.console.sync_roster(display_table=False)

    async def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.console is not None:
            self.console._running = False
            self.console._shutdown_layout()
            try:
                await self.console.client.close()
            except Exception:
                pass
        hub = self.hub if self.owns_hub else None
        self.owns_hub = False
        if hub is not None:
            try:
                await hub.stop()
            finally:
                self.hub = None
        self._exit_silence()

    async def run(self) -> None:
        try:
            await self.start()
            assert self.console is not None
            await self.console.run_ui(already_connected=True)
        finally:
            await self.shutdown()


def start_unified_console(
    uri: Optional[str] = None,
    handle: str = "@user",
    db_path: str = "acp_chatroom.db",
    auto_hub: bool = True,
    bind_host: Optional[str] = None,
    bind_port: Optional[int] = None,
    explicit_hub_options: bool = False,
) -> None:
    runner = UnifiedConsoleRunner(
        uri=uri,
        handle=handle,
        db_path=db_path,
        auto_hub=auto_hub,
        bind_host=bind_host,
        bind_port=bind_port,
        explicit_hub_options=explicit_hub_options,
    )
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        pass
    except HubConnectionError as exc:
        console.print(f"[bold red]{escape(str(exc))}[/bold red]")
        raise SystemExit(1) from exc


def start_console(uri: str = "ws://127.0.0.1:8765", handle: str = "@user") -> None:
    start_unified_console(uri=uri, handle=handle, auto_hub=True)
