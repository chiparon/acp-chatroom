"""
ACP CLI - Unified Command-Line Interface for Hub, Launcher, Console, and Chat Tools
"""

import sys
import asyncio
from pathlib import Path
import click
from rich.console import Console
from rich.table import Table

from .hub import run_hub
from .launcher import launch_agent
from .console import start_console
from .client import ACPClient, sync_send, sync_read, sync_roster
from .protocol import ActionType, MessageMode
from .agents import agent_registry
from .skills_generator import init_workspace_skills, install_skill

rich_console = Console()


@click.group()
def cli():
    """ACP (Agent Communication Protocol) - Multi-Agent Chatroom Toolkit"""
    pass


# -------------------------------------------------------------
# 1. acp hub
# -------------------------------------------------------------
@cli.command("hub")
@click.option("--host", default="127.0.0.1", help="Host interface to bind to")
@click.option("--port", default=8765, type=int, help="Port to listen on")
@click.option("--db", default="acp_chatroom.db", help="SQLite database path")
def hub_command(host, port, db):
    """Start the central ACP Chatroom WebSocket Hub server."""
    run_hub(host=host, port=port, db_path=db)


# -------------------------------------------------------------
# 2. acp launch
# -------------------------------------------------------------
@cli.command("launch")
@click.argument("command")
@click.option("--as", "--handle", "-a", "handle", help="Agent handle (profiles receive an automatic name if omitted)")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765", help="ACP Hub URI")
@click.option("--idle-timeout", default=1.5, type=float, help="Seconds of output silence to mark as IDLE")
@click.option("--non-interactive", is_flag=True, help="Disable local stdin forwarding")
def launch_command(command, handle, uri, idle_timeout, non_interactive):
    """Launch and wrap an agent process in ConPTY with automatic prompt injection."""
    try:
        launch_agent(
            command=command,
            handle=handle,
            uri=uri,
            idle_timeout=idle_timeout,
            interactive=not non_interactive,
        )
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("resume")
@click.argument("agent")
@click.argument("session_id")
@click.option("--as", "--handle", "-a", "handle", help="Preferred chat handle, if available")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765", help="ACP Hub URI")
@click.option("--non-interactive", is_flag=True, help="Disable local stdin forwarding")
def resume_command(agent, session_id, handle, uri, non_interactive):
    """Recall a native session by ID and register it with the Hub."""
    if agent not in agent_registry():
        raise click.BadParameter(f"Unknown agent {agent!r}. See 'acp agents'.", param_hint="AGENT")
    try:
        launch_agent(agent, handle, uri, interactive=not non_interactive, session_id=session_id)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc


# -------------------------------------------------------------
# 3. acp console
# -------------------------------------------------------------
@cli.command("console")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765", help="ACP Hub URI")
@click.option("--as", "--handle", "-a", "handle", default="@user", help="Your user handle")
def console_command(uri, handle):
    """Open the human observer & interaction terminal."""
    start_console(uri=uri, handle=handle)


# -------------------------------------------------------------
# 4. acp init-skills
# -------------------------------------------------------------
@cli.command("agents")
def agents_command():
    """List installed agent profiles and their default launch arguments."""
    for name, profile in agent_registry().items():
        flags = profile.command([name])[1:]
        click.echo(f"{name}: {profile.executable} {' '.join(flags)}")


@cli.command("install")
@click.argument("agent")
@click.option("--workspace", type=click.Path(path_type=Path, file_okay=False), help="Install for one workspace")
def install_command(agent, workspace):
    """Install the chatroom skill for AGENT (user-wide by default)."""
    profile = agent_registry().get(agent)
    if profile is None:
        raise click.BadParameter(f"Unknown agent {agent!r}. See 'acp agents'.", param_hint="AGENT")
    click.echo(install_skill(profile, workspace))


@cli.command("init-skills")
@click.option("--workspace", default=".", help="Target workspace path")
def init_skills_command(workspace):
    """Generate ACP skills and workspace instructions for the registered agents."""
    init_workspace_skills(workspace_root=workspace)


# -------------------------------------------------------------
# 5. acp-chat / acp send, read, roster commands
# -------------------------------------------------------------
@cli.command("send")
@click.option("--to", "-t", "target", required=True, help="Target handle (@agent) or topic (#topic)")
@click.option("--mode", "-m", type=click.Choice(["explicit", "silent"]), default="silent", help="Message mode")
@click.option("--topic", default="general", help="Topic channel name")
@click.option("--from", "sender", envvar="ACP_AGENT_HANDLE", default=None, help="Sender handle (@name)")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765", help="ACP Hub URI")
@click.argument("content")
def send_command(target, mode, topic, sender, uri, content):
    """Send an explicit prompt to an agent or a silent post to a topic."""
    # Auto normalize target
    if not target.startswith("#") and not target.startswith("@"):
        target = f"@{target}"

    # Auto infer mode if target starts with @
    if target.startswith("@") and mode == "silent":
        mode = "explicit"

    try:
        res = sync_send(uri=uri, target=target, content=content, mode=mode, topic=topic, sender=sender)
        msg_id = res.get("message_id")
        delivered = res.get("delivered")
        if mode == "explicit":
            if delivered:
                rich_console.print(f"[bold green]✓[/bold green] Explicit message [bold yellow]#{msg_id}[/bold yellow] delivered and injected into [cyan]{target}[/cyan]")
            else:
                rich_console.print(f"[bold yellow]![/bold yellow] Explicit message [bold yellow]#{msg_id}[/bold yellow] saved to queue ([dim]{target} is currently offline[/dim])")
        else:
            rich_console.print(f"[bold green]✓[/bold green] Silent message [bold yellow]#{msg_id}[/bold yellow] posted to [blue]#{topic}[/blue]")
    except Exception as e:
        rich_console.print(f"[bold red]❌ Failed to send message:[/bold red] {e}")
        sys.exit(1)


def hub_request(uri, payload):
    try:
        result = asyncio.run(ACPClient(uri).request(payload))
    except OSError as error:
        raise click.ClickException(str(error)) from error
    if result.get("status") != "ok":
        raise click.ClickException(result.get("error", "Hub request failed"))
    return result


@cli.command("broadcast")
@click.argument("content")
@click.option("--urgent", is_flag=True, help="Send immediately to every online session, including the sender")
@click.option("--from", "sender", envvar="ACP_AGENT_HANDLE", default=None)
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765")
def broadcast_command(content, urgent, sender, uri):
    """Send a room-wide announcement. Normal announcements are batched by each peer."""
    result = hub_request(uri, {"action": ActionType.SEND.value, "target": "#all", "sender": sender,
                               "mode": MessageMode.URGENT.value if urgent else MessageMode.BROADCAST.value,
                               "content": content})
    click.echo(f"Broadcast #{result['message_id']} sent to {result['sent']} online session(s)")
    if result["failed"]:
        raise click.ClickException("Delivery failed for " + ", ".join(result["failed"]))


@cli.group("task")
def task_group():
    """Claim tasks and inspect or transfer ownership."""


@task_group.command("claim")
@click.argument("task")
@click.option("--as", "sender", envvar="ACP_AGENT_HANDLE", required=True)
@click.option("--topic", default="general")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765")
def task_claim(task, sender, topic, uri):
    """Claim TASK once in this topic. A conflict reports the current owner."""
    result = hub_request(uri, {"action": ActionType.TASK_CLAIM.value, "task": task, "topic": topic, "sender": sender})
    if not result["claimed"]:
        raise click.ClickException(f"{task} already claimed by {result['owner']}")
    click.echo(f"{task} claimed by {result['owner']}")


@task_group.command("list")
@click.option("--topic", default="general")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765")
def task_list(topic, uri):
    """Show each claimed task and its owner in TOPIC."""
    result = hub_request(uri, {"action": ActionType.TASK_LIST.value, "topic": topic})
    for row in result["tasks"]:
        click.echo(f"{row['task']}\t{row['owner']}")


@task_group.command("handoff")
@click.argument("task")
@click.option("--to", "successor", required=True)
@click.option("--as", "sender", envvar="ACP_AGENT_HANDLE", required=True)
@click.option("--topic", default="general")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765")
def task_handoff(task, successor, sender, topic, uri):
    """Transfer a task you currently own."""
    result = hub_request(uri, {"action": ActionType.TASK_HANDOFF.value, "task": task, "topic": topic,
                               "sender": sender, "to": successor})
    if not result["changed"]:
        owner = result["owner"]
        raise click.ClickException(f"{task} is owned by {owner}" if owner else f"{task} is unclaimed")
    click.echo(f"{task} transferred to {result['owner']}")


@cli.command("read")
@click.option("--topic", default="general", help="Topic channel name")
@click.option("--as", "--handle", "-a", "reader", envvar="ACP_AGENT_HANDLE", default=None, help="Reader handle for cursor tracking")
@click.option("--all", "read_all", is_flag=True, help="Read all messages (ignore unread cursor)")
@click.option("--limit", default=20, type=int, help="Max messages to fetch")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765", help="ACP Hub URI")
def read_command(topic, reader, read_all, limit, uri):
    """Read unread messages from a topic board and advance your cursor."""
    try:
        messages = sync_read(
            uri=uri,
            topic=topic,
            reader=reader,
            limit=limit,
            unread_only=not read_all,
            update_cursor=True,
        )
        if not messages:
            rich_console.print(f"[dim]No new unread messages in #{topic}.[/dim]")
            return

        rich_console.print(f"[bold cyan]📢 Messages in #{topic} (count: {len(messages)}):[/bold cyan]")
        for m in messages:
            t = m.get("created_at", "")[:19].replace("T", " ")
            sender = m.get("sender", "@unknown")
            mode = m.get("mode", "silent")
            content = m.get("content", "")
            prefix = "[magenta][EXP][/magenta] " if mode == "explicit" else ""
            rich_console.print(f"  [dim]{t}[/dim] {prefix}[yellow]{sender}[/yellow]: {content}")
    except Exception as e:
        rich_console.print(f"[bold red]❌ Failed to read topic #{topic}:[/bold red] {e}")
        sys.exit(1)


@cli.command("roster")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765", help="ACP Hub URI")
def roster_command(uri):
    """List all registered agents and their current busy/idle status."""
    try:
        roster = sync_roster(uri=uri)
        table = Table(title="👥 ACP Active Agent Roster", border_style="blue")
        table.add_column("Agent Handle", style="cyan", no_wrap=True)
        table.add_column("Status", style="bold")
        table.add_column("Last Seen (UTC)", style="dim")

        for r in roster:
            status = r.get("status", "offline")
            color = "green" if status == "idle" else ("yellow" if status == "busy" else "red")
            table.add_row(r.get("handle", ""), f"[{color}]{status.upper()}[/{color}]", r.get("last_seen", "")[:19])

        rich_console.print(table)
    except Exception as e:
        rich_console.print(f"[bold red]❌ Failed to get roster:[/bold red] {e}")
        sys.exit(1)


@cli.command("model")
@click.argument("model_id")
@click.option("--to", "-t", "target", required=True, help="Online launcher handle")
@click.option("--uri", envvar="ACP_HUB_URI", default="ws://127.0.0.1:8765")
def model_command(model_id, target, uri):
    """Send the native /model command to an idle agent with an empty composer."""
    try:
        result = asyncio.run(ACPClient(uri).request({"action": ActionType.SET_MODEL.value,
                                                      "target": target, "model": model_id}))
    except OSError as error:
        raise click.ClickException(str(error)) from error
    if result.get("status") != "ok":
        raise click.ClickException(result.get("error", "Model command failed"))
    click.echo(f"Sent model selection {model_id} to {target}")


def main():
    cli()


if __name__ == "__main__":
    main()
