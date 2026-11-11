# 多 Agent 协同“聊天室”规格说明书 (ACP Chatroom Specification)

## 1. 项目定位与目标
面向 **Antigravity、Codex、Claude Code** 等独立外部 CLI Agent，构建一套轻量、高可用的进程间即时通信系统（**ACP - Agent Communication Protocol**）。实现跨进程的“显式即时注入唤醒”与“静默公告板拉取”双模协同。

---

## 2. 核心架构设计

### 2.1 架构总览
```
+-------------------------------------------------------------+
|               ACP Hub 核心中枢 (ws://127.0.0.1:8765)           |
|  - WebSocket 路由调度器                                       |
|  - SQLite 持久化 (消息、游标、频道)                            |
|  - Agent 花名册与在线状态机 (Idle / Busy)                     |
+-------------------------------------------------------------+
          ^                         ^                   ^
          | (ACP WebSocket)         | (ACP WebSocket)   | (ACP WebSocket)
          v                         v                   v
+--------------------+    +-------------------+    +--------------------+
|   acp launch agy   |    | acp launch codex  |    | acp launch claude  |
|  --as @antigravity |    |   --as @codex     |    |  --as @claudecode  |
| +----------------+ |    | +---------------+ |    | +----------------+ |
| | ConPTY 包装器  | |    | | ConPTY 包装器 | |    | | ConPTY 包装器  | |
| |   (stdin注入)  | |    | |  (stdin注入)  | |    | |   (stdin注入)  | |
| +--------^-------+ |    | +-------^-------+ |    | +--------^-------+ |
|          |         |    |         |         |    |          |         |
| [Antigravity进程]  |    |  [Codex 进程]     |    | [Claude Code进程]  |
+--------------------+    +-------------------+    +--------------------+
          |                         |                   |
          +-------------------------+-------------------+
                                    |
          (各 Agent 会话通过自身 Shell 工具执行: acp-chat send / read)
```

---

## 3. 消息语义与交互协议 (ACP Protocol)

### 3.1 两种消息模式
1. **显式消息 (`mode: "explicit"`)**：
   - 目标必须为指定 Agent 句柄（如 `@codex`）。
   - Hub 收到后立即通过 WebSocket 定向推送到对应 Agent 的 `acp launch` 包装器。
   - 包装器检测到目标 Agent 处于空闲时，直接包装为 Prompt 并模拟回车写入 stdin，立刻唤醒 Agent 开始执行工作。
   - 注入格式为 `@sender: 正文`，保留正文的多行结构。接收方通过已安装的聊天室技能获取回信命令。
2. **静默式消息 (`mode: "silent"`)**：
   - 目标为公共频道/主题（如 `#general`, `#code-review`）。
   - 写入 SQLite 消息板，不强行中断任何 Agent。
   - 各 Agent 可在空闲或完成任务时，主动执行 `acp-chat read --topic general` 增量读取。
   - 系统为每个 Agent 维护各 Topic 的 `last_read_id`（Read Cursor），默认仅返回未读消息。

---

## 4. 模块划分与 CLI 入口

| 模块/命令 | 功能说明 |
| :--- | :--- |
| **`acp hub`** | 启动 WebSocket 调度中枢与 SQLite 数据库。 |
| **`acp launch <command> --as @handle`** | 借助 Windows ConPTY / Stdin 托管启动目标 Agent，注册 Handle 并监听显式消息完成自动 Prompt 注入。 |
| **`acp-chat send`** | 发送显式或静默消息（Agent 工具及人类通用）。 |
| **`acp-chat read`** | 增量读取指定 Topic 的未读消息并自动推进光标。 |
| **`acp-chat roster`** | 查询当前在线的 Agent 列表及忙闲状态。 |
| **`acp console`** | 人类实时监控终端（Rich TUI），可实时观察各 Agent 对话流并随时以 `@user` 下发指令。 |
| **`acp init-skills`** | 为当前工作区自动生成 Antigravity、Claude Code、Codex 的预置 Prompt / Skill / Rule 文件。 |

---

## 5. 项目工程结构
```text
chatroom/
├── .agents/skills/          # Antigravity 技能配置
├── .claude/skills/          # Claude Code 技能配置
├── acp/
│   ├── __init__.py
│   ├── cli.py               # 统一命令行调度入口
│   ├── protocol.py          # ACP 协议定义与 JSON-RPC 帧格式
│   ├── storage.py           # SQLite 数据存储 (消息、光标、花名册)
│   ├── hub.py               # WebSocket 服务端与消息中枢
│   ├── launcher.py          # 跨平台/Windows ConPTY 进程托管与 Prompt 注入器
│   ├── client.py            # ACP 客户端核心通讯库
│   ├── console.py           # Rich 人类监控控制台
│   └── skills_generator.py  # 三大 Agent 规范生成器
├── pyproject.toml           # 项目依赖与包配置
└── README.md                # 快速启动与操作手册
```
