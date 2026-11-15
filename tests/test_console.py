import asyncio
import io
from unittest.mock import AsyncMock

from rich.cells import cell_len
from rich.console import Console

from acp.console import HumanConsole
from acp.protocol import MessageMode


def _make_test_console(width: int = 80, dock_active: bool = True) -> tuple[HumanConsole, io.StringIO]:
    buf = io.StringIO()
    ui = Console(file=buf, width=width, force_terminal=False, no_color=True)
    hc = HumanConsole(uri="ws://127.0.0.1:8765", handle="@user", ui_console=ui)
    hc._running = True
    hc._dock_active = dock_active
    if dock_active:
        hc._draw_dock()
    return hc, buf


def test_notices_never_corrupt_active_input_draft(monkeypatch):
    hc, buf = _make_test_console(width=80, dock_active=True)
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)

    hc.client.send_message = AsyncMock(side_effect=fake_send)
    monkeypatch.setattr(hc, "_schedule", lambda coro, loop: asyncio.run(coro))

    for ch in "/send @codex hel":
        hc.handle_key(ch)

    assert hc._input_buffer == "/send @codex hel"

    buf.seek(0)
    buf.truncate(0)

    # Interleave presence and explicit/silent notices mid-draft.
    hc.on_presence_change({"handle": "@codex", "status": "busy"})
    hc.on_explicit_message(
        {
            "message": {
                "sender": "@claude",
                "target": "@codex",
                "content": "please review [main]",
                "created_at": "2026-11-15T12:00:00Z",
            }
        }
    )

    output = buf.getvalue()
    assert "\x1b[J" in output
    assert "Presence: @codex is now BUSY" in output
    assert "please review [main]" in output

    # Notice appears before the redrawn bottom dock holding the intact partial draft.
    notice_idx = output.index("please review [main]")
    dock_idx = output.rindex("@user> /send @codex hel")
    assert dock_idx > notice_idx
    assert hc._input_buffer == "/send @codex hel"

    for ch in "lo\r":
        hc.handle_key(ch)

    assert hc._input_buffer == ""
    assert len(sent) == 1
    assert sent[0]["target"] == "@codex"
    assert sent[0]["content"] == "hello"
    assert sent[0]["mode"] == MessageMode.EXPLICIT


def test_draft_editing_navigation_history_and_wide_chars():
    hc, _ = _make_test_console(width=36, dock_active=True)

    for ch in "abde":
        hc.handle_key(ch)
    hc.handle_key("LEFT")
    hc.handle_key("LEFT")
    hc.handle_key("c")
    assert hc._input_buffer == "abcde"

    hc.handle_key("HOME")
    hc.handle_key("DELETE")
    assert hc._input_buffer == "bcde"

    hc.handle_key("END")
    hc.handle_key("\x08")
    assert hc._input_buffer == "bcd"

    # Long CJK text must stay clipped under terminal width - 1 to avoid line-wrap artifacts.
    for ch in "测试中文输入框不换行溢出检查测试文字":
        hc.handle_key(ch)
    lines, cursor_row, cursor_col = hc.render_dock_lines(width=36)
    assert lines[cursor_row].startswith("@user> ")
    for line in lines:
        assert cell_len(line) <= 35
    assert cursor_col <= 35

    hc.handle_key("\r")
    assert hc._input_buffer == ""
    hc.handle_key("UP")
    assert hc._input_buffer.startswith("bcd测试")
    hc.handle_key("\x1b")
    assert hc._input_buffer == ""


def test_non_tty_notices_omit_ansi_dock_sequences():
    hc, buf = _make_test_console(width=80, dock_active=False)
    hc.on_presence_change({"handle": "@codex", "status": "idle"})
    output = buf.getvalue()
    assert "\x1b[J" not in output
    assert "Presence: @codex is now IDLE" in output


def test_quit_and_ctrl_c_stop_console_and_clear_dock():
    hc, buf = _make_test_console(width=80, dock_active=True)
    for ch in "/quit\r":
        hc.handle_key(ch)
    assert not hc._running
    assert not hc._dock_active

    hc2, _ = _make_test_console(width=80, dock_active=True)
    hc2.handle_key("\x03")
    assert not hc2._running
    assert not hc2._dock_active


def test_help_commands_unknown_hint_and_persistent_command_bar(monkeypatch):
    hc, buf = _make_test_console(width=120, dock_active=True)

    wide_lines, cursor_row, _ = hc.render_dock_lines(width=120)
    assert lines_has_cmds(wide_lines, ["/send", "/post", "/broadcast", "/read", "/roster", "/help", "/quit"])
    assert cursor_row == 1

    narrow_lines, _, _ = hc.render_dock_lines(width=70)
    assert lines_has_cmds(narrow_lines, ["/send", "/post", "/broadcast", "/read", "/roster", "/help", "/quit"])
    for line in narrow_lines:
        assert cell_len(line) <= 69

    for alias in ("/help", "/?", "help"):
        buf.seek(0)
        buf.truncate(0)
        assert hc.handle_command(alias)
        text = buf.getvalue()
        assert "ACP Console Manual" in text
        assert "/broadcast" in text
        assert "Left/Right" in text

    buf.seek(0)
    buf.truncate(0)
    assert hc.handle_command("/unknowncmd")
    assert "Type /help" in buf.getvalue()

    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)

    hc.client.send_message = AsyncMock(side_effect=fake_send)
    monkeypatch.setattr(hc, "_schedule", lambda coro, loop: asyncio.run(coro))

    hc.handle_command("/broadcast sync checkpoint")
    hc.handle_command("/broadcast --urgent stop merge now")
    assert len(sent) == 2
    assert sent[0]["mode"] == MessageMode.BROADCAST
    assert sent[0]["content"] == "sync checkpoint"
    assert sent[1]["mode"] == MessageMode.URGENT
    assert sent[1]["content"] == "stop merge now"


def lines_has_cmds(lines: list[str], cmds: list[str]) -> bool:
    joined = "\n".join(lines)
    return all(cmd in joined for cmd in cmds)

