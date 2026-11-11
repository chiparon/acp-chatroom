import asyncio
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from acp.client import ACPClient
from acp.hub import ACPHub
from acp.launcher import PtyProcess
from acp.protocol import EventType, MessageMode


pytestmark = pytest.mark.dt


async def wait_for(check, process):
    async def poll():
        while not check():
            if not process.isalive():
                pytest.fail("Launcher exited before the terminal peer was ready")
            await asyncio.sleep(0.05)
    await asyncio.wait_for(poll(), timeout=20)


async def test_keyboard_and_direct_message_round_trip(tmp_path):
    # Exercise the installed CLI, including an npm-style Windows shim in a path with spaces.
    bin_dir = tmp_path / "agent commands"
    bin_dir.mkdir()
    script = Path(__file__).with_name("mock_agent.py").resolve()
    if os.name == "nt":
        (bin_dir / "codex.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n', "utf-8")
    else:
        shim = bin_dir / "codex"
        shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n', "utf-8")
        shim.chmod(0o755)

    hub = ACPHub(port=0, db_path=str(tmp_path / "hub.db"))
    await hub.start()
    uri = f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}"
    sender = ACPClient(uri, "@tester")
    reply = asyncio.Queue()
    sender.on(EventType.EXPLICIT_MESSAGE.value, reply.put_nowait)
    launcher = None
    try:
        await sender.connect()
        env = {**os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
               "ACP_TEST_DIR": str(tmp_path), "PYTHONUTF8": "1", "TERM": "xterm-256color"}
        launcher = PtyProcess.spawn(
            [sys.executable, "-m", "acp.cli", "launch", "codex", "--as", "@worker", "--uri", uri],
            env=env,
        )
        await wait_for(lambda: (tmp_path / "ready").exists() and "@worker" in hub.clients, launcher)
        # Simulate an operator's IME text, cursor key and Enter through the outer terminal.
        keyboard = "手动😀\x1b[D\r"
        launcher.write(keyboard)
        # A marker received by the child orders keyboard input before WebSocket delivery.
        launcher.write("KEYS_DONE")
        await wait_for(lambda: (tmp_path / "keys").exists(), launcher)
        content = "  第一行\n  second line  "
        response = await sender.send_message("@worker", content, MessageMode.EXPLICIT)
        assert response["delivered"]
        await wait_for(lambda: (tmp_path / "pasted").exists(), launcher)
        launcher.write("END_CAPTURE")
        event = await asyncio.wait_for(reply.get(), timeout=15)
        assert (event["message"]["sender"], event["message"]["content"]) == ("@worker", "received")
        flags, received = json.loads((tmp_path / "input.json").read_text("utf-8"))
        assert flags == ["--dangerously-bypass-approvals-and-sandbox", "--no-daemon"]
        assert received == keyboard + "KEYS_DONE\x1b[200~@tester: " + content + "\x1b[201~\rEND_CAPTURE"
    finally:
        if launcher:
            launcher.close(force=True)
        await sender.close()
        await hub.stop()
