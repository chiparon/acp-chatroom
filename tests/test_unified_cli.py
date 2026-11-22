"""TC-CLI-01..04 and TC-UNIFIED-01..06: routing, probe, and in-process hub lifetime."""

import asyncio
import io
import json
import logging
import time

import pytest
import websockets
from rich.console import Console
from websockets.server import serve

import acp.cli as cli_mod
import acp.console as console_mod
from acp.client import ACPClient
from acp.console import (
    HubConnectionError,
    ProbeStatus,
    UnifiedConsoleRunner,
    client_connect_uri,
    probe_hub,
)
from acp.hub import ACPHub
from acp.protocol import MessageMode
from click.testing import CliRunner


def _tty(monkeypatch, enabled: bool) -> None:
    # CliRunner swaps in click.testing._NamedTextIOWrapper streams. Their isatty
    # is what `acp` sees, so the patch has to land on that class.
    import click.testing
    monkeypatch.setattr(click.testing._NamedTextIOWrapper, "isatty", lambda _self: enabled)


def _patch_entry(monkeypatch):
    started = []
    hubs = []

    def fake_start(**kwargs):
        started.append(kwargs)

    def fake_hub(**kwargs):
        hubs.append(kwargs)

    monkeypatch.setattr(cli_mod, "start_unified_console", fake_start)
    monkeypatch.setattr(cli_mod, "run_hub", fake_hub)
    return started, hubs


def test_cli_bare_acp_tty_vs_non_tty(monkeypatch):
    started, _hubs = _patch_entry(monkeypatch)
    _tty(monkeypatch, True)
    result = CliRunner().invoke(cli_mod.cli, [])
    assert result.exit_code == 0
    assert started[-1]["handle"] == "@user"
    assert started[-1]["auto_hub"] is True

    started.clear()
    result = CliRunner().invoke(cli_mod.cli, ["--as", "@alice"])
    assert result.exit_code == 0
    assert started[-1]["handle"] == "@alice"

    started.clear()
    _tty(monkeypatch, False)
    result = CliRunner().invoke(cli_mod.cli, [])
    assert result.exit_code == 0
    assert started == []
    assert "hub" in result.output.lower()
    assert "console" in result.output.lower()


def test_cli_hub_tui_headless_and_env_isolation(monkeypatch):
    started, hubs = _patch_entry(monkeypatch)
    monkeypatch.setenv("ACP_HUB_URI", "ws://127.0.0.1:9999")
    _tty(monkeypatch, False)
    headless = CliRunner().invoke(cli_mod.cli, ["hub", "--headless", "--port", "9002"])
    assert headless.exit_code == 0
    assert hubs and hubs[-1]["port"] == 9002
    assert started == []

    hubs.clear()
    plain = CliRunner().invoke(cli_mod.cli, ["hub", "--port", "9003"])
    assert plain.exit_code == 0
    assert hubs[-1]["port"] == 9003
    assert started == []

    _tty(monkeypatch, True)
    interactive = CliRunner().invoke(
        cli_mod.cli, ["hub", "--host", "0.0.0.0", "--port", "9001", "--db", "custom.db"]
    )
    assert interactive.exit_code == 0
    kwargs = started[-1]
    assert kwargs["uri"] == "ws://127.0.0.1:9001"
    assert kwargs["bind_host"] == "0.0.0.0"
    assert kwargs["bind_port"] == 9001
    assert kwargs["db_path"] == "custom.db"
    assert "9999" not in kwargs["uri"]
    assert client_connect_uri("::", 9001) == "ws://[::1]:9001"
    ipv6 = CliRunner().invoke(cli_mod.cli, ["hub", "--host", "::", "--port", "9001"])
    assert ipv6.exit_code == 0
    assert started[-1]["uri"] == "ws://[::1]:9001"
    assert started[-1]["bind_host"] == "::"


def test_cli_console_and_root_option_inheritance(monkeypatch):
    started, _hubs = _patch_entry(monkeypatch)
    _tty(monkeypatch, False)
    no_hub = CliRunner().invoke(cli_mod.cli, ["console", "--no-auto-hub"])
    assert no_hub.exit_code == 0
    assert started[-1]["auto_hub"] is False
    inherited = CliRunner().invoke(cli_mod.cli, ["--as", "@alice", "console"])
    assert inherited.exit_code == 0
    assert started[-1]["handle"] == "@alice"


def test_probe_and_remote_uri_never_binds_locally(monkeypatch, tmp_path):
    def boom(*_args, **_kwargs):
        raise AssertionError("local ACPHub was constructed")

    monkeypatch.setattr(console_mod, "ACPHub", boom)
    remote = UnifiedConsoleRunner(
        uri="ws://192.0.2.1:8765", auto_hub=True, db_path=str(tmp_path / "remote.db")
    )
    with pytest.raises(HubConnectionError) as remote_error:
        asyncio.run(remote.start())
    assert "not reachable" in str(remote_error.value).lower()
    assert "bind" in str(remote_error.value).lower()

    disabled = UnifiedConsoleRunner(
        uri="ws://127.0.0.1:1", auto_hub=False, db_path=str(tmp_path / "disabled.db")
    )
    with pytest.raises(HubConnectionError) as disabled_error:
        asyncio.run(disabled.start())
    assert "not reachable" in str(disabled_error.value).lower()


def _ui():
    buf = io.StringIO()
    return buf, Console(file=buf, width=100, force_terminal=False, no_color=True)


async def _wait_until(predicate, timeout=2):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition was not met")


async def _port_open(port: int) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 0.3)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    await writer.wait_closed()
    return True


@pytest.mark.dt
async def test_embedded_hub_lifecycle_and_fast_shutdown_with_active_client(tmp_path):
    buf, ui = _ui()
    runner = UnifiedConsoleRunner(
        uri="ws://127.0.0.1:0", db_path=str(tmp_path / "hub.db"), ui_console=ui, auto_hub=True,
    )
    codex = None
    try:
        await runner.start()
        assert runner.owns_hub is True
        assert runner.bound_port and runner.bound_port > 0
        codex = ACPClient(runner.client_uri, "@codex")
        await codex.connect(role="agent")
        await _wait_until(lambda: "@codex" in runner.console.roster_state)
        await codex.send_message(
            target="@user", content="hello from codex", mode=MessageMode.EXPLICIT, sender="@codex",
        )
        await _wait_until(lambda: "hello from codex" in buf.getvalue())
        assert "╭───╮" in buf.getvalue()
        assert "@codex" in runner.console.render_header_lines(100)[0]
        started = time.perf_counter()
        await runner.shutdown()
        await _wait_until(lambda: not codex.connected, timeout=1)
        assert time.perf_counter() - started < 2
        assert await _port_open(runner.bound_port) is False
    finally:
        if codex is not None:
            await codex.close()
        await runner.shutdown()


@pytest.mark.dt
async def test_attaches_to_existing_hub_without_stopping_it(tmp_path):
    hub = ACPHub(port=0, db_path=str(tmp_path / "external.db"))
    await hub.start()
    uri = f"ws://127.0.0.1:{hub.bound_port}"
    buf, ui = _ui()
    runner = UnifiedConsoleRunner(
        uri=uri, db_path=str(tmp_path / "console.db"), ui_console=ui, auto_hub=True,
    )
    try:
        await runner.start()
        assert runner.owns_hub is False
        await runner.shutdown()
        status = await probe_hub(uri, timeout=1)
        assert status is ProbeStatus.ALIVE_HUB
    finally:
        await runner.shutdown()
        await hub.stop()


@pytest.mark.dt
async def test_port_occupied_by_non_acp_tcp_server_fails_fast(tmp_path):
    async def accept(reader, writer):
        try:
            await reader.read(64)
        finally:
            writer.close()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    runner = UnifiedConsoleRunner(
        uri=f"ws://127.0.0.1:{port}", db_path=str(tmp_path / "occupied.db"), auto_hub=True,
    )
    try:
        with pytest.raises(HubConnectionError) as caught:
            await asyncio.wait_for(runner.start(), timeout=3)
        assert caught.value.reason == ProbeStatus.PORT_OCCUPIED_NON_ACP.value
        assert "non-ACP" in str(caught.value)
    finally:
        server.close()
        await server.wait_closed()
        await runner.shutdown()


@pytest.mark.dt
async def test_embedded_hub_isolates_info_and_warning_logs(tmp_path):
    hub_logger = logging.getLogger("acp.hub")
    ws_logger = logging.getLogger("websockets")
    before_hub = (list(hub_logger.handlers), hub_logger.level, hub_logger.propagate)
    before_ws = (list(ws_logger.handlers), ws_logger.level, ws_logger.propagate)
    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setLevel(logging.DEBUG)
    old_root_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    runner = UnifiedConsoleRunner(uri="ws://127.0.0.1:0", db_path=str(tmp_path / "logs.db"))
    try:
        await runner.start()
        async with websockets.connect(runner.client_uri) as ws:
            await ws.send("not-json")
            await asyncio.sleep(0.1)
        guest = ACPClient(runner.client_uri, "@guest")
        await guest.connect(role="agent")
        await guest.close()
        await asyncio.sleep(0.1)
        leaked = stream.getvalue()
        assert "New connection" not in leaked
        assert "Invalid JSON" not in leaked
        assert "Error handling" not in leaked
        assert "WARNING" not in leaked
        assert "Traceback" not in leaked
    finally:
        await runner.shutdown()
        root.removeHandler(handler)
        root.setLevel(old_root_level)
    assert list(hub_logger.handlers) == before_hub[0]
    assert hub_logger.level == before_hub[1]
    assert hub_logger.propagate is before_hub[2]
    assert list(ws_logger.handlers) == before_ws[0]
    assert ws_logger.level == before_ws[1]
    assert ws_logger.propagate is before_ws[2]


@pytest.mark.dt
async def test_client_mode_detects_external_hub_disconnect(tmp_path):
    hub = ACPHub(port=0, db_path=str(tmp_path / "external.db"))
    await hub.start()
    uri = f"ws://127.0.0.1:{hub.bound_port}"
    buf, ui = _ui()
    runner = UnifiedConsoleRunner(
        uri=uri, db_path=str(tmp_path / "console.db"), ui_console=ui, auto_hub=True,
    )
    try:
        await runner.start()
        assert runner.owns_hub is False
        runner.console.begin_layout(100, 24)
        await hub.stop()
        await asyncio.wait_for(runner.console.watch_connection(), 2)
        assert runner.console.hub_mode == "disconnected"
        assert "disconnected" in runner.console.render_header_lines(100)[0]
        assert "disconnected" in buf.getvalue().lower()
    finally:
        await runner.shutdown()
        await hub.stop()


@pytest.mark.dt
async def test_wire_order_initial_sync_into_top_header(tmp_path):
    async def reply(ws):
        async for raw in ws:
            request = json.loads(raw)
            action = request.get("action")
            if action == "ping":
                await ws.send(json.dumps({"req_id": request.get("req_id"), "event": "pong"}))
                continue
            if action == "register":
                await ws.send(json.dumps({
                    "req_id": request["req_id"], "status": "ok", "handle": request["handle"],
                }))
                continue
            if action == "roster":
                await ws.send(json.dumps({"event": "presence_change", "handle": "@claude", "status": "busy"}))
                await ws.send(json.dumps({
                    "req_id": request["req_id"], "action": "roster", "status": "ok",
                    "roster": [
                        {"handle": "@claude", "status": "idle"},
                        {"handle": "@codex", "status": "idle"},
                    ],
                }))
                await ws.send(json.dumps({"event": "presence_change", "handle": "@codex", "status": "offline"}))

    buf, ui = _ui()
    async with serve(reply, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        runner = UnifiedConsoleRunner(
            uri=f"ws://127.0.0.1:{port}",
            db_path=str(tmp_path / "wire.db"),
            ui_console=ui,
            auto_hub=True,
        )
        try:
            await runner.start()
            await asyncio.sleep(0.1)
            assert runner.owns_hub is False
            assert runner.console.roster_state.get("@claude") == "idle"
            assert "@codex" not in runner.console.roster_state
            header = runner.console.render_header_lines(100)[0]
            assert "@claude" in header
            assert "@codex" not in header
            assert "Active Agent Roster" not in buf.getvalue()
        finally:
            await runner.shutdown()
