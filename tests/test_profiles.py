import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from acp.agents import agent_registry
from acp.cli import cli
from acp.launcher import AgentLauncher
from acp.protocol import format_explicit_prompt


@pytest.mark.parametrize(("name", "executable", "flag"), [
    ("claude", "claude", "--dangerously-skip-permissions"),
    ("claudecode", "claude", "--dangerously-skip-permissions"),
    ("agy", "agy", "--dangerously-skip-permissions"),
    ("antigravity", "agy", "--dangerously-skip-permissions"),
    ("codex", "codex", "--dangerously-bypass-approvals-and-sandbox"),
])
def test_bare_launch_uses_registered_defaults(name, executable, flag):
    launcher = AgentLauncher(name, "@custom")
    assert Path(launcher.argv[0]).stem.lower() == executable
    assert launcher.argv[1:] == ([flag, "--no-daemon"] if name == "codex" else [flag])


def test_custom_command_preserves_arguments():
    launcher = AgentLauncher('codex --sandbox read-only', "@worker")
    assert launcher.argv[1:] == ["--no-daemon", "--sandbox", "read-only"]
    assert AgentLauncher("codex --no-daemon", "gpt2").argv.count("--no-daemon") == 1
    assert AgentLauncher("claude --continue", "@worker").argv[1:] == ["--dangerously-skip-permissions", "--continue"]


def test_prompt_preserves_content_and_sender():
    assert format_explicit_prompt("alice", "  一\n二  ") == "@alice:   一\n二  "


def test_multiline_paste_is_atomic_and_submits_once():
    launcher = AgentLauncher("codex", "@worker")
    launcher.pty_proc = Mock()
    event = {"message": {"sender": "@one", "target": "@other", "content": "wrong"}}
    launcher.on_explicit_message(event)
    launcher.pty_proc.write.assert_not_called()
    event["message"].update(target="worker", content=" 一\r\n二\x1b[201~ ")
    launcher.on_explicit_message(event)
    assert [c.args[0] for c in launcher.pty_proc.write.call_args_list] == [
        "\x1b[200~@one:  一\n二[201~ \x1b[201~", "\r",
    ]


@pytest.mark.parametrize("name", ["codex", "agy", "claude"])
def test_agent_local_install(tmp_path, monkeypatch, name):
    monkeypatch.setenv("USERPROFILE" if os.name == "nt" else "HOME", str(tmp_path))
    runner = CliRunner()
    profile = agent_registry()[name]
    file = tmp_path / profile.user_skills / "acp-chatroom/SKILL.md"
    for _ in range(2):
        assert runner.invoke(cli, ["install", name]).exit_code == 0
        assert "@sender" in file.read_text("utf-8")
    assert list(tmp_path.rglob("SKILL.md")) == [file]


def test_init_skills_preserves_workspace_instructions(tmp_path):
    instructions = tmp_path / "AGENTS.md"
    instructions.write_text("Keep my instructions.\n", "utf-8")
    for _ in range(2):
        assert CliRunner().invoke(cli, ["init-skills", "--workspace", str(tmp_path)]).exit_code == 0
    assert instructions.read_text("utf-8").startswith("Keep my instructions.\n")
    assert instructions.read_text("utf-8").count("ACP Multi-Agent") == 1
