# 软件设计文档 (SDD): 统一 Hub + Console 类 Claude Code 交互终端

> **分支**: `feat/unified-console-tui`（基线提交 `388ad2b`，已包含 PR #16/#17/#18）  
> **状态**: 已实现
> **关联文档**: [TDD.md](./TDD.md) · [REQUIREMENTS.md](./REQUIREMENTS.md)

---

## 1. 概述与设计目标 (Overview & Goals)

当前 ACP Chatroom 需要用户先在终端 A 运行 `acp hub`，再到终端 B 运行 `acp console`，使用链路较割裂；同时存在以下交互痛点：
1. **状态变更刷屏**：每次 Agent 上线、空闲、忙碌、离线都会在主消息流中打印 `• Presence: @xx is now IDLE/BUSY`，导致真实对话内容被状态日志冲刷。
2. **成员辨识度低**：成员发言均为纯文本前缀，多 Agent 协同对话时缺乏类似 Claude Code 的视觉人格与对话层次感；且人类自己发送 `/send` 或普通 `/broadcast` 时缺乏统一的消息卡片展示。
3. **启动不够开箱即用**：直接运行 `acp` 仅输出帮助文本，无法像 `claude` / `codex` CLI 一样一步直达交互终端。

本设计将 **`acp hub` 与 `acp console` 合并为单进程一体化的类 Claude Code CLI 终端**，核心目标如下：
- **一键直达主界面**：运行 `acp`（无子命令）、`acp hub`（交互式终端）或 `acp console` 时，均直接启动统一 Console TUI；若本地端口尚无 Hub 实例，自动同进程拉起内嵌 `ACPHub`；若端口已有存活的 ACP Hub，则无缝附着为客户端模式连入。保留 `acp hub --headless` 及非 TTY 自动降级，保障后台/CI 场景兼容。
- **顶部固定通栏 + `DECSTBM` 滚动区（右上角固定在线情况）**：利用 VT100 滚动区（`\x1b[{top};{bottom}r`）锁定顶部 2 行固定通栏（左侧展示 Hub 运行模式与地址，**右侧固定展示聊天室在线成员及单列着色状态灯**）与底部 3 行固定输入 Dock（提示符 **`@user::> `**），中间区域自然滚动。彻底消灭主消息流中的 `• Presence: @xx is now XX` 刷屏，同时避免悬浮矩形带来的滚屏残影与右侧留白浪费。
- **成员像素/方块小人头像系统**：为每位发言成员（Claude、Codex、Antigravity、DeepSeek、Human 及自定义句柄）渲染 3 行高彩色 Unicode 半块像素小人头像与并排对齐的消息卡片，覆盖实时显式/静默/广播消息、人类自身发言及 `/read` 历史阅读。

---

## 2. 整体架构与进程生命周期 (Unified Hub + Console Architecture)

### 2.1 运行模式与拓扑关系

合并后的 CLI 支持在**单进程同 `asyncio` 事件循环**内协同运行 `ACPHub` 与 `HumanConsole`，同时保留独立的 Headless Hub 与跨终端接入能力：

```text
                       +---------------------------------------+
                       |  acp / acp hub / acp console          |
                       +-------------------+-------------------+
                                           |
                        WebSocket PING 协议探测（短超时 0.8s）
                        /                  |                   \
          [ConnectionRefused]       [返回 PONG 帧]         [非 ACP 服务占口 / 握手失败]
             /           \                 |                              |
     [本地回环且允许]  [远端/--no-auto]     v                              v
            |                |     +-------------------------+     直接报错退出
            v                v     | Console 客户端模式       |    （明确提示端口被
  +-----------------------+ 报错   | (owns_hub=False)        |     非 ACP 进程占用）
  | 单进程 Unified Console | 退出   | └─ ACPClient ─► 外部Hub |
  | (owns_hub=True)       |        +-------------------------+
  | ├─ 内嵌 ACPHub        |
  | └─ HumanConsole (WS)  |
  +-----------------------+
        ^             ^
        | (WS)        | (WS)
  +-----------+ +-----------+
  | acp launch| | acp launch|
  |   agy     | |   codex   |
  +-----------+ +-----------+
```

### 2.2 CLI 路由设计 (`acp/cli.py`)

1. **根命令组 (`@click.group(invoke_without_command=True)`)**：
   - 根组支持可选参数 `--uri`（`envvar="ACP_HUB_URI"`, 默认 `None` 以区分显式传参）、`--as` / `--handle` / `-a`（默认 `None`）、`--db`（默认 `"acp_chatroom.db"`），通过 `ctx.ensure_object(dict)` 透传给子命令作为回退默认值，避免 `acp --as @alice console` 场景下参数被根组吞掉。
   - 当 `ctx.invoked_subcommand is None`（即裸输 `acp` 或 `acp-chat`）：
     - 若处于交互式 TTY（`sys.stdin.isatty() and sys.stdout.isatty()`）：调用 `start_unified_console(uri=effective_uri, handle=effective_handle, db_path=db, auto_hub=True)` 直接启动统一终端。
     - 若处于非 TTY 环境（如管道、CI 脚本调用）：输出 `click.echo(ctx.get_help())` 并正常退出，避免卡死脚本。
   - 说明：`pyproject.toml` 中 `acp` 与 `acp-chat` 共享 `acp.cli:main` 入口，二者在无参 TTY 下行为一致，在非 TTY 或带子命令（如 `acp-chat send`）时保持 100% 兼容。
2. **`acp hub` 命令**：
   - 保留 `--host`（默认 `127.0.0.1`）、`--port`（默认 `8765`）、`--db`（默认 `acp_chatroom.db`）。
   - 新增 `--headless`（`is_flag=True`）与 `--as` / `--handle` / `-a`（默认 `@user`）。
   - **环境隔离**：`acp hub` 始终根据自身 `--host` 与 `--port` 构造连接地址，**绝不被环境变量 `ACP_HUB_URI` 劫持**；当 `--host` 为 `0.0.0.0` 或 `::` 时，内嵌 Hub 绑定该通配接口，本地 Console 客户端连接地址自动映射为 `ws://127.0.0.1:{port}`（IPv6 通配映射为 `ws://[::1]:{port}`）。
   - 分支策略：
     - 当 `--headless` 为真，或非交互式 TTY 时，运行原有 `run_hub(host=host, port=port, db_path=db)` 纯后台模式。
     - 当处于交互式 TTY 且未传 `--headless` 时，调用 `start_unified_console(uri=connect_uri, handle=handle, bind_host=host, bind_port=port, db_path=db, auto_hub=True, explicit_hub_options=(db != "acp_chatroom.db" or host != "127.0.0.1"))`。若目标端口已有运行中的 ACP Hub，则自动以 `client` 模式附着，并在启动 Banner 中给出明显提示（尤其是用户显式传了非默认 `--db` 或 `--host` 时，明确提醒当前正在附着外部 Hub 而未启用新数据库）。
3. **`acp console` 命令**：
   - 保留 `--uri`（`envvar="ACP_HUB_URI"`, 默认 `ws://127.0.0.1:8765`）、`--as` / `--handle` / `-a`（默认 `@user`）。
   - 新增 `--db`（默认 `acp_chatroom.db`）和 `--no-auto-hub`（`is_flag=True`，关闭自动拉起本地 Hub，强制仅客户端连接）。
   - 调用 `start_unified_console(uri=uri, handle=handle, db_path=db, auto_hub=not no_auto_hub)`。

### 2.3 三态协议探测、内嵌 Hub 生命周期与日志隔离

在 `acp/console.py`（及 `acp/hub.py` 辅助能力）中实现 `UnifiedConsoleRunner` 与 `start_unified_console(...)`：

1. **WebSocket 协议层三态探测 (`probe_hub(uri, timeout=0.8)`)**：
   - 使用 `websockets.connect(uri)` 发送 `{"action": "ping", "req_id": ...}` 并校验回包 `event == "pong"`（WebSocket 握手与 PING 共用 0.8 秒超时。Windows 可能重试已被拒绝的 TCP SYN，单独给 TCP 建连最多 5 秒，以免将空闲端口误判为非 ACP 服务），精确区分三种结果：
     - **`ALIVE_HUB`**：收到合法 `pong` ➔ 目标已是存活 ACP Hub ➔ `owns_hub = False`，直接附着连接。
     - **`PORT_FREE`**：连接被拒绝（`ConnectionRefusedError` / 本地端口无监听）：
       - 若 `auto_hub=True` 且目标为本地回环/可绑定地址（`127.0.0.1`、`localhost`、`::1`、`0.0.0.0`、`::`）：在同事件循环内创建并 `await hub.start()`，记录 `owns_hub = True`。
       - 若目标为 `port=0`（供自动化测试动态分配空闲端口）：直接跳过探测启动内嵌 `ACPHub(host=bind_host, port=0, db_path=db_path)`，启动后读取 `hub.bound_port` 回填给 `HumanConsole.uri` 与 `ACPClient.uri`，杜绝端口竞态。
       - 若并发启动时绑定发生 `OSError`（`EADDRINUSE`）：立即重做一次 `probe_hub`；若已变为 `ALIVE_HUB` 则回退为附着模式，否则抛出明确错误。
       - 若 `auto_hub=False` 或目标为远端主机 IP/域名：绝不尝试本地绑定，直接输出清晰错误并退出。
     - **`PORT_OCCUPIED_NON_ACP`**：TCP 已连通但 WebSocket 握手失败、超时或未返回 `pong` ➔ **立即报错退出**（提示端口被非 ACP 服务占用），防止误附着或静默挂起。
2. **TUI 期间完整日志隔离 (`_silence_embedded_hub_logging`)**：
   - 当 `owns_hub == True` 运行于 TUI 模式时，使用上下文管理器隔离 `acp.hub`、`websockets`、`websockets.server`、`websockets.protocol` Logger：
     - 保存原 `handlers`、`level`、`propagate`；
     - 设为 `propagate = False`（阻止冒泡到 root logger），替换为一个有界内存环形缓冲 Handler（如 `collections.deque(maxlen=200)`，**彻底阻止 Python `lastResort` 将 `WARNING`/`ERROR` 打印到 `stderr` 撕裂 TUI**）；
     - 退出 TUI 时完整还原原 Logger 配置。
     - 可选支持 `/logs` 控制台指令按需查看内存中的最近内嵌 Hub 日志（排查连接问题时非常实用）。
3. **快速优雅退出与 `ACPHub.stop()` 改进 (`acp/hub.py`)**：
   - 启动顺序：
     1. 探测或拉起内嵌 `ACPHub`（`owns_hub` 已确定）；
     2. 建立 `ACPClient` 连接并注册（`role="console"`），挂载全部事件处理器；
     3. 通过 `get_roster(on_roster=self._apply_roster_snapshot)` **按 wire-order 静默同步初始花名册**（保留 PR #16 乱序保护，不打印大表格）；
     4. 进入 `_raw_tty_mode()`，初始化 `DECSTBM` 滚动区、绘制顶部固定通栏与底部 3 行输入 Dock，并在主滚动区输出紧凑 Banner（标注 `Hub: Embedded` 或 `Hub: Attached`）。
   - 退出顺序（`/quit`、`/exit`、`:q`、`Ctrl+C` 或空行 `Ctrl+D`）：
     1. 停止接收输入，还原 `DECSTBM` 滚动区（`\x1b[r`）、复位 SGR（`\x1b[0m`）、将光标移至底部安全位置换行；
     2. `await self.client.close()` 关闭 Console 客户端连接；
     3. 若 `owns_hub == True`：执行 `await hub.stop()`。
   - **`ACPHub.stop()` 防卡死加固**：
     - 现有 `acp/hub.py` 中 `self.server.close(); await self.server.wait_closed()` 在有外部 Agent（如 `acp launch codex`）仍保持长连接时可能阻塞等待至 `close_timeout`（最长 10s）。
     - 改造 `ACPHub.stop()`：先调用 `self.server.close()` 停止接收新连接，再并发关闭 `list(self.all_sockets)`（短超时如 `1.0s`），然后 `await asyncio.wait_for(self.server.wait_closed(), timeout=1.5)`，最后执行 `self.storage.clear_roster()`（注：`Storage` 为每次操作按需建立 SQLite 短连接，无需 `storage.close()`）。确保存在外部活跃连接时终端退出仍能在 1~2 秒内完成。
   - **客户端模式断连感知**：
     - 主循环每次 tick 检查 `self.client.connected`；若外部 Hub 意外断开（`owns_hub == False` 且 `not self.client.connected`），顶部固定通栏左侧自动切换为 `⚠ Hub disconnected`，并在主消息区提示一次连接已断开，避免用户对死会话盲打。

---

## 3. 终端界面布局与渲染引擎 (`acp/console.py`)

### 3.1 顶部固定通栏 + `DECSTBM` 滚动区三段式架构

采用 **顶部固定 2 行通栏** + **中间 `DECSTBM` 独立滚动消息区** + **底部固定 3 行输入 Dock**，彻底杜绝残影与宽度挤压，消息卡片享受 100% 终端全宽：

```text
Row 1  │ ⚡ ACP :8765 (host)               Online (3): ● @claudecode IDLE · ● @codex BUSY · ◌ @agy START │
Row 2  │─────────────────────────────────────────────────────────────────────────────────────────────────│
       ┌──────────────────────────────────── DECSTBM 滚动区 (Row 3 .. H-3) ──────────────────────────────┐
Row 3  │  ╭─ ACP Chatroom · Embedded Hub ws://127.0.0.1:8765 · /help for manual ─╮                       │
       │                                                                                                 │
       │   ▐▛█▜▌   @claudecode · ⚡ EXPLICIT ➔ @codex                                          12:04:10  │
       │  ▝▜███▛▘  请检查 storage.py 的游标推进逻辑并补充相应的边界单元测试。                            │
       │   ▘▘ ▝▝                                                                                         │
       │                                                                                                 │
       │   ╭───╮   @codex · 📢 #general                                                        12:04:28  │
       │   │◈ ◈│   已完成初步排查，正在修复空主题读取分支。                                              │
       │   ╰▀─▀╯                                                                                         │
       │                                                                                                 │
       │   ╭▄█▄╮   @user · ⚡ EXPLICIT ➔ @codex                                                12:05:02  │
       │   │▀ ▀│   收到，完成后请在这里同步结果。                                                        │
       │   ╰───╯                                                                                         │
       └─────────────────────────────────────────────────────────────────────────────────────────────────┘
Row H-2│ ─ Draft Input (Enter send · Esc clear · /help manual) ───────────────────────────────────────── │
Row H-1│ @user::> /send @codex 修复完成后通知 @antigravity_                                              │
Row H  │ Commands: /send | /post | /broadcast | /read | /roster | /remove | /prune | /help | /quit       │
```

### 3.2 顶部固定通栏设计 (`render_header_lines`)

固定占据终端最顶部 **2 行**（`HEADER_LINES = 2`），高度恒定不变，避免 `DECSTBM` 顶边界抖动：
- **Row 1（左：Hub 状态；右：聊天室在线情况）**：
  - 左侧内容：
    - 内嵌模式：`⚡ ACP :{port} (host)`
    - 附着模式：`🔗 ACP :{port} (client)`
    - 断连状态：`⚠ ACP :{port} (disconnected)`
  - 右侧内容（**右对齐挂在右上角**）：
    - 统计规则：**不含当前控制台自身 `self.handle`**（与现有 `format_online_agents_bar()` 语义保持一致，避免空房间也显示 `1`），排除 `offline` 状态，按句柄名排序展示。
    - 状态指示点统一使用**单列宽着色符号**（`cell_len == 1`，避免 Windows Terminal / conhost 下 2 格 Emoji 导致宽度偏移与右端溢出）：
      - `idle` ➔ 绿色 `●` + `IDLE`
      - `busy` ➔ 黄色 `●` + `BUSY`
      - `starting` ➔ 青色 `◌` + `START`
      - `blocked` ➔ 红色 `●` + `BLK`
    - 展示格式：`Online ({N}): ● @claude IDLE · ● @codex BUSY`；当 `N == 0` 时显示 `Online (0): ○ waiting for agents`。
    - 溢出裁剪：在剩余可用列宽 `right_budget = max_cells - left_cells - 2` 内依次拼接成员；若全部放不下，自动截断并在末尾追加 `· +{K}`（如 `Online (5): ● @claude IDLE · ● @codex BUSY · +3`）。若终端极窄（如 `< 50` 列），优先保留 `Online ({N})` 与尽可能多的活跃成员，严格保证整行 `cell_len <= cols - 1`（绝不写满第 `cols` 列触发终端自动换行）。
- **Row 2（分割线）**：全宽细线 `"─" * (cols - 1)`，弱化色（`dim`）。
- **向后兼容方法**：保留 `HumanConsole.format_online_agents_bar(max_cells=None)` 公共方法（兼容 [tests/test_console_integration.py](file:///e:/Eproject/chatroom/tests/test_console_integration.py#L93) 等已有调用方），其数据源与顶部通栏共享同一份受 `_ui_lock` 保护的 `self.roster_state`。

### 3.3 底部固定 3 行输入 Dock (`render_dock_lines`)

原底部 4 行中的 `Online Agents` 行已迁移至顶部通栏右上角，底部 Dock 精简为 **3 行**（`DOCK_LINES = 3`，固定位于 `Row H-2 .. Row H`，输入光标位于 `cursor_row = 1`）：
1. **Row `H-2`（分隔栏）**：`─ Draft Input (Enter send · Esc clear · /help manual) ───...`（保留 `"Draft Input"` 文本以兼容现有集成测试）。
2. **Row `H-1`（草稿输入行）**：提示符统一采用 **`{self.handle}::> `**（如 `@user::> `），沿用 `_window_around_cursor` 实现左右水平滚动与宽字符 CJK/Emoji 裁剪，确保 `cell_len(line) <= cols - 1` 且 `cursor_col <= cols - 1`。保留 PR #16 的 Windows `utf-16-le` 增量解码以完整支持 Emoji 输入。
3. **Row `H`（快捷指令栏）**：按终端宽度自适应显示完整版或紧凑版命令列表，包含 PR #17 的 `/remove` 和 `/prune`：
   - 宽屏：`Commands: /send @agent <msg> | /post [#topic] <msg> | /broadcast <msg> | /read | /roster | /remove | /prune | /help | /quit`
   - 中屏：`Cmds: /send | /post | /broadcast | /read | /roster | /remove | /prune | /help | /quit`
   - 窄屏：按 `cols - 1` 安全裁剪。

### 3.4 `DECSTBM` 渲染协议、零刷屏与窗口缩放处理

所有终端写入操作均受 `self._ui_lock`（`threading.RLock`）保护：

1. **TUI 初始化与退出（仅在 `_raw_tty_mode()` 返回 `True` 且 `rows >= 8` 时启用）**：
   - 初始化：清屏归位 `\x1b[2J\x1b[H` ➔ 设置滚动区 `\x1b[3;{rows - 3}r` ➔ 记录 `_viewport = (cols, rows)`、`_feed_row = 3`、`_feed_at_bottom = False` ➔ 绘制顶部通栏（Rows 1~2）➔ 绘制底部 Dock（Rows `H-2..H`）➔ 将光标移至输入行 `(rows - 1, cursor_col + 1)`。
   - 退出清理（`finally`）：无论正常 `/quit` 还是异常/信号中断，**必须输出** `\x1b[r\x1b[0m\x1b[{rows};1H\n`，恢复终端全屏滚动区，绝不遗留锁死状态。
   - **极矮终端降级（`rows < 8`）**：不启用 `DECSTBM`，退化为相对位置 3 行底部 Dock 模式；非 TTY 环境（`dock_active=False`）不输出任何 ANSI 控制序列（无 `\x1b[...r`、`\x1b7`、`\x1b8`、`\x1b[J`）。
2. **统一输出通道 `_emit(fn)`**：
   - **所有**向主滚动区输出的内容——包括带头像消息、紧凑 Banner、`/help`、`/roster` 表格、`/read` 历史、`/remove`、`/prune`、错误提示以及非聊天指令回显——**全部统一走 `_emit()`**（修复 `handle_key` 原先绕过 `_emit()` 的问题）。
   - 在 `DECSTBM` 激活时，`_emit(fn)` 执行步骤：
     1. 检测终端尺寸是否变化；若变化先执行 `_handle_resize()`；
     2. 使用临时内存 `Console`（`width = cols`, `color_system` 继承自 `_out_console`）将 `fn()` 渲染为带 ANSI 样式的行列表 `lines`；
     3. 若 `_feed_at_bottom` 为 `True`，将光标移至 `\x1b[{bottom};1H`，逐行输出 `\n{line}\x1b[0m`（利用 `DECSTBM` 仅滚动 `3..bottom`，顶部通栏与底部 Dock 纹丝不动）；
     4. 若 `_feed_at_bottom` 为 `False`，将光标移至 `\x1b[{_feed_row};1H`，逐行输出并在跨过 `bottom` 时自动切入滚动状态，更新 `_feed_row` / `_feed_at_bottom`；
     5. 重绘顶部通栏（如状态有变更）并将光标归位到输入行 `\x1b[{rows - 1};{cursor_col + 1}H`。
3. **`on_presence_change` 静默原地刷新（零刷屏）**：
   - 更新 `self.roster_state`（收到 `offline` 时移除该句柄，不区分大小写；其他状态更新为小写值）。
   - **绝不向主消息区打印任何 `• Presence: ... is now ...` 文本**（无论是 TTY 还是非 TTY 模式均不再刷屏）。
   - 若 `_dock_active` 为真，仅发送单次原子序列刷新 Row 1：
     `\x1b7\x1b[1;1H\x1b[2K{styled_header_row_1}\x1b[0m\x1b8`
     输入光标全程不闪烁、草稿零中断。
   - `/remove` 和 `/prune` 修改 `self.roster_state` 后同样立即刷新顶部通栏 Row 1。
4. **窗口缩放 (`_handle_resize`)**：
   - 各平台在主循环的 0.3 秒 tick、消息输出及按键重绘前，通过 Rich Console 读取当前终端尺寸。
   - 尺寸变化时重新设置 `\x1b[3;{new_bottom}r`（或降级）、清绘顶部通栏与底部 3 行 Dock、钳制 `_feed_row = min(_feed_row, new_bottom)` 并归位输入光标。
5. **人类自身发言的头像展示与去重策略**：
   - 在 `handle_key` 按回车提交时：
     - 若为聊天指令（`/send`、`/post`、`/broadcast`），**不打印**原始灰色 `@user::> /send ...` 回显行，保持对话流干净；
     - 其他控制命令（如 `/roster`、`/read`、`/help`、`/remove`、`/prune`）仍可精简回显或直接展示执行结果（统一经由 `_emit()`）。
   - 渲染去重规则（严格对齐 [acp/hub.py](file:///e:/Eproject/chatroom/acp/hub.py#L296-L339) 的路由事实）：
     - `/send @target <msg>`：Hub 转发显式消息时排除了发送者自身 socket（`ws != websocket`），因此 Console 在 `client.send_message(...)` 返回 `status == "ok"` 后，**本地调用头像卡片渲染 `@user` 的显式消息**（当 `delivered is False` 时附加 `[dim](offline · queued)[/dim]` 提示）。
     - `/broadcast <msg>`（普通广播，不含 `--urgent`）：Hub 排除了发送者自身，因此 Console 在返回 `status == "ok"` 后，**本地渲染 `@user` 的广播头像卡片**。
     - `/post [#topic] <msg>` 与 `/broadcast --urgent <msg>`：Hub 会将事件广播回发送者自己，Console 仅在 `on_silent_message` / `on_broadcast` 事件中渲染一次，**绝不重复渲染**。

---

## 4. 成员像素/方块头像系统 (`acp/avatars.py`)

### 4.1 `AvatarSpec` 数据结构与预设形象库

新增模块 `acp/avatars.py`，定义不可变数据类 `AvatarSpec`：

```python
@dataclass(frozen=True)
class AvatarSpec:
    key: str                         # 形象类别标识："claude", "codex", "agy", "dsh", "user", "custom"
    lines: tuple[str, str, str]      # 固定 3 行，每行 rich.cells.cell_len 严格等于 7
    color: str                       # Rich 主题主色
    badge: str                       # 角色简短标签，如 "CLAUDE", "CODEX", "AGY", "DSH", "HUMAN"
```

内置 5 类核心角色形象（全部使用 `cell_len == 1` 的 Unicode 半块/制表字符，每行宽度严格为 **7 cells**）：

| 成员类别 | 匹配正则规则（归一化 `norm = handle.strip().lstrip("@").lower()`） | 3 行像素小人 (7×3) | 主色调 (`color`) |
| :--- | :--- | :--- | :--- |
| **Claude Code** | `^(claudecode\|claude)(?:$\|[\d_-])` | ` ▐▛█▜▌ `<br>`▝▜███▛▘`<br>` ▘▘ ▝▝ ` | `#E07A5F` (珊瑚橙) |
| **Codex / GPT** | `^(codex\|gpt)(?:$\|[\d_-])` | ` ╭───╮ `<br>` │◈ ◈│ `<br>` ╰▀─▀╯ ` | `#4ADE80` (翠绿) |
| **Antigravity** | `^(antigravity\|agy)(?:$\|[\d_-])` | ` ▄▀█▀▄ `<br>` ▐▛▄▜▌ `<br>`  ▀ ▀  ` | `#A78BFA` (星云紫) |
| **DeepSeek** | `^(deepseek\|dsh)(?:$\|[\d_-])` | ` ▄███▄ `<br>`▐█● ●█▌`<br>` ▀▄▄▄▀ ` | `#38BDF8` (深海蓝) |
| **Human / User** | `^(user\|human\|console\|observer)(?:$\|[\d_-])` | ` ╭▄█▄╮ `<br>` │▀ ▀│ `<br>` ╰───╯ ` | `#FBBF24` (琥珀金) |
| **Custom / 其他** | 其余任意 `@handle` | 由 `hashlib.blake2b(norm.encode())` 确定性映射到 4 套 7×3 像素模板 | 确定性 6 色调色板 |

边界规则：
- 严格要求前缀后紧跟字符串结尾、数字、`_` 或 `-`（例如 `@codex1`、`@claude-rev` 命中家族头像；`@ag` 不会误匹配 `@agy`，`@alice` 走确定性自定义头像）。
- 同家族多实例（如 `@codex1`、`@codex2`）保持一致的家族像素脸型以便一眼识别模型阵营，同时在消息 Header 中清晰展示完整句柄 `@codex1` / `@codex2`。

### 4.2 基于显示列宽的并排排版算法 (`render_message_with_avatar`)

**硬性约束**：**严禁使用 Python 标准库 `textwrap.wrap`**（其按 `len()` 字符数换行，会导致中文字段超宽 2 倍）。必须在 `acp/avatars.py` 中实现基于 `rich.cells.cell_len` 的单元格折行器 `wrap_to_cells(text: str, max_cells: int) -> list[str]`：
- 按 `\n` 拆分自然段（保留空行）；
- 每段按终端显示宽度逐字/按词切分，确保中英文混排、Emoji、长 URL 均满足每行 `cell_len <= max_cells`；
- 所有外部文本（`sender`、`target`、`topic`、`content`）写入 Rich Markup 前必须经过 `rich.markup.escape(...)` 转义，防止 `[main]` 等方括号导致 `MarkupError`。

排版结构（左侧固定 7 列头像 + 2 列间隔，共 9 列左栏；右侧 `body_width = max(20, max_width - 10)`）：

```text
第 1 行:  {avatar[0]}  {sender} · {mode_badge} {target/topic}  {timestamp}
第 2 行:  {avatar[1]}  {body_line_1}
第 3 行:  {avatar[2]}  {body_line_2}        （若正文仅 1 行则右侧留空，保证 3 行头像完整显示）
第 4+行:  {7空格缩进}  {body_line_3...}     （后续折行与正文首行严格左对齐）
```

覆盖全部消息入口：
- `HumanConsole.on_explicit_message` & 人类本地 `/send` 卡片（`⚡ EXPLICIT ➔ @target`）
- `HumanConsole.on_silent_message`（`📢 #topic`）
- `HumanConsole.on_broadcast` & 人类本地 `/broadcast` 卡片（`📣 BROADCAST` / `🚨 URGENT`）
- `HumanConsole._do_read`（`/read [topic]` 历史消息同样带头像渲染，消息间空 1 行）。

---

## 5. 模块改动清单与向后兼容性

### 5.1 改动文件清单

| 文件 | 变更类型 | 核心职责 |
| :--- | :--- | :--- |
| `acp/avatars.py` | **新增** | `AvatarSpec`、5 类内置像素头像与确定性哈希自定义头像、正则匹配、`wrap_to_cells()` 宽字符折行器、`render_message_with_avatar()` |
| `acp/console.py` | **重构增强** | 顶部固定 2 行通栏（右上角在线状态）+ `DECSTBM` 滚动区 + 底部 3 行 Dock（提示符 `@user::> `）；`on_presence_change` 零刷屏更新；统一 `_emit()`；窗口 Resize 与断连检测；人类自发消息头像渲染去重；`sync_roster(display_table=False)` 保留 PR #16 wire-order；保留 `/remove`、`/prune` 与 `format_online_agents_bar()` 兼容；新增 `probe_hub()`、`UnifiedConsoleRunner`、`start_unified_console()` |
| `acp/cli.py` | **修改** | 根命令组 `invoke_without_command=True` 与 `ctx.obj` 参数透传；`acp hub` 增加 `--headless`/`--as` 并免疫 `ACP_HUB_URI` 劫持；`acp console` 增加 `--db`/`--no-auto-hub` 接入 `start_unified_console()` |
| `acp/hub.py` | **增强** | 新增 `bound_port` 属性（支持 `port=0`）；加固 `ACPHub.stop()` 主动并发关闭活跃 socket 防止退出卡顿 |
| `tests/test_avatars.py` | **新增** | 头像匹配边界、7-cell 不变量、CJK/方括号 `wrap_to_cells` 与多行排版单测 |
| `tests/test_console.py` | **更新扩展** | 顶部固定通栏、零 `Presence:` 刷屏、`DECSTBM` 退出复位、Resize/窄屏降级、`@user::> ` 3 行 Dock、人类发言头像去重、`/remove` & `/prune` 联动 |
| `tests/test_console_integration.py` | **更新扩展** | 验证现有 Windows Emoji 与 wire-order 测试兼容，新增真实 PTY 统一启动（自动拉起内嵌 Hub ➔ Agent 通信 ➔ `/quit` 释放端口）DT 测试 |
| `tests/test_unified_cli.py` | **新增** | 三态端口探测、CLI 路由单测（`not dt`）+ 同进程内嵌 Hub 生命周期、外部活跃连接秒退、日志静默隔离（`dt`） |

### 5.2 向后兼容性承诺

1. **Headless & CI 兼容**：`acp hub --headless` 与所有非 TTY 下的 `acp hub` 行为与之前完全一致；非 TTY 下裸运行 `acp` 打印帮助安全退出。
2. **协议与外部命令 100% 兼容**：`acp/protocol.py`、`acp launch`、`acp resume`、`acp-chat send/read/roster/broadcast/task/model/remove/kick/leave/prune` 无需任何协议破坏即可接入。
3. **现有集成测试兼容**：保留 `format_online_agents_bar()` 方法、底部 `"Draft Input"` 标识、PR #16 的 Windows UTF-16-LE Emoji 输入与 `get_roster(on_roster=...)` wire-order 机制，以及 PR #17 的 `/remove`、`/kick`、`/prune` 完整能力。
