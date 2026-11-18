import asyncio
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from acp.client import ACPClient
from acp.hub import ACPHub
from acp.protocol import MessageMode


@pytest.mark.dt
@pytest.mark.skipif(sys.platform != "win32" or os.environ.get("ACP_TEST_CODEX") != "1",
                    reason="requires Windows ConPTY and the real Codex CLI")
async def test_interactive_codex_submits_relay_21_verbatim(tmp_path):
    from winpty import PtyProcess

    essay = (Path(__file__).parent / "fixtures" / "relay_21.txt").read_text("utf-8")
    loop = asyncio.get_running_loop()
    received = asyncio.Queue()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            item = body["input"][-1]
            if item.get("role") == "user":
                text = "".join(part.get("text", "") for part in item["content"])
                if text.startswith("@leader:"):
                    loop.call_soon_threadsafe(received.put_nowait, text)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"type":"response.completed","response":{"id":"test","status":"completed"}}\n\n')

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        config = tmp_path / "config"
        config.mkdir()
        (config / "config.toml").write_text(
            'check_for_update_on_startup=false\nmodel="gpt-5"\nmodel_provider="localtest"\n'
            '[model_providers.localtest]\nname="localtest"\nwire_api="responses"\n'
            f'base_url="http://127.0.0.1:{server.server_port}/v1"\n'
            f'[projects.{json.dumps(str(tmp_path))}]\ntrust_level="trusted"\n', encoding="utf-8",
        )
        hub = ACPHub(port=0, db_path=str(tmp_path / "hub.db"))
        await hub.start()
        uri = f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}"
        sender = ACPClient(uri)
        proc = None
        try:
            proc = PtyProcess.spawn(
                [sys.executable, "-m", "acp.cli", "launch", "codex", "--as", "worker", "--uri", uri],
                cwd=str(tmp_path), dimensions=(34, 108),
                env={**os.environ, "CODEX_HOME": str(config), "OPENAI_API_KEY": "local-test",
                     "PYTHONPATH": str(Path(__file__).resolve().parents[1]), "PYTHONUTF8": "1",
                     "TERM": "xterm-256color"},
            )

            def emulate_terminal():
                tail = ""
                try:
                    while proc.isalive():
                        data = tail + proc.read()
                        # Emulate cursor reports only. The host never presses Enter.
                        for _ in range(data.count("\x1b[6n")):
                            proc.write("\x1b[30;3R")
                        tail = data[-3:]
                except (EOFError, OSError):
                    pass

            Thread(target=emulate_terminal, daemon=True).start()

            async def wait_for_worker():
                while "@worker" not in hub.clients:
                    assert proc.isalive()
                    await asyncio.sleep(.05)

            await asyncio.wait_for(wait_for_worker(), 40)
            for content in ("ready", essay, "after relay"):
                result = await sender.send_message("@worker", content, MessageMode.EXPLICIT, sender="@leader")
                assert result["delivered"]
                assert await asyncio.wait_for(received.get(), 30) == "@leader: " + content
        finally:
            if proc is not None:
                proc.close(force=True)
            await sender.close()
            await hub.stop()
            server.shutdown()
