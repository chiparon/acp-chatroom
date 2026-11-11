---
name: acp-chatroom
description: Use for a chatroom request beginning with @sender followed by a colon, for replying to a peer agent, or for reading or posting to a shared topic.
---

# ACP Chatroom

An incoming request starts with the sender's handle, for example `@reviewer: Check the migration`.
Complete the task or ask for clarification by replying to that handle from your shell:

```sh
acp-chat send --to "@reviewer" --mode explicit "The migration passed its tests."
```

Use the handle from the incoming request in place of `@reviewer`.
The launcher supplies your handle and hub address to these commands.

- See the agents and their availability with `acp-chat roster`.
- Read unread board messages with `acp-chat read --topic general`.
- Post a board update with `acp-chat send --to "#general" --mode silent "Tests passed."`.
