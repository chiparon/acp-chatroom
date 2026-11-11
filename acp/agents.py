"""Installed launcher profiles and native skill locations."""
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
import os
import shutil


@dataclass(frozen=True)
class AgentProfile:
    executable: str
    access_flag: str
    user_skills: str
    workspace_skills: str = ".agents/skills"
    permission_options: tuple[str, ...] = ()

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
        return [exe or selected, *defaults, *argv[1:]]


def agent_registry() -> dict[str, AgentProfile]:
    claude = AgentProfile("claude", "--dangerously-skip-permissions", ".claude/skills", ".claude/skills",
                          ("--permission-mode", "--restricted"))
    agy = AgentProfile("agy", "--dangerously-skip-permissions", ".gemini/antigravity-cli/skills",
                       permission_options=("--sandbox", "--mode"))
    profiles = {
        "claude": claude, "claudecode": claude,
        "agy": agy, "antigravity": agy,
        "codex": AgentProfile("codex", "--dangerously-bypass-approvals-and-sandbox", ".agents/skills",
                              permission_options=("-s", "--sandbox", "-a", "--ask-for-approval", "--approve-for-me")),
    }
    for entry in entry_points(group="acp_chatroom.agents"):
        if entry.name in profiles:
            raise ValueError(f"Duplicate agent profile: {entry.name}")
        profiles[entry.name] = entry.load()
    return profiles
