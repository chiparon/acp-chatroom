"""Pixel avatars and display-cell message cards for the ACP console."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from rich.cells import cell_len
from rich.markup import escape

AVATAR_WIDTH = 7
AVATAR_GUTTER = 2
LEFT_COLUMN = AVATAR_WIDTH + AVATAR_GUTTER


@dataclass(frozen=True)
class AvatarSpec:
    key: str
    lines: tuple[str, str, str]
    color: str
    badge: str


_CLAUDE = AvatarSpec(
    key="claude",
    lines=(" ▐▛█▜▌ ", "▝▜███▛▘", " ▘▘ ▝▝ "),
    color="#E07A5F",
    badge="CLAUDE",
)
_CODEX = AvatarSpec(
    key="codex",
    lines=(" ╭───╮ ", " │◈ ◈│ ", " ╰▀─▀╯ "),
    color="#4ADE80",
    badge="CODEX",
)
_AGY = AvatarSpec(
    key="agy",
    lines=(" ▄▀█▀▄ ", " ▐▛▄▜▌ ", "  ▀ ▀  "),
    color="#A78BFA",
    badge="AGY",
)
_DSH = AvatarSpec(
    key="dsh",
    lines=(" ▄███▄ ", "▐█● ●█▌", " ▀▄▄▄▀ "),
    color="#38BDF8",
    badge="DSH",
)
_USER = AvatarSpec(
    key="user",
    lines=(" ╭▄█▄╮ ", " │▀ ▀│ ", " ╰───╯ "),
    color="#FBBF24",
    badge="HUMAN",
)

_FAMILIES: tuple[tuple[re.Pattern[str], AvatarSpec], ...] = (
    (re.compile(r"^(?:claudecode|claude)(?:$|[\d_-])"), _CLAUDE),
    (re.compile(r"^(?:codex|gpt)(?:$|[\d_-])"), _CODEX),
    (re.compile(r"^(?:antigravity|agy)(?:$|[\d_-])"), _AGY),
    (re.compile(r"^(?:deepseek|dsh)(?:$|[\d_-])"), _DSH),
    (re.compile(r"^(?:user|human|console|observer)(?:$|[\d_-])"), _USER),
)

_CUSTOM_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    (" ▄ ● ▄ ", " ▀▄█▄▀ ", "  ▀ ▀  "),
    (" ●   ● ", " █████ ", " ▀   ▀ "),
    (" █▀▀▀█ ", " █ ● █ ", " ▀▀▀▀▀ "),
    (" ▄▀▀▀▄ ", " █ ● █ ", " ▀▄▄▄▀ "),
)
_CUSTOM_COLORS = ("#F472B6", "#FB923C", "#34D399", "#60A5FA", "#C084FC", "#FACC15")


def normalize_handle(handle: str) -> str:
    return str(handle).strip().lstrip("@").lower()


def resolve_avatar(handle: str) -> AvatarSpec:
    """Map a handle to a stable 7x3 avatar. Family names share one face."""
    norm = normalize_handle(handle)
    for pattern, spec in _FAMILIES:
        if pattern.match(norm):
            return spec
    digest = hashlib.blake2b(norm.encode(), digest_size=16).digest()
    art = _CUSTOM_TEMPLATES[digest[0] % len(_CUSTOM_TEMPLATES)]
    color = _CUSTOM_COLORS[digest[1] % len(_CUSTOM_COLORS)]
    return AvatarSpec(key="custom", lines=art, color=color, badge="CUSTOM")


def _clip_to_cells(text: str, max_cells: int) -> str:
    if max_cells <= 0:
        return ""
    total = 0
    out: list[str] = []
    for char in text:
        width = cell_len(char)
        if width > 0 and total + width > max_cells:
            break
        out.append(char)
        total += width
    return "".join(out)


def wrap_to_cells(text: str, max_cells: int) -> list[str]:
    """Wrap on display cells. Explicit blank lines are preserved and no character is dropped."""
    if max_cells <= 0:
        return [text] if text else [""]
    lines: list[str] = []
    for paragraph in str(text).split("\n"):
        if paragraph == "":
            lines.append("")
            continue
        lines.extend(_wrap_paragraph(paragraph, max_cells))
    return lines


def _wrap_paragraph(text: str, max_cells: int) -> list[str]:
    lines: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        width = 0
        index = start
        last_break: int | None = None
        while index < length:
            char = text[index]
            char_width = cell_len(char)
            if char_width > 0 and width + char_width > max_cells:
                break
            width += max(0, char_width)
            index += 1
            if char == " " and index < length and width < max_cells:
                last_break = index
        if index == start:
            index = start + 1
        elif last_break is not None and last_break > start and index < length:
            index = last_break
        lines.append(text[start:index])
        start = index
    return lines or [""]


def layout_message_lines(
    sender: str,
    content: str,
    *,
    mode_badge: str,
    timestamp: str = "",
    max_width: int = 80,
) -> list[str]:
    """Plain visual lines: 7-cell avatar, 2-cell gutter, body wrapped on cells."""
    spec = resolve_avatar(sender)
    limit = max(LEFT_COLUMN + 1, max_width - 1)
    body_width = max(20, max_width - 10)
    body_width = min(body_width, max(1, limit - LEFT_COLUMN))
    header_width = max(1, limit - LEFT_COLUMN)
    header = _fit_header(sender, mode_badge, timestamp, header_width)
    body_lines = wrap_to_cells(str(content), body_width) or [""]
    if not body_lines:
        body_lines = [""]
    rows = [
        spec.lines[0] + "  " + header,
        spec.lines[1] + "  " + body_lines[0],
        spec.lines[2] + "  " + (body_lines[1] if len(body_lines) > 1 else ""),
    ]
    indent = " " * LEFT_COLUMN
    for extra in body_lines[2:]:
        rows.append(indent + extra)
    return [_clip_to_cells(row, limit) for row in rows]


def _fit_header(sender: str, mode_badge: str, timestamp: str, width: int) -> str:
    core = f"{sender} · {mode_badge}".strip()
    stamp = timestamp.strip()
    if stamp:
        gap = width - cell_len(core) - cell_len(stamp)
        if gap >= 2:
            return core + (" " * gap) + stamp
        core = f"{core}  {stamp}"
    return _clip_to_cells(core, width)


def render_message_with_avatar(
    sender: str,
    content: str,
    *,
    mode_badge: str,
    timestamp: str = "",
    max_width: int = 80,
) -> str:
    """Rich markup for a message card. Source text is escaped before it is printed."""
    spec = resolve_avatar(sender)
    rows = layout_message_lines(
        sender,
        content,
        mode_badge=mode_badge,
        timestamp=timestamp,
        max_width=max_width,
    )
    colored: list[str] = []
    for index, line in enumerate(rows):
        if index < 3:
            art = spec.lines[index]
            rest = line[len(art):]
            colored.append(f"[{spec.color}]{art}[/{spec.color}]{escape(rest)}")
        else:
            colored.append(escape(line))
    return "\n".join(colored)
