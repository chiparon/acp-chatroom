import asyncio
import io
import logging
from unittest.mock import AsyncMock, Mock

from click.testing import CliRunner
import pytest

from rich.console import Console
import acp.cli as cli
import acp.console as ui
from acp.client import ACPClient
from acp.hub import ACPHub
from acp.console import UnifiedConsoleRunner, probe_hub


def test_cli_routes(monkeypatch):
    runner = CliRunner()
    start, hub = Mock(), Mock()
    monkeypatch.setattr(cli, "start_unified_console", start)
    monkeypatch.setattr(cli, "run_hub", hub)
    monkeypatch.setenv("ACP_HUB_URI", "ws://localhost:9999")
    def invoke(*args):
        result = runner.invoke(cli.cli, args)
        assert result.exit_code == 0, result.exception
        return result.output

    monkeypatch.setattr(cli, "_interactive", lambda: False)
    assert "Usage:" in invoke()
    invoke("hub")
    hub.assert_called_once()
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    invoke()
    assert start.call_args.kwargs["handle"] == "@user"
    invoke("--as", "@alice", "console", "--no-auto-hub")
    assert start.call_args.kwargs == dict(handle="@alice", uri="ws://localhost:9999",
                                         auto_hub=False, db_path="acp_chatroom.db")
    invoke("hub", "--headless")
    assert hub.call_count == 2
    invoke("hub", "--host", "0.0.0.0", "--port", "9001", "--db", "custom.db")
    assert start.call_args.kwargs["uri"] == "ws://127.0.0.1:9001"
    assert start.call_args.kwargs["bind_host"] == "0.0.0.0"

@pytest.mark.parametrize("uri,auto", [("ws://192.0.2.1:8765", True), ("ws://localhost:8765", False)])
async def test_unreachable_does_not_bind(monkeypatch, uri, auto):
    monkeypatch.setattr(ui, "probe_hub", AsyncMock(return_value=ui.ProbeResult.PORT_FREE))
    constructor = Mock()
    monkeypatch.setattr(ui, "ACPHub", constructor)
    with pytest.raises(ValueError, match="No ACP Hub"):
        await UnifiedConsoleRunner(uri=uri, auto_hub=auto).run()
    constructor.assert_not_called()


@pytest.mark.dt
async def test_non_acp_port(monkeypatch):
    async def silent(reader, writer):
        try:
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()

    async with await asyncio.start_server(silent, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(ValueError, match="non-ACP"):
            await asyncio.wait_for(UnifiedConsoleRunner(uri=f"ws://127.0.0.1:{port}").run(), 2)


@pytest.mark.dt
async def test_embedded_shutdown_and_log_restoration(tmp_path, monkeypatch, capsys):
    runner = UnifiedConsoleRunner(uri="ws://127.0.0.1:0", db_path=str(tmp_path / "room.db"))
    peer = ACPClient(handle="@codex")
    logger = logging.getLogger("acp.hub")
    original = logger.handlers, logger.level, logger.propagate

    async def interact(console):
        assert runner.owns_hub and runner.hub.bound_port
        peer.uri = console.uri
        await peer.connect()
        assert "@codex" in console.roster_state
        logger.error("unexpected frame in test")
        assert console.handle_command("/quit") is False

    monkeypatch.setattr(ui.HumanConsole, "_input_loop", interact)
    try:
        await asyncio.wait_for(runner.run(), 2)
        assert not peer.connected
        assert await probe_hub(peer.uri) is ui.ProbeResult.PORT_FREE
        assert (logger.handlers, logger.level, logger.propagate) == original
        assert "unexpected frame in test" not in capsys.readouterr().err
    finally:
        await peer.close()


@pytest.mark.dt
async def test_attaching_preserves_external_hub(tmp_path, monkeypatch):
    hub = ACPHub(port=0, db_path=str(tmp_path / "external.db"))
    await hub.start()
    uri = f"ws://127.0.0.1:{hub.bound_port}"
    runner = UnifiedConsoleRunner(uri=uri)
    monkeypatch.setattr(ui.HumanConsole, "_input_loop", AsyncMock())
    try:
        await runner.run()
        assert not runner.owns_hub
        assert await probe_hub(uri) is ui.ProbeResult.ALIVE_HUB
    finally:
        await hub.stop()


@pytest.mark.dt
async def test_back_keeps_host_and_peer_online_for_rejoin(tmp_path, monkeypatch):
    host = UnifiedConsoleRunner(uri="ws://127.0.0.1:0", db_path=str(tmp_path / "room.db"))
    peer = ACPClient(handle="@peer")
    leaving = asyncio.Event()

    async def leave(console):
        if console.hub_mode == "host":
            peer.uri = console.uri
            await peer.connect()
            leaving.set()
        assert console.handle_command("/back") is False

    monkeypatch.setattr(ui.HumanConsole, "_input_loop", leave)
    task = asyncio.create_task(host.run())
    try:
        await asyncio.wait_for(leaving.wait(), 3)
        async def wait_for_departure():
            while any(row["handle"] == "@user" for row in await peer.get_roster()):
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_for_departure(), 2)
        await asyncio.wait_for(UnifiedConsoleRunner(uri=host.uri).run(), 2)
        assert not task.done()
        assert any(row["handle"] == "@peer" for row in await peer.get_roster())
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await peer.close()
    assert await probe_hub(host.uri) is ui.ProbeResult.PORT_FREE


@pytest.mark.dt
async def test_attached_console_detects_disconnect(tmp_path, monkeypatch):
    hub = ACPHub(port=0, db_path=str(tmp_path / "external.db"))
    await hub.start()
    runner = UnifiedConsoleRunner(uri=f"ws://127.0.0.1:{hub.bound_port}")
    output = io.StringIO()
    monkeypatch.setattr(ui, "console", Console(file=output))
    import threading
    finish = threading.Event()
    monkeypatch.setattr("builtins.input", lambda: finish.wait(5) and "/quit")
    task = asyncio.create_task(runner.run())
    async def wait_for(text):
        while text not in output.getvalue():
            await asyncio.sleep(0.01)
    try:
        await asyncio.wait_for(wait_for("Attached"), 3)
        await hub.stop()
        await asyncio.wait_for(wait_for("Hub disconnected."), 2)
        assert runner.console.hub_mode == "disconnected"
    finally:
        finish.set()
        await asyncio.wait_for(task, 2)
