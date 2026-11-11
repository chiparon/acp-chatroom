"""A real Codex TUI must submit the complete paste to a loopback-only model provider."""
import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Event, Thread

import pytest

from acp.client import ACPClient
from acp.hub import ACPHub
from acp.launcher import AgentLauncher
from acp.protocol import EventType, MessageMode


@pytest.mark.dt
@pytest.mark.skipif(os.environ.get("ACP_TEST_CODEX") != "1", reason="requires the pinned Codex CLI")
async def test_codex_process_isolation_and_resume(tmp_path, monkeypatch):
    requests = Queue()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            items = json.loads(body)["input"]
            output = []
            texts = [part.get("text", "") for item in items if item.get("role") == "user"
                     for part in item["content"]]
            if items[-1].get("role") == "user" and texts[-1].startswith("@tester: "):
                requests.put((self.headers["thread-id"], texts))
                # An actual shell reply exercises the handle and hub URI inherited by this session.
                output.append({"type": "function_call", "name": "exec_command", "call_id": "call_ack",
                               "arguments": json.dumps({"cmd": "acp-chat send --to tester --mode explicit ack"})})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = [{"type": "response.output_item.done", "item": item} for item in output]
            events.append({"type": "response.completed", "response": {"id": "resp_test", "status": "completed"}})
            for event in events:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        config = tmp_path / "config"
        config.mkdir()
        (config / "config.toml").write_text(
            'model = "gpt-5"\nmodel_provider = "localtest"\n'
            '[model_providers.localtest]\nname = "localtest"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            f'[projects.{json.dumps(str(tmp_path))}]\ntrust_level = "trusted"\n', "utf-8",
        )
        hub = ACPHub(port=0, db_path=str(tmp_path / "hub.db"))
        await hub.start()
        uri = f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}"
        sender = ACPClient(uri, "@tester")
        replies = asyncio.Queue()
        sender.on(EventType.EXPLICIT_MESSAGE.value, replies.put_nowait)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CODEX_HOME", str(config))
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.setenv("OPENAI_API_KEY", "local-test")
        processes = []

        async def start(handle, session_id=None):
            launcher = AgentLauncher("codex", handle, uri, interactive=False, session_id=session_id)
            ready = Event()
            screen = []

            def read_terminal():
                proc = launcher.pty_proc
                tail = ""
                try:
                    while True:
                        chunk = proc.read()
                        tail += chunk
                        screen.append(chunk)
                        if "\x1b[6n" in tail:
                            proc.write("\x1b[1;1R")
                            tail = tail.replace("\x1b[6n", "")
                        if "]7501;state=idle" in tail:
                            ready.set()
                        tail = tail[-128:]
                except (EOFError, OSError):
                    pass

            monkeypatch.setattr(launcher, "_output_reader_pty", read_terminal)
            task = asyncio.create_task(launcher.run())
            processes.append((launcher, task))
            assert await asyncio.to_thread(ready.wait, 40), repr("".join(screen)[-2000:])
            return launcher, task

        async def turn(handle, text):
            assert (await sender.send_message(handle, text, MessageMode.EXPLICIT))["delivered"]
            thread_id, texts = await asyncio.to_thread(requests.get, True, 20)
            assert texts[-1] == f"@tester: {text}"
            reply = (await asyncio.wait_for(replies.get(), 30))["message"]
            assert (reply["sender"], reply["content"]) == (handle, "ack")
            return thread_id, texts

        try:
            await sender.connect()
            first, first_task = await start("@gpt1")
            await start("@gpt2")
            await start("@gpt3")
            marker = "中文 first line\nsecond line"
            original_id, _ = await turn("@gpt1", marker)
            # Exit this terminal and wait for the real launcher connection to disappear.
            first.pty_proc.close(force=True)
            await asyncio.wait_for(asyncio.shield(first_task), 15)
            assert not (await sender.send_message("@gpt1", "offline", MessageMode.EXPLICIT))["delivered"]
            second_id, _ = await turn("@gpt2", "second survives")
            third_id, _ = await turn("@gpt3", "third survives")
            assert len({original_id, second_id, third_id}) == 3
            recalled, _ = await start(None, session_id=original_id)
            duplicate = AgentLauncher("codex", "@other", uri, interactive=False, session_id=original_id)
            await asyncio.wait_for(duplicate.run(), 5)
            assert duplicate.pty_proc is None and duplicate.handle == recalled.handle
            resumed_id, history = await turn(recalled.handle, "recall")
            assert resumed_id == original_id
            assert f"@tester: {marker}" in history
            roster = {r["handle"]: r["status"] for r in await sender.get_roster()}
            assert roster["@gpt1"] == "offline"
            assert all(roster[h] in {"idle", "busy"} for h in ("@gpt2", "@gpt3", recalled.handle))
        finally:
            for launcher, task in processes:
                launcher._running = False
            await asyncio.gather(*(task for _, task in processes))
            await sender.close()
            await hub.stop()
            server.shutdown()
