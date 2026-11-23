import asyncio
import io
import json
import os
import socket
import sys

import pytest
from rich.console import Console
from websockets.server import serve

from acp.avatars import resolve_avatar
from acp.client import ACPClient
from acp.console import HumanConsole, ProbeStatus, probe_hub
from acp.hub import ACPHub
from acp.protocol import EventType, MessageMode


pytestmark = pytest.mark.dt


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn_pty(argv: list[str], env: dict[str, str]):
    if sys.platform == "win32":
        from winpty import PtyProcess
        return PtyProcess.spawn(argv, env=env, dimensions=(24, 100))
    from ptyprocess import PtyProcessUnicode
    return PtyProcessUnicode.spawn(argv, env=env, dimensions=(24, 100))


# TC-E2E-01
async def test_unified_console_pty_autostarts_hub_and_receives_agent(tmp_path):
    port = _free_tcp_port()
    db_path = str(tmp_path / "e2e_unified.db")
    uri = f"ws://127.0.0.1:{port}"
    proc = None
    agent = ACPClient(uri, "@codex")
    try:
        proc = _spawn_pty(
            [sys.executable, "-m", "acp.cli", "hub", "--port", str(port), "--db", db_path],
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        screen = ""

        async def wait_for_text(target_substrings: tuple[str, ...]):
            nonlocal screen
            while not all(sub in screen for sub in target_substrings):
                chunk = await asyncio.to_thread(proc.read)
                if not chunk:
                    await asyncio.sleep(0.02)
                    continue
                screen += chunk

        await asyncio.wait_for(wait_for_text(("Draft Input", "@user::>")), timeout=12)

        await agent.connect()
        await agent.send_message(
            target="@user",
            content="e2e unified message from codex",
            mode=MessageMode.EXPLICIT,
        )

        codex_avatar = resolve_avatar("@codex")
        await asyncio.wait_for(
            wait_for_text(("@codex", "e2e unified message from codex", codex_avatar.lines[0].strip())),
            timeout=10,
        )
        assert "Presence: @codex is now" not in screen

        proc.write("/quit\r")

        async def wait_for_exit():
            nonlocal screen
            while proc.isalive():
                try:
                    chunk = await asyncio.to_thread(proc.read)
                    if chunk:
                        screen += chunk
                        continue
                except (EOFError, OSError):
                    pass
                await asyncio.sleep(0.05)

        await asyncio.wait_for(wait_for_exit(), timeout=8)
        assert not proc.isalive()
        assert await probe_hub(uri, timeout=0.6) == ProbeStatus.PORT_FREE
    finally:
        await agent.close()
        if proc is not None and proc.isalive():
            try:
                proc.close(force=True)
            except TypeError:
                proc.terminate(force=True)


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows console input")
async def test_console_posts_emoji_and_keeps_accepting_commands(tmp_path):
    from winpty import PtyProcess

    hub = ACPHub(port=0, db_path=str(tmp_path / "hub.db"))
    await hub.start()
    uri = f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}"
    observer = ACPClient(uri, "@observer")
    posts = asyncio.Queue()
    observer.on(EventType.SILENT_MESSAGE.value, posts.put_nowait)
    proc = None
    try:
        await observer.connect(role="console")
        proc = PtyProcess.spawn(
            [sys.executable, "-m", "acp.cli", "console", "--uri", uri],
            env={**os.environ, "PYTHONUTF8": "1"},
        )

        async def wait_for_dock():
            screen = ""
            while "Draft Input" not in screen:
                screen += await asyncio.to_thread(proc.read)

        await asyncio.wait_for(wait_for_dock(), 10)
        for text in ("中文😀🚀", "still typing"):
            proc.write(f"/post {text}\r")
            event = await asyncio.wait_for(posts.get(), 5)
            assert event["message"]["content"] == text
    finally:
        if proc is not None:
            proc.close(force=True)
        await observer.close()
        await hub.stop()


async def test_roster_refresh_and_presence_follow_wire_order(monkeypatch):
    async def reply(ws):
        async for raw in ws:
            request = json.loads(raw)
            if request["action"] == "register":
                await ws.send(json.dumps({"req_id": request["req_id"], "status": "ok", "handle": request["handle"]}))
                continue
            assert request["action"] == "roster"
            await ws.send(json.dumps({"event": "presence_change", "handle": "@claude", "status": "busy"}))
            await ws.send(json.dumps({
                "req_id": request["req_id"], "action": "roster", "status": "ok",
                "roster": [{"handle": "@claude", "status": "idle"}, {"handle": "@codex", "status": "idle"}],
            }))
            await ws.send(json.dumps({"event": "presence_change", "handle": "@codex", "status": "offline"}))

    async with serve(reply, "127.0.0.1", 0) as server:
        uri = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        console = HumanConsole(uri=uri, ui_console=Console(file=io.StringIO()))
        await console.client.connect(role="console")
        offline = asyncio.Event()

        def presence(data):
            console.on_presence_change(data)
            if data["status"] == "offline":
                offline.set()

        console.client.on(EventType.PRESENCE_CHANGE.value, presence)
        get_roster = console.client.get_roster

        async def get_roster_before_resume(**kwargs):
            roster = await get_roster(**kwargs)
            # Reproduce both frames being consumed before show_roster resumes.
            await offline.wait()
            return roster

        monkeypatch.setattr(console.client, "get_roster", get_roster_before_resume)
        try:
            await asyncio.wait_for(console.show_roster(), 5)
            assert console.format_online_agents_bar() == "Online Agents (1): @claude [IDLE]"
        finally:
            await console.client.close()
