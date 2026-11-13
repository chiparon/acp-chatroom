"""Drive one DeepSeek Harness session over its native ACP stdio connection."""
import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager


class DshRequestError(ValueError):
    """The server rejected one request but the connection is still usable."""


class DshSession:
    def __init__(self, process, interactive):
        self.process = process
        self.interactive = interactive
        self.session_id = None
        self.permission_reply = None
        self.request_id = 0
        self.model_provider = "deepseek-official"

    async def _write(self, message):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", **message}).encode() + b"\n")
        await self.process.stdin.drain()

    async def _permission(self, params):
        if self.interactive:
            options = params["options"]
            print(params["toolCall"].get("title", "DSH permission request"), flush=True)
            for i, option in enumerate(options, 1):
                print(f"  {i}. {option['name']}", flush=True)
            print("Enter a number, or press Enter to cancel:", flush=True)
            self.permission_reply = asyncio.get_running_loop().create_future()
            try:
                answer = await self.permission_reply
                for i, option in enumerate(options, 1):
                    if answer.strip() == str(i):
                        return {"outcome": "selected", "optionId": option["optionId"]}
            finally:
                self.permission_reply = None
        return {"outcome": "cancelled"}

    async def request(self, method, params):
        # The launcher serializes turns. Server requests and updates can arrive
        # before the matching response, even when their numeric IDs coincide.
        self.request_id += 1
        await self._write({"id": self.request_id, "method": method, "params": params})
        while line := await self.process.stdout.readline():
            message = json.loads(line)
            if "method" in message:
                if message["method"] == "session/request_permission":
                    outcome = await self._permission(message["params"])
                    await self._write({"id": message["id"], "result": {"outcome": outcome}})
                elif "id" in message:
                    await self._write({"id": message["id"], "error": {"code": -32601, "message": "Unknown method"}})
                elif message["method"] == "session/update":
                    update = message["params"]["update"]
                    if update["sessionUpdate"] == "agent_message_chunk" and update["content"]["type"] == "text":
                        print(update["content"]["text"], end="", flush=True)
            elif message.get("id") == self.request_id:
                if "error" in message:
                    raise DshRequestError(f"DSH {method}: {message['error']['message']}")
                return message["result"]
        raise OSError(f"DSH closed its protocol stream during {method}")

    @classmethod
    @asynccontextmanager
    async def open(cls, argv, env, session_id, interactive):
        process = await asyncio.create_subprocess_exec(
            *argv, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
        )
        session = cls(process, interactive)
        try:
            await session.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
            params = {"cwd": os.getcwd(), "mcpServers": []}
            if session_id:
                result = await session.request("session/resume", {**params, "sessionId": session_id})
                session.session_id = session_id
            else:
                result = await session.request("session/new", params)
                session.session_id = result["sessionId"]
            for option in result.get("configOptions", []):
                if option["id"] == "model":
                    session.model_provider, _ = json.loads(option["currentValue"])
            yield session
        finally:
            # EOF releases the native session and lets DSH flush its durable log.
            process.stdin.close()
            try:
                await asyncio.wait_for(process.communicate(), 10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def turn(self, event, text):
        try:
            if event == "set_model":
                provider, separator, model = text.partition("/")
                if not separator:
                    provider, model = self.model_provider, text
                await self.request("session/set_config_option", {
                    "sessionId": self.session_id, "configId": "model",
                    "value": json.dumps([provider, model], separators=(",", ":"), ensure_ascii=False),
                })
                self.model_provider = provider
            else:
                await self.request("session/prompt", {
                    "sessionId": self.session_id, "prompt": [{"type": "text", "text": text}],
                })
                print(flush=True)
        except DshRequestError as exc:
            print(exc, file=sys.stderr, flush=True)
