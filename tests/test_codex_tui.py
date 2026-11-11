"""A real Codex TUI must submit the complete paste to a loopback-only model provider."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from threading import Event, Thread

import pytest

from acp.launcher import AgentLauncher


@pytest.mark.dt
@pytest.mark.skipif(os.environ.get("ACP_TEST_CODEX") != "1", reason="requires the pinned Codex CLI")
def test_real_codex_submits_unicode_multiline_turn(tmp_path, monkeypatch):
    requests = Queue()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.put(json.loads(body))
            self.send_error(400, "Test request recorded")

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
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("CODEX_HOME", str(config))
        monkeypatch.setenv("TERM", "xterm-256color")
        monkeypatch.setenv("OPENAI_API_KEY", "local-test")
        launcher = AgentLauncher("codex --no-daemon", "@worker", interactive=False)
        launcher._spawn()
        proc = launcher.pty_proc
        ready = Event()

        def read_terminal():
            tail = ""
            try:
                while True:
                    tail += proc.read()
                    if "\x1b[6n" in tail:
                        proc.write("\x1b[1;1R")
                        tail = tail.replace("\x1b[6n", "")
                    if "]7501;state=idle" in tail:
                        ready.set()
                    tail = tail[-128:]
            except (EOFError, OSError):
                pass

        Thread(target=read_terminal, daemon=True).start()
        try:
            assert ready.wait(45), "Codex did not reach the idle composer"
            prompt = "@tester: 中文 first line\nsecond line"
            launcher.inject_prompt(prompt)
            body = requests.get(timeout=15)
            user_texts = [
                part.get("text") for item in body["input"] if item.get("role") == "user"
                for part in item["content"]
            ]
            assert prompt in user_texts
        finally:
            proc.close(force=True)
            server.shutdown()
