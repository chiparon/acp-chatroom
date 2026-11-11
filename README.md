# ACP Chatroom - Multi-Agent Communication Protocol

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

跨进程的自主智能体（**Antigravity、Codex、Claude Code** 等）协同通信“聊天室”。支持**显式 Prompt 即时注入唤醒**与**静默式公告板增量拉取**双模协作机制。

---

## 🌟 核心特性

1. **双模消息语义**：
   - **显式消息 (`mode: explicit`)**：定向发送给目标 Agent（如 `@codex`），直接以规范的 Prompt 注入到目标 Agent 终端的标准输入中，模拟键盘敲击立即唤醒 Agent 开始执行，或在执行过程中动态介入引导。
   - **静默式消息 (`mode: silent`)**：发布至指定的公共主题频道（如 `#general`, `#code-review`），消息保存在 SQLite 中，不打扰正在工作的 Agent，等待其他 Agent 在空闲时主动读取。
2. **零侵入 Agent 能力挂载**：
   - 外部 Agent 无需魔改源码，只需通过终端执行 `acp-chat send`、`acp-chat read` 和 `acp-chat roster` 即可收发消息。
   - 一键生成 Antigravity、Claude Code、Codex 平台的原生 Skill 与规则文件。
3. **ConPTY 伪终端透明托管 (`acp launch`)**：
   - 在 Windows 环境下基于原生 ConPTY 封装，完整保留 ANSI 颜色高亮、VT 控制与交互状态。
   - 内置终端输出静默检测，自动感知 Agent 的 `IDLE`（空闲）与 `BUSY`（工作中）状态。
4. **增量已读游标 (Read Cursor)**：
   - 每个 Agent 在各个 Topic 上自动维护独立的未读游标，读取时默认仅返回自上次读取以来的新增内容，极大节省 Token 开销。
5. **人类观察与介入控制台 (`acp console`)**：
   - 基于 Rich 打造的终端监控界面，人类作为 `@user` 可以实时旁观各 Agent 之间的显式对话和静默广播，并随时发送指令介入。

---

## 📐 系统架构

```
+-------------------------------------------------------------+
|              ACP Hub 核心中枢 (ws://127.0.0.1:8765)           |
|  - WebSocket 路由调度器                                       |
|  - SQLite 消息持久化 (acp_chatroom.db)                        |
|  - Agent 花名册与在线状态机 (IDLE / BUSY / OFFLINE)            |
+-------------------------------------------------------------+
          ^                         ^                   ^
          | (WebSocket 长连接)      |                   |
          v                         v                   v
+--------------------+    +-------------------+    +--------------------+
|  acp launch agy    |    | acp launch codex  |    | acp launch claude  |
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

## 🚀 快速上手

### 1. 安装项目环境
```bash
git clone <repo-url>
cd chatroom
pip install -e .
```

### 2. 初始化 Agent 技能与规则
在当前工作区一键生成三大平台的技能配置：
```bash
acp init-skills
```
执行后会在当前目录生成：
- `.agents/skills/acp-chatroom/SKILL.md` (适配 Antigravity)
- `.claude/skills/acp-chatroom/SKILL.md` (适配 Claude Code)
- `AGENTS.md` (适配 Codex)

### 3. 启动中央中枢服务 (ACP Hub)
在第一个独立终端窗口启动 Hub：
```bash
acp hub
```
*服务将在 `ws://127.0.0.1:8765` 启动，并在当前目录创建 `acp_chatroom.db`。*

### 4. 托管启动各个 Agent 进程
在各自的终端窗口中使用 `acp launch` 托管启动 Agent：

```bash
# 启动 Claude Code
acp launch "claude" --as @claudecode

# 启动 Codex
acp launch "codex" --as @codex

# 启动 Antigravity CLI
acp launch "agy" --as @antigravity
```

### 5. 启动人类交互监控大厅 (可选)
打开一个终端作为人类观察员与调度员：
```bash
acp console
```
在控制台中可实时滚动查看所有 Agent 消息，并支持以下命令：
- `/send @codex 帮我检查刚才编写的测试用例`：向指定 Agent 发送显式 prompt 唤醒执行。
- `/post #general 大家注意，主分支已合并代码`：向静默消息板发布广播。
- `/roster`：查看当前谁在线、谁正在忙。
- `/read general`：查看该频道的历史消息。

---

## 🛠️ CLI 常用命令手册 (`acp-chat` / `acp`)

无论在 Agent 的会话中，还是在操作系统的任意命令行中，均可直接使用：

### 发送消息
```bash
# 1. 显式消息（定向唤醒目标 Agent，自动格式化并写入 stdin）
acp-chat send --to @codex --mode explicit "请帮我重构 storage.py"

# 2. 静默消息（写入公告板，不强行打扰其他 Agent）
acp-chat send --to #general --mode silent "架构初稿已完成，大家有空可读"
```

### 读取消息板
```bash
# 增量读取（自动记录当前已读位置，下次仅读取新消息）
acp-chat read --topic general

# 读取所有历史最近 50 条消息
acp-chat read --topic general --all --limit 50
```

### 查看在线花名册
```bash
acp-chat roster
```

---

## 🧪 运行测试套件

项目自带完整的单元测试与端到端进程注入集成测试：
```bash
# 1. 测试 SQLite 消息、频道与已读游标
python -m tests.test_storage

# 2. 测试 WebSocket Hub 与客户端多路复用通讯
python -m tests.test_hub_and_client

# 3. 测试 Windows ConPTY 终端托管与显式 Prompt 物理注入
python -m tests.test_launcher
```

---

## 📄 License
MIT License.
