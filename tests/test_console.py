"""TC-TUI-01..08: fixed header, DECSTBM feed, silent presence, and the 3-line dock."""

import asyncio
import io
from unittest.mock import AsyncMock

from rich.cells import cell_len
from rich.console import Console

from acp.avatars import resolve_avatar
from acp.console import HumanConsole
from acp.protocol import MessageMode


def _make_test_console(width: int = 80, dock_active: bool = True) -> tuple[HumanConsole, io.StringIO]:
    buf = io.StringIO()
    ui = Console(file=buf, width=width, force_terminal=False, no_color=True)
    hc = HumanConsole(uri="ws://127.0.0.1:8765", handle="@user", ui_console=ui)
    hc._running = True
    hc._dock_active = dock_active
    hc.display_port = 8765
    if dock_active:
        hc._draw_dock()
    return hc, buf


def _reset(buf: io.StringIO) -> None:
    buf.seek(0)
    buf.truncate(0)


def lines_has_cmds(lines: list[str], cmds: list[str]) -> bool:
    joined = "\n".join(lines)
    return all(cmd in joined for cmd in cmds)


def test_presence_change_silent_header_update_zero_spam():
    hc, buf = _make_test_console(width=100, dock_active=True)
    hc.begin_layout(100, 24)
    _reset(buf)
    for status in ("starting", "idle", "busy", "blocked"):
        hc.on_presence_change({"handle": "@Codex", "status": status})
        assert hc.roster_state["@Codex"] == status
    hc.on_presence_change({"handle": "@codex", "status": "offline"})
    assert not any(key.lower() == "@codex" for key in hc.roster_state)
    output = buf.getvalue()
    assert "Presence:" not in output
    assert "is now" not in output
    assert "\x1b7\x1b[1;1H\x1b[2K" in output
    assert "\x1b[0m\x1b8" in output
    assert "EXPLICIT" not in output

    quiet, quiet_buf = _make_test_console(width=80, dock_active=False)
    quiet.on_presence_change({"handle": "@codex", "status": "idle"})
    quiet.on_presence_change({"handle": "@codex", "status": "offline"})
    assert "@codex" not in quiet.roster_state
    quiet_out = quiet_buf.getvalue()
    assert "Presence:" not in quiet_out
    assert "is now" not in quiet_out
    assert "\x1b7" not in quiet_out
    assert "\x1b[1;1H" not in quiet_out


def test_top_header_layout_alignment_and_overflow():
    hc, _ = _make_test_console(width=100, dock_active=False)
    hc.hub_mode = "host"
    hc.display_port = 8765
    empty = hc.render_header_lines(width=100)
    assert len(empty) == 2
    assert "(host)" in empty[0]
    assert "Online (0):" in empty[0]
    assert "○" in empty[0]
    assert all(cell_len(line) <= 99 for line in empty)

    hc.roster_state = {
        "@user": "idle",
        "@agy": "starting",
        "@claude": "idle",
        "@codex": "busy",
        "@dsh": "blocked",
    }
    mixed = hc.render_header_lines(width=120)
    assert len(mixed) == 2
    assert all(cell_len(line) <= 119 for line in mixed)
    assert "Online (4):" in mixed[0]
    assert "●" in mixed[0] and "◌" in mixed[0]
    assert "@claude" in mixed[0] and "IDLE" in mixed[0]
    assert "@codex" in mixed[0] and "BUSY" in mixed[0]
    assert "@agy" in mixed[0] and "START" in mixed[0]
    assert "@dsh" in mixed[0] and "BLK" in mixed[0]
    assert "@user" not in mixed[0]
    assert "🟢" not in mixed[0] and "🟡" not in mixed[0] and "🔴" not in mixed[0]

    hc.roster_state = {f"@agent{i:02d}": "idle" for i in range(8)}
    narrow = hc.render_header_lines(width=60)
    assert len(narrow) == 2
    assert all(cell_len(line) <= 59 for line in narrow)
    assert "Online (8):" in narrow[0]
    assert "+7" in narrow[0] or "+" in narrow[0]
    assert "\n" not in narrow[0]


def test_decstbm_setup_emit_scroll_and_clean_exit_reset():
    hc, buf = _make_test_console(width=100, dock_active=True)
    hc.begin_layout(100, 24)
    assert "\x1b[3;21r" in buf.getvalue()
    for ch in "/send @codex hel":
        hc.handle_key(ch)
    assert hc._input_buffer == "/send @codex hel"
    _reset(buf)
    hc.on_presence_change({"handle": "@codex", "status": "busy"})
    hc.on_explicit_message({
        "message": {
            "sender": "@claude",
            "target": "@codex",
            "content": "please review storage",
            "created_at": "2026-11-15T12:00:00Z",
        }
    })
    output = buf.getvalue()
    assert "please review storage" in output
    assert "\x1b[23;" in output
    assert hc._input_buffer == "/send @codex hel"
    hc._input_buffer = ""
    hc._cursor_pos = 0
    _reset(buf)
    for ch in "/quit\r":
        hc.handle_key(ch)
    assert "\x1b[r" in buf.getvalue()
    assert not hc._running
    assert not hc._dock_active

    hc2, buf2 = _make_test_console(width=100, dock_active=True)
    hc2.begin_layout(100, 24)
    _reset(buf2)
    hc2.handle_key("\x03")
    assert "\x1b[r" in buf2.getvalue()
    assert not hc2._running
    assert not hc2._dock_active


def test_compact_3_line_dock_with_custom_prompt_and_cjk():
    hc, _ = _make_test_console(width=36, dock_active=True)
    for width in (120, 70, 36):
        lines, cursor_row, cursor_col = hc.render_dock_lines(width=width)
        assert len(lines) == 3
        assert cursor_row == 1
        assert "Draft Input" in lines[0]
        assert lines[1].startswith("@user::> ")
        assert "Online Agents (" not in "\n".join(lines)
        assert all(cell_len(line) <= width - 1 for line in lines)
        assert cursor_col <= width - 1
    wide, _, _ = hc.render_dock_lines(width=120)
    medium, _, _ = hc.render_dock_lines(width=70)
    assert lines_has_cmds(wide, ["/remove", "/prune", "/send", "/quit"])
    assert lines_has_cmds(medium, ["/remove", "/prune", "/send", "/quit"])

    for ch in "测试中文输入框不换行溢出检查测试文字":
        hc.handle_key(ch)
    lines, cursor_row, cursor_col = hc.render_dock_lines(width=36)
    assert lines[cursor_row].startswith("@user::> ")
    assert all(cell_len(line) <= 35 for line in lines)
    assert cursor_col <= 35


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
    hc.handle_key("\r")
    assert hc._input_buffer == ""
    hc.handle_key("UP")
    assert hc._input_buffer.startswith("bcd")
    hc.handle_key("\x1b")
    assert hc._input_buffer == ""


def test_human_self_messages_render_once_with_avatar(monkeypatch):
    user_art = resolve_avatar("@user").lines[0]

    def console_with(result):
        hc, buf = _make_test_console(width=100, dock_active=True)
        hc.begin_layout(100, 24)

        async def fake_send(**kwargs):
            fake_send.calls.append(kwargs)
            return result

        fake_send.calls = []
        hc.client.send_message = AsyncMock(side_effect=fake_send)
        monkeypatch.setattr(hc, "_schedule", lambda coro, loop: asyncio.run(coro))
        return hc, buf, fake_send

    def submit(hc, buf, text):
        for ch in text:
            hc.handle_key(ch)
        _reset(buf)
        hc.handle_key("\r")
        return buf.getvalue()

    hc, buf, sent = console_with({"status": "ok", "delivered": True})
    output = submit(hc, buf, "/send @codex hi")
    assert "@user::> /send" not in output
    assert output.count(user_art) == 1
    assert "hi" in output
    assert "queued" not in output
    assert sent.calls[0]["content"] == "hi"
    assert sent.calls[0]["mode"] == MessageMode.EXPLICIT

    hc, buf, _sent = console_with({"status": "ok", "delivered": False})
    output = submit(hc, buf, "/send @codex hi")
    assert output.count(user_art) == 1
    assert "offline" in output and "queued" in output

    hc, buf, _sent = console_with({"status": "ok"})
    output = submit(hc, buf, "/post hello")
    assert output.count(user_art) == 0
    assert "@user::> /post" not in output
    hc.on_silent_message({
        "message": {"sender": "@user", "topic": "general", "content": "hello", "created_at": "2026-11-15T12:00:00Z"}
    })
    assert buf.getvalue().count(user_art) == 1
    assert "@user::> /post" not in buf.getvalue()

    hc, buf, _sent = console_with({"status": "ok"})
    output = submit(hc, buf, "/broadcast all")
    assert output.count(user_art) == 1
    assert "BROADCAST" in buf.getvalue()

    hc, buf, _sent = console_with({"status": "ok"})
    output = submit(hc, buf, "/broadcast --urgent now")
    assert output.count(user_art) == 0
    assert "/broadcast --urgent" not in output
    hc.on_broadcast({
        "message": {"sender": "@user", "mode": "urgent", "content": "now", "id": 1, "created_at": "2026-11-15T12:03:00Z"}
    })
    assert buf.getvalue().count(user_art) == 1
    assert "URGENT" in buf.getvalue()


def test_remove_and_prune_refresh_top_header(monkeypatch):
    hc, buf = _make_test_console(width=100, dock_active=True)
    hc.begin_layout(100, 24)
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
    _reset(buf)
    assert hc.handle_command("/remove @codex")
    assert hc.handle_command("/kick claude")
    assert removed_targets == ["@codex", "claude"]
    assert "@codex" not in hc.roster_state
    assert "@claude" not in hc.roster_state
    rendered = buf.getvalue()
    assert "\x1b7\x1b[1;1H\x1b[2K" in rendered
    assert "\x1b[0m\x1b8" in rendered
    header = hc.render_header_lines(width=100)[0]
    assert "@codex" not in header
    assert "@claude" not in header

    _reset(buf)
    assert hc.handle_command("/prune")
    assert "@stale" not in hc.roster_state
    assert "Pruned 1 offline agent(s): @stale" in buf.getvalue()
    assert "\x1b7\x1b[1;1H\x1b[2K" in buf.getvalue()
    assert "@stale" not in hc.render_header_lines(width=100)[0]


def test_resize_and_short_or_narrow_terminal_degradation():
    hc, buf = _make_test_console(width=100, dock_active=True)
    hc.begin_layout(100, 24)
    _reset(buf)
    hc.apply_resize(70, 16)
    output = buf.getvalue()
    assert "\x1b[3;13r" in output
    assert "Draft Input" in output
    assert "(client)" in output or "Online (" in output
    assert all(cell_len(line) <= 69 for line in hc.render_header_lines(70))
    assert all(cell_len(line) <= 69 for line in hc.render_dock_lines(70)[0])

    short, short_buf = _make_test_console(width=80, dock_active=False)
    short.begin_layout(80, 6)
    assert "\x1b[3;" not in short_buf.getvalue()
    assert short._decstbm_active is False
    lines, _row, _col = short.render_dock_lines(80)
    assert len(lines) == 3


def test_non_tty_omits_ansi_and_preserves_legacy_bar_helper():
    hc, buf = _make_test_console(width=100, dock_active=False)
    hc.roster_state = {"@claude": "idle", "@user": "idle", "@agy": "offline"}
    hc.on_presence_change({"handle": "@codex", "status": "busy"})
    hc.on_explicit_message({
        "message": {"sender": "@claude", "target": "@codex", "content": "plain note", "created_at": "2026-11-15T12:00:00Z"}
    })
    hc.client.read_board = AsyncMock(return_value=[
        {"created_at": "2026-11-15T12:00:01Z", "sender": "@codex", "mode": "silent", "topic": "general", "content": "history"},
    ])
    asyncio.run(hc._do_read("general"))
    output = buf.getvalue()
    assert "\x1b[" not in output
    assert "\x1b7" not in output
    assert "\x1b8" not in output
    assert "Presence:" not in output
    assert "plain note" in output
    assert "history" in output
    assert hc.format_online_agents_bar() == "Online Agents (2): @claude [IDLE] · @codex [BUSY]"


def test_help_commands_unknown_hint_and_persistent_command_bar(monkeypatch):
    hc, buf = _make_test_console(width=120, dock_active=True)
    wide_lines, cursor_row, _ = hc.render_dock_lines(width=120)
    assert lines_has_cmds(wide_lines, ["/send", "/post", "/broadcast", "/read", "/roster", "/remove", "/prune", "/help", "/quit"])
    assert cursor_row == 1
    assert wide_lines[cursor_row].startswith("@user::> ")
    for alias in ("/help", "/?", "help"):
        _reset(buf)
        assert hc.handle_command(alias)
        text = buf.getvalue()
        assert "ACP Console Manual" in text
        assert "/broadcast" in text
        assert "Left/Right" in text
    _reset(buf)
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
