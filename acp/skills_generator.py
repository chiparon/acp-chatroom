"""Install the chatroom skill in an agent's native discovery directory."""
from pathlib import Path
import os

from .agents import AgentProfile, agent_registry


CODEX_RULES = """# ACP Multi-Agent Chatroom Rules for Codex

This workspace is connected to an ACP Chatroom where multiple agent processes collaborate.

## Shell Tools Available:
1. `acp-chat send --to @agent --mode explicit "message"`: Directly wakes up or steers the target agent with this prompt.
2. `acp-chat send --to #topic --mode silent "message"`: Posts a passive update to the shared board.
3. `acp-chat read --topic general`: Fetches unread messages from the board.
4. `acp-chat roster`: Checks which agents (@antigravity, @claudecode, etc.) are online and idle.

Always reply to explicit sender requests via `acp-chat send --to @sender --mode explicit ...`.
"""


def install_skill(profile: AgentProfile, workspace: Path | None = None) -> Path:
    root = Path.home() / profile.user_skills if workspace is None else workspace / profile.workspace_skills
    if workspace is None and profile.executable == "dsh" and os.environ.get("DSH_HOME"):
        root = Path(os.environ["DSH_HOME"]).expanduser() / "skills"
    target = root / "acp-chatroom" / "SKILL.md"
    content = Path(__file__).with_name("chatroom_skill.md").read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists() or target.read_bytes() != content:
        target.write_bytes(content)
    return target


def init_workspace_skills(workspace_root: str = ".") -> None:
    root = Path(workspace_root).resolve()
    for profile in dict.fromkeys(agent_registry().values()):
        print(install_skill(profile, root))

    # 3. Codex Rules (AGENTS.md)
    agents_file = root / "AGENTS.md"
    if agents_file.exists():
        existing = agents_file.read_text(encoding="utf-8")
        if "acp-chat" not in existing:
            agents_file.write_text(existing + "\n\n" + CODEX_RULES, encoding="utf-8")
            print(f"✅ Appended ACP rules to existing: {agents_file.relative_to(root)}")
    else:
        agents_file.write_text(CODEX_RULES, encoding="utf-8")
        print(f"✅ Generated Codex rules: {agents_file.relative_to(root)}")
