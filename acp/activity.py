"""Interpret child terminal output without treating repaints as a working turn."""
import re
import time

from .protocol import AgentStatus


# Consume complete CSI/OSC sequences or retain an incomplete suffix for the next read.
_ESCAPE = re.compile(r"\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\)|\[[0-?]*[ -/]*[@-~]|"
                     r"(?P<partial>\][^\x07\x1b]*\x1b?|\[[0-?]*[ -/]*|)\Z|.)", re.DOTALL)
_STATES = {"idle": AgentStatus.IDLE, "done": AgentStatus.IDLE,
           "working": AgentStatus.BUSY, "blocked": AgentStatus.BLOCKED}


class TerminalActivity:
    def __init__(self, app: str | None):
        self.app = app
        self.native = AgentStatus.STARTING if app else None
        self.started = False
        self.partial = ""
        self.last_output = float("-inf")
        self.startup_text = ""
        self.skipped = set()
        self.cursor_query = False

    def feed(self, chunk: str) -> bool:
        """Return True once for each optional Codex startup dialog offering Esc to skip."""
        data = self.partial + chunk
        self.partial = ""
        self.cursor_query = False

        def control(match):
            seq = match[0]
            if match["partial"] is not None:
                self.partial = seq
            elif seq == "\x1b[6n":
                self.cursor_query = True
            elif self.app and seq.startswith("\x1b]7501;"):
                fields = dict(item.split("=", 1) for item in seq[7:].rstrip("\x07\x1b\\").split(":") if "=" in item)
                state = _STATES.get(fields.get("state"))
                if state and fields.get("app") == self.app:
                    self.native = state
                    if state in (AgentStatus.IDLE, AgentStatus.BUSY):
                        self.started = True
                        self.startup_text = ""
            return ""

        visible = _ESCAPE.sub(control, data)
        if visible.strip():
            self.last_output = time.monotonic()
        if self.app == "codex" and not self.started:
            self.startup_text = (self.startup_text + "".join(visible.lower().split()))[-4096:]
            for choice in ("continuewithouttrusting", "skipuntilnextversion"):
                if choice in self.startup_text and "escskip" in self.startup_text and choice not in self.skipped:
                    self.skipped.add(choice)
                    self.startup_text = ""
                    return True
        return False

    def status(self, now: float, idle_timeout: float) -> AgentStatus:
        if self.native is not None:
            return self.native
        return AgentStatus.BUSY if now - self.last_output < idle_timeout else AgentStatus.IDLE
