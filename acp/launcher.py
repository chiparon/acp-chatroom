"""
ACP Launcher - Terminal & ConPTY Wrapper for CLI Agents (Codex, Claude Code, Antigravity)
Enables automatic prompt injection and Idle/Busy presence reporting.
"""

import asyncio
import os
import sys
import time
import threading
import subprocess
from typing import Optional, List, Any

try:
    from winpty import PtyProcess
    HAS_WINPTY = True
except ImportError:
    HAS_WINPTY = False

from .protocol import format_explicit_prompt, AgentStatus, EventType
from .client import ACPClient


class AgentLauncher:
    def __init__(
        self,
        command: str,
        handle: str,
        uri: str = "ws://127.0.0.1:8765",
        idle_timeout: float = 1.5,
        interactive: bool = True,
    ):
        self.command = command
        self.handle = handle if handle.startswith("@") else f"@{handle}"
        self.uri = uri
        self.idle_timeout = idle_timeout
        self.interactive = interactive

        self.client = ACPClient(uri=self.uri, handle=self.handle)
        self.pty_proc: Optional[Any] = None
        self.sub_proc: Optional[subprocess.Popen] = None
        self.last_output_time = time.time()
        self.current_status = AgentStatus.IDLE
        self._running = False
        self._lock = threading.Lock()

    def write_input(self, text: str) -> None:
        """Write raw text into the running agent process."""
        with self._lock:
            # Ensure proper carriage return / line feed for Windows terminals
            if not text.endswith("\r\n") and not text.endswith("\n"):
                text += "\r\n"
            elif text.endswith("\n") and not text.endswith("\r\n"):
                text = text[:-1] + "\r\n"

            if self.pty_proc and self.pty_proc.isalive():
                self.pty_proc.write(text)
            elif self.sub_proc and self.sub_proc.poll() is None:
                if self.sub_proc.stdin:
                    self.sub_proc.stdin.write(text.encode("utf-8", errors="ignore"))
                    self.sub_proc.stdin.flush()

    def on_explicit_message(self, event_data: dict) -> None:
        """Callback when an explicit message arrives for this agent from ACP Hub."""
        msg = event_data.get("message", {})
        sender = msg.get("sender", "@unknown")
        content = msg.get("content", "")

        # Format prompt with source tag and instructions
        formatted_prompt = format_explicit_prompt(sender, content)
        print(f"\n⚡ [ACP Launcher] Injecting explicit prompt from {sender} into {self.handle} ...", flush=True)

        # Inject into agent's terminal stdin
        self.write_input(formatted_prompt)

    def _output_reader_winpty(self) -> None:
        """Read output from ConPTY and stream to stdout."""
        while self._running and self.pty_proc and self.pty_proc.isalive():
            try:
                chunk = self.pty_proc.read()
                if chunk:
                    self.last_output_time = time.time()
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
                    self.last_output_time = time.time()
                    sys.stdout.write(line.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                else:
                    time.sleep(0.02)
            except Exception:
                break

    def _user_input_reader(self) -> None:
        """Forward local human keyboard input into the agent process."""
        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                self.write_input(line)
            except (KeyboardInterrupt, EOFError):
                break
            except Exception:
                break

    async def _watchdog_loop(self) -> None:
        """Watchdog to update presence status between IDLE and BUSY."""
        while self._running:
            now = time.time()
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

    async def run(self) -> None:
        self._running = True
        print(f"🔗 [ACP Launcher] Connecting {self.handle} to ACP Hub ({self.uri}) ...")
        await self.client.connect(initial_status=AgentStatus.IDLE)

        # Register explicit message event handler
        self.client.on(EventType.EXPLICIT_MESSAGE.value, self.on_explicit_message)
        print(f"✅ [ACP Launcher] {self.handle} registered on ACP Hub. Listening for messages.")

        # Spawn the child process
        print(f"🚀 [ACP Launcher] Spawning agent process: '{self.command}'")
        use_winpty = HAS_WINPTY and os.name == "nt"

        if use_winpty:
            try:
                # Direct WinPTY spawn
                self.pty_proc = PtyProcess.spawn(self.command)
                reader_thread = threading.Thread(target=self._output_reader_winpty, daemon=True)
            except Exception as e:
                # If direct spawn fails (e.g. built-in cmd commands), try with cmd.exe /c wrapped in outer quotes
                try:
                    self.pty_proc = PtyProcess.spawn(f'cmd.exe /c "{self.command}"')
                    reader_thread = threading.Thread(target=self._output_reader_winpty, daemon=True)
                except Exception as e2:
                    print(f"⚠️ [ACP Launcher] WinPTY spawn failed ({e2}), falling back to subprocess.")
                    use_winpty = False

        if not use_winpty:
            self.sub_proc = subprocess.Popen(
                self.command,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            reader_thread = threading.Thread(target=self._output_reader_subprocess, daemon=True)

        reader_thread.start()

        # Start interactive keyboard forwarding if enabled
        if self.interactive:
            input_thread = threading.Thread(target=self._user_input_reader, daemon=True)
            input_thread.start()

        # Start watchdog loop
        watchdog_task = asyncio.create_task(self._watchdog_loop())

        try:
            while self._running:
                is_alive = (
                    (self.pty_proc and self.pty_proc.isalive())
                    if use_winpty
                    else (self.sub_proc and self.sub_proc.poll() is None)
                )
                if not is_alive:
                    print("\n🛑 [ACP Launcher] Agent child process terminated.")
                    break
                await asyncio.sleep(0.2)
        finally:
            self._running = False
            watchdog_task.cancel()
            await self.client.close()
            if self.pty_proc and self.pty_proc.isalive():
                self.pty_proc.close()
            elif self.sub_proc and self.sub_proc.poll() is None:
                self.sub_proc.terminate()


def launch_agent(
    command: str,
    handle: str,
    uri: str = "ws://127.0.0.1:8765",
    idle_timeout: float = 1.5,
    interactive: bool = True,
) -> None:
    launcher = AgentLauncher(
        command=command,
        handle=handle,
        uri=uri,
        idle_timeout=idle_timeout,
        interactive=interactive,
    )
    asyncio.run(launcher.run())
