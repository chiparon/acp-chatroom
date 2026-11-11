"""
ACP Console - Human Observation & Interaction Terminal
Allows human operators to monitor multi-agent chat activity and inject explicit prompts.
"""

import asyncio
import sys
import threading
from typing import Optional
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from .protocol import MessageMode, AgentStatus, EventType
from .client import ACPClient

console = Console()


class HumanConsole:
    def __init__(self, uri: str = "ws://127.0.0.1:8765", handle: str = "@user"):
        self.uri = uri
        self.handle = handle if handle.startswith("@") else f"@{handle}"
        self.client = ACPClient(uri=self.uri, handle=self.handle)
        self._running = False

    def print_banner(self) -> None:
        console.print(
            Panel.fit(
                "[bold cyan]🤖 ACP Multi-Agent Chatroom Console[/bold cyan]\n"
                "[dim]Observing agent-to-agent communication & Human-in-the-loop steering[/dim]\n\n"
                "Commands:\n"
                "  [green]/send @agent <message>[/green]  - Send explicit prompt directly to agent\n"
                "  [cyan]/post #topic <message>[/cyan]  - Post silent message to message board\n"
                "  [yellow]/roster[/yellow]                - List active online agents & status\n"
                "  [yellow]/read <topic>[/yellow]          - Read recent messages from topic\n"
                "  [red]/quit[/red] or [red]/exit[/red]         - Exit console",
                border_style="cyan",
            )
        )

    def on_explicit_message(self, data: dict) -> None:
        msg = data.get("message", {})
        sender = msg.get("sender", "@unknown")
        target = msg.get("target", "@unknown")
        content = msg.get("content", "")
        time_str = msg.get("created_at", "")[:19].replace("T", " ")

        console.print(
            f"[dim]{time_str}[/dim] [bold magenta]⚡ EXPLICIT[/bold magenta] "
            f"[bold yellow]{sender}[/bold yellow] ➔ [bold green]{target}[/bold green]:\n"
            f"   [white]{content}[/white]"
        )

    def on_silent_message(self, data: dict) -> None:
        msg = data.get("message", {})
        sender = msg.get("sender", "@unknown")
        topic = msg.get("topic", "general")
        content = msg.get("content", "")
        time_str = msg.get("created_at", "")[:19].replace("T", " ")

        console.print(
            f"[dim]{time_str}[/dim] [bold blue]📢 BOARD [#{topic}][/bold blue] "
            f"[bold yellow]{sender}[/bold yellow]:\n"
            f"   [white]{content}[/white]"
        )

    def on_presence_change(self, data: dict) -> None:
        handle = data.get("handle", "")
        status = data.get("status", "")
        color = "green" if status == "idle" else ("yellow" if status == "busy" else "red")
        console.print(f"[dim]• Presence:[/dim] [bold]{handle}[/bold] is now [{color}]{status.upper()}[/{color}]")

    async def show_roster(self) -> None:
        roster = await self.client.get_roster()
        table = Table(title="👥 Active Agent Roster", border_style="blue")
        table.add_column("Agent Handle", style="cyan", no_wrap=True)
        table.add_column("Status", style="bold")
        table.add_column("Last Seen (UTC)", style="dim")

        for r in roster:
            status = r.get("status", "offline")
            color = "green" if status == "idle" else ("yellow" if status == "busy" else "red")
            table.add_row(r.get("handle", ""), f"[{color}]{status.upper()}[/{color}]", r.get("last_seen", "")[:19])

        console.print(table)

    async def run(self) -> None:
        self._running = True
        self.print_banner()

        try:
            await self.client.connect(initial_status=AgentStatus.IDLE)
        except Exception as e:
            console.print(f"[bold red]❌ Failed to connect to ACP Hub at {self.uri}: {e}[/bold red]")
            return

        self.client.on(EventType.EXPLICIT_MESSAGE.value, self.on_explicit_message)
        self.client.on(EventType.SILENT_MESSAGE.value, self.on_silent_message)
        self.client.on(EventType.PRESENCE_CHANGE.value, self.on_presence_change)

        await self.show_roster()

        loop = asyncio.get_running_loop()

        def user_input_thread():
            while self._running:
                try:
                    raw = input().strip()
                    if not raw:
                        continue
                    if raw in ("/quit", "/exit", ":q"):
                        self._running = False
                        break
                    elif raw == "/roster":
                        asyncio.run_coroutine_threadsafe(self.show_roster(), loop)
                    elif raw.startswith("/read"):
                        parts = raw.split(maxsplit=1)
                        topic = parts[1] if len(parts) > 1 else "general"
                        asyncio.run_coroutine_threadsafe(self._do_read(topic), loop)
                    elif raw.startswith("/send"):
                        # /send @target message
                        parts = raw.split(maxsplit=2)
                        if len(parts) < 3:
                            console.print("[red]Usage: /send @target <message>[/red]")
                            continue
                        target = parts[1]
                        msg = parts[2]
                        asyncio.run_coroutine_threadsafe(
                            self.client.send_message(
                                target=target,
                                content=msg,
                                mode=MessageMode.EXPLICIT,
                                sender=self.handle,
                            ),
                            loop,
                        )
                    elif raw.startswith("/post"):
                        # /post #topic message OR /post message
                        parts = raw.split(maxsplit=2)
                        if len(parts) < 2:
                            console.print("[red]Usage: /post [#topic] <message>[/red]")
                            continue
                        if parts[1].startswith("#"):
                            topic = parts[1][1:]
                            msg = parts[2] if len(parts) > 2 else ""
                        else:
                            topic = "general"
                            msg = raw.split(maxsplit=1)[1]
                        asyncio.run_coroutine_threadsafe(
                            self.client.send_message(
                                target=f"#{topic}",
                                content=msg,
                                mode=MessageMode.SILENT,
                                topic=topic,
                                sender=self.handle,
                            ),
                            loop,
                        )
                    else:
                        console.print(f"[yellow]Unknown command. Type /send, /post, /roster, or /read.[/yellow]")
                except (EOFError, KeyboardInterrupt):
                    self._running = False
                    break
                except Exception as e:
                    console.print(f"[red]Error: {e}[/red]")

        input_thread = threading.Thread(target=user_input_thread, daemon=True)
        input_thread.start()

        try:
            while self._running:
                await asyncio.sleep(0.3)
        finally:
            self._running = False
            await self.client.close()
            console.print("\n[dim]Console disconnected.[/dim]")

    async def _do_read(self, topic: str) -> None:
        messages = await self.client.read_board(topic=topic, reader=self.handle, unread_only=False, limit=20)
        console.print(f"\n[bold cyan]📖 Recent messages in #{topic} (count: {len(messages)}):[/bold cyan]")
        for m in messages:
            t = m.get("created_at", "")[:19].replace("T", " ")
            console.print(f"  [dim]{t}[/dim] [yellow]{m.get('sender')}[/yellow]: {m.get('content')}")
        console.print()


def start_console(uri: str = "ws://127.0.0.1:8765", handle: str = "@user") -> None:
    hc = HumanConsole(uri=uri, handle=handle)
    try:
        asyncio.run(hc.run())
    except KeyboardInterrupt:
        pass
