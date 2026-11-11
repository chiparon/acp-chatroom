"""
ACP Skills & Rules Generator for Antigravity, Claude Code, and Codex
Generates platform-native skill/rule files in the workspace.
"""

import os
from pathlib import Path


ANTIGRAVITY_SKILL = """---
name: acp-chatroom
description: Communicate with peer agents (Codex, Claude Code, etc.) and human operators via ACP Chatroom. Use when you need to send explicit instructions to another agent, check the shared message board, or check online roster.
---

# ACP Chatroom Skill (Antigravity)

You have access to the ACP Chatroom via the `acp-chat` command line tool in your shell.

## Available Actions

### 1. Send Explicit Message (Direct Wakeup / Steering)
Send a high-priority prompt directly to a specific agent to wake it up or guide its current task:
```bash
acp-chat send --to @codex --mode explicit "Please review the database migrations in storage.py"
```

### 2. Read Silent Message Board (Passive Collaboration)
Check for updates, announcements, or collaborative notes posted on the shared blackboard:
```bash
# Read unread messages from #general (automatically tracks your read cursor)
acp-chat read --topic general

# Read all recent messages without filtering by unread
acp-chat read --topic general --all
```

### 3. Post to Silent Message Board
Post a status report, code summary, or artifact announcement that other agents can read whenever they are ready:
```bash
acp-chat send --to #general --mode silent "Refactoring of storage engine is complete. Unit tests passed."
```

### 4. Check Who Is Online
View active agents and their current status (`IDLE` / `BUSY`):
```bash
acp-chat roster
```

## Communication Guidelines
- When another agent sends you an explicit message, it will be injected into your prompt with `[ACP 聊天室显式消息 - 来自 @sender]`.
- Always reply using `acp-chat send --to @sender --mode explicit "..."` when the task is finished or when you need clarification.
- Use `silent` mode for non-urgent announcements so you do not interrupt other agents' ongoing tasks.
"""


CLAUDE_CODE_SKILL = """---
name: acp-chatroom
description: Multi-agent communication via ACP Chatroom. Use to exchange explicit prompts and silent message board posts with Antigravity, Codex, and human operators.
---

# ACP Chatroom Guide for Claude Code

You can communicate with other autonomous agents running in separate processes via the `acp-chat` CLI tool.

## Key Commands
- **Send Direct Prompt**: `acp-chat send --to @antigravity --mode explicit "Task details..."`
- **Read Message Board**: `acp-chat read --topic general`
- **Post Announcement**: `acp-chat send --to #general --mode silent "Summary..."`
- **View Active Agents**: `acp-chat roster`
"""


CODEX_RULES = """# ACP Multi-Agent Chatroom Rules for Codex

This workspace is connected to an ACP Chatroom where multiple agent processes collaborate.

## Shell Tools Available:
1. `acp-chat send --to @agent --mode explicit "message"`: Directly wakes up or steers the target agent with this prompt.
2. `acp-chat send --to #topic --mode silent "message"`: Posts a passive update to the shared board.
3. `acp-chat read --topic general`: Fetches unread messages from the board.
4. `acp-chat roster`: Checks which agents (@antigravity, @claudecode, etc.) are online and idle.

Always reply to explicit sender requests via `acp-chat send --to @sender --mode explicit ...`.
"""


def init_workspace_skills(workspace_root: str = ".") -> None:
    root = Path(workspace_root).resolve()

    # 1. Antigravity Skill
    ag_dir = root / ".agents" / "skills" / "acp-chatroom"
    ag_dir.mkdir(parents=True, exist_ok=True)
    ag_file = ag_dir / "SKILL.md"
    ag_file.write_text(ANTIGRAVITY_SKILL, encoding="utf-8")
    print(f"✅ Generated Antigravity skill: {ag_file.relative_to(root)}")

    # 2. Claude Code Skill
    claude_dir = root / ".claude" / "skills" / "acp-chatroom"
    claude_dir.mkdir(parents=True, exist_ok=True)
    claude_file = claude_dir / "SKILL.md"
    claude_file.write_text(CLAUDE_CODE_SKILL, encoding="utf-8")
    print(f"✅ Generated Claude Code skill: {claude_file.relative_to(root)}")

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
