import asyncio
import io
import json
import os
import re
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
        # Split an emoji across the 256-unit drain boundary; keep key-like words literal.
        long_text = "中" * 249 + "😀" + "中文LEFT" * 300
        for text in ("中文😀🚀", long_text, "still typing"):
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
            await asyncio.wait_for(console.sync_roster(display_table=False), 5)
            assert "Roster" not in console._out_console.file.getvalue()
            assert "Online (1): ● @claude IDLE" in console.render_header_lines(90)[0]
            assert console.format_online_agents_bar() == "Online Agents (1): @claude [IDLE]"
        finally:
            await console.client.close()

@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows ConPTY")
async def test_unified_console_pty_autostarts_hub_and_receives_agent(tmp_path):
    from winpty import PtyProcess
    from acp.console import probe_hub, ProbeResult
    from acp.protocol import MessageMode

    proc = PtyProcess.spawn(
        [sys.executable, "-m", "acp.cli", "hub", "--port", "0", "--db", str(tmp_path / "pty.db")],
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    screen = ""
    async def expect(pattern):
        async def read_until_match():
            nonlocal screen
            while True:
                # Rich and ConPTY may insert SGR changes inside the URI or artwork.
                plain = re.sub(r"\x1b\[[0-9;:]*m", "", screen)
                if match := re.search(pattern, plain):
                    return match
                screen += await asyncio.to_thread(proc.read)
        try:
            return await asyncio.wait_for(read_until_match(), 10)
        except asyncio.TimeoutError:
            pytest.fail(f"Missing {pattern!r} in ConPTY output {screen[-4000:]!r}")

    agent = ACPClient(handle="@codex")
    try:
        banner = await expect(r"Hub: Embedded (ws://127\.0\.0\.1:\d+) ")
        agent.uri = banner[1]
        assert "Draft Input" in screen and "@user::>" in screen
        await agent.connect()
        await agent.send_message("@user", "PTY delivery confirmed", MessageMode.EXPLICIT)
        await expect("PTY delivery confirmed")
        await expect("╰▀─▀╯")
        assert "@codex" in screen and "Presence:" not in screen
        proc.write("/quit\r")
        await asyncio.wait_for(asyncio.to_thread(proc.wait), 3)
        assert proc.exitstatus == 0
        assert await probe_hub(agent.uri) == ProbeResult.PORT_FREE
    finally:
        proc.close(force=True)
        await agent.close()
