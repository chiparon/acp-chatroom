"""
ACP Client - Multiplexed Library for Agents & CLI Tools to Connect to ACP Hub
"""

import asyncio
import json
import uuid
from typing import Optional, Dict, Any, List, Callable
import websockets
from websockets.client import WebSocketClientProtocol

from .protocol import (
    Message,
    MessageMode,
    AgentStatus,
    ActionType,
    EventType,
    SessionKey,
)


class ACPClient:
    def __init__(self, uri: str = "ws://127.0.0.1:8765", handle: Optional[str] = None):
        self.uri = uri
        self.handle = handle if not handle or handle.startswith("@") else f"@{handle}"
        self.ws: Optional[WebSocketClientProtocol] = None
        self._pending_requests: Dict[str, asyncio.Future] = {}
        self.supports_model_switch = False
        self._listener_task: Optional[asyncio.Task] = None
        self.event_handlers: Dict[str, List[Callable[[Dict[str, Any]], Any]]] = {
            EventType.EXPLICIT_MESSAGE.value: [],
            EventType.SILENT_MESSAGE.value: [],
            EventType.PRESENCE_CHANGE.value: [],
            "any": [],
        }

    async def connect(self, initial_status: AgentStatus = AgentStatus.IDLE, role: str = "agent") -> None:
        self.ws = await websockets.connect(self.uri)
        # Start background receiver loop to demultiplex events and req_id responses
        self._listener_task = asyncio.create_task(self._reader_loop())
        if self.handle:
            try:
                await self.register(self.handle, initial_status, role=role)
            except BaseException:
                await self.close()
                raise

    async def close(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            self._listener_task = None
        if self.ws:
            await self.ws.close()
            self.ws = None

    @property
    def connected(self) -> bool:
        return self._listener_task is not None and not self._listener_task.done()

    async def claim(self, harness: str, preferred: Optional[str], session: Optional[SessionKey]) -> dict:
        # A one-shot request would release the reservation as soon as it returned.
        if not self.connected:
            raise RuntimeError("A claim requires a connected client")
        result = await self.request({
            "action": ActionType.CLAIM.value, "harness": harness, "handle": preferred,
            "session": session._asdict() if session else None,
        })
        if result.get("status") != "ok":
            raise ValueError(result.get("error", "Handle claim failed"))
        return result

    def on(self, event_type: str, handler: Callable[[Dict[str, Any]], Any]) -> None:
        if event_type not in self.event_handlers:
            self.event_handlers[event_type] = []
        self.event_handlers[event_type].append(handler)

    def _dispatch_event(self, event_data: Dict[str, Any]) -> None:
        event = event_data.get("event")
        # Call specific handlers
        if event in self.event_handlers:
            for h in self.event_handlers[event]:
                try:
                    res = h(event_data)
                    if asyncio.iscoroutine(res):
                        asyncio.create_task(res)
                except Exception:
                    pass
        # Call 'any' handlers
        for h in self.event_handlers.get("any", []):
            try:
                res = h(event_data)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)
            except Exception:
                pass

    async def _reader_loop(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    data = json.loads(raw)
                    req_id = data.get("req_id")
                    if req_id and req_id in self._pending_requests:
                        fut = self._pending_requests.pop(req_id)
                        if not fut.done():
                            fut.set_result(data)
                    elif "event" in data:
                        self._dispatch_event(data)
                except json.JSONDecodeError:
                    pass
        except (websockets.exceptions.ConnectionClosed, asyncio.CancelledError):
            pass
        finally:
            # Cancel any pending futures
            for fut in self._pending_requests.values():
                if not fut.done():
                    fut.cancel()
            self._pending_requests.clear()

    async def request(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        req_id = uuid.uuid4().hex[:8]
        payload["req_id"] = req_id

        should_close = False
        if not self.ws:
            self.ws = await websockets.connect(self.uri)
            self._listener_task = asyncio.create_task(self._reader_loop())
            should_close = True

        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending_requests[req_id] = fut

        try:
            await self.ws.send(json.dumps(payload))
            return await fut
        finally:
            self._pending_requests.pop(req_id, None)
            if should_close:
                await self.close()

    async def register(self, handle: str, status: AgentStatus = AgentStatus.IDLE, role: str = "agent") -> Dict[str, Any]:
        payload = {
            "action": ActionType.REGISTER.value,
            "handle": handle if handle.startswith("@") else f"@{handle}",
            "status": status.value if isinstance(status, AgentStatus) else status,
            "role": role,
            "supports_model_switch": self.supports_model_switch,
        }
        result = await self.request(payload)
        if result.get("status") != "ok":
            raise ValueError(result.get("error", "Registration failed"))
        self.handle = result["handle"]
        return result

    async def send_message(
        self,
        target: str,
        content: str,
        mode: MessageMode = MessageMode.SILENT,
        topic: str = "general",
        sender: Optional[str] = None,
    ) -> Dict[str, Any]:
        sender_name = sender or self.handle or "@anonymous"
        if not sender_name.startswith("@"):
            sender_name = f"@{sender_name}"

        payload = {
            "action": ActionType.SEND.value,
            "sender": sender_name,
            "target": target,
            "mode": mode.value if isinstance(mode, MessageMode) else str(mode),
            "topic": topic,
            "content": content,
        }
        return await self.request(payload)

    async def read_board(
        self,
        topic: str = "general",
        reader: Optional[str] = None,
        limit: int = 50,
        unread_only: bool = True,
        update_cursor: bool = True,
    ) -> List[Dict[str, Any]]:
        reader_name = reader or self.handle
        if reader_name and not reader_name.startswith("@"):
            reader_name = f"@{reader_name}"

        payload = {
            "action": ActionType.READ.value,
            "topic": topic,
            "reader": reader_name,
            "limit": limit,
            "unread_only": unread_only,
            "update_cursor": update_cursor,
        }
        resp = await self.request(payload)
        return resp.get("messages", [])

    async def get_roster(self) -> List[Dict[str, Any]]:
        payload = {"action": ActionType.ROSTER.value}
        resp = await self.request(payload)
        return resp.get("roster", [])

    async def set_presence(self, status: AgentStatus) -> Dict[str, Any]:
        payload = {
            "action": ActionType.PRESENCE.value,
            "handle": self.handle,
            "status": status.value if isinstance(status, AgentStatus) else status,
        }
        return await self.request(payload)


# Synchronous convenience wrappers for one-shot CLI commands
def sync_send(
    uri: str,
    target: str,
    content: str,
    mode: str = "silent",
    topic: str = "general",
    sender: Optional[str] = None,
) -> Dict[str, Any]:
    client = ACPClient(uri=uri, handle=sender)
    m = MessageMode.EXPLICIT if mode == "explicit" else MessageMode.SILENT
    return asyncio.run(client.send_message(target=target, content=content, mode=m, topic=topic, sender=sender))


def sync_read(
    uri: str,
    topic: str = "general",
    reader: Optional[str] = None,
    limit: int = 50,
    unread_only: bool = True,
    update_cursor: bool = True,
) -> List[Dict[str, Any]]:
    client = ACPClient(uri=uri, handle=reader)
    return asyncio.run(client.read_board(topic=topic, reader=reader, limit=limit, unread_only=unread_only, update_cursor=update_cursor))


def sync_roster(uri: str) -> List[Dict[str, Any]]:
    client = ACPClient(uri=uri)
    return asyncio.run(client.get_roster())
