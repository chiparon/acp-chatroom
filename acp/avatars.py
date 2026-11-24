"""Terminal-cell artwork and literal, styled chat messages."""
from dataclasses import dataclass
from hashlib import blake2b
import re

from rich.cells import cell_len
from rich.text import Text


@dataclass(frozen=True)
class AvatarSpec:
    key: str
    lines: tuple[str, str, str]
    color: str
    badge: str


_FAMILIES = (
    (r"claudecode|claude", AvatarSpec("claude", (" ▐▛█▜▌ ", "▝▜███▛▘", " ▘▘ ▝▝ "), "#E07A5F", "CLAUDE")),
    (r"codex|gpt", AvatarSpec("codex", (" ╭───╮ ", " │◈ ◈│ ", " ╰▀─▀╯ "), "#4ADE80", "CODEX")),
    (r"antigravity|agy", AvatarSpec("agy", (" ▄▀█▀▄ ", " ▐▛▄▜▌ ", "  ▀ ▀  "), "#A78BFA", "AGY")),
    (r"deepseek|dsh", AvatarSpec("dsh", (" ▄███▄ ", "▐█● ●█▌", " ▀▄▄▄▀ "), "#38BDF8", "DSH")),
    (r"user|human|console|observer", AvatarSpec("user", (" ╭▄█▄╮ ", " │▀ ▀│ ", " ╰───╯ "), "#FBBF24", "HUMAN")),
)

_CUSTOM_EYES = (" │▪ ▪│ ", " │◉ ◉│ ", " │• •│ ", " │◇ ◇│ ")
_COLORS = ("cyan", "magenta", "yellow", "green", "blue", "bright_red")


def resolve_avatar(handle: str) -> AvatarSpec:
    name = handle.strip().lstrip("@").lower()
    for aliases, spec in _FAMILIES:
        if re.match(rf"(?:{aliases})(?:$|[\d_-])", name):
            return spec
    face, color = blake2b(name.encode(), digest_size=2).digest()
    lines = (" ╭▀─▀╮ ", _CUSTOM_EYES[face % len(_CUSTOM_EYES)], " ╰─▄─╯ ")
    return AvatarSpec("custom", lines, _COLORS[color % len(_COLORS)], "CUSTOM")


def terminal_text(value: object) -> str:
    return re.sub(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]", " ", str(value))


def wrap_to_cells(text: str, max_cells: int) -> list[str]:
    lines = []
    for paragraph in text.split("\n"):
        line = []
        width = 0
        for ch in paragraph:
            size = cell_len(ch)
            if size > max_cells:
                ch, size = "�", 1
            if line and width + size > max_cells:
                lines.append("".join(line))
                line, width = [], 0
            line.append(ch)
            width += size
        lines.append("".join(line))
    return lines


def render_message_with_avatar(message: dict, max_width: int) -> Text:
    sender = terminal_text(message.get("sender", "@unknown")).replace("\n", " ")
    spec = resolve_avatar(sender)
    mode = message.get("mode", "silent")
    if mode == "explicit":
        badge = "⚡ EXPLICIT ➔ " + str(message.get("target", "@unknown"))
    elif mode == "silent":
        badge = "📢 #" + str(message.get("topic", "general"))
    else:
        badge = "🚨 URGENT" if mode == "urgent" else "📣 BROADCAST"
    if message.get("delivered") is False:
        badge += " (offline · queued)"
    stamp = str(message.get("created_at", ""))[:19].replace("T", " ")
    header = terminal_text(f"{sender} · {badge}  {stamp}".rstrip()).replace("\n", " ")
    width = max(1, max_width - 1)
    indent = 9 if width > 10 else 0
    rows = wrap_to_cells(header, width - indent)
    header_rows = len(rows)
    rows.extend(wrap_to_cells(terminal_text(message.get("content", "")), width - indent))
    result = Text()
    for index in range(max(3 if indent else 0, len(rows))):
        if index:
            result.append("\n")
        if indent:
            result.append(spec.lines[index] if index < 3 else " " * 7, style=spec.color)
            result.append("  ")
        if index < len(rows):
            result.append(rows[index], style="bold" if index < header_rows else None)
    return result
