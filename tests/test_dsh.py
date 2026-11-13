"""Exercise the real DSH CLI against a local Messages API fixture."""
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Thread

import pytest

from acp.client import ACPClient
from acp.hub import ACPHub
from acp.launcher import AgentLauncher
from acp.protocol import MessageMode


@pytest.mark.dt
@pytest.mark.skipif(os.environ.get("ACP_TEST_DSH") != "1", reason="requires DeepSeek Harness CLI")
async def test_dsh_chat_and_resume(tmp_path, monkeypatch, capsys):
    requests = Queue()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.put(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [
                ("message_start", {"message": {"id": "reply", "type": "message", "role": "assistant",
                                               "model": body["model"], "content": [],
                                               "usage": {"input_tokens": 10, "output_tokens": 0}}}),
                ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
                ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "fixture reply"}}),
                ("content_block_stop", {"index": 0}),
                ("message_delta", {"delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
                ("message_stop", {}),
            ]
            for kind, payload in events:
                data = json.dumps({"type": kind, **payload})
                self.wfile.write(f"event: {kind}\ndata: {data}\n\n".encode())

        def log_message(self, *args):
            pass

    async def wait_until(predicate, task):
        async def poll():
            while not predicate():
                if task.done():
                    await task
                    pytest.fail("DSH launcher exited early")
                await asyncio.sleep(.05)
        await asyncio.wait_for(poll(), 60)

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("DSH_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only")
        hub = ACPHub(port=0, db_path=str(tmp_path / "room.db"))
        await hub.start()
        uri = f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}"
        sender = ACPClient(uri, "@tester")
        native_id = None
        try:
            await sender.connect(role="console")
            for content in ("中文\nsecond line", "continue"):
                launcher = AgentLauncher("dsh", None, uri, interactive=False, session_id=native_id)
                run = asyncio.create_task(launcher.run())
                try:
                    await wait_until(lambda: "@dsh1" in hub.clients, run)
                    session = hub.claims[hub.clients["@dsh1"]].session
                    if native_id:
                        assert native_id == session.session_id
                        changed = await sender.request({
                            "action": "set_model", "target": "@dsh1", "model": "deepseek-v4-pro",
                        })
                        assert changed["status"] == "ok"
                    native_id = session.session_id
                    duplicate = AgentLauncher("dsh", "@other", uri, interactive=False, session_id=native_id)
                    await asyncio.wait_for(duplicate.run(), 5)
                    result = await sender.send_message("@dsh1", content, MessageMode.EXPLICIT)
                    assert result["delivered"]
                    body = await asyncio.to_thread(requests.get, True, 60)
                    history = json.dumps(body["messages"], ensure_ascii=False)
                    assert "@tester: 中文\\nsecond line" in history
                    if content == "continue":
                        assert "@tester: continue" in history and "fixture reply" in history
                        assert body["model"] == "deepseek-v4-pro"
                    await wait_until(lambda: "fixture reply" in capsys.readouterr().out, run)
                finally:
                    launcher._running = False
                    await asyncio.wait_for(run, 20)
                await wait_for_disconnect(hub)
        finally:
            await sender.close()
            await hub.stop()
            server.shutdown()


async def wait_for_disconnect(hub):
    async def poll():
        while "@dsh1" in hub.clients:
            await asyncio.sleep(.05)
    await asyncio.wait_for(poll(), 5)
