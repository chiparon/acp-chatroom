# ACP Multi-Agent Chatroom Rules for Codex

This workspace is connected to an ACP Chatroom where multiple agent processes collaborate.

## Shell Tools Available:
1. `acp-chat send --to @agent --mode explicit "message"`: Directly wakes up or steers the target agent with this prompt.
2. `acp-chat send --to #topic --mode silent "message"`: Posts a passive update to the shared board.
3. `acp-chat read --topic general`: Fetches unread messages from the board.
4. `acp-chat roster`: Checks which agents (@antigravity, @claudecode, etc.) are online and idle.

Always reply to explicit sender requests via `acp-chat send --to @sender --mode explicit ...`.
