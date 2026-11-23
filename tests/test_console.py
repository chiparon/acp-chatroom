import asyncio
import io
from unittest.mock import AsyncMock

from click.testing import CliRunner
from rich.cells import cell_len
from rich.console import Console

import acp.cli as cli_mod
from acp.avatars import resolve_avatar
from acp.console import HumanConsole
from acp.protocol import MessageMode


def _make_test_console(
    width: int = 80,
    height: int = 24,
    dock_active: bool = True,
    hub_mode: str = "host",
) -> tuple[HumanConsole, io.StringIO]:
    buf = io.StringIO()
    ui = Console(
        file=buf,
        width=width,
        height=height,
        force_terminal=False,
        no_color=True,
        legacy_windows=False,
    )
    hc = HumanConsole(
        uri="ws://127.0.0.1:8765",
        handle="@user",
        ui_console=ui,
        hub_mode=hub_mode,
    )
    hc._running = True
    if dock_active:
        hc._enter_tui()
    else:
        hc._dock_active = False
    return hc, buf


def lines_has_cmds(lines: list[str], cmds: list[str]) -> bool:
    joined = "\n".join(lines)
    return all(cmd in joined for cmd in cmds)


# TC-TUI-01
def test_presence_change_silent_header_update_zero_spam():
    # 1. dock_active=True
    hc, buf = _make_test_console(width=100, height=24, dock_active=True)
    buf.seek(0)
    buf.truncate(0)

    for handle, status in (
        ("@agy", "starting"),
        ("@codex", "idle"),
        ("@claude", "busy"),
        ("@dsh", "blocked"),
        ("@agy", "offline"),
    ):
        hc.on_presence_change({"handle": handle, "status": status})

    out = buf.getvalue()
    assert "Presence:" not in out
    assert "is now" not in out
    assert "@agy" not in hc.roster_state
    assert hc.roster_state["@codex"] == "idle"
    assert hc.roster_state["@claude"] == "busy"
    assert hc.roster_state["@dsh"] == "blocked"
    assert "\x1b7\x1b[1;1H\x1b[2K" in out
    assert "\x1b[0m\x1b8" in out
    assert "Online (3):" in out

    # 2. dock_active=False
    hc_non_tty, buf_non_tty = _make_test_console(width=100, height=24, dock_active=False)
    hc_non_tty.on_presence_change({"handle": "@codex", "status": "busy"})
    hc_non_tty.on_presence_change({"handle": "@codex", "status": "offline"})
    out_non_tty = buf_non_tty.getvalue()
    assert out_non_tty == ""
    assert "Presence:" not in out_non_tty
    assert "@codex" not in hc_non_tty.roster_state


# TC-TUI-02
def test_top_header_layout_alignment_and_overflow():
    hc, _ = _make_test_console(width=100, height=24, dock_active=False, hub_mode="host")

    # 1. Empty room
    empty_lines = hc.render_header_lines(width=100)
    assert len(empty_lines) == 2
    assert all(cell_len(line) <= 99 for line in empty_lines)
    assert "ACP :8765 (host)" in empty_lines[0]
    assert "Online (0): ○ waiting for agents" in empty_lines[0]

    # 2. Mixed 4 statuses (self @user and offline excluded)
    hc.roster_state = {
        "@user": "idle",
        "@claude": "idle",
        "@codex": "busy",
        "@agy": "starting",
        "@dsh": "blocked",
        "@ghost": "offline",
    }
    mixed_lines = hc.render_header_lines(width=110)
    assert len(mixed_lines) == 2
    assert all(cell_len(line) <= 109 for line in mixed_lines)
    assert "Online (4):" in mixed_lines[0]
    assert "◌ @agy START" in mixed_lines[0]
    assert "● @claude IDLE" in mixed_lines[0]
    assert "● @codex BUSY" in mixed_lines[0]
    assert "● @dsh BLK" in mixed_lines[0]
    for double_width_emoji in ("🟢", "🟡", "🔴", "🔵"):
        assert double_width_emoji not in mixed_lines[0]

    # 3. Overflow with 8 agents on narrow width=60
    hc.roster_state = {f"@agent{i}": "idle" for i in range(1, 9)}
    narrow_lines = hc.render_header_lines(width=60)
    assert len(narrow_lines) == 2
    assert all(cell_len(line) <= 59 for line in narrow_lines)
    assert "Online (8):" in narrow_lines[0]
    assert "+" in narrow_lines[0]


# TC-TUI-03
def test_decstbm_setup_emit_scroll_and_clean_exit_reset(monkeypatch):
    hc, buf = _make_test_console(width=100, height=24, dock_active=True)
    init_out = buf.getvalue()
    assert "\x1b[3;21r" in init_out

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

    hc.on_presence_change({"handle": "@codex", "status": "busy"})
    hc.on_explicit_message({
        "message": {
            "sender": "@claude",
            "target": "@codex",
            "content": "please review [main]",
            "created_at": "2026-11-15T12:00:00Z",
        }
    })

    mid_out = buf.getvalue()
    assert "Presence:" not in mid_out
    assert "please review [main]" in mid_out
    assert hc._input_buffer == "/send @codex hel"

    for ch in "lo\r":
        hc.handle_key(ch)
    assert hc._input_buffer == ""
    assert len(sent) == 1
    assert sent[0]["target"] == "@codex"
    assert sent[0]["content"] == "hello"

    # Exit via /quit resets DECSTBM
    buf.seek(0)
    buf.truncate(0)
    for ch in "/quit\r":
        hc.handle_key(ch)
    quit_out = buf.getvalue()
    assert not hc._running
    assert not hc._dock_active
    assert "\x1b[r" in quit_out

    # Exit via Ctrl+C also resets DECSTBM
    hc2, buf2 = _make_test_console(width=100, height=24, dock_active=True)
    buf2.seek(0)
    buf2.truncate(0)
    hc2.handle_key("\x03")
    assert not hc2._running
    assert not hc2._dock_active
    assert "\x1b[r" in buf2.getvalue()


# TC-TUI-04
def test_compact_3_line_dock_with_custom_prompt_and_cjk():
    hc, _ = _make_test_console(width=120, height=24, dock_active=True)

    for w in (120, 70):
        lines, cursor_row, cursor_col = hc.render_dock_lines(width=w)
        assert len(lines) == 3
        assert cursor_row == 1
        assert "Draft Input" in lines[0]
        assert lines[1].startswith("@user::> ")
        assert lines_has_cmds(lines, ["/send", "/post", "/broadcast", "/read", "/roster", "/remove", "/prune", "/help", "/quit"])
        assert not any("Online Agents (" in line for line in lines)
        assert all(cell_len(line) <= w - 1 for line in lines)
        assert cursor_col <= w - 1

    # CJK editing and narrow width=36 clipping
    hc_narrow, _ = _make_test_console(width=36, height=24, dock_active=True)
    for ch in "abde":
        hc_narrow.handle_key(ch)
    hc_narrow.handle_key("LEFT")
    hc_narrow.handle_key("LEFT")
    hc_narrow.handle_key("c")
    assert hc_narrow._input_buffer == "abcde"

    hc_narrow.handle_key("HOME")
    hc_narrow.handle_key("DELETE")
    assert hc_narrow._input_buffer == "bcde"

    hc_narrow.handle_key("END")
    hc_narrow.handle_key("\x08")
    assert hc_narrow._input_buffer == "bcd"

    for ch in "测试中文输入框不换行溢出检查测试文字":
        hc_narrow.handle_key(ch)
    narrow_lines, cursor_row, cursor_col = hc_narrow.render_dock_lines(width=36)
    assert len(narrow_lines) == 3
    assert cursor_row == 1
    assert narrow_lines[cursor_row].startswith("@user::> ")
    assert all(cell_len(line) <= 35 for line in narrow_lines)
    assert cursor_col <= 35

    hc_narrow.handle_key("\r")
    assert hc_narrow._input_buffer == ""
    hc_narrow.handle_key("UP")
    assert hc_narrow._input_buffer.startswith("bcd测试")
    hc_narrow.handle_key("\x1b")
    assert hc_narrow._input_buffer == ""


# TC-TUI-05
def test_human_self_messages_render_once_with_avatar(monkeypatch):
    hc, buf = _make_test_console(width=100, height=24, dock_active=True)
    monkeypatch.setattr(hc, "_schedule", lambda coro, loop: asyncio.run(coro))
    user_avatar = resolve_avatar("@user")

    # 1. /send @codex hi (delivered=True) -> rendered once locally with @user avatar, no raw echo
    hc.client.send_message = AsyncMock(return_value={"status": "ok", "message_id": 1, "delivered": True})
    for ch in "/send @codex hi":
        hc.handle_key(ch)
    buf.seek(0)
    buf.truncate(0)
    hc.handle_key("\r")
    out1 = buf.getvalue()
    assert "@user::> /send @codex hi" not in out1
    assert out1.count(user_avatar.lines[0]) == 1
    assert "hi" in out1
    assert "(offline · queued)" not in out1

    # 2. /send @codex offline_msg (delivered=False) -> includes offline queued hint
    hc.client.send_message = AsyncMock(return_value={"status": "ok", "message_id": 2, "delivered": False})
    for ch in "/send @codex offline_msg":
        hc.handle_key(ch)
    buf.seek(0)
    buf.truncate(0)
    hc.handle_key("\r")
    out2 = buf.getvalue()
    assert out2.count(user_avatar.lines[0]) == 1
    assert "(offline · queued)" in out2

    # 3. /post hello -> Hub echoes SILENT_MESSAGE back to sender; local submit does not duplicate
    async def fake_post(**kwargs):
        hc.on_silent_message({
            "message": {
                "sender": "@user",
                "topic": kwargs.get("topic", "general"),
                "content": kwargs["content"],
                "created_at": "2026-11-15T12:05:00Z",
            }
        })
        return {"status": "ok", "message_id": 3}

    hc.client.send_message = AsyncMock(side_effect=fake_post)
    for ch in "/post hello board":
        hc.handle_key(ch)
    buf.seek(0)
    buf.truncate(0)
    hc.handle_key("\r")
    out3 = buf.getvalue()
    assert "@user::> /post hello board" not in out3
    assert out3.count(user_avatar.lines[0]) == 1
    assert "hello board" in out3

    # 4. /broadcast all -> Hub excludes sender for normal broadcast; Console renders once locally
    hc.client.send_message = AsyncMock(return_value={"status": "ok", "message_id": 4, "sent": 2, "failed": []})
    for ch in "/broadcast sync checkpoint":
        hc.handle_key(ch)
    buf.seek(0)
    buf.truncate(0)
    hc.handle_key("\r")
    out4 = buf.getvalue()
    assert "@user::> /broadcast sync checkpoint" not in out4
    assert out4.count(user_avatar.lines[0]) == 1
    assert "BROADCAST" in out4
    assert "sync checkpoint" in out4

    # 5. /broadcast --urgent now -> Hub echoes BROADCAST back to sender; rendered once via event
    async def fake_urgent(**kwargs):
        hc.on_broadcast({
            "message": {
                "id": 5,
                "sender": "@user",
                "mode": "urgent",
                "content": kwargs["content"],
                "created_at": "2026-11-15T12:06:00Z",
            }
        })
        return {"status": "ok", "message_id": 5, "sent": 2, "failed": []}

    hc.client.send_message = AsyncMock(side_effect=fake_urgent)
    for ch in "/broadcast --urgent stop now":
        hc.handle_key(ch)
    buf.seek(0)
    buf.truncate(0)
    hc.handle_key("\r")
    out5 = buf.getvalue()
    assert "@user::> /broadcast --urgent stop now" not in out5
    assert out5.count(user_avatar.lines[0]) == 1
    assert "URGENT" in out5
    assert "stop now" in out5


# TC-TUI-06
def test_remove_and_prune_refresh_top_header(monkeypatch):
    hc, buf = _make_test_console(width=100, height=24, dock_active=True)
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

    buf.seek(0)
    buf.truncate(0)
    assert hc.handle_command("/remove")
    assert "Usage: /remove @agent" in buf.getvalue()

    buf.seek(0)
    buf.truncate(0)
    assert hc.handle_command("/remove @codex")
    out_rem = buf.getvalue()
    assert "@codex" not in hc.roster_state
    assert "\x1b7\x1b[1;1H\x1b[2K" in out_rem
    assert "@codex" not in hc.render_header_lines(width=100)[0]
    assert "Removed @codex" in out_rem

    buf.seek(0)
    buf.truncate(0)
    assert hc.handle_command("/kick claude")
    out_kick = buf.getvalue()
    assert "@claude" not in hc.roster_state
    assert "\x1b7\x1b[1;1H\x1b[2K" in out_kick
    assert "Online (0):" in hc.render_header_lines(width=100)[0]
    assert "Removed @claude" in out_kick
    assert removed_targets == ["@codex", "claude"]

    buf.seek(0)
    buf.truncate(0)
    assert hc.handle_command("/prune")
    out_prune = buf.getvalue()
    assert "@stale" not in hc.roster_state
    assert "\x1b7\x1b[1;1H\x1b[2K" in out_prune
    assert "Pruned 1 offline agent(s): @stale" in out_prune


# TC-TUI-07
def test_resize_and_short_or_narrow_terminal_degradation():
    # 1. Resize from 100x24 -> 70x16
    hc, buf = _make_test_console(width=100, height=24, dock_active=True)
    assert "\x1b[3;21r" in buf.getvalue()

    buf.seek(0)
    buf.truncate(0)
    hc._console._width = 70
    hc._console._height = 16
    hc._emit(lambda: hc._out_console.print("after resize"))
    resize_out = buf.getvalue()
    assert "\x1b[3;13r" in resize_out
    assert hc._viewport == (70, 16)
    assert hc._feed_row <= 13

    # 2. Very short terminal 80x6 (< 8 rows) skips DECSTBM safely
    hc_short, buf_short = _make_test_console(width=80, height=6, dock_active=True)
    short_init = buf_short.getvalue()
    assert not hc_short._decstbm_active
    assert "\x1b[3;" not in short_init
    hc_short.on_presence_change({"handle": "@codex", "status": "idle"})
    hc_short.on_explicit_message({
        "message": {
            "sender": "@claude",
            "target": "@codex",
            "content": "short terminal works",
            "created_at": "2026-11-15T12:00:00Z",
        }
    })
    assert "short terminal works" in buf_short.getvalue()
    for ch in "/quit\r":
        hc_short.handle_key(ch)
    assert not hc_short._running


# TC-TUI-08
def test_non_tty_omits_ansi_and_preserves_legacy_bar_helper(monkeypatch):
    hc, buf = _make_test_console(width=90, height=24, dock_active=False)
    hc.on_presence_change({"handle": "@codex", "status": "idle"})
    hc.on_presence_change({"handle": "@claude", "status": "busy"})
    assert hc.format_online_agents_bar() == "Online Agents (2): @claude [BUSY] · @codex [IDLE]"

    hc.on_explicit_message({
        "message": {
            "sender": "@claude",
            "target": "@codex",
            "content": "non tty explicit",
            "created_at": "2026-11-15T12:00:00Z",
        }
    })
    sample = [
        {"created_at": "2026-11-15T12:00:01Z", "sender": "@codex", "mode": "silent", "topic": "general", "content": "first update [1]"},
        {"created_at": "2026-11-15T12:01:02Z", "sender": "@claude", "mode": "explicit", "target": "@codex", "content": "second reply"},
    ]
    hc.client.read_board = AsyncMock(return_value=sample)
    asyncio.run(hc._do_read("general"))

    out = buf.getvalue()
    assert "\x1b[" not in out
    assert "\x1b7" not in out
    assert "\x1b8" not in out
    assert "Presence:" not in out
    assert "non tty explicit" in out
    assert "first update [1]" in out
    assert "second reply" in out

    # CLI read subcommand remains backward-compatible
    cli_buf = io.StringIO()
    monkeypatch.setattr(cli_mod, "rich_console", Console(file=cli_buf, width=100, force_terminal=False, no_color=True))
    monkeypatch.setattr(cli_mod, "sync_read", lambda **kwargs: sample)
    res = CliRunner().invoke(cli_mod.cli, ["read", "--topic", "general", "--all"])
    assert res.exit_code == 0
    cli_out = cli_buf.getvalue().replace("\r\n", "\n")
    assert "@codex:\n    first update [1]\n\n  2026-11-15 12:01:02 [EXP] @claude:\n    second reply" in cli_out


def test_help_commands_and_unknown_hint():
    hc, buf = _make_test_console(width=120, height=24, dock_active=True)

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
