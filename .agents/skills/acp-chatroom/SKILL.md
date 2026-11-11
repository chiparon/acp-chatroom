---
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
