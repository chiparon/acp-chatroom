import queue
import threading
import time
from unittest.mock import Mock

import pytest

from acp.activity import TerminalActivity
from acp.launcher import AgentLauncher
from acp.protocol import AgentStatus


@pytest.mark.parametrize("app", ["codex", "antigravity"])
@pytest.mark.parametrize("ending", ["\x07", "\x1b\\"])
def test_native_state_across_reads_and_repaints(app, ending):
    activity = TerminalActivity(app)
    assert activity.native == AgentStatus.STARTING
    for state, expected in [("idle", "idle"), ("working", "busy"), ("blocked", "blocked"), ("done", "idle")]:
        for char in f"\x1b]7501;state={state}:app={app}{ending}":
            activity.feed(char)
        for chunk in ("\x1b]0;title spinner\x07", "\x1b[21;3H", "status line refreshed", "\n"):
            activity.feed(chunk)
            assert activity.status(time.monotonic(), 1.5).value == expected
            assert activity.status(time.monotonic() + 30, 1.5).value == expected
    activity.feed("\x1b]7501;state=working:app=other\x07")
    assert activity.native == AgentStatus.IDLE


def test_legacy_activity_ignores_cursor_and_title_updates():
    activity = TerminalActivity(None)
    for chunk in ("\x1b[", "?25h\x1b]0;", "new title\x1b", "\\ \r\n"):
        activity.feed(chunk)
    assert activity.status(time.monotonic(), 1.5) == AgentStatus.IDLE
    activity.feed("running")
    assert activity.status(time.monotonic(), 1.5) == AgentStatus.BUSY
    assert activity.status(time.monotonic() + 3, 1.5) == AgentStatus.IDLE


def test_headless_cursor_reply_can_arrive_between_paste_and_enter(monkeypatch):
    launcher = AgentLauncher("codex", "worker", interactive=False)
    replies = Mock()
    monkeypatch.setattr(launcher, "_write", replies)
    monkeypatch.setattr(launcher, "write_raw", Mock(side_effect=AssertionError("waiting on the paste lock")))
    launcher._observe_output("\x1b[6")
    replies.assert_not_called()
    launcher._observe_output("n")
    replies.assert_called_once_with("\x1b[1;1R")


def test_startup_dialog_and_broadcast_during_work(monkeypatch):
    launcher = AgentLauncher("codex", "worker", interactive=False)
    keys = Mock()
    monkeypatch.setattr(launcher, "write_raw", keys)
    launcher._observe_output("Do you trust this folder? Enter confirm")
    keys.assert_not_called()
    launcher._observe_output("Continue\x1b[11;2Hwithout trusting (hooks won't run) enter confirm")
    launcher._observe_output(" · esc skip")
    launcher._advance_startup(time.monotonic() + 1)
    keys.assert_called_once_with("\x1b")
    assert not launcher.input_ready.is_set()
    inbox = queue.SimpleQueue()
    launcher._receive_broadcast({"message": {"mode": "broadcast"}}, inbox)
    launcher._flush_broadcasts(inbox, time.monotonic() + 10)
    assert inbox.empty()
    launcher._observe_output("\x1b]7501;state=working:app=codex\x07")
    assert launcher.input_ready.is_set()
    launcher._flush_broadcasts(inbox, time.monotonic() + 10)
    assert inbox.empty()
    launcher._observe_output("\x1b]7501;state=blocked:app=codex\x07")
    assert not launcher.input_ready.is_set()
    launcher._observe_output("\x1b]7501;state=idle:app=codex\x07")
    launcher._observe_output("\x1b[21;3Hredraw")
    launcher._flush_broadcasts(inbox, time.monotonic() + 10)
    assert launcher.input_ready.is_set() and len(inbox.get_nowait()["batch"]) == 1


def test_interactive_cursor_reply_bypasses_paste_lock_and_extends_settle(monkeypatch):
    launcher = AgentLauncher("codex", "worker", interactive=True)
    monkeypatch.setattr("acp.launcher.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("acp.launcher.os.name", "nt")

    events = []

    class FakePty:
        def isalive(self):
            return True

        def write(self, text):
            events.append((time.monotonic(), text))

    launcher.pty_proc = FakePty()

    def simulate_tui_and_host():
        time.sleep(0.05)
        launcher._observe_output("\x1b[6n")
        time.sleep(0.05)
        launcher._forward_user_input("\x1b[21;5Rx")

    driver = threading.Thread(target=simulate_tui_and_host, daemon=True)
    driver.start()
    launcher.inject_prompt("hello")
    driver.join(timeout=2)

    texts = [text for _, text in events]
    assert texts == ["\x1b[200~hello\x1b[201~", "\x1b[21;5R", "\x1b[13;28;13;1;0;1_\x1b[13;28;13;0;0;1_", "x"]
    t_reply = events[1][0]
    t_enter = events[2][0]
    assert t_enter - t_reply >= 0.25


def test_interactive_cursor_query_falls_back_when_host_silent(monkeypatch):
    launcher = AgentLauncher("codex", "worker", interactive=True)
    monkeypatch.setattr("acp.launcher.sys.stdin.isatty", lambda: True)
    replies = []
    monkeypatch.setattr(launcher, "_write", lambda text: replies.append((time.monotonic(), text)))

    launcher._observe_output("\x1b[6n")
    assert replies == []
    started = time.monotonic()
    launcher._wait_codex_paste(settle=0.08, reply_grace=0.03)
    finished = time.monotonic()

    assert [text for _, text in replies] == ["\x1b[1;1R"]
    assert finished - replies[0][0] >= 0.06
    assert finished - started >= 0.09

