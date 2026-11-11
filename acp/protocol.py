"""
ACP (Agent Communication Protocol) - Protocol Definitions & Schemas
"""

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Dict, Any, List


class MessageMode(str, Enum):
    EXPLICIT = "explicit"
    SILENT = "silent"


class AgentStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    OFFLINE = "offline"


class ActionType(str, Enum):
    REGISTER = "register"
    SEND = "send"
    READ = "read"
    ROSTER = "roster"
    PRESENCE = "presence"
    PING = "ping"


class EventType(str, Enum):
    EXPLICIT_MESSAGE = "explicit_message"
    SILENT_MESSAGE = "silent_message"
    PRESENCE_CHANGE = "presence_change"
    ERROR = "error"
    PONG = "pong"


def get_utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Message:
    id: Optional[int] = None
    sender: str = ""
    target: str = ""  # e.g., "@codex" or "#general"
    mode: MessageMode = MessageMode.SILENT
    topic: str = "general"
    content: str = ""
    created_at: str = field(default_factory=get_utc_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["mode"] = self.mode.value if isinstance(self.mode, MessageMode) else self.mode
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        mode_val = data.get("mode", "silent")
        if isinstance(mode_val, str):
            mode_val = MessageMode(mode_val)
        return cls(
            id=data.get("id"),
            sender=data.get("sender", ""),
            target=data.get("target", ""),
            mode=mode_val,
            topic=data.get("topic", "general"),
            content=data.get("content", ""),
            created_at=data.get("created_at") or get_utc_now_iso(),
        )


def format_explicit_prompt(sender: str, content: str) -> str:
    """Format an explicit message for injecting directly into the target agent's prompt."""
    normalized_sender = sender if sender.startswith("@") else f"@{sender}"
    return f"{normalized_sender}: {content}"
