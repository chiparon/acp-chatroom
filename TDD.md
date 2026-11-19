# 测试驱动开发方案 (TDD): 统一 Hub + Console 类 Claude Code 交互终端

> **分支**: `feat/unified-console-tui`（基于 `main`）  
> **状态**: 待审阅（Draft for Review）  
> **关联文档**: [SDD.md](./SDD.md) · [REQUIREMENTS.md](./REQUIREMENTS.md)

---

## 1. TDD 开发原则与分阶段实施路径 (Red ➔ Green ➔ Refactor)

本项目严格遵循 **Red ➔ Green ➔ Refactor** 循环，按模块依赖自底向上分 3 个独立迭代推进，每个迭代先编写失败测试（Red），再编写最小完整实现使测试全绿（Green），最后进行结构提取与防重复重构（Refactor）。

### Iteration 1: `acp/avatars.py` 像素头像映射与消息卡片排版
1. **Red**：创建 `tests/test_avatars.py`，编写 `TC-AVATAR-01 ~ 05`，验证 `resolve_avatar`、`AvatarSpec` 不变量及 `render_message_with_avatar` 的并排折行输出（此时因模块不存在而失败）。
2. **Green**：实现 `acp/avatars.py`，包含 5 类内置角色半块像素形象、哈希自定义头像、Rich Markup 转义及基于 `cell_len` 的中英文混排折行器，运行 `pytest tests/test_avatars.py` 全绿。
3. **Refactor**：提取通用的单元格宽度折行函数，消除对长句与多行换行处理的重复逻辑。

### Iteration 2: 右上角固定花名册悬浮窗、零刷屏与防滚屏残影 (`acp/console.py`)
1. **Red**：更新并扩展 `tests/test_console.py`，编写 `TC-ROSTER-01 ~ 05`（包含修改原有对 `Presence: ... is now ...` 的旧断言），验证零刷屏、右上角面板状态灯与坐标擦除重绘、3 行精简底部 Dock 及消息流头像展示（此时旧代码因仍打印 `Presence:` 且底部为 4 行而失败）。
2. **Green**：改造 `HumanConsole`：
   - 精简 `render_dock_lines` 为 3 行（移除 `Online Agents` 底部行）。
   - 实现 `render_roster_overlay_lines`、`_clear_roster_overlay`、`_draw_roster_overlay` 并在 `_emit()` 中串联防残影顺序。
   - 改造 `on_presence_change` 为静默刷新右上角面板，移除主区文本打印。
   - 在 `on_explicit_message`、`on_silent_message`、`on_broadcast` 及 `_do_read` 中接入 `render_message_with_avatar`。
3. **Refactor**：抽离渲染坐标计算与安全右侧留白计算辅助方法，确保宽字符裁剪与窗口缩放边界安全。

### Iteration 3: 同进程 Hub + Console 生命周期与 CLI 路由 (`acp/hub.py`, `acp/cli.py`, `acp/console.py`)
1. **Red**：创建 `tests/test_unified_cli.py`，编写 `TC-UNIFIED-01 ~ 05`，验证无 Hub 时自动内嵌启动与退出关闭、已有 Hub 时客户端降级接入且不误杀外部 Hub、CLI 各入口（`acp`、`acp console`、`acp hub`、`acp hub --headless`）的路由分派与嵌入式 Hub 日志静默。
2. **Green**：实现 `UnifiedConsoleRunner` / `start_unified_console`、端口/URI 探测逻辑、TUI 期间 `acp.hub`/`websockets` 日志隔离，并更新 `acp/cli.py` 路由。
3. **Refactor**：跑通全部非 DT 单元测试与 DT 集成测试，检查代码规范与跨平台兼容性。

---

## 2. 详细测试用例规格矩阵 (Test Case Specifications)

### 2.1 `tests/test_avatars.py` — 头像与排版测试

| 用例编号 | 测试名称 | 测试场景与输入 | 预期断言 (Expected Assertions) |
| :--- | :--- | :--- | :--- |
| **TC-AVATAR-01** | `test_builtin_avatar_matching` | 分别传入 `@claude`、`@claudecode`、`@codex1`、`@gpt-2`、`@agy`、`@antigravity`、`@dsh`、`@deepseek`、`@user`、`@human`（含大小写混合与不带 `@` 前缀） | 分别稳定命中 `claude` / `codex` / `agy` / `dsh` / `user` 预设角色；不同角色返回各自对应的主题色与 `badge`。 |
| **TC-AVATAR-02** | `test_custom_handle_deterministic_and_cell_width` | 对未知句柄 `@reviewer_a`、`@qa-bot` 连续两次调用 `resolve_avatar()`；同时遍历所有内置与随机样本头像 | 1. 同一句柄多次调用返回的 `AvatarSpec` 完全一致。<br>2. **宽度不变量**：任意头像 `len(spec.lines) == 3`，且每一行 `rich.cells.cell_len(line) == 7`。 |
| **TC-AVATAR-03** | `test_single_line_message_keeps_3_line_avatar` | 对单行短消息 `"hello"`（来自 `@claude`）调用 `render_message_with_avatar(...)` 并在固定宽度（如 `width=80`）下渲染 | 输出至少包含 3 行（header + body 1 + 空正文占位以承托头像第 3 行），且头像 3 行像素图案全部完整出现、第 1 行包含发送者 `@claude` 与时间戳。 |
| **TC-AVATAR-04** | `test_multiline_and_long_cjk_wrapping_alignment` | 传入包含显式换行符 `\n` 的多行中文/英文混合长消息（含 `[main]` 等方括号特殊字符）在 `width=50` 下渲染 | 1. 不会触发 `rich.errors.MarkupError` 且原文 `[main]` 完整保留。<br>2. 第 1~3 行左侧为头像，第 4 行起左侧填充 7 个空格 + 2 格间隔与第 2 行正文左对齐。<br>3. 所有输出行实际单元格宽度严格 `<= width - 1`。 |
| **TC-AVATAR-05** | `test_console_messages_and_read_render_avatars` | 在 `HumanConsole` 中分别触发 `on_explicit_message`、`on_silent_message`、`on_broadcast` 及 `_do_read`（模拟 `@codex`、`@claude`、`@user` 发言） | 终端输出流中均能观察到对应成员的 3 行像素头像片段、模式徽章（`EXPLICIT`/`BOARD`/`BROADCAST`/`URGENT`）与消息正文，且不抛出任何异常。 |

### 2.2 `tests/test_console.py` — 右上角固定面板、防残影与 3 行 Dock 测试

| 用例编号 | 测试名称 | 测试场景与输入 | 预期断言 (Expected Assertions) |
| :--- | :--- | :--- | :--- |
| **TC-ROSTER-01** | `test_presence_change_never_prints_notice_spam` | 在 `dock_active=True` 与 `dock_active=False` 两种模式下连续调用 `on_presence_change`（状态依次为 `starting`、`idle`、`busy`、`blocked`、`offline`） | 1. 输出流中**绝对不包含** `"Presence:"` 或 `"is now"` 字样。<br>2. `hc.roster_state` 正确更新对应成员状态；收到 `offline` 时将成员从在线花名册移除（或标记离线不计入在线计数）。<br>3. `dock_active=True` 时，输出包含 `\x1b7` 与 `\x1b8` 在右上角原地更新卡片而不触发主区换行滚屏。 |
| **TC-ROSTER-02** | `test_top_right_roster_overlay_content_and_truncation` | 设置 `roster_state` 包含 `@claude (idle)`、`@codex (busy)`、`@agy (starting)`、`@dsh (blocked)` 及大量在线节点测试截断 | 1. `render_roster_overlay_lines()` 首行展示正确在线计数（如 `Room Online (4)`）。<br>2. 包含 4 种状态灯标识及成员句柄。<br>3. 成员数为 0 时显示空房间提示；成员数超过高度上限时末行显示 `+K more` 且边框完整闭合、每行宽度一致。 |
| **TC-ROSTER-03** | `test_emit_clears_overlay_before_print_and_redraws_after` | 在 `dock_active=True` 且用户输入部分草稿 `/send @codex hel` 时，连续穿插 `on_presence_change` 与 `on_explicit_message` | 1. 字符流顺序严格满足：旧 Dock 擦除 (`\x1b[J`) ➔ 旧右上角悬浮框擦除（向旧矩形位置写空白）➔ 新带头像消息打印 ➔ 新右上角悬浮框绘制 (`\x1b7...\x1b[1;...H...\x1b8`) ➔ 底部草稿输入框重绘。<br>2. 用户正在输入的草稿 `_input_buffer` 保持为 `"/send @codex hel"` 完好无损。 |
| **TC-ROSTER-04** | `test_compact_3_line_bottom_dock` | 调用 `hc.render_dock_lines(width=120)` 与 `hc.render_dock_lines(width=70)` | 1. 返回固定 **3 行**（分隔栏、输入行、`Commands:`/`Cmds:` 栏），`cursor_row == 1`。<br>2. 底部 Dock **不再包含** `Online Agents (` 字符串。<br>3. 宽字符 CJK 草稿仍满足所有行 `cell_len <= width - 1` 且 `cursor_col <= width - 1`。 |
| **TC-ROSTER-05** | `test_non_tty_omits_ansi_overlay_and_dock_sequences` | 在 `dock_active=False` 下触发 `on_presence_change`、`on_explicit_message` 与 `_do_read` | 输出流中不包含 `\x1b[J`、`\x1b7`、`\x1b8` 等终端控制序列，且消息头像与正文清晰可读。 |

### 2.3 `tests/test_unified_cli.py` — 统一 Hub + Console 与 CLI 路由测试

| 用例编号 | 测试名称 | 测试场景与输入 | 预期断言 (Expected Assertions) |
| :--- | :--- | :--- | :--- |
| **TC-UNIFIED-01** | `test_unified_console_starts_embedded_hub_when_port_free` | 指定空闲随机端口（例如动态探测可用端口）启动 `UnifiedConsoleRunner` / `start_unified_console`，运行中用真实外部 `ACPClient` 连入并发送一条消息，随后触发 `/quit` | 1. 控制台报告 `owns_hub is True`。<br>2. 外部 `ACPClient` 可正常连接该端口、注册并向聊天室发消息，Console 实时收到带头像消息。<br>3. Console 退出后，内嵌 `ACPHub` 自动停止，端口释放，再次尝试连接应被拒绝。 |
| **TC-UNIFIED-02** | `test_unified_console_attaches_to_existing_hub_without_stopping_it` | 先独立启动一个真实 `ACPHub`（随机端口），再在该 URI 上启动 `UnifiedConsoleRunner`，随后退出 Console | 1. 控制台检测到已有 Hub 在线，`owns_hub is False`，无端口冲突异常。<br>2. 控制台可正常拉取花名册并收发消息。<br>3. Console 退出后，外部 `ACPHub` 仍存活并可继续响应 `PING` 请求。 |
| **TC-UNIFIED-03** | `test_cli_default_and_console_dispatch` | 使用 `click.testing.CliRunner`（Monkeypatch `start_unified_console` 与 `run_hub`，模拟 `isatty()` 为 `True` 和 `False`）分别调用 `acp`（无参数）、`acp --as @alice`、`acp console` | 1. TTY 环境下无参数 `acp` 直接调用 `start_unified_console`，`handle` 透传正确。<br>2. `acp console` 调用 `start_unified_console`。<br>3. 非 TTY 下调用裸 `acp` 时回退打印帮助或遵循约定安全退出（不卡死 CI）。 |
| **TC-UNIFIED-04** | `test_cli_hub_tui_vs_headless_mode` | 使用 `CliRunner` 分别调用：`acp hub --headless`、非 TTY 下 `acp hub`、以及 TTY 下 `acp hub --port 9001` | 1. `--headless` 与非 TTY 下均调用 `run_hub(host=..., port=..., db_path=...)`。<br>2. TTY 且无 `--headless` 时调用 `start_unified_console`，传递绑定 `host`/`port`/`db_path` 并在同进程内嵌 Hub。 |
| **TC-UNIFIED-05** | `test_embedded_hub_suppresses_console_logging` | 在 `UnifiedConsoleRunner` 以 `owns_hub=True` 运行并接收外部客户端连接与断开时，捕获 `stdout`/`stderr` | `acp.hub` 与 `websockets` 的 `INFO` 日志不会直接打印到终端输出流中污染 Console TUI 界面。 |

---

## 3. 验证命令与覆盖标准 (Verification Commands & Acceptance Criteria)

### 3.1 单元测试与集成测试执行命令

```bash
# 1. 运行全部快速单元测试（含 avatars、console、unified_cli、profiles、storage 等）
python -m pytest -m "not dt"

# 2. 运行端到端集成测试（含真实 WebSocket Hub、嵌入 Hub 互连、ConPTY 启动器测试）
python -m pytest -m dt

# 3. 定向运行本次新增与变更的测试文件
python -m pytest tests/test_avatars.py tests/test_console.py tests/test_unified_cli.py -v
```

### 3.2 验收完成标准 (Definition of Done)

1. **功能完整性**：
   - `acp`、`acp hub`（交互终端）、`acp console` 均能直接启动合并终端，端口空闲自动拉起内嵌 Hub，端口已用则自动降级接入。
   - `acp hub --headless` 与非 TTY 行为 100% 保持兼容。
   - 右上角固定花名册实时反映所有在线成员状态，主消息流彻底无 `Presence: ... is now ...` 刷屏文本，滚动输出不留残影。
   - 每位发言成员（实时与 `/read`）均带有 3 行高彩色像素小人头像且多行正文对齐。
2. **测试通过标准**：
   - `tests/test_avatars.py`、`tests/test_console.py`、`tests/test_unified_cli.py` 全部用例 100% 通过，无 skipped 或放松断言。
   - `python -m pytest -m "not dt"` 与 `python -m pytest -m dt` 全量回归通过（现有 `test_hub_and_client`、`test_message_isolation`、`test_session_claims` 等不受回归破坏）。
