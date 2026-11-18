import asyncio
import io
import json
import os
import sys

import pytest
from rich.console import Console
from websockets.server import serve

from acp.client import ACPClient
from acp.console import HumanConsole
from acp.hub import ACPHub
from acp.protocol import EventType


pytestmark = pytest.mark.dt


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
