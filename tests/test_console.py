import asyncio
import io
from unittest.mock import AsyncMock

from click.testing import CliRunner
from rich.cells import cell_len
from rich.console import Console

import acp.cli as cli_mod
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


def test_batched_literal_edits_only_the_draft_row():
    hc, buf = _make_test_console()
    hc.handle_key("前后", literal=True)
    hc.handle_key("LEFT")
    buf.seek(0)
    buf.truncate(0)
    hc.handle_key("HOME", literal=True)
    hc.handle_key("😀中", literal=True)
    assert hc._input_buffer == "前HOME😀中后"
    assert hc._cursor_pos == len("前HOME😀中")
    assert "Commands" not in buf.getvalue()
    assert "Draft Input" not in buf.getvalue()


def test_notices_never_corrupt_active_input_draft(monkeypatch):
    hc, buf = _make_test_console(width=80, dock_active=True)
    sent = []

    async def fake_send(**kwargs):
        sent.append(kwargs)
        return {"status": "ok", "delivered": True}

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
    assert "\x1b7\x1b[1;1H\x1b[2K" in output
    assert "Presence:" not in output
    assert "@codex BUSY" in output
    assert "please review [main]" in output

    # Notice appears before the redrawn bottom dock holding the intact partial draft.
    notice_idx = output.index("please review [main]")
    col = cell_len("@user::> /send @codex hel") + 1
    dock_idx = output.rindex(f"\x1b[{hc._out_console.height - 1};{col}H")
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
    assert lines[cursor_row].startswith("@user::> ")
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
    assert output == ""


def test_quit_and_ctrl_c_stop_console_and_clear_dock():
    hc, buf = _make_test_console(width=80, dock_active=True)
    for ch in "/quit\r":
        hc.handle_key(ch)
    assert not hc._running
    assert not hc._dock_active

    assert "\x1b[r" in buf.getvalue()
    hc2, buf2 = _make_test_console(width=80, dock_active=True)
    hc2.handle_key("\x03")
    assert not hc2._running
    assert not hc2._dock_active
    assert "\x1b[r" in buf2.getvalue()


def test_help_commands_unknown_hint_and_persistent_command_bar(monkeypatch):
    hc, buf = _make_test_console(width=120, dock_active=True)

    wide_lines, cursor_row, _ = hc.render_dock_lines(width=120)
    assert lines_has_cmds(wide_lines, ["/send", "/post", "/broadcast", "/read", "/roster", "/remove", "/prune", "/help", "/quit"])
    assert cursor_row == 1
    assert len(wide_lines) == 3
    assert wide_lines[cursor_row].startswith("@user::> ")

    narrow_lines, _, _ = hc.render_dock_lines(width=70)
    assert lines_has_cmds(narrow_lines, ["/send", "/post", "/broadcast", "/read", "/roster", "/remove", "/prune", "/help", "/quit"])
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
        return {"status": "ok", "delivered": True}

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


def test_persistent_online_agents_bar_updates_with_roster_and_presence():
    hc, buf = _make_test_console(width=100, dock_active=True)

    initial_lines = hc.render_header_lines(width=100)
    assert "Online (0): ○ waiting for agents" in initial_lines[0]

    hc._display_roster([
        {"handle": "@user", "status": "idle", "last_seen": "2026-11-15T12:00:00"},
        {"handle": "@codex", "status": "idle", "last_seen": "2026-11-15T12:00:00"},
        {"handle": "@claude", "status": "busy", "last_seen": "2026-11-15T12:00:00"},
        {"handle": "@agy", "status": "offline", "last_seen": "2026-11-15T11:55:00"},
    ])
    dock_after_roster = hc.render_header_lines(width=100)
    assert any("Online (2): ● @claude BUSY · ● @codex IDLE" in line for line in dock_after_roster)

    buf.seek(0)
    buf.truncate(0)
    hc.on_presence_change({"handle": "@codex", "status": "busy"})
    hc.on_presence_change({"handle": "@claude", "status": "offline"})
    rendered = buf.getvalue()
    assert "Online (1): ● @codex BUSY" in rendered


def test_read_message_spacing_in_console_and_cli(monkeypatch):
    sample = [
        {"created_at": "2026-11-15T12:00:01Z", "sender": "@codex", "mode": "silent", "content": "first update [1]"},
        {"created_at": "2026-11-15T12:01:02Z", "sender": "@claude", "mode": "explicit", "content": "second reply"},
    ]

    hc, buf = _make_test_console(width=100, dock_active=False)
    hc.client.read_board = AsyncMock(return_value=sample)
    asyncio.run(hc._do_read("general"))
    console_out = buf.getvalue().replace("\r\n", "\n")
    assert "first update [1]" in console_out and "second reply" in console_out
    assert console_out.index("@codex") < console_out.index("@claude")
    assert " ╰▀─▀╯ " in console_out

    cli_buf = io.StringIO()
    monkeypatch.setattr(cli_mod, "rich_console", Console(file=cli_buf, width=100, force_terminal=False, no_color=True))
    monkeypatch.setattr(cli_mod, "sync_read", lambda **kwargs: sample)
    res = CliRunner().invoke(cli_mod.cli, ["read", "--topic", "general", "--all"])
    assert res.exit_code == 0
    cli_out = cli_buf.getvalue().replace("\r\n", "\n")
    assert "@codex:\n    first update [1]\n\n  2026-11-15 12:01:02 [EXP] @claude:\n    second reply" in cli_out


def test_console_remove_and_prune_commands(monkeypatch):
    hc, buf = _make_test_console(width=100, dock_active=True)
    hc.roster_state = {"@codex": "idle", "@claude": "busy", "@stale": "offline"}
    removed_targets = []

    async def fake_remove(target):
        removed_targets.append(target)
        return {"status": "ok", "handle": "@" + target.lstrip("@"), "was_online": True}

    async def fake_prune():
        return {"status": "ok", "removed": ["@stale"], "count": 1}

    hc.client.remove_agent = AsyncMock(side_effect=fake_remove)
    hc.client.prune_roster = AsyncMock(side_effect=fake_prune)
    monkeypatch.setattr(hc, "_schedule", lambda coro, loop: asyncio.run(coro))

    assert hc.handle_command("/remove")
    assert "Usage: /remove @agent" in buf.getvalue()

    buf.seek(0)
    buf.truncate(0)
    assert hc.handle_command("/remove @codex")
    assert hc.handle_command("/kick claude")
    assert removed_targets == ["@codex", "claude"]
    assert "@codex" not in hc.roster_state
    assert "@claude" not in hc.roster_state
    assert "Removed @codex" in buf.getvalue()
    assert "Removed @claude" in buf.getvalue()
    assert "\x1b7\x1b[1;1H\x1b[2K" in buf.getvalue()
    assert "Online (0)" in hc.render_header_lines(100)[0]

    buf.seek(0)
    buf.truncate(0)
    assert hc.handle_command("/prune")
    assert "@stale" not in hc.roster_state
    assert "Pruned 1 offline agent(s): @stale" in buf.getvalue()
    assert "\x1b7\x1b[1;1H\x1b[2K" in buf.getvalue()



def test_resize_scroll_region_and_short_terminal():
    hc, buf = _make_test_console(width=100)
    for height in (24, 16, 6):
        hc._out_console.height = height
        buf.seek(0)
        buf.truncate(0)
        hc._redraw_dock()
        if height >= 8:
            assert f"\x1b[3;{height - 3}r" in buf.getvalue()
        else:
            assert "\x1b[r" in buf.getvalue()
            assert "\x1b[3;3r" not in buf.getvalue()


def test_header_overflow():
    hc, _ = _make_test_console()
    hc.roster_state = {f"@codex{i}": "starting" for i in range(8)}
    header, rule = hc.render_header_lines(60)
    assert "Online (8)" in header and "+" in header and "◌" in header
    assert cell_len(header) == cell_len(rule) == 59

def test_chat_submissions_show_one_card_and_no_command_echo(monkeypatch):
    hc, buf = _make_test_console(dock_active=False)
    delivered = True
    async def reply(**msg):
        if msg["mode"] == MessageMode.SILENT:
            hc.on_silent_message({"message": msg})
        if msg["mode"] == MessageMode.URGENT:
            hc.on_broadcast({"message": msg})
        return {"status": "ok", "delivered": delivered}
    hc.client.send_message = reply
    monkeypatch.setattr(hc, "_schedule", lambda coro, loop: asyncio.run(coro))
    for command in ("/send @codex", "/send @offline", "/post", "/broadcast", "/broadcast --urgent"):
        delivered = command != "/send @offline"
        hc._input_buffer = command + " message123"
        buf.seek(0)
        buf.truncate(0)
        hc.handle_key("\r")
        output = buf.getvalue()
        assert output.count("message123") == output.count("╭▄█▄╮") == 1
        assert command not in output and "\x1b" not in output
        assert ("queued" in output) == (not delivered)
