"""
ACP Launcher - Terminal & ConPTY Wrapper for CLI Agents (Codex, Claude Code, Antigravity)
Enables automatic prompt injection and Idle/Busy presence reporting.
"""

import asyncio
from contextlib import ExitStack
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Any

from .agents import agent_registry
from .activity import TerminalActivity
from .client import ACPClient
from .dsh import DshSession
from .protocol import format_explicit_prompt, AgentStatus, EventType, MessageMode, valid_model_id
from .terminal import keyboard_input

if os.name == "nt":
    from winpty import PtyProcess
else:
    from ptyprocess import PtyProcessUnicode as PtyProcess


def split_command(command: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(command)
    # ConPTY accepts argv. Use Windows quoting rules for paths and empty arguments.
    import ctypes
    from ctypes import wintypes

    split = ctypes.windll.shell32.CommandLineToArgvW
    split.restype = ctypes.POINTER(wintypes.LPWSTR)
    count = ctypes.c_int()
    argv = split(command, ctypes.byref(count))
    if not argv:
        raise ctypes.WinError()
    try:
        return argv[:count.value]
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))


class AgentLauncher:
    def __init__(
        self,
        command: str,
        handle: Optional[str],
        uri: str = "ws://127.0.0.1:8765",
        idle_timeout: float = 1.5,
        interactive: bool = True,
        session_id: Optional[str] = None,
        output_status: bool = False,
    ):
        self.command = command
        registry = agent_registry()
        if not command.strip():
            raise ValueError("Agent command is empty")
        args = split_command(command.strip())
        executable = Path(args[0])
        name = executable.name
        if os.name == "nt":
            name = name.lower()
            if executable.suffix.lower() in {".exe", ".cmd", ".bat"}:
                name = executable.stem.lower()
        self.profile = registry.get(name)
        # Other DSH profiles (e.g. web or a custom TUI) keep the custom-command path.
        self.native_dsh = name == "dsh" and args[1:] in ([], ["acp"], ["--profile", "acp"])
        if name == "dsh" and not self.native_dsh:
            self.profile = None
        self.session = None
        if session_id is not None:
            if not self.profile or len(args) != 1:
                raise ValueError(f"Unknown resume profile: {command}")
            self.session = self.profile.session_key(session_id)
            if self.profile.resume_args:
                args.extend((*self.profile.resume_args, self.session.session_id))
        elif self.profile and self.profile.resume_args:
            # Recognize the documented legacy command even with explicit launcher defaults.
            defaults = {self.profile.access_flag, "--no-daemon"}
            operation = tuple(arg for arg in args[1:-1] if arg not in defaults)
            if operation == self.profile.resume_args:
                try:
                    self.session = self.profile.session_key(args[-1])
                except ValueError:
                    pass  # Native pickers and named searches have no stable ID at launch.
        if not handle and not self.profile:
            raise ValueError("Custom launch commands require --as HANDLE")
        self.argv = self.profile.command(args) if self.profile else args
        if not interactive and self.profile and self.profile.executable == "codex":
            # An unattended TUI has no operator for the optional upgrade menu.
            # Put the launch-scoped default first so explicit user overrides still win.
            self.argv[1:1] = ["-c", "check_for_update_on_startup=false"]
        self.shell_command = subprocess.list2cmdline(self.argv) if self.profile else command
        self.handle = ("@" + handle.lstrip("@")) if handle else None
        self.uri = uri
        self.idle_timeout = idle_timeout
        self.interactive = interactive

        self.needs_claim = self.session is not None or not self.handle
        self.client = ACPClient(uri=self.uri, handle=None if self.needs_claim or self.native_dsh else self.handle)
        self.client.supports_model_switch = bool(self.profile and self.profile.executable in {"codex", "claude", "agy", "dsh"})
        self.pty_proc: Optional[Any] = None
        self.sub_proc: Optional[subprocess.Popen] = None
        native_apps = {"codex": "codex", "agy": "antigravity"}
        app = native_apps.get(self.profile.executable) if self.profile and not output_status else None
        self.activity = TerminalActivity(app)
        self.input_ready = threading.Event()
        self.skip_startup_at = None
        if not app:
            self.input_ready.set()
        self.pending_broadcasts = []
        self.batch_started = 0.0
        self.current_status = self.activity.status(time.monotonic(), idle_timeout)
        self._running = False
        self._lock = threading.Lock()

    def write_raw(self, text: str) -> None:
        """Write raw characters directly into the terminal stream."""
        with self._lock:
            self._write(text)

    def _write(self, text: str) -> None:
        if self.pty_proc and self.pty_proc.isalive():
            self.pty_proc.write(text)
        elif self.sub_proc and self.sub_proc.stdin and self.sub_proc.poll() is None:
            self.sub_proc.stdin.write(text.encode("utf-8"))
            self.sub_proc.stdin.flush()

    def write_input(self, text: str) -> None:
        ending = "\r" if self.pty_proc else "\n"
        self.write_raw(text.rstrip("\r\n") + ending)

    def inject_prompt(self, text: str) -> None:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Keep message content from terminating a paste or issuing terminal controls.
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)
        if self.profile and self.pty_proc:
            # One editor paste and one Enter, including for multiline messages.
            with self._lock:
                self._write(f"\x1b[200~{text}\x1b[201~")
                # Codex on ConPTY folds an immediate Enter into its paste burst.
                # Keep operator input out of the editor until this turn is submitted.
                if os.name == "nt" and self.profile.executable == "codex":
                    time.sleep(0.3)
                self._write("\r")
        else:
            self.write_input(text)

    def on_explicit_message(self, event_data: dict) -> None:
        """Callback when an explicit message arrives for this agent from ACP Hub."""
        prompt = self._explicit_prompt(event_data)
        if prompt is not None:
            self.inject_prompt(prompt)

    def _explicit_prompt(self, event_data: dict) -> str | None:
        msg = event_data.get("message", {})
        target = msg.get("target", "").strip()
        norm_target = target if target.startswith("@") else f"@{target}"

        # CRITICAL ROUTING CHECK: Ensure this message was actually addressed to this agent!
        if norm_target.lower() != self.handle.lower():
            return None

        sender = msg.get("sender", "@unknown")
        content = msg.get("content", "")

        return format_explicit_prompt(sender, content)

    def _receive_broadcast(self, event: dict, messages: queue.SimpleQueue) -> None:
        msg = event["message"]
        if msg["mode"] == MessageMode.URGENT.value:
            messages.put(event)
        else:
            if not self.pending_broadcasts:
                self.batch_started = time.monotonic()
            self.pending_broadcasts.append(msg)

    def _flush_broadcasts(self, messages: queue.SimpleQueue, now: float) -> None:
        # Use a fixed window from the first notice so a stream of new notices
        # cannot postpone a batch indefinitely while the terminal is quiet.
        if (self.pending_broadcasts and now - self.batch_started >= 1.0
                and self.activity.status(now, self.idle_timeout) == AgentStatus.IDLE):
            messages.put({"event": EventType.BROADCAST.value, "batch": self.pending_broadcasts})
            self.pending_broadcasts = []

    def _inject_broadcast(self, event: dict) -> None:
        self.inject_prompt(self._broadcast_prompt(event))

    def _broadcast_prompt(self, event: dict) -> str:
        if "batch" in event:
            label, notices = "Broadcast", event["batch"]
        else:
            label, notices = "Urgent broadcast", [event["message"]]
        lines = [f"#{m['id']} {m['sender']}: {m['content']}" for m in notices]
        # Custom, line-oriented agents receive one input line for the entire batch.
        body = "\n".join(lines) if self.profile else " | ".join(" ".join(line.splitlines()) for line in lines)
        return f"[{label}] {body}"

    def _observe_output(self, chunk: str) -> None:
        skip = self.activity.feed(chunk)
        if not self.interactive and self.activity.cursor_query:
            # The TUI can request a cursor report between paste and Enter.
            # Answer before submitting; queuing behind the paste lock loses Enter.
            self._write("\x1b[1;1R")
        if self.activity.app:
            if self.activity.native in (AgentStatus.IDLE, AgentStatus.BUSY):
                self.input_ready.set()
            else:
                self.input_ready.clear()
        if not self.interactive and skip:
            # ConPTY can paint the modal before its input handler is installed.
            self.skip_startup_at = time.monotonic() + 0.5

    def _advance_startup(self, now: float) -> None:
        if self.skip_startup_at is not None and now >= self.skip_startup_at:
            self.skip_startup_at = None
            if self.activity.native == AgentStatus.STARTING:
                self.write_raw("\x1b")

    def _output_reader_pty(self) -> None:
        """Read output from the child PTY and stream to stdout."""
        while self._running and self.pty_proc and self.pty_proc.isalive():
            try:
                chunk = self.pty_proc.read()
                if chunk:
                    self._observe_output(chunk)
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                else:
                    time.sleep(0.02)
            except Exception:
                break

    def _output_reader_subprocess(self) -> None:
        """Fallback output reader using standard pipes."""
        if not self.sub_proc or not self.sub_proc.stdout:
            return
        while self._running and self.sub_proc.poll() is None:
            try:
                line = self.sub_proc.stdout.readline()
                if line:
                    self._observe_output(line.decode("utf-8", errors="replace"))
                    sys.stdout.write(line.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                else:
                    time.sleep(0.02)
            except Exception:
                break

    def _user_input_reader(self, read) -> None:
        while self._running:
            try:
                chunk = read()
                if chunk and self._running:
                    self.write_raw(chunk)
            except (EOFError, OSError):
                break

    async def _watchdog_loop(self, messages: queue.SimpleQueue) -> None:
        """Watchdog to update presence status between IDLE and BUSY."""
        while self._running:
            now = time.monotonic()
            self._flush_broadcasts(messages, now)
            status = self.activity.status(now, self.idle_timeout)
            if status != self.current_status:
                try:
                    await self.client.set_presence(status)
                    self.current_status = status
                except Exception:
                    pass
            await asyncio.sleep(0.5)

    def _spawn(self):
        env = {**os.environ, "ACP_AGENT_HANDLE": self.handle, "ACP_HUB_URI": self.uri}
        if self.activity.app == "antigravity":
            env["AGY_CLI_OSC7501"] = "on"
        if self.profile or os.name == "nt" or self.interactive:
            if env.get("TERM", "dumb") == "dumb":
                env["TERM"] = "xterm-256color"
            size = shutil.get_terminal_size()
            argv = self.argv
            if os.name == "nt":
                executable = shutil.which(argv[0])
                if not executable or executable.lower().endswith((".cmd", ".bat")):
                    # Let cmd expand the command after /c parsing, preserving quoted paths.
                    env["ACP_LAUNCH_COMMAND"] = self.shell_command
                    argv = ["cmd.exe", "/d", "/s", "/c", "%ACP_LAUNCH_COMMAND%"]
            elif not self.profile:
                argv = ["/bin/sh", "-c", self.command]
            self.pty_proc = PtyProcess.spawn(argv, env=env, dimensions=(size.lines, size.columns))
            return self._output_reader_pty

        self.sub_proc = subprocess.Popen(
            self.command, shell=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env,
        )
        return self._output_reader_subprocess

    def _dsh_keyboard(self, dsh, messages):
        loop = asyncio.get_running_loop()

        def local_line(line):
            if dsh.permission_reply is not None and not dsh.permission_reply.done():
                dsh.permission_reply.set_result(line)
            elif line.strip():
                messages.put({"event": "keyboard", "text": line})

        def read_lines():
            try:
                while self._running:
                    line = input()
                    if self._running:
                        loop.call_soon_threadsafe(local_line, line)
            except (EOFError, OSError):
                if self._running:
                    loop.call_soon_threadsafe(setattr, self, "_running", False)

        threading.Thread(target=read_lines, daemon=True).start()

    def _dsh_input(self, event):
        kind = event["event"]
        if kind == EventType.BROADCAST.value:
            return self._broadcast_prompt(event)
        if kind == EventType.SET_MODEL.value:
            model = event.get("model")
            return model if valid_model_id(model) else None
        if kind == "keyboard":
            return event["text"]
        return self._explicit_prompt(event)

    async def _run_dsh(self, messages):
        env = {**os.environ, "ACP_AGENT_HANDLE": self.handle, "ACP_HUB_URI": self.uri}
        interactive = self.interactive and sys.stdin.isatty()
        native_id = self.session.session_id if self.session else None
        async with DshSession.open(self.argv, env, native_id, interactive) as dsh:
            if not self.needs_claim:
                await self.client.connect()
            await self.client.register(self.handle, session=self.profile.session_key(dsh.session_id))
            print(f"{self.handle} DSH session {dsh.session_id}", flush=True)
            if interactive:
                self._dsh_keyboard(dsh, messages)

            turn = None
            try:
                while self._running:
                    if dsh.process.returncode is not None:
                        raise OSError(f"DSH exited with status {dsh.process.returncode}")
                    if not self.client.connected:
                        raise ConnectionError("Lost the Hub connection owning this handle")
                    if turn is not None and turn.done():
                        await turn
                        turn = None
                        self.last_output_time = time.monotonic()
                        await self.client.set_presence(AgentStatus.IDLE)
                    if turn is None:
                        self._flush_broadcasts(messages, time.monotonic())
                        if not messages.empty():
                            event = messages.get_nowait()
                            prompt = self._dsh_input(event)
                            if prompt is not None:
                                await self.client.set_presence(AgentStatus.BUSY)
                                turn = asyncio.create_task(dsh.turn(event["event"], prompt))
                    await asyncio.sleep(0.1)
            finally:
                if turn is not None:
                    turn.cancel()
                    await asyncio.gather(turn, return_exceptions=True)

    async def run(self) -> None:
        self._running = True
        messages = queue.SimpleQueue()
        self.client.on(EventType.EXPLICIT_MESSAGE.value, messages.put)
        self.client.on(EventType.BROADCAST.value, lambda event: self._receive_broadcast(event, messages))
        if self.client.supports_model_switch:
            self.client.on(EventType.SET_MODEL.value, messages.put)

        def inject_messages():
            for event in iter(messages.get, None):
                self.input_ready.wait()
                if not self._running:
                    return
                if event.get("event") == EventType.SET_MODEL.value:
                    if valid_model_id(event.get("model")):
                        self.inject_prompt("/model " + event["model"])
                elif event.get("event") == EventType.BROADCAST.value:
                    self._inject_broadcast(event)
                else:
                    self.on_explicit_message(event)

        injector = threading.Thread(target=inject_messages, daemon=True)
        watchdog = None
        try:
            if self.needs_claim:
                await self.client.connect()
                result = await self.client.claim(self.profile.executable, self.handle, self.session)
                self.handle = result["handle"]
                if not result["claimed"]:
                    print(f"{self.handle} is already {result['state']}", flush=True)
                    return
                if not shutil.which(self.argv[0]):
                    raise ValueError(f"Executable not found: {self.argv[0]}")
                print(f"Starting {self.handle}", flush=True)
            if self.native_dsh:
                await self._run_dsh(messages)
                return
            # Register only after stdin is writable. A sender may act on presence immediately.
            reader = self._spawn()
            injector.start()
            with ExitStack() as stack:
                read = None
                if self.interactive and sys.stdin.isatty():
                    read = stack.enter_context(keyboard_input())
                threading.Thread(target=reader, daemon=True).start()
                if read:
                    threading.Thread(target=self._user_input_reader, args=(read,), daemon=True).start()
                if self.needs_claim:
                    await self.client.register(self.handle, self.current_status)
                else:
                    await self.client.connect(initial_status=self.current_status)
                watchdog = asyncio.create_task(self._watchdog_loop(messages))
                startup_notice = time.monotonic() + 45
                while self._running:
                    alive = self.pty_proc.isalive() if self.pty_proc else self.sub_proc.poll() is None
                    if not alive:
                        if self.session and self.pty_proc.exitstatus:
                            raise ValueError(f"Native resume exited with status {self.pty_proc.exitstatus}")
                        break
                    if self.needs_claim and not self.client.connected:
                        raise ConnectionError("Lost the Hub connection owning this handle")
                    if self.activity.app and not self.interactive and not self.activity.started:
                        self._advance_startup(time.monotonic())
                        if self.activity.native == AgentStatus.BLOCKED:
                            raise ValueError(f"{self.handle}: native startup requires operator input. "
                                             "Open the agent interactively to complete setup.")
                        if time.monotonic() >= startup_notice:
                            print(f"{self.handle}: still waiting for the native input screen. Messages are queued. "
                                  "Inspect the startup screen; use --output-status for older CLIs.",
                                  file=sys.stderr, flush=True)
                            startup_notice = float("inf")
                    await asyncio.sleep(0.1)
                self._running = False
        finally:
            self._running = False
            self.input_ready.set()
            messages.put(None)
            if injector.is_alive():
                await asyncio.to_thread(injector.join)
            if watchdog:
                watchdog.cancel()
                await asyncio.gather(watchdog, return_exceptions=True)
            await self.client.close()
            if self.pty_proc:
                self.pty_proc.close(force=True)
            elif self.sub_proc:
                if self.sub_proc.poll() is None:
                    self.sub_proc.terminate()
                self.sub_proc.wait()
                self.sub_proc.stdin.close()
                self.sub_proc.stdout.close()


def launch_agent(
    command: str,
    handle: Optional[str],
    uri: str = "ws://127.0.0.1:8765",
    idle_timeout: float = 1.5,
    interactive: bool = True,
    session_id: Optional[str] = None,
    output_status: bool = False,
) -> None:
    launcher = AgentLauncher(
        command=command,
        handle=handle,
        uri=uri,
        idle_timeout=idle_timeout,
        interactive=interactive,
        session_id=session_id,
        output_status=output_status,
    )
    asyncio.run(launcher.run())
