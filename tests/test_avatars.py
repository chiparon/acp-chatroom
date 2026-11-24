from rich.cells import cell_len
from acp.avatars import resolve_avatar, wrap_to_cells, render_message_with_avatar


def test_avatar_families_and_custom_handles():
    for key, handles in {
        "claude": ["Claude", "@claudecode"], "codex": ["@codex1", "GPT-2"],
        "agy": ["agy", "@antigravity"], "dsh": ["dsh", "@deepseek"],
        "user": ["user", "@human"], "custom": ["ag", "codexer", "alice", *map(str, range(50))],
    }.items():
        for handle in handles:
            avatar = resolve_avatar(handle)
            assert avatar.key == key
            assert avatar == resolve_avatar("@" + handle.upper().lstrip("@"))
            assert [cell_len(line) for line in avatar.lines] == [7, 7, 7]


def test_card_alignment_and_literal_content():
    content = "中文😀[main]e\u0301" * 20
    wrapped = wrap_to_cells(content + "\n\nurl", 20)
    assert "".join(wrapped) == content + "url"
    assert wrapped[-2:] == ["", "url"]
    assert all(cell_len(line) <= 20 for line in wrapped)
    lines = render_message_with_avatar({"sender": "@codex", "content": content}, 54).plain.splitlines()
    assert all(cell_len(line) <= 53 for line in lines)
    assert all(line.startswith(" " * 9) for line in lines[3:])
    assert "[main]" in "".join(lines)
    short = render_message_with_avatar({"sender": "@codex", "content": "hi"}, 54)
    assert len(short.plain.splitlines()) == 3
