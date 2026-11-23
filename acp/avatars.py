"""
ACP Avatars - 3-line Unicode pixel avatars and cell-width aware message card renderer.
"""

from dataclasses import dataclass
import hashlib
import re
from typing import Optional
from rich.cells import cell_len
from rich.markup import escape


@dataclass(frozen=True)
class AvatarSpec:
    key: str
    lines: tuple[str, str, str]
    color: str
    badge: str


_BUILTIN_AVATARS: tuple[tuple[re.Pattern[str], AvatarSpec], ...] = (
    (
        re.compile(r"^(claudecode|claude)(?:$|[\d_-])"),
        AvatarSpec(
            key="claude",
            lines=(" ▐▛█▜▌ ", "▝▜███▛▘", " ▘▘ ▝▝ "),
            color="#E07A5F",
            badge="CLAUDE",
        ),
    ),
    (
        re.compile(r"^(codex|gpt)(?:$|[\d_-])"),
        AvatarSpec(
            key="codex",
            lines=(" ╭───╮ ", " │◈ ◈│ ", " ╰▀─▀╯ "),
            color="#4ADE80",
            badge="CODEX",
        ),
    ),
    (
        re.compile(r"^(antigravity|agy)(?:$|[\d_-])"),
        AvatarSpec(
            key="agy",
            lines=(" ▄▀█▀▄ ", " ▐▛▄▜▌ ", "  ▀ ▀  "),
            color="#A78BFA",
            badge="AGY",
        ),
    ),
    (
        re.compile(r"^(deepseek|dsh)(?:$|[\d_-])"),
        AvatarSpec(
            key="dsh",
            lines=(" ▄███▄ ", "▐█● ●█▌", " ▀▄▄▄▀ "),
            color="#38BDF8",
            badge="DSH",
        ),
    ),
    (
        re.compile(r"^(user|human|console|observer)(?:$|[\d_-])"),
        AvatarSpec(
            key="user",
            lines=(" ╭▄█▄╮ ", " │▀ ▀│ ", " ╰───╯ "),
            color="#FBBF24",
            badge="HUMAN",
        ),
    ),
)

_CUSTOM_TEMPLATES: tuple[tuple[str, str, str], ...] = (
    (" ╭───╮ ", " │• •│ ", " ╰───╯ "),
    (" ┏━━━┓ ", " ┃▪ ▪┃ ", " ┗━▀━┛ "),
    ("  ▄█▄  ", " ▐▀ ▀▌ ", "  ▀▄▀  "),
    (" ▛▀▀▀▜ ", " ▌○ ○▐ ", " ▙▄▄▄▟ "),
)

_CUSTOM_PALETTE: tuple[str, ...] = (
    "#F472B6",
    "#2DD4BF",
    "#FB923C",
    "#60A5FA",
    "#C084FC",
    "#A3E635",
)


def resolve_avatar(handle: str) -> AvatarSpec:
    """Resolve a 3-line 7-cell pixel avatar specification for any member handle."""
    norm = (handle or "").strip().lstrip("@").lower()
    for pattern, spec in _BUILTIN_AVATARS:
        if pattern.match(norm):
            return spec
    digest = hashlib.blake2b(norm.encode("utf-8"), digest_size=8).digest()
    template_idx = digest[0] % len(_CUSTOM_TEMPLATES)
    color_idx = digest[1] % len(_CUSTOM_PALETTE)
    return AvatarSpec(
        key="custom",
        lines=_CUSTOM_TEMPLATES[template_idx],
        color=_CUSTOM_PALETTE[color_idx],
        badge="CUSTOM",
    )


def _grapheme_units(text: str) -> list[str]:
    """Group characters with trailing zero-width / variation-selector / ZWJ codepoints."""
    units: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        i += 1
        while i < n:
            nxt = text[i]
            if nxt == "\u200d" and i + 1 < n:
                ch += nxt + text[i + 1]
                i += 2
                continue
            if cell_len(nxt) == 0:
                ch += nxt
                i += 1
                continue
            break
        units.append(ch)
    return units


def _tokenize_paragraph(paragraph: str) -> list[str]:
    """Split a paragraph into wrappable tokens (ASCII words, spaces, wide CJK/Emoji units)."""
    units = _grapheme_units(paragraph)
    tokens: list[str] = []
    current_word: list[str] = []
    for unit in units:
        if unit in (" ", "\t"):
            if current_word:
                tokens.append("".join(current_word))
                current_word = []
            tokens.append(" " if unit == " " else "    ")
        elif cell_len(unit) > 1:
            if current_word:
                tokens.append("".join(current_word))
                current_word = []
            tokens.append(unit)
        else:
            current_word.append(unit)
    if current_word:
        tokens.append("".join(current_word))
    return tokens


def _hard_wrap_units(text: str, max_cells: int, initial: str = "") -> list[str]:
    """Break a single overlong token across lines strictly respecting max_cells."""
    lines: list[str] = []
    cur = initial
    for unit in _grapheme_units(text):
        candidate = cur + unit
        if cur and cell_len(candidate) > max_cells:
            lines.append(cur)
            cur = unit
        else:
            cur = candidate
    if cur or not lines:
        lines.append(cur)
    return lines


def wrap_to_cells(text: str, max_cells: int) -> list[str]:
    """Wrap text by terminal display cell width (rich.cells.cell_len), preserving paragraphs."""
    limit = max(1, int(max_cells))
    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = normalized.split("\n")
    result: list[str] = []

    for para in paragraphs:
        if not para:
            result.append("")
            continue
        if cell_len(para) <= limit:
            result.append(para)
            continue

        tokens = _tokenize_paragraph(para)
        cur = ""
        just_wrapped = False
        for tok in tokens:
            if tok == " " and not cur and just_wrapped:
                continue
            candidate = cur + tok
            if cell_len(candidate) <= limit:
                cur = candidate
                just_wrapped = False
                continue

            if tok.strip() == "":
                if cur:
                    result.append(cur)
                    cur = ""
                    just_wrapped = True
                continue

            if cell_len(tok) <= limit:
                if cur:
                    result.append(cur.rstrip(" "))
                cur = tok
                just_wrapped = False
            else:
                if cur and cur.strip():
                    result.append(cur.rstrip(" "))
                    cur = ""
                pieces = _hard_wrap_units(tok, limit, initial=cur)
                result.extend(pieces[:-1])
                cur = pieces[-1]
                just_wrapped = False

        if cur or not result:
            result.append(cur)

    return result


def _clip_plain_to_cells(text: str, max_cells: int) -> str:
    if max_cells <= 0:
        return ""
    cur = ""
    for unit in _grapheme_units(text):
        if cell_len(cur + unit) > max_cells:
            break
        cur += unit
    return cur


def _format_timestamp(timestamp: str) -> str:
    ts = (timestamp or "").strip()
    if not ts:
        return ""
    if "T" in ts and len(ts) >= 19:
        return ts[11:19]
    if " " in ts and len(ts) >= 19:
        return ts[11:19]
    return ts[:19]


def render_message_with_avatar(
    sender: str,
    content: str,
    *,
    mode: str = "silent",
    target: Optional[str] = None,
    topic: Optional[str] = None,
    timestamp: str = "",
    note: str = "",
    max_width: int = 80,
) -> str:
    """Render a message card with a 3-line 7-cell pixel avatar on the left and wrapped text on the right."""
    raw_sender = (sender or "@unknown").strip() or "@unknown"
    if not raw_sender.startswith("@"):
        raw_sender = f"@{raw_sender}"
    spec = resolve_avatar(raw_sender)

    max_line_cells = max(14, int(max_width) - 1)
    body_width = max(4, max_line_cells - 9)

    mode_norm = (mode or "silent").lower()
    if mode_norm == "explicit":
        tgt = (target or "@unknown").strip() or "@unknown"
        if not tgt.startswith("@") and not tgt.startswith("#"):
            tgt = f"@{tgt}"
        badge_plain = f"⚡ EXPLICIT ➔ {tgt}"
        badge_markup = f"[bold magenta]⚡ EXPLICIT[/bold magenta] ➔ [bold green]{escape(tgt)}[/bold green]"
    elif mode_norm == "urgent":
        badge_plain = "🚨 URGENT"
        badge_markup = "[bold red]🚨 URGENT[/bold red]"
    elif mode_norm == "broadcast":
        badge_plain = "📣 BROADCAST"
        badge_markup = "[bold cyan]📣 BROADCAST[/bold cyan]"
    else:
        t_name = (topic or "general").strip().lstrip("#") or "general"
        badge_plain = f"📢 #{t_name}"
        badge_markup = f"[bold blue]📢 #{escape(t_name)}[/bold blue]"

    note_clean = (note or "").strip()
    left_plain = f"{raw_sender} · {badge_plain}" + (f" {note_clean}" if note_clean else "")
    left_markup = (
        f"[bold {spec.color}]{escape(raw_sender)}[/bold {spec.color}] · {badge_markup}"
        + (f" [dim]{escape(note_clean)}[/dim]" if note_clean else "")
    )

    short_ts = _format_timestamp(timestamp)
    left_cells = cell_len(left_plain)
    ts_cells = cell_len(short_ts)

    if left_cells <= body_width:
        if short_ts and left_cells + 2 + ts_cells <= body_width:
            pad = " " * (body_width - left_cells - ts_cells)
            header_right = f"{left_markup}{pad}[dim]{escape(short_ts)}[/dim]"
        else:
            header_right = left_markup
    else:
        clipped_meta = _clip_plain_to_cells(left_plain, body_width)
        header_right = f"[bold {spec.color}]{escape(clipped_meta)}[/bold {spec.color}]"

    body_lines = wrap_to_cells(content or "", body_width)
    if not body_lines:
        body_lines = [""]

    av0 = f"[{spec.color}]{escape(spec.lines[0])}[/{spec.color}]"
    av1 = f"[{spec.color}]{escape(spec.lines[1])}[/{spec.color}]"
    av2 = f"[{spec.color}]{escape(spec.lines[2])}[/{spec.color}]"

    out_lines: list[str] = [
        f"{av0}  {header_right}",
        f"{av1}  [white]{escape(body_lines[0])}[/white]" if body_lines[0] else av1,
    ]
    if len(body_lines) >= 2 and body_lines[1]:
        out_lines.append(f"{av2}  [white]{escape(body_lines[1])}[/white]")
    else:
        out_lines.append(av2)

    indent = " " * 9
    for extra in body_lines[2:]:
        if extra:
            out_lines.append(f"{indent}[white]{escape(extra)}[/white]")
        else:
            out_lines.append("")

    return "\n".join(out_lines)
