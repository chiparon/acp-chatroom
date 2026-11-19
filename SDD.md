# 软件设计文档 (SDD): 统一 Hub + Console 类 Claude Code 交互终端

> **分支**: `feat/unified-console-tui`（基于 `main`）  
> **状态**: 待审阅（Draft for Review）  
> **关联文档**: [TDD.md](./TDD.md) · [REQUIREMENTS.md](./REQUIREMENTS.md)

---

## 1. 概述与设计目标 (Overview & Goals)

当前 ACP Chatroom 需要用户先在终端 A 运行 `acp hub`，再到终端 B 运行 `acp console`，使用链路较割裂；同时存在以下交互痛点：
1. **状态变更刷屏**：每次 Agent 上线、空闲、忙碌、离线都会在主消息流中打印 `• Presence: @xx is now IDLE/BUSY`，导致真实对话内容被状态日志冲刷。
2. **成员辨识度低**：成员发言均为纯文本单色/浅色前缀，多 Agent 协同对话时缺乏如 Claude Code 般的视觉人格与对话层次感。
3. **启动不够开箱即用**：直接运行 `acp` 仅输出帮助文本，无法像 `claude` / `codex` CLI 一样直接进入可交互终端。

本设计旨在将 **`acp hub` 与 `acp console` 合并为单进程一体化的类 Claude Code CLI 终端**，核心目标如下：
- **一键直达主界面**：运行 `acp`、`acp hub`（交互式终端）或 `acp console` 时，均直接启动 Console TUI；若本地端口尚无 Hub 实例，自动同进程拉起内嵌 `ACPHub`；若端口已有 Hub 运行，则无缝退化为客户端模式连入。保留 `acp hub --headless` 及非 TTY 自动降级，保障后台/CI 场景兼容。
- **右上角固定在线花名册悬浮面板**：通过 ANSI 屏幕坐标与保存/恢复光标机制，将聊天室在线成员及状态灯（`🟢 IDLE`、`🟡 BUSY`、`🔵 STARTING`、`🔴 BLOCKED`）固定悬挂在终端右上角，彻底取消主消息流中的 `Presence: @xx is now XX` 文本刷屏。
- **成员像素/方块小人头像系统**：为每位发言成员（Claude、Codex、Antigravity、DeepSeek、Human 及自定义句柄）渲染 3 行高彩色 Unicode 半块像素小人头像与并排对齐的消息卡片，覆盖实时消息与 `/read` 历史阅读。

---

## 2. 整体架构与进程生命周期 (Unified Hub + Console Architecture)

### 2.1 运行模式与拓扑关系

合并后的 CLI 支持在**单进程同事件循环**内协同运行 `ACPHub` 与 `HumanConsole`，同时保留独立的 Headless Hub 与跨终端接入能力：

```text
                       +-----------------------------------+
                       |  acp / acp hub / acp console      |
                       +-----------------+-----------------+
                                         |
                        探测目标 URI 端口是否已被 Hub 监听
                         /                               \
            [未占用 / 本地 host 模式]                [已有 Hub 在线 / client 模式]
                       /                                   \
  +-----------------------------------+       +-----------------------------------+
  | 单进程 Unified Console (owns_hub)  |       | Console 客户端模式 (client only)   |
  |  ├─ asyncio Task: 内嵌 ACPHub     |       |  └─ ACPClient ──► 外部已有 Hub    |
  |  └─ 协程: HumanConsole (本地 WS)  |       +-----------------------------------+
  +-----------------------------------+
            ^                 ^
            | (WebSocket)     | (WebSocket)
  +-----------------+ +-----------------+
  | acp launch agy  | | acp launch codex|
  +-----------------+ +-----------------+
```

### 2.2 CLI 路由设计 (`acp/cli.py`)

1. **根命令组 (`@click.group(invoke_without_command=True)`)**：
   - 增加 `@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765")` 与 `@click.option("--as", "--handle", "-a", "handle", default="@user")`（或配合子命令 `ctx.invoked_subcommand is None` 判定）。
   - 当用户直接执行 `acp`（未传任何子命令）时，若处于交互式 TTY，调用 `start_unified_console(uri=uri, handle=handle)` 直接启动 Console 页面；非 TTY 环境或带 `--help` 时按常规输出帮助信息（或遵循 `start_unified_console` 的非 TTY 行为）。
2. **`acp hub` 命令**：
   - 保留 `--host`（默认 `127.0.0.1`）、`--port`（默认 `8765`）、`--db`（默认 `acp_chatroom.db`）。
   - 新增 `--headless`（`is_flag=True`）和 `--as` / `--handle` / `-a`（默认 `@user`）。
   - 分支策略：
     - 当 `--headless` 为真，或当前标准输入/输出非交互式 TTY（如 CI 流水线、后台守护进程重定向）时，调用 `run_hub(host=host, port=port, db_path=db)` 运行纯后台服务端。
     - 当处于交互式 TTY 且未指定 `--headless` 时，构造 `ws://{connect_host}:{port}`（若 `host` 为 `0.0.0.0` 或 `::`，客户端连接地址映射为 `127.0.0.1`），调用 `start_unified_console(uri=..., handle=handle, host=host, port=port, db_path=db, require_hub_ownership=True/auto)` 直接进入包含内嵌 Hub 的 Console TUI。
3. **`acp console` 命令**：
   - 保留 `--uri`（默认 `ws://127.0.0.1:8765`）、`--as`/`--handle`（默认 `@user`）。
   - 新增可选参数 `--db`（默认 `acp_chatroom.db`）和 `--no-auto-hub`（可选显式关闭自动拉起，默认允许自动探测并拉起本地 Hub）。
   - 调用 `start_unified_console(uri=uri, handle=handle, db_path=db)`。

### 2.3 自动探测、启动与协同关闭生命周期

在 `acp/console.py`（及 `acp/hub.py` 辅助接口）中实现 `UnifiedConsoleRunner` / `start_unified_console(...)`：

1. **URI 解析与端口探测**：
   - 解析 `uri`（如 `ws://127.0.0.1:8765`）提取 `scheme`、`host`、`port`。
   - 先尝试轻量级探测目标地址是否已有服务响应（例如尝试 TCP 连接或发起一次短超时的 `ACPClient.request({"action": "ping"})`）：
     - **已有 Hub 在线**：记录 `owns_hub = False`，直接以客户端模式连接 `HumanConsole`，跳过本地启动 Hub。
     - **目标未监听且为本地回环/可绑定地址**（如 `127.0.0.1`、`localhost`、`0.0.0.0`、`::1`）：创建内嵌 `ACPHub(host=bind_host, port=port, db_path=db_path)`，在同一 `asyncio` 事件循环中先 `await hub.start()`，记录 `owns_hub = True`。若绑定端口恰好出现竞态占用（`OSError`），回退重试客户端探测连接。
     - **目标为远端主机且不可达**：不尝试本地绑定远端 IP，直接进入客户端连接并给出明确错误提示。
2. **日志静默保护 (TUI Log Isolation)**：
   - 当 `owns_hub == True` 运行于 TUI 模式时，`acp.hub`、`websockets`、`websockets.server` 的 Logger 若继续向 `stdout/stderr` 打印 `INFO` 日志，会直接撕裂终端的滚动区与悬浮窗。
   - 因此内嵌启动 Hub 时：
     - 不调用向控制台输出文本的 `logging.basicConfig(...)`（不同于 `run_hub()` 的 headless 模式）。
     - 显式将 `logging.getLogger("acp.hub")` 与 `logging.getLogger("websockets")` 在交互式 TUI 运行期间的传播或控制台 Handler 设为静默（或重定向至可选日志文件 / `NullHandler`），退出 TUI 时恢复原日志级别。
3. **统一启动顺序与优雅退出 (`try ... finally`)**：
   - 启动顺序：`await hub.start()`（若 `owns_hub`） ➔ `await console.run()`（内部连接 `ACPClient` 并立即拉取一次 `get_roster()` 初始化右上角花名册）。
   - 退出触发：用户在输入框输入 `/quit`、`/exit`、`:q` 或按 `Ctrl+C` / 空行 `Ctrl+D`。
   - 清理顺序：
     1. 停止接收键盘输入，清理底部 Dock 与右上角悬浮卡片。
     2. `await console.client.close()` 断开控制台 WebSocket 连接。
     3. 若 `owns_hub == True`：执行 `await hub.stop()`，优雅关闭 `websockets` Server、清理花名册在线状态并关闭 SQLite 连接；若 `owns_hub == False`：仅退出本控制台，**绝对不关闭外部共享 Hub**。
   - **Banner 状态标注**：启动 Banner 展示当前 Hub 运行模式（例如 `Hub: Embedded (ws://127.0.0.1:8765)` 或 `Hub: Attached (ws://127.0.0.1:8765)`），让操作者清晰知晓关闭当前终端是否会终止房间服务。

---

## 3. 终端界面布局与渲染引擎 (`acp/console.py`)

### 3.1 三区布局架构

整体终端界面模拟 Claude Code 风格，分为 **主消息滚动区**、**右上角固定在线花名册悬浮卡片** 与 **底部 3 行输入 Dock**：

```text
┌────────────────────────────────────────────────────────┬───────────────────────────┐
│  [主消息滚动区：自适应安全列宽]                        │ ╭─ Room Roster (3) ──────╮│
│                                                        │ │ 🟢 @claudecode   IDLE  ││
│   ▐▛█▜▌   @claudecode · ⚡ EXPLICIT ➔ @codex  12:04:10 │ │ 🟡 @codex        BUSY  ││
│  ▝▜███▛▘  请检查 storage.py 的游标推进逻辑并补充       │ │ 🔵 @antigravity  START ││
│   ▘▘ ▝▝   相应的边界单元测试。                         │ ╰────────────────────────╯│
│                                                        └───────────────────────────┘
│   ╭───╮   @codex · 📢 #general                12:04:28                              
│   │◈ ◈│   已完成初步排查，正在修复空主题读取分支。                                  
│   ╰▀─▀╯                                                                             
│                                                                                     
│   ╭▄█▄╮   @user · 📢 #general                 12:05:02                              
│   │▀ ▀│   收到，完成后请在这里同步结果。                                            
│   ╰───╯                                                                             
│                                                                                     
├─────────────────────────────────────────────────────────────────────────────────────┤
│ ─ Draft Input (Enter send · Esc clear · /help manual) ───────────────────────────── │
│ @user ❯ /send @codex 修复完成后通知 @antigravity_                                   │
│ Commands: /send @agent <msg> | /post [#topic] <msg> | /broadcast | /read | /help    │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.2 底部精简 3 行输入 Dock

原有 `render_dock_lines` 返回 4 行（包含 `Online Agents (N): ...` 行）。因为在线情况已正式移至右上角固定面板，底部 Dock 精简为 3 行（`cursor_row = 1`）：
1. **第 0 行（分隔标题栏）**：`─ Draft Input (Enter send · Esc clear · /help manual) ────...`
2. **第 1 行（草稿输入行）**：`{self.handle} ❯ {visible_draft}`（或保留紧凑前缀并支持水平窗口滚动与宽字符 CJK 裁剪，确保所有行宽度严格 `<= width - 1`）。
3. **第 2 行（快捷命令提示栏）**：根据终端宽度自适应展示完整版或紧凑版命令提示。

### 3.3 右上角悬浮卡片设计与尺寸预算 (`render_roster_overlay`)

1. **卡片内容**：
   - 标题栏：`╭─ Room Online (N) ──...╮`（`N` 为当前除自身外或全房间活跃非离线成员数，具体见规则：展示除自身 `@user` 外的所有在线 Agent；若有其他 console 也可展示或统一按 `roster_state` 中的在线条目）。
   - 成员行：每个在线成员占 1 行：`│ {dot} {handle:<W} {STATE} │`
     - `idle` ➔ `🟢` + 绿色 `IDLE`
     - `busy` ➔ `🟡` + 黄色 `BUSY`
     - `starting` ➔ `🔵` + 青色 `STARTING`
     - `blocked` ➔ `🔴` + 红色 `BLOCKED`
   - 空状态：无其他在线成员时显示 `│ (no agents online) │`。
   - 高度限制：当在线成员过多时，最多显示 `max_rows` 行（例如 `min(len(online), max(3, terminal_height // 3))`），超出部分末行显示 `│ ... +K more │`，避免遮挡底部输入框。
   - 宽度预算：`overlay_width` 动态适应内容与终端宽度，典型为 `26 ~ 34` 列（不超过终端总宽度的 `40%`），当终端过窄（如 `width < 56`）时自动切换为精简模式或暂停右上角悬浮以避免挤压主内容。
2. **主消息区安全右侧留白 (Safe Right Margin)**：
   - 为避免较长的消息文本直接横穿打印到右上角卡片下方造成文字覆盖，消息文本折行宽度 `content_width` 在终端顶部可视安全区内考虑右上角面板占位（或统一将消息渲染宽度设为 `max(24, term_width - overlay_width - 2)`，当终端足够宽时既美观阅读又完全不与右上角卡片重叠）。

### 3.4 零残影擦除与重绘机制 (Zero-Artifact Scrollback Protocol)

在滚动终端中绘制右上角固定坐标面板时，如果不先擦除旧面板直接滚屏，旧边框就会留在滚动历史中变成“残影”。本设计通过以下严格协议彻底杜绝残影，且全过程受 `self._ui_lock` (`threading.RLock`) 保护：

1. **ANSI 定位原语**：
   - 使用 DEC 光标保存/恢复序列 `\x1b7` 与 `\x1b8`（Windows ConPTY/Windows Terminal 与 POSIX 终端均广泛支持）。
   - 光标定位序列：`\x1b[{row};{col}H`（1-based 行列坐标）。
   - 记录当前屏幕已绘制的悬浮框矩形区域：`_overlay_rect = (start_row, start_col, height, width)`。
2. **擦除旧悬浮框 (`_clear_roster_overlay`)**：
   - 若 `_overlay_rect` 存在，输出：
     `\x1b7` + 对 `r` in `range(start_row, start_row + height)` 输出 `\x1b[{r};{start_col}H` + `" " * width` + `\x1b8`。
   - 重置 `_overlay_rect = None`。
3. **绘制新悬浮框 (`_draw_roster_overlay`)**：
   - 计算当前终端尺寸 `(cols, rows)` 及卡片文本行 `overlay_lines`。
   - 起始列 `start_col = max(1, cols - box_width + 1)`，起始行 `start_row = 1`。
   - 输出：`\x1b7` + 逐行 `\x1b[{start_row + i};{start_col}H{styled_line}` + `\x1b8`。
   - 保存 `_overlay_rect = (start_row, start_col, len(overlay_lines), box_width)`。
4. **核心 `_emit(fn)` 时序**：
   当主区域有任何内容输出（新消息、`/roster`、`/read`、`/help`、用户输入回显）时：
   ```text
   1. _clear_dock()             # 擦除底部 3 行输入框
   2. _clear_roster_overlay()   # 擦除当前屏幕右上角旧花名册卡片（防止随滚屏上移留下残影！）
   3. fn()                      # 在主滚动区打印带头像消息或命令结果（此时屏幕可能发生自然滚屏）
   4. _draw_roster_overlay()    # 在滚屏后的新屏幕第 1 行右上角重新钉住花名册卡片
   5. _draw_dock()              # 重绘底部 3 行输入框并还原输入光标到草稿编辑位置
   ```
5. **`on_presence_change` 静默原地刷新**：
   - 收到 `EventType.PRESENCE_CHANGE` 事件时：
     1. 持有 `_ui_lock`，更新 `self.roster_state`（若为 `offline` 则移除或标记移除，其他状态更新为小写状态字符串）。
     2. **绝不调用** `_out_console.print("• Presence: ...")` 向主消息区打印任何行！
     3. 若 `_dock_active` 为真，仅执行 `_clear_roster_overlay()` ➔ `_draw_roster_overlay()` 原地刷新右上角悬浮窗，光标在 `\x1b7` / `\x1b8` 保护下纹丝不动，用户正在敲击的草稿零闪烁、零中断。
   - **非 TTY 环境降级**：当 `self._dock_active` 为 `False`（如测试缓冲或管道重定向）时，不输出任何 `\x1b7`/`\x1b8`/`\x1b[J` 等 ANSI 定位序列，仅更新 `self.roster_state`，且同样不再打印 `Presence:` 刷屏文本（用户仍可随时通过 `/roster` 查看完整花名册表）。

---

## 4. 成员像素/方块头像系统 (`acp/avatars.py`)

### 4.1 `AvatarSpec` 数据结构与预设形象库

新增模块 `acp/avatars.py`，定义不可变数据类 `AvatarSpec`：

```python
@dataclass(frozen=True)
class AvatarSpec:
    key: str                         # 形象类别标识，如 "claude", "codex", "agy", "dsh", "user", "custom"
    lines: tuple[str, str, str]      # 固定 3 行，每行 display cell width 严格为 7
    color: str                       # Rich 主题主色，如 "#E07A5F", "bright_green"
    badge: str                       # 角色简短标签，如 "CLAUDE", "CODEX", "AGY", "DSH", "HUMAN"
```

内置 5 类核心角色形象（全部经过终端等宽校验，每行 `rich.cells.cell_len == 7`）：

| 成员类别 | 匹配句柄规则（不区分大小写） | 3 行像素小人预览 (7 cells × 3 lines) | 主色调 (`color`) |
| :--- | :--- | :--- | :--- |
| **Claude Code** | `@claude*`（含 `@claudecode`） | ` ▐▛█▜▌ `<br>`▝▜███▛▘`<br>` ▘▘ ▝▝ ` | `#E07A5F` (珊瑚橙) |
| **Codex / GPT** | `@codex*`, `@gpt*` | ` ╭───╮ `<br>` │◈ ◈│ `<br>` ╰▀─▀╯ ` | `#4ADE80` (翠绿) |
| **Antigravity** | `@agy*`, `@antigravity*` | ` ▄▀█▀▄ `<br>` ▐▛▄▜▌ `<br>`  ▀ ▀  ` | `#A78BFA` (星云紫) |
| **DeepSeek** | `@dsh*`, `@deepseek*` | ` ▄███▄ `<br>`▐█● ●█▌`<br>` ▀▄▄▄▀ ` | `#38BDF8` (深海蓝) |
| **Human / User** | `@user`, `@human`, `@console`, `@observer` | ` ╭▄█▄╮ `<br>` │▀ ▀│ `<br>` ╰───╯ ` | `#FBBF24` (琥珀金) |
| **Custom / 其他** | 任意其他自定义 `@handle` | 由句柄 SHA-256/BLAKE2 确定性映射到内置自定义模板池 | 确定性调色板 (青/粉/金/蓝绿等) |

匹配优先级与多实例处理：
- 归一化句柄 `norm = handle.strip().lstrip("@").lower()`。
- 去掉尾部数字编号或连字符后缀进行前缀匹配（例如 `@codex1`、`@codex-2`、`@claude_reviewer` 均可命中对应家族头像），确保 `acp launch codex` 自动分配的 `@codex1`、`@codex2` 依然拥有 Codex 家族专属头像，同时保持各角色颜色或头部标记清晰可辨。

### 4.2 多行消息并排排版算法 (`render_message_with_avatar`)

在 `acp/avatars.py` 中提供纯文本与 Rich Renderable 两种渲染辅助，核心结构如下：

```text
第 1 行:  {avatar[0]}  {sender} {mode_badge} {target/topic}  {timestamp}
第 2 行:  {avatar[1]}  {body_line_1}
第 3 行:  {avatar[2]}  {body_line_2}        （若正文不足 2 行则右侧留空，保证 3 行头像完整显示）
第 4+行:  {7空格缩进}  {body_line_3...}     （长文本按可用列宽自动折行，保持左边界对齐）
```

具体渲染规则：
1. **Rich Markup 注入安全**：所有来自网络的 `sender`、`target`、`topic`、`content` 在嵌入 Rich 格式化串前必须经过 `rich.markup.escape(...)` 转义，防止正文中的 `[main]` 等方括号引发 `MarkupError`。
2. **折行对齐**：使用 `textwrap.wrap` 或按终端显示单元格宽度 (`cell_len`) 的换行器，确保中英文混排、Emoji 正文均在 `body_width = max(20, max_line_width - 9)` 内折行（7 列头像 + 2 列间隔），后续所有行与正文首行垂直对齐。
3. **覆盖范围**：
   - `HumanConsole.on_explicit_message`（标注 `⚡ EXPLICIT ➔ @target`）
   - `HumanConsole.on_silent_message`（标注 `📢 #topic`）
   - `HumanConsole.on_broadcast`（标注 `📣 BROADCAST` 或 `🚨 URGENT`）
   - `HumanConsole._do_read`（`/read [topic]` 回溯历史消息同样带头像渲染，保持视觉统一）。
   - 消息之间保留 1 行空行呼吸感，形成类似 Claude Code 对话气泡流的阅读体验。

---

## 5. 模块改动清单与向后兼容性

### 5.1 改动文件清单

| 文件 | 变更类型 | 核心职责 |
| :--- | :--- | :--- |
| `acp/avatars.py` | **新增** | `AvatarSpec` 定义、5 大角色与哈希自定义头像库、`resolve_avatar()`、`render_message_with_avatar()` 并排折行排版引擎 |
| `acp/console.py` | **重构增强** | 精简底部 3 行 Dock；新增右上角固定悬浮花名册绘制/擦除逻辑；`on_presence_change` 零刷屏更新；消息流集成头像渲染；新增 `UnifiedConsoleRunner` / `start_unified_console()` 实现同进程 Hub 探测、拉起与协同关闭 |
| `acp/cli.py` | **修改** | 根命令 `acp` 默认进入统一终端；`acp hub` 增加 `--headless` 与交互模式自适应；`acp console` 默认调用 `start_unified_console()` |
| `acp/hub.py` | **微调** | 支持在嵌入模式下静默日志配置（避免与 `run_hub()` 强制 `basicConfig` 冲突），提供可被控制台安全复用的端口/生命周期辅助 |
| `tests/test_avatars.py` | **新增** | 头像匹配、宽字符单元格宽度不变量、多行消息对齐单元测试 |
| `tests/test_console.py` | **更新扩展** | 更新对零刷屏 `on_presence_change`、右上角悬浮卡片防残影、3 行 Dock 与头像消息流的断言 |
| `tests/test_unified_cli.py` | **新增** | 同进程内嵌 Hub 生命周期、端口已占用复用外部 Hub 降级、CLI 路由（`acp` / `acp hub` / `acp hub --headless` / `acp console`）测试 |

### 5.2 向后兼容性承诺

1. **Headless & CI 兼容**：`acp hub --headless` 以及所有非 TTY 环境下调用 `acp hub` 的行为与改造前完全一致，不影响现有后台部署脚本或自动化测试。
2. **外部客户端协议 100% 兼容**：WebSocket 协议帧格式（`acp/protocol.py`）、`acp launch`、`acp resume`、`acp-chat send`、`acp-chat read`、`acp-chat roster`、`acp-chat broadcast`、`acp task` 均无需修改协议即可无缝接入合并后的统一终端。
3. **非 TTY 控制台兼容**：当标准输入/输出不是 TTY 时，`HumanConsole` 自动关闭 ANSI 定位与 Dock 控制序列，仅按清晰格式输出消息卡片与命令响应。
