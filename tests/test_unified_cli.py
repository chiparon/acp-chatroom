import asyncio
import io
import json
import logging
import time
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from rich.console import Console
from websockets.server import serve

import acp.cli as cli_mod
from acp.avatars import resolve_avatar
from acp.client import ACPClient
from acp.console import (
    HumanConsole,
    ProbeStatus,
    UnifiedConsoleRunner,
    _silence_embedded_hub_logging,
    probe_hub,
)
from acp.hub import ACPHub
from acp.protocol import EventType, MessageMode


# TC-CLI-01 (not dt)
def test_cli_bare_acp_tty_vs_non_tty(monkeypatch):
    calls = []
    monkeypatch.setattr(cli_mod, "start_unified_console", lambda **kw: calls.append(kw))

    # 1. Interactive TTY -> bare `acp` and `acp --as @alice` launch unified console
    monkeypatch.setattr(cli_mod, "_is_interactive_tty", lambda: True)
    runner = CliRunner()
    res1 = runner.invoke(cli_mod.cli, [])
    assert res1.exit_code == 0
    assert len(calls) == 1
    assert calls[0]["handle"] == "@user"
    assert calls[0]["uri"] == "ws://127.0.0.1:8765"
    assert calls[0]["auto_hub"] is True

    res2 = runner.invoke(cli_mod.cli, ["--as", "@alice"])
    assert res2.exit_code == 0
    assert len(calls) == 2
    assert calls[1]["handle"] == "@alice"

    # 2. Non-TTY -> bare `acp` prints help and exits 0 without starting TUI
    calls.clear()
    monkeypatch.setattr(cli_mod, "_is_interactive_tty", lambda: False)
    res_non_tty = runner.invoke(cli_mod.cli, [])
    assert res_non_tty.exit_code == 0
    assert len(calls) == 0
    assert "Usage:" in res_non_tty.output


# TC-CLI-02 (not dt)
def test_cli_hub_tui_headless_and_env_isolation(monkeypatch):
    hub_calls = []
    tui_calls = []
    monkeypatch.setattr(cli_mod, "run_hub", lambda **kw: hub_calls.append(kw))
    monkeypatch.setattr(cli_mod, "start_unified_console", lambda **kw: tui_calls.append(kw))
    monkeypatch.setenv("ACP_HUB_URI", "ws://127.0.0.1:9999")

    runner = CliRunner()

    # 1. --headless in TTY runs headless run_hub
    monkeypatch.setattr(cli_mod, "_is_interactive_tty", lambda: True)
    res_headless = runner.invoke(cli_mod.cli, ["hub", "--headless", "--port", "8800"])
    assert res_headless.exit_code == 0
    assert len(hub_calls) == 1
    assert hub_calls[0] == {"host": "127.0.0.1", "port": 8800, "db_path": "acp_chatroom.db"}
    assert len(tui_calls) == 0

    # 2. Non-TTY `acp hub` runs headless run_hub
    hub_calls.clear()
    monkeypatch.setattr(cli_mod, "_is_interactive_tty", lambda: False)
    res_nontty = runner.invoke(cli_mod.cli, ["hub", "--port", "8801"])
    assert res_nontty.exit_code == 0
    assert len(hub_calls) == 1
    assert hub_calls[0]["port"] == 8801
    assert len(tui_calls) == 0

    # 3. Interactive TTY `acp hub --host 0.0.0.0 --port 9001 --db custom.db`
    # Must ignore ACP_HUB_URI=9999 and map 0.0.0.0 -> ws://127.0.0.1:9001
    monkeypatch.setattr(cli_mod, "_is_interactive_tty", lambda: True)
    res_tty = runner.invoke(cli_mod.cli, ["hub", "--host", "0.0.0.0", "--port", "9001", "--db", "custom.db"])
    assert res_tty.exit_code == 0
    assert len(tui_calls) == 1
    assert tui_calls[0]["uri"] == "ws://127.0.0.1:9001"
    assert tui_calls[0]["bind_host"] == "0.0.0.0"
    assert tui_calls[0]["bind_port"] == 9001
    assert tui_calls[0]["db_path"] == "custom.db"
    assert tui_calls[0]["auto_hub"] is True
    assert tui_calls[0]["explicit_hub_options"] is True


# TC-CLI-03 (not dt)
def test_cli_console_and_root_option_inheritance(monkeypatch):
    tui_calls = []
    monkeypatch.setattr(cli_mod, "start_unified_console", lambda **kw: tui_calls.append(kw))
    runner = CliRunner()

    # 1. `acp console --no-auto-hub` passes auto_hub=False
    res1 = runner.invoke(cli_mod.cli, ["console", "--no-auto-hub"])
    assert res1.exit_code == 0
    assert len(tui_calls) == 1
    assert tui_calls[0]["auto_hub"] is False
    assert tui_calls[0]["handle"] == "@user"

    # 2. `acp --as @alice console` inherits root --as option via ctx.obj
    res2 = runner.invoke(cli_mod.cli, ["--as", "@alice", "--db", "shared.db", "console"])
    assert res2.exit_code == 0
    assert len(tui_calls) == 2
    assert tui_calls[1]["handle"] == "@alice"
    assert tui_calls[1]["db_path"] == "shared.db"
    assert tui_calls[1]["auto_hub"] is True


# TC-CLI-04 (not dt)
def test_probe_and_remote_uri_never_binds_locally(monkeypatch, tmp_path):
    hub_created = False
    orig_init = ACPHub.__init__

    def spy_init(self, *args, **kwargs):
        nonlocal hub_created
        hub_created = True
        return orig_init(self, *args, **kwargs)

    monkeypatch.setattr(ACPHub, "__init__", spy_init)
    monkeypatch.setattr("acp.console.probe_hub", AsyncMock(return_value=ProbeStatus.PORT_FREE))

    # 1. Remote unreachable URI must never attempt local bind
    buf1 = io.StringIO()
    ui1 = Console(file=buf1, width=90, force_terminal=False, no_color=True, legacy_windows=False)
    runner_remote = UnifiedConsoleRunner(
        uri="ws://192.0.2.1:8765",
        handle="@user",
        db_path=str(tmp_path / "remote.db"),
        auto_hub=True,
        ui_console=ui1,
    )
    with pytest.raises(RuntimeError, match="192.0.2.1"):
        asyncio.run(runner_remote.start())
    assert not hub_created

    # 2. Local URI with auto_hub=False must never attempt local bind
    buf2 = io.StringIO()
    ui2 = Console(file=buf2, width=90, force_terminal=False, no_color=True, legacy_windows=False)
    runner_no_auto = UnifiedConsoleRunner(
        uri="ws://127.0.0.1:8765",
        handle="@user",
        db_path=str(tmp_path / "noauto.db"),
        auto_hub=False,
        ui_console=ui2,
    )
    with pytest.raises(RuntimeError, match="No ACP Hub"):
        asyncio.run(runner_no_auto.start())
    assert not hub_created


# TC-UNIFIED-01
@pytest.mark.dt
async def test_embedded_hub_lifecycle_and_fast_shutdown_with_active_client(tmp_path):
    buf = io.StringIO()
    ui = Console(file=buf, width=100, height=24, force_terminal=False, no_color=True, legacy_windows=False)
    runner = UnifiedConsoleRunner(
        uri="ws://127.0.0.1:0",
        handle="@user",
        bind_host="127.0.0.1",
        bind_port=0,
        db_path=str(tmp_path / "embedded.db"),
        auto_hub=True,
        ui_console=ui,
    )

    await runner.start()
    assert runner.owns_hub is True
    assert runner.bound_port > 0
    uri = f"ws://127.0.0.1:{runner.bound_port}"

    agent = ACPClient(uri=uri, handle="@codex")
    try:
        await agent.connect()
        for _ in range(40):
            if "@codex" in runner.console.roster_state:
                break
            await asyncio.sleep(0.02)
        assert "@codex" in runner.console.roster_state
        assert "@codex" in runner.console.render_header_lines(width=100)[0]

        res = await agent.send_message(
            target="@user",
            content="hello unified console",
            mode=MessageMode.EXPLICIT,
        )
        assert res.get("status") == "ok"

        codex_avatar = resolve_avatar("@codex")
        for _ in range(40):
            if "hello unified console" in buf.getvalue():
                break
            await asyncio.sleep(0.02)
        out = buf.getvalue()
        assert "hello unified console" in out
        assert codex_avatar.lines[0] in out

        # Trigger /quit while external @codex client is STILL actively connected
        t0 = time.monotonic()
        runner.console.handle_command("/quit")
        await asyncio.wait_for(runner.stop(), timeout=2.0)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0

        for _ in range(40):
            if not agent.connected:
                break
            await asyncio.sleep(0.02)
        assert not agent.connected
        assert await probe_hub(uri, timeout=0.4) == ProbeStatus.PORT_FREE
    finally:
        await agent.close()
        await runner.stop()


# TC-UNIFIED-02
@pytest.mark.dt
async def test_attaches_to_existing_hub_without_stopping_it(tmp_path):
    external_hub = ACPHub(host="127.0.0.1", port=0, db_path=str(tmp_path / "ext.db"))
    await external_hub.start()
    uri = f"ws://127.0.0.1:{external_hub.bound_port}"

    buf = io.StringIO()
    ui = Console(file=buf, width=100, height=24, force_terminal=False, no_color=True, legacy_windows=False)
    runner = UnifiedConsoleRunner(
        uri=uri,
        handle="@user",
        db_path=str(tmp_path / "unused.db"),
        auto_hub=True,
        explicit_hub_options=True,
        ui_console=ui,
    )
    try:
        await runner.start()
        assert runner.owns_hub is False
        assert runner.console._hub_mode == "client"
        runner.console.print_banner()
        assert "Attached Hub" in buf.getvalue()

        await runner.stop()
        # External hub must remain alive and respond to PING
        assert await probe_hub(uri, timeout=0.8) == ProbeStatus.ALIVE_HUB
    finally:
        await runner.stop()
        await external_hub.stop()


# TC-UNIFIED-03
@pytest.mark.dt
async def test_port_occupied_by_non_acp_tcp_server_fails_fast(tmp_path):
    active_writers = []

    async def dummy_tcp(reader, writer):
        active_writers.append(writer)
        try:
            await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(dummy_tcp, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    uri = f"ws://127.0.0.1:{port}"
    try:
        t0 = time.monotonic()
        status = await probe_hub(uri, timeout=0.4)
        assert status == ProbeStatus.PORT_OCCUPIED_NON_ACP
        assert time.monotonic() - t0 < 1.5

        buf = io.StringIO()
        ui = Console(file=buf, width=90, force_terminal=False, no_color=True, legacy_windows=False)
        runner = UnifiedConsoleRunner(
            uri=uri,
            handle="@user",
            bind_host="127.0.0.1",
            bind_port=port,
            db_path=str(tmp_path / "occupied.db"),
            auto_hub=True,
            ui_console=ui,
        )
        with pytest.raises(RuntimeError, match="non-ACP"):
            await runner.start(probe_timeout=0.4)
    finally:
        for w in active_writers:
            w.close()
        server.close()


# TC-UNIFIED-04
@pytest.mark.dt
async def test_embedded_hub_isolates_info_and_warning_logs(tmp_path, capsys):
    target_names = ("acp.hub", "websockets", "websockets.server", "websockets.protocol")
    before = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
        )
        for name in target_names
    }

    log_buf = None
    with _silence_embedded_hub_logging() as captured_logs:
        log_buf = captured_logs
        hub = ACPHub(host="127.0.0.1", port=0, db_path=str(tmp_path / "quiet.db"))
        await hub.start()
        uri = f"ws://127.0.0.1:{hub.bound_port}"
        try:
            c = ACPClient(uri=uri, handle="@codex")
            await c.connect()
            # Send invalid JSON frame and trigger warning/error logs explicitly
            await c.ws.send("{not-valid-json")
            logging.getLogger("acp.hub").warning("synthetic hub warning")
            logging.getLogger("websockets.server").error("synthetic ws error")
            await asyncio.sleep(0.05)
            await c.close()
        finally:
            await hub.stop()

    captured = capsys.readouterr()
    assert "synthetic hub warning" not in captured.out
    assert "synthetic hub warning" not in captured.err
    assert "synthetic ws error" not in captured.out
    assert "synthetic ws error" not in captured.err
    assert "Registered agent" not in captured.out
    assert "Registered agent" not in captured.err
    assert log_buf is not None and any("synthetic hub warning" in line for line in log_buf)

    for name in target_names:
        lg = logging.getLogger(name)
        assert (list(lg.handlers), lg.level, lg.propagate) == before[name]


# TC-UNIFIED-05
@pytest.mark.dt
async def test_client_mode_detects_external_hub_disconnect(tmp_path):
    ext_hub = ACPHub(host="127.0.0.1", port=0, db_path=str(tmp_path / "ext_disc.db"))
    await ext_hub.start()
    uri = f"ws://127.0.0.1:{ext_hub.bound_port}"

    buf = io.StringIO()
    ui = Console(file=buf, width=100, height=24, force_terminal=False, no_color=True, legacy_windows=False)
    runner = UnifiedConsoleRunner(
        uri=uri,
        handle="@user",
        db_path=str(tmp_path / "client_disc.db"),
        auto_hub=False,
        ui_console=ui,
    )
    await runner.start()
    runner.console._enter_tui()
    try:
        await ext_hub.stop()
        for _ in range(50):
            if not runner.console.client.connected:
                break
            await asyncio.sleep(0.02)
        assert not runner.console.client.connected

        runner.console._notify_external_disconnect()
        header_row1 = runner.console.render_header_lines(width=100)[0]
        assert "disconnected" in header_row1
        assert "Hub disconnected" in buf.getvalue()
    finally:
        runner.console._teardown_tui()
        await runner.stop()


# TC-UNIFIED-06
@pytest.mark.dt
async def test_wire_order_initial_sync_into_top_header(monkeypatch):
    async def reply(ws):
        async for raw in ws:
            request = json.loads(raw)
            if request["action"] == "register":
                await ws.send(json.dumps({"req_id": request["req_id"], "status": "ok", "handle": request["handle"]}))
                continue
            assert request["action"] == "roster"
            await ws.send(json.dumps({"event": "presence_change", "handle": "@claude", "status": "busy"}))
            await ws.send(json.dumps({
                "req_id": request["req_id"],
                "action": "roster",
                "status": "ok",
                "roster": [{"handle": "@claude", "status": "idle"}, {"handle": "@codex", "status": "idle"}],
            }))
            await ws.send(json.dumps({"event": "presence_change", "handle": "@codex", "status": "offline"}))

    async with serve(reply, "127.0.0.1", 0) as server:
        uri = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        buf = io.StringIO()
        hc = HumanConsole(
            uri=uri,
            ui_console=Console(file=buf, width=100, height=24, force_terminal=False, no_color=True, legacy_windows=False),
        )
        await hc.client.connect(role="console")
        offline = asyncio.Event()

        def presence(data):
            hc.on_presence_change(data)
            if data["status"] == "offline":
                offline.set()

        hc.client.on(EventType.PRESENCE_CHANGE.value, presence)
        get_roster = hc.client.get_roster

        async def get_roster_before_resume(**kwargs):
            roster = await get_roster(**kwargs)
            await offline.wait()
            return roster

        monkeypatch.setattr(hc.client, "get_roster", get_roster_before_resume)
        try:
            await asyncio.wait_for(hc.sync_roster(display_table=False), 5)
            header_row1 = hc.render_header_lines(width=100)[0]
            assert "Online (1): ● @claude IDLE" in header_row1
            assert "@codex" not in header_row1
            assert "Active Agent Roster" not in buf.getvalue()
        finally:
            await hc.client.close()
