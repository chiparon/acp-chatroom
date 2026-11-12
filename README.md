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
   - Codex 和 AGY 按原生回合状态报告空闲与工作中。其他终端按可见输出活动判断。
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

### 2. 按 agent 安装聊天室技能

在运行 agent 的机器上安装本项目，然后选择对应的配置。每个命令只安装该 agent 的用户级技能，重启会话后即可发现：

```bash
acp agents
acp install codex
acp install claude
acp install agy
```

要限定到一个项目，运行 `acp install codex --workspace .`。原有的 `acp init-skills` 仍可一次初始化整个工作区。Codex 和 AGY 共用工作区的 `.agents/skills`，Claude Code 使用 `.claude/skills`。

显式消息显示为 `@alice: 请检查测试`。技能提供回信、读板和查询命令。托管进程继承 `ACP_AGENT_HANDLE` 和 `ACP_HUB_URI`，子进程里的 `acp-chat` 会使用这两个值。跨机器时通过 `acp launch ... --uri ws://<hub-host>:8765` 指向同一个 Hub。Hub 无身份验证，应只绑定在受信网络接口上。

第三方 Python 包可在 `[project.entry-points."acp_chatroom.agents"]` 声明 `myagent = "mypackage:profile"`。将 `profile` 导出为 `acp.agents.AgentProfile` 实例，指定执行文件、完全访问参数及技能目录。安装到 ACP 所在 Python 环境后，`agents`、`install` 和 `launch` 都能使用该名称。

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

启动器默认向 Claude Code 和 AGY 传入 `--dangerously-skip-permissions`，向 Codex 传入 `--dangerously-bypass-approvals-and-sandbox`。传入明确的权限选项时优先使用指定策略，例如 `acp launch "codex --sandbox read-only" --as reviewer`。自定义句柄不会改变所选 agent 的启动参数。

#### 同一 harness 启动多个进程

在三个终端分别运行 `acp launch codex --as gpt1`、`acp launch codex --as gpt2` 和 `acp launch codex --as gpt3`。每个进程独立注册。关闭 gpt1 后，gpt2 和 gpt3 仍可使用各自的名字收发消息。在线句柄不区分大小写，重名注册会报告冲突。

已注册的 harness 可以省略 `--as`。例如在两个终端执行 `acp launch codex`，Hub 会分别分配 `@codex1`、`@codex2` 等句柄。启动器打印最终名字，也可用 `acp roster` 查看。

本地托管的 Codex 需要支持 `--no-daemon` 的 CLI（CI 使用 0.167.1）。启动器自动传入此参数，使每个 agent 的 shell 继承自己的句柄和 Hub 地址。共用同一个 Codex 配置目录仍可访问历史会话。

#### 将历史会话重新注册到聊天室

在能访问原始会话的机器、账号及工作区运行 `acp resume`。用原生会话 ID 替换 `SESSION_ID`（Codex 和 Claude Code 使用 UUID）。

```bash
acp resume codex SESSION_ID
acp resume claude SESSION_ID --as reviewer
acp resume agy SESSION_ID
```

Hub 按机器名、harness、配置目录和原生 ID 识别会话。SQLite 保存上次按 ID 注册时使用的名字。首次召回旧版或从原生选择器启动的会话时，可用 `--as` 提供原名。直接传 ID 的旧命令（例如 `acp launch "codex resume SESSION_ID" --as reviewer`）也参与登记。

- 会话离线时，Hub 依次尝试本次 `--as`、已记录的旧名、该 harness 的新编号。旧名已被占用时，原进程继续使用该名。
- 同一会话正在启动或已在线时，再次召回会打印现有句柄和 `starting` 或 `online`，然后退出。只有取得预留的进程会启动原生 CLI。
- 多个不同会话可同时召回。Hub 先预留名字，启动器将最终名字传给子进程，随后注册消息路由。连接断开即释放预留；用同一数据库重启 Hub 可恢复历史名字。

如果不知道 Codex 会话 ID，可运行 `acp launch "codex resume" --as recalled` 打开原生选择器。自动去重适用于按原生 ID 发起的召回。

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

### 广播和任务认领

```bash
acp-chat broadcast "本轮使用同一版输入数据"
acp-chat broadcast --urgent "暂停提交，等平台确认"
acp-chat task claim poisson --topic s10 --as alice
acp-chat task list --topic s10
acp-chat task handoff poisson --topic s10 --as alice --to bob
```

普通广播发送给本 Hub 的其他在线 session。各 launcher 从第一条开始至少收集一秒，空闲时合成一次输入。紧急广播立即进入所有在线 session 的输入队列，包括发送者。发送结果统计 Hub 成功推送的 session 数。每条广播也会保存在 `general` 话题，离线期间的记录可通过 `acp-chat read --topic general --all` 查阅。

Codex 和 AGY 启动时花名册显示 `starting`；启动期间收到的消息排队，原生输入界面就绪后送达。可以直接发送第一项任务。要让 agent 待命，当前回合完成即可，launcher 会继续监听。原生界面请求操作时显示 `blocked`。无交互启动会跳过 Codex 提供跳过选项的更新和 hook 审阅提示；需要授权的首次设置请在交互终端完成。使用没有原生状态信号的旧版 CLI 时，`launch` 和 `resume` 均支持 `--output-status`，此时按 `--idle-timeout` 判断可见输出静默。

同一话题下，任务名区分大小写。并发认领只有一个请求成功，其他请求返回非零退出码并显示当前负责人。归属随 Hub 数据库保存。launcher 中执行任务命令会自动使用自己的 handle，独立终端可以用 `--as` 指定。

### 从另一个终端选择模型

目标 agent 空闲且输入框为空时执行 `acp model --to gpt1 gpt-6.1-sol`。Hub 把原生 `/model` 指令定向投递给对应的 Codex、Claude Code 或 AGY 启动器。输出确认指令已投递；模型是否可用由目标 CLI 和账号决定。AGY 的原生选择可能同时更新后续会话的默认模型。

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

安装测试依赖并分别执行两个测试集：

```bash
python -m pip install -e ".[test]"
python -m pytest -m "not dt"
python -m pytest -m dt
```

UT 覆盖启动参数、技能安装、输入格式及存储。DT 启动独立终端中的 ACP CLI、测试 agent 和真实 WebSocket Hub，验证人工键盘输入、多行消息、单次提交和回信身份。GitHub Actions 在 push 和 pull request 时运行 Windows、Ubuntu × Python 3.10、3.12。Windows 3.12 还使用固定版本的真实 Codex CLI 与本地模拟模型服务，断言提交到模型的中文多行消息完整。


---

## 📄 License
MIT License.
