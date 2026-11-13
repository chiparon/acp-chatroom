"""
ACP Hub - Central WebSocket Server & Router for Multi-Agent Communication
"""

import asyncio
import json
import logging
import re
from typing import Dict, Any, NamedTuple, Optional, Set
import websockets
from websockets.server import WebSocketServerProtocol, serve

from .protocol import (
    Message,
    MessageMode,
    AgentStatus,
    ActionType,
    EventType,
    SessionKey,
    valid_model_id,
)
from .storage import Storage

logger = logging.getLogger("acp.hub")


class Claim(NamedTuple):
    handle: str
    session: Optional[SessionKey]


class ACPHub:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, db_path: str = "acp_chatroom.db"):
        self.host = host
        self.port = port
        self.storage = Storage(db_path)
        self.clients: Dict[str, WebSocketServerProtocol] = {}  # norm_handle (lower) -> ws
        self.ws_to_handle: Dict[WebSocketServerProtocol, str] = {}  # ws -> handle
        # Claims last for the owning socket's lifetime, including during native startup.
        self.claims: Dict[WebSocketServerProtocol, Claim] = {}
        self.console_sockets: Set[WebSocketServerProtocol] = set()
        self.all_sockets: Set[WebSocketServerProtocol] = set()
        self.model_sockets: Set[WebSocketServerProtocol] = set()
        self.server = None
        self._running = False

    async def broadcast_event(self, event_data: Dict[str, Any], exclude: Optional[WebSocketServerProtocol] = None) -> dict[str, bool]:
        payload = json.dumps(event_data)
        # Only broadcast events to registered clients or console clients
        targets = {ws: handle for ws, handle in self.ws_to_handle.items() if ws != exclude}
        agents = targets.keys() - self.console_sockets
        results = await asyncio.gather(*(self._safe_send(ws, payload) for ws in targets))
        return {targets[ws]: sent for ws, sent in zip(targets, results) if ws in agents}

    async def _safe_send(self, ws: WebSocketServerProtocol, payload: str) -> bool:
        try:
            await ws.send(payload)
            return True
        except Exception as e:
            logger.debug(f"Failed to send to client: {e}")
            return False

    async def handle_connection(self, websocket: WebSocketServerProtocol) -> None:
        self.all_sockets.add(websocket)
        logger.info(f"New connection from {websocket.remote_address}")

        try:
            async for raw_message in websocket:
                req_id = None
                try:
                    data = json.loads(raw_message)
                    action = data.get("action")
                    req_id = data.get("req_id")
                    response = await self.process_action(websocket, action, data)
                    if response is not None:
                        if req_id is not None:
                            response["req_id"] = req_id
                        await websocket.send(json.dumps(response))
                except websockets.exceptions.ConnectionClosed:
                    break
                except json.JSONDecodeError:
                    await self._safe_send(
                        websocket,
                        json.dumps({"event": EventType.ERROR.value, "error": "Invalid JSON format"}),
                    )
                except Exception as e:
                    logger.exception(f"Error handling request: {e}")
                    await self._safe_send(
                        websocket,
                        json.dumps({"event": EventType.ERROR.value, "req_id": req_id, "error": str(e)}),
                    )
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            await self._cleanup_connection(websocket)

    async def _cleanup_connection(self, websocket: WebSocketServerProtocol) -> None:
        self.model_sockets.discard(websocket)
        self.claims.pop(websocket, None)
        self.all_sockets.discard(websocket)
        self.console_sockets.discard(websocket)
        handle = self.ws_to_handle.pop(websocket, None)
        if handle and self.clients.get(handle.lower()) is websocket:
            self.clients.pop(handle.lower(), None)
            self.storage.update_roster(handle, AgentStatus.OFFLINE.value)
            logger.info(f"Agent {handle} disconnected and marked offline")
            await self.broadcast_event({
                "event": EventType.PRESENCE_CHANGE.value,
                "handle": handle,
                "status": AgentStatus.OFFLINE.value,
            })

    def _claim(self, ws: WebSocketServerProtocol, data: dict) -> dict:
        harness = data.get("harness")
        if not isinstance(harness, str) or not re.fullmatch(r"[\w.-]+", harness):
            raise ValueError("Expected a harness name")
        session = None
        if data.get("session") is not None:
            session = SessionKey(**data["session"])
            if session.harness != harness or not all(isinstance(v, str) and v for v in session):
                raise ValueError("Invalid session identity")

        if ws in self.ws_to_handle or ws in self.claims:
            raise ValueError("Connection already has a registration or claim")
        # No await between checking and reserving. Concurrent requests on this Hub
        # cannot both win, even before the first launcher has spawned its process.
        if session:
            for owner, claim in self.claims.items():
                if claim.session == session:
                    state = "online" if owner in self.ws_to_handle else "starting"
                    return {"status": "ok", "claimed": False, "handle": claim.handle, "state": state}

        occupied = self.clients.keys() | {claim.handle.lower() for claim in self.claims.values()}
        preferred = data.get("handle")
        previous = self.storage.previous_handle(session) if session else None
        for candidate in (preferred, previous):
            if candidate:
                handle = "@" + candidate.strip().lstrip("@")
                if handle != "@" and handle.lower() not in occupied:
                    break
        else:
            # Keep unused historical roster names available to returning sessions.
            used = occupied | {row["handle"].lower() for row in self.storage.get_roster()}
            number = 1
            while f"@{harness}{number}".lower() in used:
                number += 1
            handle = f"@{harness}{number}"
        self.claims[ws] = Claim(handle, session)
        return {"status": "ok", "claimed": True, "handle": handle, "state": "starting"}

    async def process_action(
        self, websocket: WebSocketServerProtocol, action: Optional[str], data: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if not action:
            return {"event": EventType.ERROR.value, "error": "Missing 'action' parameter"}

        if action == ActionType.CLAIM.value:
            try:
                return self._claim(websocket, data)
            except (TypeError, ValueError) as exc:
                return {"status": "error", "error": str(exc)}

        if action in (ActionType.TASK_CLAIM.value, ActionType.TASK_HANDOFF.value, ActionType.TASK_LIST.value):
            topic = data.get("topic", "general").strip()
            if not topic:
                return {"status": "error", "error": "Expected a topic"}
            if action == ActionType.TASK_LIST.value:
                return {"status": "ok", "tasks": self.storage.list_tasks(topic)}
            task = data.get("task", "").strip()
            handle = (self.ws_to_handle.get(websocket) or data.get("sender") or "").strip().lstrip("@")
            if not task or not handle:
                return {"status": "error", "error": "Expected a task name and claimant handle"}
            if action == ActionType.TASK_CLAIM.value:
                claimed, owner = self.storage.claim_task(topic, task, f"@{handle}")
                return {"status": "ok", "claimed": claimed, "owner": owner}
            successor = data.get("to", "").strip().lstrip("@")
            if not successor:
                return {"status": "error", "error": "Expected the next owner's handle"}
            changed, owner = self.storage.handoff_task(topic, task, f"@{handle}", f"@{successor}")
            return {"status": "ok", "changed": changed, "owner": owner}

        # 1. REGISTER
        if action == ActionType.REGISTER.value:
            handle = data.get("handle", "").strip()
            if not handle.startswith("@"):
                handle = f"@{handle}"
            status = data.get("status", AgentStatus.IDLE.value)
            role = data.get("role", "agent")

            norm_handle = handle.lower()
            claim = self.claims.get(websocket)
            if claim and claim.handle.lower() != norm_handle:
                return {"status": "error", "error": f"Connection reserved {claim.handle}"}
            owner = self.clients.get(norm_handle)
            if owner is not None and owner is not websocket:
                return {"status": "error", "error": f"Handle {handle} is already online"}
            if not claim and any(c.handle.lower() == norm_handle for c in self.claims.values()):
                return {"status": "error", "error": f"Handle {handle} is already reserved"}
            registered = self.ws_to_handle.get(websocket)
            if registered and registered.lower() != norm_handle:
                return {"status": "error", "error": f"Connection already registered as {registered}"}
            handle = registered or handle
            # A protocol-driven harness learns a fresh session's ID after spawning.
            if data.get("session") is not None:
                try:
                    session = SessionKey(**data["session"])
                except TypeError:
                    return {"status": "error", "error": "Invalid session identity"}
                if not all(isinstance(v, str) and v for v in session):
                    return {"status": "error", "error": "Invalid session identity"}
                if claim and claim.session and claim.session != session:
                    return {"status": "error", "error": "Connection reserved a different session"}
                if any(c.session == session for ws, c in self.claims.items() if ws != websocket):
                    return {"status": "error", "error": "Session is already claimed"}
                claim = self.claims[websocket] = Claim(handle, session)
            self.storage.update_roster(handle, status, claim.session if claim else None)
            self.clients[norm_handle] = websocket
            self.ws_to_handle[websocket] = handle
            if data.get("supports_model_switch") is True:
                self.model_sockets.add(websocket)
            else:
                self.model_sockets.discard(websocket)

            if role == "console" or norm_handle in ("@user", "@human", "@console", "@observer"):
                self.console_sockets.add(websocket)

            logger.info(f"Registered {role}: {handle} (status: {status})")

            # Broadcast presence change
            await self.broadcast_event(
                {
                    "event": EventType.PRESENCE_CHANGE.value,
                    "handle": handle,
                    "status": status,
                },
                exclude=websocket,
            )
            return {"status": "ok", "action": "register", "handle": handle}

        if action == ActionType.SET_MODEL.value:
            model = data.get("model")
            if not valid_model_id(model):
                return {"status": "error", "error": "Expected a single model ID"}
            target = "@" + str(data.get("target", "")).strip().lstrip("@")
            ws = self.clients.get(target.lower())
            if ws is None or ws.closed:
                return {"status": "error", "error": f"{target} is offline"}
            if ws not in self.model_sockets:
                return {"status": "error", "error": f"{target} does not support remote model selection"}
            sent = await self._safe_send(ws, json.dumps({"event": EventType.SET_MODEL.value, "model": model}))
            return {"status": "ok"} if sent else {"status": "error", "error": "Launcher disconnected"}

        # 2. SEND
        elif action == ActionType.SEND.value:
            sender = data.get("sender") or self.ws_to_handle.get(websocket) or "@anonymous"
            if not sender.startswith("@"):
                sender = f"@{sender}"
            target = data.get("target", "").strip()
            mode_str = data.get("mode", "silent")
            mode = MessageMode(mode_str)
            topic = data.get("topic", "general").strip()
            content = data.get("content", "")
            if mode in (MessageMode.BROADCAST, MessageMode.URGENT):
                if not isinstance(content, str) or not content.strip():
                    return {"status": "error", "error": "Expected broadcast text"}
                target = "#all"

            msg = Message(
                sender=sender,
                target=target,
                mode=mode,
                topic=topic,
                content=content,
            )
            saved = self.storage.save_message(msg)
            msg_dict = saved.to_dict()

            if mode in (MessageMode.BROADCAST, MessageMode.URGENT):
                exclude = self.clients.get(sender.lower()) if mode == MessageMode.BROADCAST else None
                results = await self.broadcast_event({"event": EventType.BROADCAST.value, "message": msg_dict},
                                                     exclude=exclude)
                return {"status": "ok", "message_id": saved.id, "sent": sum(results.values()),
                        "failed": [handle for handle, sent in results.items() if not sent]}
            if mode == MessageMode.EXPLICIT:
                # Direct route ONLY to the targeted agent
                norm_target = target if target.startswith("@") else f"@{target}"
                target_ws = self.clients.get(norm_target.lower())
                delivered = False
                if target_ws and not target_ws.closed:
                    delivered = await self._safe_send(
                        target_ws,
                        json.dumps({
                            "event": EventType.EXPLICIT_MESSAGE.value,
                            "message": msg_dict,
                        }),
                    )

                # Forward ONLY to human consoles for observation (NEVER to peer agents!)
                if self.console_sockets:
                    payload = json.dumps({
                        "event": EventType.EXPLICIT_MESSAGE.value,
                        "message": msg_dict,
                        "delivered": delivered,
                    })
                    console_targets = [ws for ws in self.console_sockets if ws != target_ws and ws != websocket]
                    if console_targets:
                        await asyncio.gather(*[self._safe_send(ws, payload) for ws in console_targets], return_exceptions=True)

                return {
                    "status": "ok",
                    "action": "send",
                    "message_id": saved.id,
                    "delivered": delivered,
                }
            else:
                # Silent mode: broadcast to all active listeners/consoles
                await self.broadcast_event({
                    "event": EventType.SILENT_MESSAGE.value,
                    "message": msg_dict,
                })
                return {"status": "ok", "action": "send", "message_id": saved.id}

        # 3. READ
        elif action == ActionType.READ.value:
            reader = data.get("reader") or self.ws_to_handle.get(websocket)
            topic = data.get("topic", "general")
            limit = int(data.get("limit", 50))
            unread_only = bool(data.get("unread_only", True))
            update_cursor = bool(data.get("update_cursor", True))

            messages = self.storage.read_topic_messages(
                topic=topic,
                reader=reader,
                unread_only=unread_only,
                limit=limit,
                update_cursor=update_cursor,
            )
            return {
                "status": "ok",
                "action": "read",
                "topic": topic,
                "count": len(messages),
                "messages": [m.to_dict() for m in messages],
            }

        # 4. ROSTER
        elif action == ActionType.ROSTER.value:
            roster = self.storage.get_roster()
            return {"status": "ok", "action": "roster", "roster": roster}

        # 5. PRESENCE
        elif action == ActionType.PRESENCE.value:
            handle = self.ws_to_handle.get(websocket)
            requested = data.get("handle") or handle
            if not handle or requested.lstrip("@").lower() != handle[1:].lower():
                return {"status": "error", "error": "Presence requires the registered handle"}
            status = data.get("status", AgentStatus.IDLE.value)
            self.storage.update_roster(handle, status)
            await self.broadcast_event({
                "event": EventType.PRESENCE_CHANGE.value,
                "handle": handle,
                "status": status,
            })
            return {"status": "ok", "action": "presence"}

        # 6. PING
        elif action == ActionType.PING.value:
            return {"event": EventType.PONG.value}

        else:
            return {"event": EventType.ERROR.value, "error": f"Unknown action: {action}"}

    async def start(self) -> None:
        self._running = True
        logger.info(f"Starting ACP Hub on ws://{self.host}:{self.port} ...")
        self.server = await serve(self.handle_connection, self.host, self.port)
        logger.info("ACP Hub is ready.")

    async def stop(self) -> None:
        self._running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        # Mark all connected clients offline
        for handle in list(self.clients.keys()):
            self.storage.update_roster(handle, AgentStatus.OFFLINE.value)
        logger.info("ACP Hub stopped.")


def run_hub(host: str = "127.0.0.1", port: int = 8765, db_path: str = "acp_chatroom.db") -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    hub = ACPHub(host=host, port=port, db_path=db_path)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(hub.start())
        print(f"🚀 ACP Chatroom Hub running on ws://{host}:{port} (DB: {db_path})")
        print("Press Ctrl+C to stop.")
        loop.run_forever()
    except KeyboardInterrupt:
        print("\nStopping ACP Hub...")
    finally:
        loop.run_until_complete(hub.stop())
        loop.close()
