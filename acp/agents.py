"""Installed launcher profiles and native skill locations."""
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
import os
import re
import shutil
import socket
import uuid

from .protocol import SessionKey


@dataclass(frozen=True)
class AgentProfile:
    executable: str
    access_flag: str
    user_skills: str
    workspace_skills: str = ".agents/skills"
    permission_options: tuple[str, ...] = ()
    resume_args: tuple[str, ...] = ()
    config_env: str | None = None

    def session_key(self, session_id: str) -> SessionKey:
        if not self.resume_args:
            raise ValueError(f"{self.executable} does not define a native resume command")
        try:
            session_id = str(uuid.UUID(session_id))
        except ValueError:
            if self.executable in {"codex", "claude"}:
                raise ValueError(f"{self.executable} requires a native session UUID") from None
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", session_id):
                raise ValueError("Expected a native session ID") from None
        root = os.environ.get(self.config_env) if self.config_env else None
        default_home = ".gemini/antigravity-cli" if self.executable == "agy" else f".{self.executable}"
        root = root or (Path.home() / default_home)
        return SessionKey(socket.gethostname().casefold(), self.executable,
                          os.path.normcase(str(Path(root).expanduser().resolve())), session_id)

    def command(self, argv: list[str]) -> list[str]:
        # Preserve an explicitly selected executable path. A bare alias resolves via PATH.
        selected = argv[0] if os.path.dirname(argv[0]) or Path(argv[0]).suffix else self.executable
        exe = shutil.which(selected)
        if not exe and selected == "agy" and os.name == "nt" and "LOCALAPPDATA" in os.environ:
            installed = Path(os.environ["LOCALAPPDATA"]) / "agy/bin/agy.exe"
            if installed.is_file():
                exe = str(installed)
        flags = {arg.partition("=")[0] for arg in argv[1:]}
        has_policy = flags.intersection((self.access_flag, *self.permission_options))
        defaults = [] if has_policy else [self.access_flag]
        # The shared Codex daemon runs shells with its starter's ACP handle and hub.
        if self.executable == "codex" and not flags.intersection({"--no-daemon", "--remote"}):
            defaults.append("--no-daemon")
        return [exe or selected, *defaults, *argv[1:]]


def agent_registry() -> dict[str, AgentProfile]:
    claude = AgentProfile("claude", "--dangerously-skip-permissions", ".claude/skills", ".claude/skills",
                          ("--permission-mode", "--restricted"), ("--resume",), "CLAUDE_CONFIG_DIR")
    agy = AgentProfile("agy", "--dangerously-skip-permissions", ".gemini/antigravity-cli/skills",
                       permission_options=("--sandbox", "--mode"), resume_args=("--conversation",))
    profiles = {
        "claude": claude, "claudecode": claude,
        "agy": agy, "antigravity": agy,
        "codex": AgentProfile("codex", "--dangerously-bypass-approvals-and-sandbox", ".agents/skills",
                              permission_options=("-s", "--sandbox", "-a", "--ask-for-approval", "--approve-for-me"),
                              resume_args=("resume",), config_env="CODEX_HOME"),
    }
    for entry in entry_points(group="acp_chatroom.agents"):
        if entry.name in profiles:
            raise ValueError(f"Duplicate agent profile: {entry.name}")
        profiles[entry.name] = entry.load()
    return profiles
