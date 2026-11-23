"""
ACP Console - Unified Human Observation & Interaction Terminal
Allows human operators to monitor multi-agent chat activity and inject explicit prompts.
"""

import asyncio
import codecs
from collections import deque
from contextlib import contextmanager
from enum import Enum
import io
import json
import logging
import os
import select
import shutil
import signal
import socket
import sys
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlparse
import uuid
from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
import websockets

from .avatars import render_message_with_avatar
from .hub import ACPHub
from .protocol import ActionType, MessageMode, AgentStatus, EventType, get_utc_now_iso
from .client import ACPClient

if os.name == "nt":
    import ctypes
    import msvcrt
else:
    import termios

console = Console()

HEADER_LINES = 2
DOCK_LINES = 3
MIN_DECSTBM_ROWS = 8


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
    out: list[str] = []
    cur = ""
    for ch in text:
        candidate = cur + ch
        if cell_len(candidate) > max_cells:
            break
        out.append(ch)
        cur = candidate
    return cur


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
        w = left_widths[start - 1]
        if used + w > left_budget:
            break
        used += w
        start -= 1

    visible_right = _clip_to_cells(text[cursor_pos:], max_cells - used)
    visible = text[start:cursor_pos] + visible_right
    return visible, used


def _status_token(status: str) -> tuple[str, str, str]:
    """Return (single_cell_symbol, short_label, ansi_sgr_code) for an agent status."""
    s = (status or "").lower()
    if s == AgentStatus.IDLE.value:
        return "●", "IDLE", "\x1b[32m"
    if s == AgentStatus.BUSY.value:
        return "●", "BUSY", "\x1b[33m"
    if s == AgentStatus.STARTING.value:
        return "◌", "START", "\x1b[36m"
    if s == AgentStatus.BLOCKED.value:
        return "●", "BLK", "\x1b[31m"
    return "●", (s.upper()[:5] or "ON"), "\x1b[37m"


class HumanConsole:
    def __init__(
        self,
        uri: str = "ws://127.0.0.1:8765",
        handle: str = "@user",
        ui_console: Optional[Console] = None,
        *,
        hub_mode: str = "client",
        hub_warning: Optional[str] = None,
        log_buffer: Optional[deque[str]] = None,
    ):
        self.uri = uri
        self.handle = handle if handle.startswith("@") else f"@{handle}"
        self.client = ACPClient(uri=self.uri, handle=self.handle)
        self._console = ui_console
        self._render_console: Optional[Console] = None
        self._running = False
        self._ui_lock = threading.RLock()
        self._input_buffer = ""
        self._cursor_pos = 0
        self._history: list[str] = []
        self._history_index: Optional[int] = None
        self._draft_stash = ""
        self._dock_active = False
        self._decstbm_active = False
        self._viewport: tuple[int, int] = (80, 24)
        self._feed_top = HEADER_LINES + 1
        self._feed_bottom = 24 - DOCK_LINES
        self._feed_row = HEADER_LINES + 1
        self._feed_at_bottom = False
        self._resize_pending = False
        self._dock_lines = 0
        self._dock_cursor_row = 0
        self.roster_state: dict[str, str] = {}
        self._hub_mode = hub_mode
        self._hub_warning = hub_warning
        self._log_buffer = log_buffer
        self._disconnected = False
        self._disconnect_notified = False

    @property
    def _base_console(self) -> Console:
        return self._console if self._console is not None else console

    @property
    def _out_console(self) -> Console:
        if self._render_console is not None:
            return self._render_console
        return self._base_console

    def _use_color(self) -> bool:
        base = self._base_console
        return not base.no_color and base.color_system is not None

    def _terminal_size(self) -> tuple[int, int]:
        ts = shutil.get_terminal_size((80, 24))
        if self._console is not None:
            cols = self._console._width if self._console._width is not None else ts.columns
            rows = self._console._height if self._console._height is not None else ts.lines
            return max(20, int(cols)), max(1, int(rows))
        return max(20, int(ts.columns)), max(1, int(ts.lines))

    def _write_raw_stdout(self, text: str) -> None:
        out = self._base_console.file
        out.write(text)
        out.flush()

    def _port_label(self) -> int:
        parsed = urlparse(self.uri)
        return parsed.port or 8765

    def _sorted_online_entries(self) -> list[tuple[str, str]]:
        with self._ui_lock:
            return [
                (handle, status)
                for handle, status in sorted(self.roster_state.items())
                if handle and handle.lower() != self.handle.lower() and status.lower() != AgentStatus.OFFLINE.value
            ]

    def format_online_agents_bar(self, max_cells: Optional[int] = None) -> str:
        """Build a single-line summary of currently online agents and their statuses."""
        online = self._sorted_online_entries()
        summary = " · ".join(f"{h} [{s.upper()}]" for h, s in online) if online else "none"
        line = f"Online Agents ({len(online)}): {summary}"
        return _clip_to_cells(line, max_cells) if max_cells is not None else line

    def _build_header_row1(self, width: Optional[int] = None, styled: bool = False) -> str:
        cols = max(20, width if width is not None else self._terminal_size()[0])
        max_cells = cols - 1
        port = self._port_label()

        if self._disconnected:
            left_plain = f"⚠ ACP :{port} (disconnected)"
            left_styled = f"\x1b[1;31m{left_plain}\x1b[0m"
            short_left_plain = f"⚠ :{port}"
            short_left_styled = f"\x1b[1;31m{short_left_plain}\x1b[0m"
        elif self._hub_mode == "host":
            left_plain = f"⚡ ACP :{port} (host)"
            left_styled = f"\x1b[1;36m⚡ ACP :{port}\x1b[0m \x1b[32m(host)\x1b[0m"
            short_left_plain = f"⚡ :{port}"
            short_left_styled = f"\x1b[1;36m{short_left_plain}\x1b[0m"
        else:
            left_plain = f"🔗 ACP :{port} (client)"
            left_styled = f"\x1b[1;36m🔗 ACP :{port}\x1b[0m \x1b[34m(client)\x1b[0m"
            short_left_plain = f"🔗 :{port}"
            short_left_styled = f"\x1b[1;36m{short_left_plain}\x1b[0m"

        online = self._sorted_online_entries()
        n_online = len(online)

        if cols < 50:
            left_plain = short_left_plain
            left_styled = short_left_styled

        left_cells = cell_len(left_plain)
        right_budget = max_cells - left_cells - 2
        if right_budget < 14 and left_plain != short_left_plain:
            left_plain = short_left_plain
            left_styled = short_left_styled
            left_cells = cell_len(left_plain)
            right_budget = max(8, max_cells - left_cells - 1)

        prefix_plain = f"Online ({n_online}): "
        prefix_styled = f"\x1b[1mOnline ({n_online}):\x1b[0m "

        if n_online == 0:
            full_right_plain = f"{prefix_plain}○ waiting for agents"
            full_right_styled = f"{prefix_styled}\x1b[2m○ waiting for agents\x1b[0m"
            if cell_len(full_right_plain) <= right_budget:
                right_plain = full_right_plain
                right_styled = full_right_styled
            else:
                right_plain = _clip_to_cells(full_right_plain, max(0, right_budget))
                right_styled = right_plain
        else:
            item_plains: list[str] = []
            item_styleds: list[str] = []
            for h, st in online:
                sym, lbl, sgr = _status_token(st)
                item_plains.append(f"{sym} {h} {lbl}")
                item_styleds.append(f"{sgr}{sym}\x1b[0m {h} {sgr}{lbl}\x1b[0m")

            all_plain = prefix_plain + " · ".join(item_plains)
            if cell_len(all_plain) <= right_budget:
                right_plain = all_plain
                right_styled = prefix_styled + " · ".join(item_styleds)
            else:
                chosen_k = 0
                for k in range(n_online - 1, 0, -1):
                    rem = n_online - k
                    cand = prefix_plain + " · ".join(item_plains[:k]) + f" · +{rem}"
                    if cell_len(cand) <= right_budget:
                        chosen_k = k
                        break
                if chosen_k > 0:
                    rem = n_online - chosen_k
                    right_plain = prefix_plain + " · ".join(item_plains[:chosen_k]) + f" · +{rem}"
                    right_styled = (
                        prefix_styled
                        + " · ".join(item_styleds[:chosen_k])
                        + f" · \x1b[2m+{rem}\x1b[0m"
                    )
                else:
                    fallback = f"Online ({n_online}): +{n_online}"
                    if cell_len(fallback) <= right_budget:
                        right_plain = fallback
                        right_styled = f"\x1b[1mOnline ({n_online}):\x1b[0m \x1b[2m+{n_online}\x1b[0m"
                    else:
                        right_plain = _clip_to_cells(fallback, max(0, right_budget))
                        right_styled = right_plain

        right_cells = cell_len(right_plain)
        if left_cells + 1 + right_cells > max_cells:
            avail_left = max(0, max_cells - right_cells - 1)
            left_plain = _clip_to_cells(left_plain, avail_left)
            left_styled = left_plain
            left_cells = cell_len(left_plain)

        gap_len = max(1, max_cells - left_cells - right_cells)
        gap = " " * gap_len
        plain_line = _clip_to_cells(f"{left_plain}{gap}{right_plain}", max_cells)
        if not styled or not self._use_color():
            return plain_line
        return f"{left_styled}{gap}{right_styled}"

    def render_header_lines(self, width: Optional[int] = None) -> list[str]:
        """Render the fixed 2-line top header bar (plain text without ANSI escapes)."""
        cols = max(20, width if width is not None else self._terminal_size()[0])
        max_cells = cols - 1
        row1 = self._build_header_row1(width=cols, styled=False)
        row2 = "─" * max_cells
        return [row1, row2]

    def render_dock_lines(self, width: Optional[int] = None) -> tuple[list[str], int, int]:
        """Build the 3-line bottom dock (separator, @user::> draft input, command bar)."""
        cols = max(20, width if width is not None else self._terminal_size()[0])
        max_cells = cols - 1

        label = "─ Draft Input (Enter send · Esc clear · /help manual) "
        fill = "─" * max(0, max_cells - cell_len(label))
        sep_line = _clip_to_cells(label + fill, max_cells)

        prompt = f"{self.handle}::> "
        prompt_cells = cell_len(prompt)
        if prompt_cells >= max_cells:
            input_line = _clip_to_cells(prompt, max_cells)
            cursor_col = cell_len(input_line)
        else:
            avail = max(4, max_cells - prompt_cells)
            visible, rel_col = _window_around_cursor(self._input_buffer, self._cursor_pos, avail)
            input_line = _clip_to_cells(prompt + visible, max_cells)
            cursor_col = min(max_cells, prompt_cells + rel_col)

        cmd_bar_full = (
            "Commands: /send @agent <msg> | /post [#topic] <msg> | "
            "/broadcast <msg> | /read | /roster | /remove | /prune | /help | /quit"
        )
        cmd_bar_medium = (
            "Cmds: /send | /post | /broadcast | /read | /roster | /remove | /prune | /help | /quit"
        )
        cmd_bar_compact = (
            "Cmds: /send /post /broadcast /read /roster /remove /prune /help /quit"
        )
        if cell_len(cmd_bar_full) <= max_cells:
            cmd_line = cmd_bar_full
        elif cell_len(cmd_bar_medium) <= max_cells:
            cmd_line = cmd_bar_medium
        elif cell_len(cmd_bar_compact) <= max_cells:
            cmd_line = cmd_bar_compact
        else:
            cmd_line = _clip_to_cells(cmd_bar_compact, max_cells)

        return [sep_line, input_line, cmd_line], 1, cursor_col

    def _build_header_draw_seq(self, cols: int) -> str:
        max_cells = cols - 1
        row1 = self._build_header_row1(width=cols, styled=True)
        row2_plain = "─" * max_cells
        row2 = f"\x1b[2m{row2_plain}\x1b[0m" if self._use_color() else row2_plain
        return f"\x1b[1;1H\x1b[2K{row1}\x1b[0m\x1b[2;1H\x1b[2K{row2}\x1b[0m"

    def _build_dock_draw_seq(self, cols: int, rows: int) -> str:
        lines, cursor_row, cursor_col = self.render_dock_lines(width=cols)
        dock_top = rows - DOCK_LINES + 1
        use_color = self._use_color()
        l0 = f"\x1b[2m{lines[0]}\x1b[0m" if use_color else lines[0]
        l1 = lines[1]
        l2 = f"\x1b[2m{lines[2]}\x1b[0m" if use_color else lines[2]
        input_row = dock_top + cursor_row
        return (
            f"\x1b[{dock_top};1H\x1b[2K{l0}\x1b[0m"
            f"\x1b[{dock_top + 1};1H\x1b[2K{l1}\x1b[0m"
            f"\x1b[{dock_top + 2};1H\x1b[2K{l2}\x1b[0m"
            f"\x1b[{input_row};{cursor_col + 1}H"
        )

    def _clear_relative_dock(self) -> None:
        if not self._dock_lines:
            return
        parts = ["\r"]
        if self._dock_cursor_row > 0:
            parts.append(f"\x1b[{self._dock_cursor_row}A")
        parts.append("\x1b[J")
        self._write_raw_stdout("".join(parts))
        self._dock_lines = 0
        self._dock_cursor_row = 0

    def _draw_relative_dock(self, cols: Optional[int] = None) -> None:
        width = cols if cols is not None else self._terminal_size()[0]
        lines, cursor_row, cursor_col = self.render_dock_lines(width)
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

    def _enter_tui(self) -> None:
        """Initialize the fixed top header, DECSTBM scroll region, and 3-line bottom dock."""
        with self._ui_lock:
            cols, rows = self._terminal_size()
            self._viewport = (cols, rows)
            self._dock_active = True
            self._resize_pending = False
            if rows >= MIN_DECSTBM_ROWS:
                self._decstbm_active = True
                self._feed_top = HEADER_LINES + 1
                self._feed_bottom = rows - DOCK_LINES
                self._feed_row = self._feed_top
                self._feed_at_bottom = False
                seq = (
                    "\x1b[2J\x1b[H"
                    f"\x1b[{self._feed_top};{self._feed_bottom}r"
                    + self._build_header_draw_seq(cols)
                    + self._build_dock_draw_seq(cols, rows)
                )
                self._write_raw_stdout(seq)
            else:
                self._decstbm_active = False
                self._draw_relative_dock(cols)

    def _teardown_tui(self) -> None:
        """Reset DECSTBM scroll region and restore clean terminal state on exit."""
        with self._ui_lock:
            if not self._dock_active:
                return
            if self._decstbm_active:
                _, rows = self._viewport
                self._write_raw_stdout(f"\x1b[r\x1b[0m\x1b[{rows};1H\n")
                self._decstbm_active = False
            else:
                self._clear_relative_dock()
            self._dock_active = False

    def _handle_resize(self, cols: int, rows: int) -> None:
        """Reconfigure DECSTBM boundaries and redraw top header & bottom dock on resize."""
        self._viewport = (cols, rows)
        if rows >= MIN_DECSTBM_ROWS:
            was_decstbm = self._decstbm_active
            self._decstbm_active = True
            self._feed_top = HEADER_LINES + 1
            self._feed_bottom = rows - DOCK_LINES
            if not was_decstbm:
                self._clear_relative_dock()
                self._feed_row = self._feed_top
                self._feed_at_bottom = False
            else:
                self._feed_row = max(self._feed_top, min(self._feed_row, self._feed_bottom))
                if self._feed_row >= self._feed_bottom:
                    self._feed_at_bottom = True
            seq = (
                f"\x1b[{self._feed_top};{self._feed_bottom}r"
                + self._build_header_draw_seq(cols)
                + self._build_dock_draw_seq(cols, rows)
            )
            self._write_raw_stdout(seq)
        else:
            if self._decstbm_active:
                self._write_raw_stdout("\x1b[r\x1b[0m")
                self._decstbm_active = False
            self._clear_relative_dock()
            if self._running:
                self._draw_relative_dock(cols)

    def _check_resize_locked(self) -> bool:
        if not self._dock_active:
            return False
        cols, rows = self._terminal_size()
        if not self._resize_pending and (cols, rows) == self._viewport:
            return False
        self._resize_pending = False
        self._handle_resize(cols, rows)
        return True

    def _refresh_header_row1(self) -> None:
        """Atomically refresh Row 1 of the fixed top header without moving the active input cursor."""
        with self._ui_lock:
            if not self._dock_active:
                return
            self._check_resize_locked()
            if not self._decstbm_active:
                return
            cols, _ = self._viewport
            row1 = self._build_header_row1(width=cols, styled=True)
            self._write_raw_stdout(f"\x1b7\x1b[1;1H\x1b[2K{row1}\x1b[0m\x1b8")

    def _redraw_dock(self) -> None:
        with self._ui_lock:
            if not self._dock_active:
                return
            if not self._running:
                self._teardown_tui()
                return
            self._check_resize_locked()
            if self._decstbm_active:
                cols, rows = self._viewport
                self._write_raw_stdout(self._build_dock_draw_seq(cols, rows))
            else:
                self._clear_relative_dock()
                self._draw_relative_dock(self._viewport[0])

    def _emit(self, fn: Callable[[], None]) -> None:
        """Render fn() into the main message stream (inside DECSTBM scroll region when active)."""
        with self._ui_lock:
            if not self._dock_active:
                fn()
                return

            self._check_resize_locked()

            if not self._decstbm_active:
                self._clear_relative_dock()
                fn()
                if self._running:
                    self._draw_relative_dock(self._viewport[0])
                return

            cols, rows = self._viewport
            base = self._base_console
            buf = io.StringIO()
            use_color = self._use_color()
            tmp_console = Console(
                file=buf,
                width=cols,
                color_system=base.color_system if use_color else None,
                force_terminal=use_color,
                no_color=not use_color,
                legacy_windows=False,
            )
            prev_render = self._render_console
            self._render_console = tmp_console
            try:
                fn()
            finally:
                self._render_console = prev_render

            raw_text = buf.getvalue().replace("\r\n", "\n")
            if not raw_text:
                return
            lines = raw_text.split("\n")
            if raw_text.endswith("\n"):
                lines.pop()

            parts: list[str] = []
            for line in lines:
                if self._feed_at_bottom:
                    parts.append(f"\x1b[{self._feed_bottom};1H\n\x1b[2K{line}\x1b[0m")
                else:
                    parts.append(f"\x1b[{self._feed_row};1H\x1b[2K{line}\x1b[0m")
                    if self._feed_row >= self._feed_bottom:
                        self._feed_at_bottom = True
                    else:
                        self._feed_row += 1

            if self._running:
                _, cursor_row, cursor_col = self.render_dock_lines(width=cols)
                input_row = (rows - DOCK_LINES + 1) + cursor_row
                parts.append(f"\x1b[{input_row};{cursor_col + 1}H")

            self._write_raw_stdout("".join(parts))

    def _emit_message_card(
        self,
        *,
        sender: str,
        content: str,
        mode: str,
        target: Optional[str] = None,
        topic: Optional[str] = None,
        timestamp: str = "",
        note: str = "",
    ) -> None:
        def _render() -> None:
            width = self._viewport[0] if self._dock_active else self._terminal_size()[0]
            card = render_message_with_avatar(
                sender=sender,
                content=content,
                mode=mode,
                target=target,
                topic=topic,
                timestamp=timestamp,
                note=note,
                max_width=width,
            )
            self._out_console.print(card, highlight=False)

        self._emit(_render)

    def print_banner(self) -> None:
        mode_tag = "Embedded Hub" if self._hub_mode == "host" else "Attached Hub"
        banner_line = (
            f"  [bold cyan]╭─ ACP Chatroom · {mode_tag} {escape(self.uri)} · "
            f"as {escape(self.handle)} · /help for manual ─╮[/bold cyan]"
        )
        def _render() -> None:
            self._out_console.print(banner_line, highlight=False)
            if self._hub_warning:
                self._out_console.print(f"  [yellow]⚠ {escape(self._hub_warning)}[/yellow]", highlight=False)
        self._emit(_render)

    def print_help(self) -> None:
        """Display full command manual and keyboard shortcuts in the scroll area."""
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
                    "  [blue]/logs[/blue]                          Show recent embedded Hub diagnostics\n"
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
        note = "(offline · queued)" if data.get("delivered") is False else ""
        self._emit_message_card(
            sender=str(msg.get("sender", "@unknown")),
            content=str(msg.get("content", "")),
            mode=MessageMode.EXPLICIT.value,
            target=str(msg.get("target", "@unknown")),
            timestamp=str(msg.get("created_at", "")),
            note=note,
        )

    def on_silent_message(self, data: dict) -> None:
        msg = data.get("message", {})
        self._emit_message_card(
            sender=str(msg.get("sender", "@unknown")),
            content=str(msg.get("content", "")),
            mode=MessageMode.SILENT.value,
            topic=str(msg.get("topic", "general")),
            timestamp=str(msg.get("created_at", "")),
        )

    def on_presence_change(self, data: dict) -> None:
        raw_handle = str(data.get("handle", ""))
        raw_status = str(data.get("status", "")).lower()
        if raw_handle and raw_handle.lower() != self.handle.lower():
            with self._ui_lock:
                if raw_status == AgentStatus.OFFLINE.value:
                    for key in [k for k in self.roster_state if k.lower() == raw_handle.lower()]:
                        self.roster_state.pop(key, None)
                else:
                    for key in [k for k in self.roster_state if k.lower() == raw_handle.lower() and k != raw_handle]:
                        self.roster_state.pop(key, None)
                    self.roster_state[raw_handle] = raw_status
        self._refresh_header_row1()

    def on_broadcast(self, data: dict) -> None:
        msg = data.get("message", {})
        mode = str(msg.get("mode", MessageMode.BROADCAST.value)).lower()
        self._emit_message_card(
            sender=str(msg.get("sender", "@unknown")),
            content=str(msg.get("content", "")),
            mode=mode,
            timestamp=str(msg.get("created_at", "")),
        )

    def _apply_roster_snapshot(self, roster: list[dict]) -> None:
        with self._ui_lock:
            self.roster_state = {
                str(r.get("handle", "")): str(r.get("status", "offline")).lower()
                for r in roster
                if r.get("handle")
                and str(r.get("handle", "")).lower() != self.handle.lower()
                and str(r.get("status", "offline")).lower() != AgentStatus.OFFLINE.value
            }
        self._refresh_header_row1()

    async def sync_roster(self, *, display_table: bool = False) -> list[dict]:
        """Synchronize roster snapshot in wire-order; optionally display the full table."""
        handler = self._display_roster if display_table else self._apply_roster_snapshot
        return await self.client.get_roster(on_roster=handler)

    async def show_roster(self) -> None:
        await self.sync_roster(display_table=True)

    def _display_roster(self, roster: list[dict]) -> None:
        self._apply_roster_snapshot(roster)
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

    @staticmethod
    def _is_chat_command(raw: str) -> bool:
        stripped = raw.strip()
        if not stripped:
            return False
        head = stripped.split(maxsplit=1)[0]
        return head in ("/send", "/post", "/broadcast")

    def handle_command(self, raw: str, loop: Optional[asyncio.AbstractEventLoop] = None) -> bool:
        """Process a complete console command line. Returns False when quitting."""
        raw = raw.strip()
        if not raw:
            return True
        if raw in ("/quit", "/exit", ":q"):
            self._running = False
            self._teardown_tui()
            return False
        if raw in ("/help", "/?", "help"):
            self.print_help()
        elif raw == "/roster":
            self._schedule(self.show_roster(), loop)
        elif raw == "/logs":
            self._print_logs()
        elif raw == "/read" or raw.startswith("/read "):
            parts = raw.split(maxsplit=1)
            topic = parts[1].lstrip("#") if len(parts) > 1 else "general"
            self._schedule(self._do_read(topic or "general"), loop)
        elif raw == "/send" or raw.startswith("/send "):
            parts = raw.split(maxsplit=2)
            if len(parts) < 3:
                self._emit(lambda: self._out_console.print("[red]Usage: /send @target <message>[/red]"))
                return True
            target = parts[1]
            msg = parts[2]
            self._schedule(self._do_send_explicit(target, msg), loop)
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
            self._schedule(self._do_broadcast(rest, urgent=urgent), loop)
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

    def _print_logs(self) -> None:
        if not self._log_buffer:
            self._emit(lambda: self._out_console.print("[dim]No embedded Hub logs recorded.[/dim]"))
            return
        snapshot = list(self._log_buffer)[-30:]
        self._emit(
            lambda: self._out_console.print(
                "\n".join(f"[dim]{escape(line)}[/dim]" for line in snapshot),
                highlight=False,
            )
        )

    async def _do_send_explicit(self, target: str, msg: str) -> None:
        try:
            res = await self.client.send_message(
                target=target,
                content=msg,
                mode=MessageMode.EXPLICIT,
                sender=self.handle,
            )
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]❌ Send failed: {escape(str(exc))}[/red]"))
            return
        if not isinstance(res, dict) or res.get("status") != "ok":
            if isinstance(res, dict) and res.get("error"):
                err = escape(str(res["error"]))
                self._emit(lambda: self._out_console.print(f"[red]❌ {err}[/red]"))
            return
        norm_target = target if target.startswith("@") else f"@{target}"
        if norm_target.lower() == self.handle.lower() and res.get("delivered") is True:
            return
        note = "(offline · queued)" if res.get("delivered") is False else ""
        self._emit_message_card(
            sender=self.handle,
            content=msg,
            mode=MessageMode.EXPLICIT.value,
            target=norm_target,
            timestamp=get_utc_now_iso(),
            note=note,
        )

    async def _do_post(self, topic: str, msg: str) -> None:
        try:
            res = await self.client.send_message(
                target=f"#{topic}",
                content=msg,
                mode=MessageMode.SILENT,
                topic=topic,
                sender=self.handle,
            )
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]❌ Post failed: {escape(str(exc))}[/red]"))
            return
        if isinstance(res, dict) and res.get("status") not in (None, "ok"):
            err = escape(str(res.get("error", "Post failed")))
            self._emit(lambda: self._out_console.print(f"[red]❌ {err}[/red]"))

    async def _do_broadcast(self, content: str, *, urgent: bool) -> None:
        mode = MessageMode.URGENT if urgent else MessageMode.BROADCAST
        try:
            res = await self.client.send_message(
                target="#general",
                content=content,
                mode=mode,
                topic="general",
                sender=self.handle,
            )
        except Exception as exc:
            self._emit(lambda: self._out_console.print(f"[red]❌ Broadcast failed: {escape(str(exc))}[/red]"))
            return
        if not isinstance(res, dict) or res.get("status") != "ok":
            if isinstance(res, dict) and res.get("error"):
                err = escape(str(res["error"]))
                self._emit(lambda: self._out_console.print(f"[red]❌ {err}[/red]"))
            return
        if not urgent:
            self._emit_message_card(
                sender=self.handle,
                content=content,
                mode=MessageMode.BROADCAST.value,
                timestamp=get_utc_now_iso(),
            )

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
                    self._teardown_tui()
                else:
                    self._redraw_dock()

        if submit_line is not None and submit_line.strip():
            stripped = submit_line.strip()
            if (
                self._dock_active
                and not self._is_chat_command(stripped)
                and stripped not in ("/quit", "/exit", ":q")
            ):
                self._emit(
                    lambda: self._out_console.print(
                        f"[dim]{escape(self.handle)}::> {escape(stripped)}[/dim]"
                    )
                )
            self.handle_command(stripped, loop=loop)
            if not self._running:
                self._teardown_tui()

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

    def _notify_external_disconnect(self) -> None:
        with self._ui_lock:
            if self._disconnected:
                return
            self._disconnected = True
        self._refresh_header_row1()
        if not self._disconnect_notified:
            self._disconnect_notified = True
            self._emit(
                lambda: self._out_console.print(
                    "[bold red]⚠ Hub disconnected. Type /quit or press Ctrl+C to exit.[/bold red]"
                )
            )

    async def run(self, *, already_connected: bool = False) -> None:
        self._running = True

        if not already_connected:
            try:
                await self.client.connect(initial_status=AgentStatus.IDLE, role="console")
            except Exception as e:
                self._out_console.print(f"[bold red]❌ Failed to connect to ACP Hub at {self.uri}: {e}[/bold red]")
                self._running = False
                return

        self.client.on(EventType.EXPLICIT_MESSAGE.value, self.on_explicit_message)
        self.client.on(EventType.SILENT_MESSAGE.value, self.on_silent_message)
        self.client.on(EventType.BROADCAST.value, self.on_broadcast)
        self.client.on(EventType.PRESENCE_CHANGE.value, self.on_presence_change)

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
            except Exception as e:
                self._emit(lambda: self._out_console.print(f"[red]Error: {e}[/red]"))

        prev_sigwinch = None
        sigwinch = getattr(signal, "SIGWINCH", None)

        with _raw_tty_mode() as tty_raw:
            if tty_raw:
                self._enter_tui()
                if sigwinch is not None and threading.current_thread() is threading.main_thread():
                    try:
                        prev_sigwinch = signal.getsignal(sigwinch)
                        signal.signal(sigwinch, lambda *_: setattr(self, "_resize_pending", True))
                    except (ValueError, OSError):
                        prev_sigwinch = None

            self.print_banner()

            input_thread = threading.Thread(target=user_input_thread, args=(tty_raw,), daemon=True)
            input_thread.start()

            try:
                while self._running:
                    await asyncio.sleep(0.1)
                    if self._dock_active:
                        with self._ui_lock:
                            self._check_resize_locked()
                    if self._running and not self.client.connected and not self._disconnected:
                        self._notify_external_disconnect()
            finally:
                self._running = False
                if sigwinch is not None and prev_sigwinch is not None:
                    try:
                        signal.signal(sigwinch, prev_sigwinch)
                    except (ValueError, OSError):
                        pass
                self._teardown_tui()
                await self.client.close()
                self._out_console.print("\n[dim]Console disconnected.[/dim]")

    async def _do_read(self, topic: str) -> None:
        messages = await self.client.read_board(topic=topic, reader=self.handle, unread_only=False, limit=20)
        def _render() -> None:
            width = self._viewport[0] if self._dock_active else self._terminal_size()[0]
            self._out_console.print(
                f"\n[bold cyan]📖 Recent messages in #{escape(topic)} (count: {len(messages)}):[/bold cyan]\n"
            )
            for idx, m in enumerate(messages):
                if idx > 0:
                    self._out_console.print()
                mode = str(m.get("mode", MessageMode.SILENT.value)).lower()
                card = render_message_with_avatar(
                    sender=str(m.get("sender", "@unknown")),
                    content=str(m.get("content", "")),
                    mode=mode,
                    target=str(m.get("target", "")) or None,
                    topic=str(m.get("topic", topic)) or topic,
                    timestamp=str(m.get("created_at", "")),
                    max_width=width,
                )
                self._out_console.print(card, highlight=False)
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
        self._refresh_header_row1()
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
        self._refresh_header_row1()
        if removed:
            names = escape(", ".join(removed))
            self._emit(lambda: self._out_console.print(f"[green]✓ Pruned {len(removed)} offline agent(s): {names}[/green]"))
        else:
            self._emit(lambda: self._out_console.print("[dim]No offline agents to prune.[/dim]"))


class ProbeStatus(str, Enum):
    ALIVE_HUB = "alive_hub"
    PORT_FREE = "port_free"
    PORT_OCCUPIED_NON_ACP = "port_occupied_non_acp"


_LOCAL_BINDABLE_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "::"}


def _is_local_bindable_host(host: Optional[str]) -> bool:
    if not host:
        return True
    return host.strip().lower() in _LOCAL_BINDABLE_HOSTS


def _connect_host_for_bind(host: str) -> str:
    h = (host or "127.0.0.1").strip()
    if h in ("0.0.0.0", "localhost", ""):
        return "127.0.0.1"
    if h == "::":
        return "::1"
    return h


def _format_ws_uri(host: str, port: int, scheme: str = "ws") -> str:
    connect_host = _connect_host_for_bind(host)
    if ":" in connect_host and not connect_host.startswith("["):
        return f"{scheme}://[{connect_host}]:{port}"
    return f"{scheme}://{connect_host}:{port}"


async def probe_hub(uri: str, timeout: float = 0.8) -> ProbeStatus:
    """Three-state WebSocket PING/PONG protocol probe for an ACP Hub endpoint."""
    parsed = urlparse(uri)
    raw_host = parsed.hostname or "127.0.0.1"
    port = parsed.port if parsed.port is not None else 8765
    if port == 0:
        return ProbeStatus.PORT_FREE

    connect_host = _connect_host_for_bind(raw_host)
    is_local = connect_host in ("127.0.0.1", "::1")
    tcp_timeout = min(0.25, max(0.05, timeout * 0.5)) if is_local else timeout

    loop = asyncio.get_running_loop()
    family = socket.AF_INET6 if ":" in connect_host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (connect_host, port)), timeout=tcp_timeout)
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        return ProbeStatus.PORT_FREE

    try:
        async def _ws_ping() -> ProbeStatus:
            async with websockets.connect(
                uri,
                sock=sock,
                open_timeout=timeout,
                close_timeout=0.2,
            ) as ws:
                req_id = uuid.uuid4().hex[:8]
                await ws.send(json.dumps({"action": ActionType.PING.value, "req_id": req_id}))
                raw = await ws.recv()
                data = json.loads(raw)
                if isinstance(data, dict) and data.get("event") == EventType.PONG.value:
                    return ProbeStatus.ALIVE_HUB
                return ProbeStatus.PORT_OCCUPIED_NON_ACP

        return await asyncio.wait_for(_ws_ping(), timeout=timeout)
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        return ProbeStatus.PORT_OCCUPIED_NON_ACP


class _RingBufferLogHandler(logging.Handler):
    def __init__(self, buffer: deque[str]):
        super().__init__(level=logging.DEBUG)
        self.buffer = buffer
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass


@contextmanager
def _silence_embedded_hub_logging(maxlen: int = 200):
    """Isolate acp.hub and websockets loggers into an in-memory ring buffer during TUI execution."""
    target_names = ("acp.hub", "websockets", "websockets.server", "websockets.protocol")
    ring: deque[str] = deque(maxlen=maxlen)
    handler = _RingBufferLogHandler(ring)
    saved: dict[str, tuple[list[logging.Handler], int, bool]] = {}

    for name in target_names:
        lg = logging.getLogger(name)
        saved[name] = (list(lg.handlers), lg.level, lg.propagate)
        lg.handlers = [handler]
        lg.propagate = False

    try:
        yield ring
    finally:
        for name, (orig_handlers, orig_level, orig_propagate) in saved.items():
            lg = logging.getLogger(name)
            lg.handlers = orig_handlers
            lg.level = orig_level
            lg.propagate = orig_propagate


class UnifiedConsoleRunner:
    """Manage optional embedded ACPHub + HumanConsole within a single asyncio event loop."""

    def __init__(
        self,
        uri: str = "ws://127.0.0.1:8765",
        handle: str = "@user",
        *,
        bind_host: Optional[str] = None,
        bind_port: Optional[int] = None,
        db_path: str = "acp_chatroom.db",
        auto_hub: bool = True,
        explicit_hub_options: bool = False,
        ui_console: Optional[Console] = None,
        log_buffer: Optional[deque[str]] = None,
    ):
        parsed = urlparse(uri)
        self._scheme = parsed.scheme or "ws"
        self.bind_host = bind_host if bind_host is not None else (parsed.hostname or "127.0.0.1")
        self.bind_port = bind_port if bind_port is not None else (parsed.port if parsed.port is not None else 8765)
        self.uri = _format_ws_uri(self.bind_host, self.bind_port, self._scheme) if bind_host is not None or bind_port is not None else uri
        self.handle = handle if handle.startswith("@") else f"@{handle}"
        self.db_path = db_path
        self.auto_hub = auto_hub
        self.explicit_hub_options = explicit_hub_options
        self._ui_console = ui_console
        self._log_buffer = log_buffer

        self.owns_hub: bool = False
        self.hub: Optional[ACPHub] = None
        self.bound_port: int = self.bind_port
        self.console: HumanConsole = HumanConsole(
            uri=self.uri,
            handle=self.handle,
            ui_console=self._ui_console,
            hub_mode="client",
            log_buffer=self._log_buffer,
        )

    async def start(self, *, probe_timeout: float = 0.8) -> None:
        """Probe or start embedded ACPHub, then connect HumanConsole client."""
        parsed = urlparse(self.uri)
        target_host = self.bind_host or parsed.hostname or "127.0.0.1"

        if self.bind_port == 0 and self.auto_hub and _is_local_bindable_host(target_host):
            await self._start_embedded_hub(target_host, 0, probe_timeout=probe_timeout)
        else:
            status = await probe_hub(self.uri, timeout=probe_timeout)
            if status == ProbeStatus.ALIVE_HUB:
                self.owns_hub = False
                self.bound_port = parsed.port or self.bind_port or 8765
                warning = None
                if self.explicit_hub_options:
                    warning = (
                        f"Attached to existing ACP Hub at {self.uri} "
                        f"(custom --db/--host options were not applied to the already-running Hub)."
                    )
                self.console._hub_mode = "client"
                self.console._hub_warning = warning
            elif status == ProbeStatus.PORT_OCCUPIED_NON_ACP:
                raise RuntimeError(
                    f"Port at {self.uri} is occupied by a non-ACP service (WebSocket PING probe failed)."
                )
            else:
                if not _is_local_bindable_host(target_host):
                    raise RuntimeError(
                        f"Cannot reach remote ACP Hub at {self.uri} (refused to bind non-local host {target_host!r})."
                    )
                if not self.auto_hub:
                    raise RuntimeError(
                        f"No ACP Hub is running at {self.uri} (--no-auto-hub disabled automatic Hub startup)."
                    )
                await self._start_embedded_hub(target_host, self.bind_port, probe_timeout=probe_timeout)

        await self.console.client.connect(initial_status=AgentStatus.IDLE, role="console")
        self.console.client.on(EventType.EXPLICIT_MESSAGE.value, self.console.on_explicit_message)
        self.console.client.on(EventType.SILENT_MESSAGE.value, self.console.on_silent_message)
        self.console.client.on(EventType.BROADCAST.value, self.console.on_broadcast)
        self.console.client.on(EventType.PRESENCE_CHANGE.value, self.console.on_presence_change)
        await self.console.sync_roster(display_table=False)

    async def _start_embedded_hub(self, host: str, port: int, *, probe_timeout: float = 0.8) -> None:
        hub = ACPHub(host=host, port=port, db_path=self.db_path)
        try:
            await hub.start()
        except OSError as exc:
            retry_uri = _format_ws_uri(host, port, self._scheme)
            retry_status = await probe_hub(retry_uri, timeout=probe_timeout)
            if retry_status == ProbeStatus.ALIVE_HUB:
                self.owns_hub = False
                self.bound_port = port
                self.uri = retry_uri
                self.console.uri = retry_uri
                self.console.client.uri = retry_uri
                self.console._hub_mode = "client"
                return
            raise RuntimeError(f"Failed to bind embedded ACP Hub on {host}:{port}: {exc}") from exc

        self.hub = hub
        self.owns_hub = True
        self.bound_port = hub.bound_port
        self.uri = _format_ws_uri(host, self.bound_port, self._scheme)
        self.console.uri = self.uri
        self.console.client.uri = self.uri
        self.console._hub_mode = "host"

    async def stop(self) -> None:
        """Close Console client and stop embedded ACPHub if owned by this runner."""
        self.console._running = False
        self.console._teardown_tui()
        await self.console.client.close()
        if self.owns_hub and self.hub is not None:
            hub = self.hub
            self.hub = None
            await hub.stop()

    async def run(self) -> None:
        """Run the full unified Console + optional embedded Hub lifecycle."""
        with _silence_embedded_hub_logging() as ring:
            self._log_buffer = ring
            self.console._log_buffer = ring
            await self.start()
            try:
                await self.console.run(already_connected=True)
            finally:
                await self.stop()


def start_unified_console(
    uri: str = "ws://127.0.0.1:8765",
    handle: str = "@user",
    *,
    bind_host: Optional[str] = None,
    bind_port: Optional[int] = None,
    db_path: str = "acp_chatroom.db",
    auto_hub: bool = True,
    explicit_hub_options: bool = False,
) -> None:
    runner = UnifiedConsoleRunner(
        uri=uri,
        handle=handle,
        bind_host=bind_host,
        bind_port=bind_port,
        db_path=db_path,
        auto_hub=auto_hub,
        explicit_hub_options=explicit_hub_options,
    )
    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        console.print(f"[bold red]❌ {escape(str(exc))}[/bold red]")
        sys.exit(1)


def start_console(uri: str = "ws://127.0.0.1:8765", handle: str = "@user") -> None:
    start_unified_console(uri=uri, handle=handle, auto_hub=True)
