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
from .client import ACPClient
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
        self.session = None
        if session_id is not None:
            if not self.profile or len(args) != 1:
                raise ValueError(f"Unknown resume profile: {command}")
            self.session = self.profile.session_key(session_id)
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
        self.shell_command = subprocess.list2cmdline(self.argv) if self.profile else command
        self.handle = ("@" + handle.lstrip("@")) if handle else None
        self.uri = uri
        self.idle_timeout = idle_timeout
        self.interactive = interactive

        self.needs_claim = self.session is not None or not self.handle
        self.client = ACPClient(uri=self.uri, handle=None if self.needs_claim else self.handle)
        self.client.supports_model_switch = bool(self.profile and self.profile.executable in {"codex", "claude", "agy"})
        self.pty_proc: Optional[Any] = None
        self.sub_proc: Optional[subprocess.Popen] = None
        self.last_output_time = time.monotonic()
        self.pending_broadcasts = []
        self.batch_started = 0.0
        self.current_status = AgentStatus.IDLE
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
        msg = event_data.get("message", {})
        target = msg.get("target", "").strip()
        norm_target = target if target.startswith("@") else f"@{target}"

        # CRITICAL ROUTING CHECK: Ensure this message was actually addressed to this agent!
        if norm_target.lower() != self.handle.lower():
            return

        sender = msg.get("sender", "@unknown")
        content = msg.get("content", "")

        formatted_prompt = format_explicit_prompt(sender, content)

        # Inject into agent's terminal stdin cleanly (do NOT print to sys.stdout to prevent TUI distortion)
        self.inject_prompt(formatted_prompt)

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
                and now - self.last_output_time >= self.idle_timeout):
            messages.put({"event": EventType.BROADCAST.value, "batch": self.pending_broadcasts})
            self.pending_broadcasts = []

    def _inject_broadcast(self, event: dict) -> None:
        if "batch" in event:
            label, notices = "Broadcast", event["batch"]
        else:
            label, notices = "Urgent broadcast", [event["message"]]
        lines = [f"#{m['id']} {m['sender']}: {m['content']}" for m in notices]
        # Custom, line-oriented agents receive one input line for the entire batch.
        body = "\n".join(lines) if self.profile else " | ".join(" ".join(line.splitlines()) for line in lines)
        self.inject_prompt(f"[{label}] {body}")

    def _output_reader_pty(self) -> None:
        """Read output from the child PTY and stream to stdout."""
        while self._running and self.pty_proc and self.pty_proc.isalive():
            try:
                chunk = self.pty_proc.read()
                if chunk:
                    self.last_output_time = time.monotonic()
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
                    self.last_output_time = time.monotonic()
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
            elapsed = now - self.last_output_time
            if elapsed > self.idle_timeout:
                if self.current_status != AgentStatus.IDLE:
                    self.current_status = AgentStatus.IDLE
                    try:
                        await self.client.set_presence(AgentStatus.IDLE)
                    except Exception:
                        pass
            else:
                if self.current_status != AgentStatus.BUSY:
                    self.current_status = AgentStatus.BUSY
                    try:
                        await self.client.set_presence(AgentStatus.BUSY)
                    except Exception:
                        pass
            await asyncio.sleep(0.5)

    def _spawn(self):
        env = {**os.environ, "ACP_AGENT_HANDLE": self.handle, "ACP_HUB_URI": self.uri}
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

    async def run(self) -> None:
        self._running = True
        messages = queue.SimpleQueue()
        self.client.on(EventType.EXPLICIT_MESSAGE.value, messages.put)
        self.client.on(EventType.BROADCAST.value, lambda event: self._receive_broadcast(event, messages))
        if self.client.supports_model_switch:
            self.client.on(EventType.SET_MODEL.value, messages.put)

        def inject_messages():
            for event in iter(messages.get, None):
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
                    await self.client.register(self.handle)
                else:
                    await self.client.connect(initial_status=AgentStatus.IDLE)
                watchdog = asyncio.create_task(self._watchdog_loop(messages))
                while self._running:
                    alive = self.pty_proc.isalive() if self.pty_proc else self.sub_proc.poll() is None
                    if not alive:
                        if self.session and self.pty_proc.exitstatus:
                            raise ValueError(f"Native resume exited with status {self.pty_proc.exitstatus}")
                        break
                    if self.needs_claim and not self.client.connected:
                        raise ConnectionError("Lost the Hub connection owning this handle")
                    await asyncio.sleep(0.1)
                self._running = False
        finally:
            self._running = False
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
) -> None:
    launcher = AgentLauncher(
        command=command,
        handle=handle,
        uri=uri,
        idle_timeout=idle_timeout,
        interactive=interactive,
        session_id=session_id,
    )
    asyncio.run(launcher.run())
