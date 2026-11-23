import io
from unittest.mock import AsyncMock

from click.testing import CliRunner
from rich.cells import cell_len
from rich.console import Console

import acp.cli as cli_mod
from acp.avatars import (
    AvatarSpec,
    render_message_with_avatar,
    resolve_avatar,
    wrap_to_cells,
)
from acp.console import HumanConsole


# TC-AVATAR-01
def test_builtin_avatar_matching_and_boundaries():
    builtin_cases = [
        ("@claude", "claude", "#E07A5F", "CLAUDE"),
        ("CLAUDECODE", "claude", "#E07A5F", "CLAUDE"),
        ("@claude-rev", "claude", "#E07A5F", "CLAUDE"),
        ("@codex", "codex", "#4ADE80", "CODEX"),
        ("@codex1", "codex", "#4ADE80", "CODEX"),
        ("gpt-2", "codex", "#4ADE80", "CODEX"),
        ("@agy", "agy", "#A78BFA", "AGY"),
        ("@antigravity_1", "agy", "#A78BFA", "AGY"),
        ("@dsh", "dsh", "#38BDF8", "DSH"),
        ("DeepSeek-2", "dsh", "#38BDF8", "DSH"),
        ("@user", "user", "#FBBF24", "HUMAN"),
        ("@human", "user", "#FBBF24", "HUMAN"),
        ("@console", "user", "#FBBF24", "HUMAN"),
        ("@observer_1", "user", "#FBBF24", "HUMAN"),
    ]
    for handle, expected_key, expected_color, expected_badge in builtin_cases:
        spec = resolve_avatar(handle)
        assert isinstance(spec, AvatarSpec)
        assert spec.key == expected_key, f"{handle} expected {expected_key}, got {spec.key}"
        assert spec.color == expected_color
        assert spec.badge == expected_badge

    for non_builtin in ("@ag", "@codexer", "@alice", "@claude_x" if False else "@claudette", "@dshaper", "@users"):
        spec = resolve_avatar(non_builtin)
        assert spec.key == "custom", f"{non_builtin} should fall back to custom, got {spec.key}"


# TC-AVATAR-02
def test_custom_handle_deterministic_and_cell_width():
    for handle in ("@reviewer_a", "@qa-bot", "reviewer_a"):
        s1 = resolve_avatar(handle)
        s2 = resolve_avatar(handle)
        assert s1 == s2
        assert s1.key == "custom"

    assert resolve_avatar("@reviewer_a") == resolve_avatar("REVIEWER_A")

    all_handles = [
        "@claude",
        "@claudecode",
        "@codex",
        "@gpt",
        "@agy",
        "@antigravity",
        "@dsh",
        "@deepseek",
        "@user",
        "@human",
        "@console",
        "@observer",
    ] + [f"@custom_agent_{i}" for i in range(50)]

    for h in all_handles:
        spec = resolve_avatar(h)
        assert len(spec.lines) == 3
        for line in spec.lines:
            assert cell_len(line) == 7, f"Avatar line {line!r} for {h} has cell_len {cell_len(line)} != 7"


# TC-AVATAR-03
def test_wrap_to_cells_handles_cjk_emoji_and_paragraphs():
    cjk_text = "这是一段用于测试中文宽字符自动折行算法是否严格遵守显示列宽限制的长句子"
    cjk_lines = wrap_to_cells(cjk_text, max_cells=20)
    assert len(cjk_lines) > 1
    assert all(cell_len(line) <= 20 for line in cjk_lines)
    assert "".join(cjk_lines) == cjk_text

    mixed_text = "请检查 storage.py 的游标推进逻辑并补充 Emoji 😀🚀🎉 测试\n\nhttps://example.com/very/long/path/without/any/spaces/1234567890"
    mixed_lines = wrap_to_cells(mixed_text, max_cells=20)
    assert all(cell_len(line) <= 20 for line in mixed_lines)
    assert "" in mixed_lines  # Explicit blank line between paragraphs preserved

    reconstructed = "".join(mixed_lines)
    for token in ("请检查", "storage.py", "游标推进逻辑", "😀🚀🎉", "https://example.com/very/long/path/without/any/spaces/1234567890"):
        assert token in reconstructed


# TC-AVATAR-04
def test_single_and_multiline_avatar_card_alignment():
    claude_avatar = resolve_avatar("@claude")

    # 1. Single-line message still renders all 3 avatar lines
    buf_short = io.StringIO()
    c_short = Console(file=buf_short, width=54, force_terminal=False, no_color=True, legacy_windows=False)
    card_short = render_message_with_avatar(
        sender="@claude",
        content="hello",
        mode="explicit",
        target="@codex",
        timestamp="2026-11-15T12:04:10Z",
        max_width=54,
    )
    c_short.print(card_short, highlight=False)
    short_lines = buf_short.getvalue().splitlines()
    assert len(short_lines) == 3
    for idx in range(3):
        assert short_lines[idx].startswith(claude_avatar.lines[idx])
        assert cell_len(short_lines[idx]) <= 53

    # 2. Multi-line message with [main] brackets and CJK characters
    buf_long = io.StringIO()
    c_long = Console(file=buf_long, width=54, force_terminal=False, no_color=True, legacy_windows=False)
    long_content = "请检查 [main] 分支下 storage.py 的游标推进逻辑，并确认 [bold] 标签不会被误解析为 Rich 样式。"
    card_long = render_message_with_avatar(
        sender="@claude",
        content=long_content,
        mode="explicit",
        target="@codex",
        timestamp="2026-11-15T12:04:10Z",
        max_width=54,
    )
    c_long.print(card_long, highlight=False)
    long_lines = buf_long.getvalue().splitlines()
    assert len(long_lines) >= 4
    for idx in range(3):
        assert long_lines[idx].startswith(claude_avatar.lines[idx])
    for extra_line in long_lines[3:]:
        assert extra_line.startswith(" " * 9)
        assert extra_line[9:10] != ""
    for line in long_lines:
        assert cell_len(line) <= 53
    joined_long = "\n".join(long_lines)
    assert "[main]" in joined_long
    assert "[bold]" in joined_long


# TC-AVATAR-05
def test_console_incoming_and_read_render_avatars(monkeypatch):
    import asyncio

    buf = io.StringIO()
    ui = Console(file=buf, width=90, force_terminal=False, no_color=True, legacy_windows=False)
    hc = HumanConsole(uri="ws://127.0.0.1:8765", handle="@user", ui_console=ui)
    hc._running = True
    hc._dock_active = False

    hc.on_explicit_message({
        "message": {
            "sender": "@claude",
            "target": "@codex",
            "content": "explicit hello [main]",
            "created_at": "2026-11-15T12:04:10Z",
        }
    })
    hc.on_silent_message({
        "message": {
            "sender": "@codex",
            "topic": "general",
            "content": "silent board update",
            "created_at": "2026-11-15T12:04:28Z",
        }
    })
    hc.on_broadcast({
        "message": {
            "id": 3,
            "sender": "@agy",
            "mode": "urgent",
            "content": "urgent broadcast alert",
            "created_at": "2026-11-15T12:05:00Z",
        }
    })

    sample = [
        {"created_at": "2026-11-15T12:00:01Z", "sender": "@codex", "mode": "silent", "topic": "general", "content": "first update [1]"},
        {"created_at": "2026-11-15T12:01:02Z", "sender": "@claude", "mode": "explicit", "target": "@codex", "content": "second reply"},
    ]
    hc.client.read_board = AsyncMock(return_value=sample)
    asyncio.run(hc._do_read("general"))

    out = buf.getvalue()
    for handle in ("@claude", "@codex", "@agy"):
        spec = resolve_avatar(handle)
        for avatar_line in spec.lines:
            assert avatar_line in out

    assert "EXPLICIT" in out
    assert "#general" in out
    assert "URGENT" in out
    assert "first update [1]" in out
    assert "second reply" in out

    # Non-interactive CLI `acp read` keeps legacy output compatibility
    cli_buf = io.StringIO()
    monkeypatch.setattr(cli_mod, "rich_console", Console(file=cli_buf, width=100, force_terminal=False, no_color=True))
    monkeypatch.setattr(cli_mod, "sync_read", lambda **kwargs: sample)
    res = CliRunner().invoke(cli_mod.cli, ["read", "--topic", "general", "--all"])
    assert res.exit_code == 0
    cli_out = cli_buf.getvalue().replace("\r\n", "\n")
    assert "@codex:\n    first update [1]\n\n  2026-11-15 12:01:02 [EXP] @claude:\n    second reply" in cli_out
