"""TC-AVATAR-01..05: avatar identity, cell wrap, and console cards."""

import asyncio
import io
import random

from click.testing import CliRunner
from rich.cells import cell_len
from rich.console import Console
from unittest.mock import AsyncMock

import acp.cli as cli_mod
from acp.avatars import layout_message_lines, render_message_with_avatar, resolve_avatar, wrap_to_cells
from acp.console import HumanConsole


def _visual_lines(markup: str, width: int = 54) -> list[str]:
    buf = io.StringIO()
    Console(file=buf, width=width, force_terminal=False, no_color=True).print(markup)
    return buf.getvalue().splitlines()


def test_builtin_avatar_matching_and_boundaries():
    expected = {
        "@claude": ("claude", "#E07A5F", "CLAUDE"),
        "CLAUDECODE": ("claude", "#E07A5F", "CLAUDE"),
        "@Claude-rev": ("claude", "#E07A5F", "CLAUDE"),
        "@codex1": ("codex", "#4ADE80", "CODEX"),
        "GPT-2": ("codex", "#4ADE80", "CODEX"),
        "@agy": ("agy", "#A78BFA", "AGY"),
        "@antigravity": ("agy", "#A78BFA", "AGY"),
        "@dsh": ("dsh", "#38BDF8", "DSH"),
        "DeepSeek": ("dsh", "#38BDF8", "DSH"),
        "@user": ("user", "#FBBF24", "HUMAN"),
        "human": ("user", "#FBBF24", "HUMAN"),
    }
    for handle, (key, color, badge) in expected.items():
        spec = resolve_avatar(handle)
        assert spec.key == key
        assert spec.color == color
        assert spec.badge == badge
    for handle in ("@ag", "@codexer", "@alice", "ag", "codexer"):
        assert resolve_avatar(handle).key == "custom"


def test_custom_handle_deterministic_and_cell_width():
    for handle in ("@reviewer_a", "@qa-bot"):
        assert resolve_avatar(handle) == resolve_avatar(handle)
        assert resolve_avatar(handle).key == "custom"
    rng = random.Random(7)
    handles = [f"@agent_{rng.randrange(100000)}" for _ in range(50)]
    handles.extend(["@claude", "@codex", "@agy", "@dsh", "@user", "@alice"])
    for handle in handles:
        spec = resolve_avatar(handle)
        assert len(spec.lines) == 3
        assert all(cell_len(line) == 7 for line in spec.lines)


def test_wrap_to_cells_handles_cjk_emoji_and_paragraphs():
    cjk = "统一控制台滚动区必须按显示宽度折行避免溢出"
    mixed = "mix 中文😀 URL"
    long_token = "https://example.com/" + ("a" * 40)
    text = cjk + "\n\n" + mixed + "\n" + long_token
    lines = wrap_to_cells(text, 20)
    assert all(cell_len(line) <= 20 for line in lines)
    paragraphs = text.split("\n")
    index = 0
    for paragraph in paragraphs:
        if paragraph == "":
            assert lines[index] == ""
            index += 1
            continue
        built = ""
        while built != paragraph:
            assert index < len(lines)
            built += lines[index]
            index += 1
    assert index == len(lines)
    # A character-count wrap of 11 CJK glyphs stays on one line and exceeds 20 cells.
    wide = "测" * 11
    assert cell_len(wide) > 20
    assert len(wide) < 20
    assert len(wrap_to_cells(wide, 20)) >= 2


def test_single_and_multiline_avatar_card_alignment():
    short = render_message_with_avatar(
        "@codex",
        "hello",
        mode_badge="⚡ EXPLICIT ➔ @user",
        timestamp="12:04:10",
        max_width=54,
    )
    body = "请检查 storage.py 的游标推进逻辑并补充 [main] 边界测试。" * 4
    long = render_message_with_avatar(
        "@claude",
        body,
        mode_badge="⚡ EXPLICIT ➔ @codex",
        timestamp="12:04:10",
        max_width=54,
    )
    short_lines = _visual_lines(short)
    long_lines = _visual_lines(long)
    codex = resolve_avatar("@codex")
    claude = resolve_avatar("@claude")
    assert len(short_lines) >= 3
    for index in range(3):
        assert codex.lines[index] in short_lines[index]
    assert "hello" in short_lines[1]
    assert len(long_lines) >= 4
    for index in range(3):
        assert claude.lines[index] in long_lines[index]
    assert long_lines[3].startswith(" " * 9)
    assert "[main]" in "\n".join(long_lines)
    for line in short_lines + long_lines:
        assert cell_len(line) <= 53
    plain = layout_message_lines("@claude", body, mode_badge="⚡ EXPLICIT ➔ @codex", max_width=54)
    assert plain[3].startswith(" " * 9)
    assert all(cell_len(line) <= 53 for line in plain)


def test_console_incoming_and_read_render_avatars(monkeypatch):
    buf = io.StringIO()
    ui = Console(file=buf, width=80, force_terminal=False, no_color=True)
    hc = HumanConsole(uri="ws://127.0.0.1:8765", handle="@user", ui_console=ui)
    hc.on_explicit_message({
        "message": {
            "sender": "@claude",
            "target": "@codex",
            "content": "please review [main]",
            "created_at": "2026-11-15T12:00:00Z",
        }
    })
    hc.on_silent_message({
        "message": {
            "sender": "@codex",
            "topic": "general",
            "content": "board note",
            "created_at": "2026-11-15T12:01:00Z",
        }
    })
    hc.on_broadcast({
        "message": {
            "sender": "@agy",
            "mode": "broadcast",
            "content": "room update",
            "id": 3,
            "created_at": "2026-11-15T12:02:00Z",
        }
    })
    sample = [
        {"created_at": "2026-11-15T12:00:01Z", "sender": "@codex", "mode": "silent", "topic": "general", "content": "first update [1]"},
        {"created_at": "2026-11-15T12:01:02Z", "sender": "@claude", "mode": "explicit", "target": "@user", "content": "second reply"},
    ]
    hc.client.read_board = AsyncMock(return_value=sample)
    asyncio.run(hc._do_read("general"))
    output = buf.getvalue()
    for sender in ("@claude", "@codex", "@agy"):
        spec = resolve_avatar(sender)
        for line in spec.lines:
            assert line in output
    assert "EXPLICIT" in output
    assert "board note" in output
    assert "room update" in output
    assert "please review [main]" in output
    assert "first update [1]" in output
    assert "second reply" in output

    cli_buf = io.StringIO()
    monkeypatch.setattr(cli_mod, "rich_console", Console(file=cli_buf, width=100, force_terminal=False, no_color=True))
    monkeypatch.setattr(cli_mod, "sync_read", lambda **kwargs: sample)
    result = CliRunner().invoke(cli_mod.cli, ["read", "--topic", "general", "--all"])
    assert result.exit_code == 0
    cli_out = cli_buf.getvalue().replace("\r\n", "\n")
    assert "@codex:\n    first update [1]\n\n  2026-11-15 12:01:02 [EXP] @claude:\n    second reply" in cli_out
    assert "▐▛█▜▌" not in cli_out
