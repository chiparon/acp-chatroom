# 测试驱动开发方案 (TDD): 统一 Hub + Console 类 Claude Code 交互终端

> **分支**: `feat/unified-console-tui`（基线提交 `388ad2b`，已包含 PR #16/#17/#18）  
> **状态**: 修订版（已对齐 CI `not dt` / `dt` 分层与全量审阅意见，待最终确认）  
> **关联文档**: [SDD.md](./SDD.md) · [REQUIREMENTS.md](./REQUIREMENTS.md)

---

## 1. TDD 开发原则与分阶段实施路径 (Red ➔ Green ➔ Refactor)

本项目严格遵循 **Red ➔ Green ➔ Refactor** 循环，并与 `.github/workflows/ci.yml` 的双层测试体系对齐：
- **单元测试（`not dt`）**：纯内存、无真实网络端口、毫秒级执行。
- **系统/集成测试（`@pytest.mark.dt`）**：启动真实本地 `ACPHub`（统一使用 `port=0` 由内核分配空闲端口，杜绝 CI 端口冲突）、真实 WebSocket 连接或真实 ConPTY/PTY 子进程。

分 4 个渐进迭代实施：

### Iteration 1: `acp/avatars.py` 像素头像映射与 `cell_len` 消息卡片排版
1. **Red**：创建 `tests/test_avatars.py`，编写 `TC-AVATAR-01 ~ 05`，覆盖正则边界匹配、7-cell 宽度不变量、CJK/Emoji `wrap_to_cells` 折行与 Rich Markup 防注入。
2. **Green**：实现 `acp/avatars.py` 并运行 `pytest tests/test_avatars.py` 全绿。
3. **Refactor**：整理常量表与单元格切分边界（零宽组合字符、超长无空格 token、空字符串）。

### Iteration 2: 顶部固定通栏、`DECSTBM` 滚动区、零刷屏与 `@user::> ` 3 行 Dock (`acp/console.py`)
1. **Red**：更新并扩展 `tests/test_console.py`，编写 `TC-TUI-01 ~ 08`（更新旧的 `Presence:` 与 `@user> ` 断言），验证顶部 2 行固定通栏、零 `Presence:` 刷屏、`DECSTBM` 设置与退出复位 `\x1b[r`、Resize 与窄屏/矮屏降级、3 行 `@user::> ` Dock、人类自发消息头像去重以及 `/remove` & `/prune` 刷新顶部通栏。
2. **Green**：改造 `HumanConsole`（接入 `acp/avatars.py`、统一 `_emit()`、实现 `render_header_lines()` / `DECSTBM` 管理 / `sync_roster(display_table=False)` 保留 PR #16 wire-order）。
3. **Refactor**：抽离视口尺寸与 ANSI 转义拼接辅助函数，验证 `tests/test_console.py` 与 `tests/test_console_integration.py` 全绿。

### Iteration 3: `ACPHub` 秒退加固、三态探测与同进程生命周期 (`acp/hub.py`, `acp/console.py`)
1. **Red**：创建 `tests/test_unified_cli.py`，编写三态探测与同进程生命周期用例 `TC-UNIFIED-01 ~ 06`（含非 ACP 占口、远端不可达、活跃客户端下 `< 2s` 秒退、日志静默隔离）。
2. **Green**：为 `ACPHub` 新增 `bound_port` 与快速关闭活跃连接的 `stop()`；实现 `probe_hub()`、`_silence_embedded_hub_logging()`、`UnifiedConsoleRunner` 和 `start_unified_console()`。
3. **Refactor**：确保所有异步任务与日志 Handler 在异常路径下也 100% 清理还原。

### Iteration 4: CLI 路由改造与真实 PTY 端到端回归 (`acp/cli.py`, `tests/test_console_integration.py`)
1. **Red**：在 `tests/test_unified_cli.py` 补全 CLI 路由单测 `TC-CLI-01 ~ 04`，并在 `tests/test_console_integration.py` 增加真实 PTY 一键启动统一终端的端到端测试 `TC-E2E-01`。
2. **Green**：改造 `acp/cli.py`（根命令 `invoke_without_command=True` + `ctx.obj` 透传、`acp hub --headless` 免疫 `ACP_HUB_URI`、`acp console --no-auto-hub`）。
3. **Refactor**：执行全量 `not dt` 与 `dt` 回归测试套件。

---

## 2. 详细测试用例规格矩阵 (Test Case Specifications)

### 2.1 `tests/test_avatars.py` — 头像与宽字符排版单元测试（`not dt`）

| 用例编号 | 测试名称 | 测试场景与输入 | 预期断言 (Expected Assertions) |
| :--- | :--- | :--- | :--- |
| **TC-AVATAR-01** | `test_builtin_avatar_matching_and_boundaries` | 测试 `@claude`、`@claudecode`、`@codex1`、`@gpt-2`、`@agy`、`@antigravity`、`@dsh`、`@deepseek`、`@user`、`@human`（含大小写及无 `@`）；同时测试反例 `@ag`、`@codexer`、`@alice` | 1. 合法内置句柄精确命中对应角色 `key`、`color`、`badge`。<br>2. 反例 `@ag`、`@codexer`、`@alice` 不误命中内置家族，落入 `custom`。 |
| **TC-AVATAR-02** | `test_custom_handle_deterministic_and_cell_width` | 对未知句柄 `@reviewer_a`、`@qa-bot` 多次调用 `resolve_avatar()`；遍历所有内置与 50 个随机句柄 | 1. 同句柄多次返回完全一致的 `AvatarSpec`。<br>2. **宽度不变量**：任意头像 `len(spec.lines) == 3`，每行 `rich.cells.cell_len(line) == 7`。 |
| **TC-AVATAR-03** | `test_wrap_to_cells_handles_cjk_emoji_and_paragraphs` | 对纯中文长句、中英混排、Emoji、无空格超长 URL、含空行的多段文本调用 `wrap_to_cells(text, max_cells=20)` | 1. 每行 `cell_len(line) <= 20`（验证未使用按字符数计算的 `textwrap.wrap`）。<br>2. 显式 `\n\n` 段落空行得以保留，内容无字符丢失。 |
| **TC-AVATAR-04** | `test_single_and_multiline_avatar_card_alignment` | 对单行短消息 `"hello"` 与多行含 `[main]` 方括号的中文长消息调用 `render_message_with_avatar(..., max_width=54)` | 1. 单行消息仍完整输出 3 行像素头像。<br>2. 含 `[main]` 文本不抛 `MarkupError` 且原文保留。<br>3. 第 4 行起左侧为 9 格对齐缩进，所有渲染行 `cell_len <= 53`。 |
| **TC-AVATAR-05** | `test_console_incoming_and_read_render_avatars` | 在 `HumanConsole` 分别触发 `on_explicit_message`、`on_silent_message`、`on_broadcast` 及 `_do_read` | 输出中完整呈现发送者的 3 行像素头像、模式徽章与正文，且 `acp read`（CLI 非交互子命令）仍保持原有输出格式兼容。 |

### 2.2 `tests/test_console.py` — 顶部通栏、`DECSTBM`、零刷屏与 `@user::> ` Dock 单测（`not dt`）

| 用例编号 | 测试名称 | 测试场景与输入 | 预期断言 (Expected Assertions) |
| :--- | :--- | :--- | :--- |
| **TC-TUI-01** | `test_presence_change_silent_header_update_zero_spam` | 在 `dock_active=True` 与 `dock_active=False` 下连续触发 `on_presence_change`（`starting` / `idle` / `busy` / `blocked` / `offline`） | 1. 输出流中**绝对不包含** `"Presence:"` 或 `"is now"`。<br>2. `roster_state` 正确更新且 `offline` 立即移除。<br>3. `dock_active=True` 时仅向第 1 行发出 `\x1b7\x1b[1;1H\x1b[2K...\x1b[0m\x1b8` 刷新通栏，不污染主区。 |
| **TC-TUI-02** | `test_top_header_layout_alignment_and_overflow` | 测试空房间、4 种状态成员混排（`idle`/`busy`/`starting`/`blocked`）、超多成员（8 个 Agent）及窄屏（`width=60`）下的 `render_header_lines(width=...)` | 1. 恒定返回 2 行（信息行 + 分割线），每行 `cell_len <= width - 1`。<br>2. 左侧含 Hub 模式标识，右侧含 `Online (N):` 与单列符号 `●`/`◌`（不含双格彩色 Emoji）。<br>3. 超宽时出现 `+K` 截断提示且不换行。 |
| **TC-TUI-03** | `test_decstbm_setup_emit_scroll_and_clean_exit_reset` | 在 `width=100, height=24, dock_active=True` 下启动区域布局、输入草稿 `/send @codex hel`、穿插 `on_presence_change` 和 `on_explicit_message`、最后执行 `/quit` 和 `Ctrl+C` | 1. 初始化发出 `\x1b[3;21r` 锁定滚动区。<br>2. 新消息写入滚动区后输入光标归位，草稿 `_input_buffer` 丝毫无损。<br>3. 退出时**必须发出 `\x1b[r`** 复位滚动区并停用 `_dock_active`。 |
| **TC-TUI-04** | `test_compact_3_line_dock_with_custom_prompt_and_cjk` | 调用 `render_dock_lines(width=120)`、`width=70` 与 `width=36`（含长中文草稿） | 1. 返回固定 **3 行**，`cursor_row == 1`。<br>2. 第 0 行包含 `"Draft Input"`，第 1 行以 **`@user::> `** 开头，第 2 行包含 `/remove` 与 `/prune`（宽/中屏），且不再包含 `Online Agents (`。<br>3. 所有行 `cell_len <= width - 1`，`cursor_col <= width - 1`。 |
| **TC-TUI-05** | `test_human_self_messages_render_once_with_avatar` | 通过 `handle_key` 依次输入并回车提交：`/send @codex hi`（分别模拟 `delivered=True` 和 `False`）、`/post hello`、`/broadcast all`、`/broadcast --urgent now` | 1. 聊天命令不在主区打印冗余灰色 `@user::> /send ...` 回显行。<br>2. `/send` 与普通 `/broadcast` 在收到 `status=ok` 后本地渲染 1 张 `@user` 头像卡片（离线时带 queued 提示）。<br>3. `/post` 与 `--urgent` 仅由 Hub 回推事件渲染 1 次，无重复卡片。 |
| **TC-TUI-06** | `test_remove_and_prune_refresh_top_header` | 在 `dock_active=True` 下执行 `/remove @codex`、`/kick claude`、`/prune` | `roster_state` 移除对应条目，且输出流中包含第 1 行顶部通栏的即时刷新序列，移除后的句柄不再出现在顶部通栏。 |
| **TC-TUI-07** | `test_resize_and_short_or_narrow_terminal_degradation` | 模拟终端从 `100x24` 缩放至 `70x16`，以及极矮终端 `80x6` | 1. Resize 后重新下发 `\x1b[3;13r` 并按新宽度重绘顶部通栏与底部 Dock。<br>2. `height=6 (< 8)` 时自动跳过 `DECSTBM` 不抛异常不越界。 |
| **TC-TUI-08** | `test_non_tty_omits_ansi_and_preserves_legacy_bar_helper` | 在 `dock_active=False` 下触发消息、presence 与 `_do_read`，并调用 `format_online_agents_bar()` | 1. 输出流无 `\x1b[`, `\x1b7`, `\x1b8` 等控制序列。<br>2. `format_online_agents_bar()` 仍返回准确格式（保障已有集成测试兼容）。 |

### 2.3 `tests/test_unified_cli.py` — 探测、CLI 路由（`not dt`）与同进程 Hub 集成（`dt`）

#### A. 单元测试（`not dt`）
| 用例编号 | 测试名称 | 测试场景与输入 | 预期断言 (Expected Assertions) |
| :--- | :--- | :--- | :--- |
| **TC-CLI-01** | `test_cli_bare_acp_tty_vs_non_tty` | Monkeypatch `start_unified_console` 与 `isatty()`，使用 `CliRunner` 调用 `acp`、`acp --as @alice`（TTY vs 非 TTY） | 1. TTY 下裸 `acp` 调用 `start_unified_console(handle="@user", ...)`；`acp --as @alice` 透传 `@alice`。<br>2. 非 TTY 下裸 `acp` 打印帮助信息并 `exit_code == 0`，不启动 TUI。 |
| **TC-CLI-02** | `test_cli_hub_tui_headless_and_env_isolation` | 设置环境变量 `ACP_HUB_URI=ws://127.0.0.1:9999`，分别调用 `acp hub --headless`、非 TTY `acp hub`、TTY `acp hub --host 0.0.0.0 --port 9001 --db custom.db` | 1. `--headless` 与非 TTY 调用 `run_hub()`。<br>2. TTY 下 `acp hub --host 0.0.0.0 --port 9001` 调用 `start_unified_console`，连接 URI 映射为 `ws://127.0.0.1:9001`（无视 `ACP_HUB_URI` 9999），`bind_host="0.0.0.0"`。 |
| **TC-CLI-03** | `test_cli_console_and_root_option_inheritance` | 调用 `acp console --no-auto-hub` 以及 `acp --as @alice console` | 1. `--no-auto-hub` 透传 `auto_hub=False`。<br>2. 根组 `--as @alice` 通过 `ctx.obj` 正确继承到 `console` 子命令（不会静默丢失）。 |
| **TC-CLI-04** | `test_probe_and_remote_uri_never_binds_locally` | 传入不可达远端 URI（如 `ws://192.0.2.1:8765`）或 `auto_hub=False` | 不会尝试创建本地 `ACPHub`，返回清晰连接失败提示。 |

#### B. 系统/网络集成测试（`@pytest.mark.dt`）
| 用例编号 | 测试名称 | 测试场景与输入 | 预期断言 (Expected Assertions) |
| :--- | :--- | :--- | :--- |
| **TC-UNIFIED-01** | `test_embedded_hub_lifecycle_and_fast_shutdown_with_active_client` | 使用 `port=0` 启动 `UnifiedConsoleRunner`，外部真实 `ACPClient(@codex)` 连入并发送显式消息，在 **外部客户端未主动断开** 时触发 Console `/quit` | 1. `owns_hub is True`，`bound_port > 0`。<br>2. Console 实时收到 `@codex` 的带头像消息且顶部花名册含 `@codex`。<br>3. `/quit` 后 **2 秒内**完成退出、内嵌 Hub 停止且端口释放，外部客户端收到连接关闭。 |
| **TC-UNIFIED-02** | `test_attaches_to_existing_hub_without_stopping_it` | 先独立启动真实 `ACPHub(port=0)`，再以该 URI 启动 `UnifiedConsoleRunner`，随后退出 Console | 1. `owns_hub is False`，无端口冲突。<br>2. Console 退出后，外部 `ACPHub` 仍存活并正常响应 `PING`。 |
| **TC-UNIFIED-03** | `test_port_occupied_by_non_acp_tcp_server_fails_fast` | 用 `asyncio.start_server` 在本地随机端口起一个普通非 WebSocket TCP 服务（接受连接但不响应 WS/PONG），向该端口启动 `UnifiedConsoleRunner` | `probe_hub` 在短超时内判定为 `PORT_OCCUPIED_NON_ACP`，立即报错退出，既不卡死也不尝试静默附着。 |
| **TC-UNIFIED-04** | `test_embedded_hub_isolates_info_and_warning_logs` | 在 `owns_hub=True` 期间制造客户端正常连接、断开以及异常非 JSON 帧触发 `logger.info` 与 `logger.warning/exception`，捕获 `stdout`/`stderr` | `acp.hub` 和 `websockets` 的 `INFO` / `WARNING` / `ERROR` 日志均未泄露到终端流中；退出后 Logger 原配置恢复。 |
| **TC-UNIFIED-05** | `test_client_mode_detects_external_hub_disconnect` | 以 `client` 模式附着外部 `ACPHub` 后，主动停止外部 `ACPHub` | Console 在主循环内检测到 `not client.connected`，顶部通栏状态切换为 `disconnected` 并在主区给出断连提示。 |
| **TC-UNIFIED-06** | `test_wire_order_initial_sync_into_top_header` | 复用 PR #16 乱序帧场景（`presence_change` 紧随 `roster` 响应到达），验证 `sync_roster(display_table=False)` | 顶部固定通栏最终展示严格符合 wire-order 的最新在线状态，且启动阶段未向主区打印大张 Roster 表格。 |

### 2.4 `tests/test_console_integration.py` — 真实 PTY 端到端测试（`@pytest.mark.dt`）

| 用例编号 | 测试名称 | 测试场景与输入 | 预期断言 (Expected Assertions) |
| :--- | :--- | :--- | :--- |
| **TC-E2E-01** | `test_unified_console_pty_autostarts_hub_and_receives_agent` | Windows ConPTY（`winpty.PtyProcess`）下直接启动 `python -m acp.cli hub --port <free> --db <tmp>`（不预先起任何外部 Hub），待终端渲染出 `Draft Input` 和 `@user::>` 后，外部 `ACPClient(@codex)` 连入并发送消息，随后向 PTY 写入 `/quit\r` | 1. 终端首屏直接进入 Console 且顶部显示 `@codex` 在线状态，无 `Presence: @codex is now` 字样。<br>2. 屏幕出现 `@codex` 头像与消息内容。<br>3. 输入 `/quit\r` 后进程正常退出（退出码 0），端口关闭。 |
| **TC-E2E-REG** | 保留现有 2 个回归测试 | `test_console_posts_emoji_and_keeps_accepting_commands` & `test_roster_refresh_and_presence_follow_wire_order` | 保持 100% 通过，验证 Windows Emoji 输入与 wire-order 无回归。 |

---

## 3. 验证命令与验收标准 (Verification Commands & Definition of Done)

### 3.1 执行命令（与 CI 一致）

```bash
# 1. 快速单元测试（纯内存，无端口绑定）
python -m pytest -q -m "not dt"

# 2. 系统与集成测试（真实 Hub、三态探测、ConPTY 终端）
python -m pytest -q -m dt

# 3. 定向查看本次新增与变更用例明细
python -m pytest tests/test_avatars.py tests/test_console.py tests/test_unified_cli.py tests/test_console_integration.py -v
```

### 3.2 验收完成标准 (Definition of Done)

1. **功能与体验达标**：
   - 裸输 `acp`、`acp hub`（交互 TTY）、`acp console` 均一步直达 Console TUI，空闲端口自动内嵌启动 Hub，已有 Hub 自动附着，非 ACP 服务占口明确报错；`acp hub --headless` 与非 TTY 100% 兼容。
   - 顶部固定 2 行通栏常驻右上角在线情况（单列着色符号 `●`/`◌`），中间 `DECSTBM` 滚动区无残影无截断，退出必发 `\x1b[r]` 复位终端，主消息流彻底消除 `Presence: ... is now ...` 刷屏。
   - 底部精简为 3 行输入 Dock，提示符为 **`@user::> `**，支持 CJK/Emoji 输入与 `/remove`、`/prune` 指令。
   - 所有成员（含人类自身 `/send`、`/post`、`/broadcast`）发言与 `/read` 均以 3 行高彩色像素小人头像渲染，无双重回显、无缺失头像、无 CJK 折行溢出。
2. **测试全量通过**：
   - `python -m pytest -q -m "not dt"` 与 `python -m pytest -q -m dt` 均 100% 通过（0 failure，不放松断言）。
