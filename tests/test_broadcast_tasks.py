import asyncio
from concurrent.futures import ThreadPoolExecutor
import os
import queue
import subprocess
from threading import Barrier

import pytest

from acp.client import ACPClient
from acp.hub import ACPHub
from acp.launcher import AgentLauncher
from acp.protocol import EventType
from acp.storage import Storage


def test_task_claim_is_atomic_across_connections_and_survives_restart(tmp_path):
    db = str(tmp_path / "hub.db")
    store = Storage(db)
    owners = ["@alice", "@bob", "@carol"]
    ready = Barrier(len(owners))

    def race(owner):
        ready.wait(timeout=10)
        return store.claim_task("s10", "poisson", owner)

    with ThreadPoolExecutor(max_workers=len(owners)) as pool:
        results = list(pool.map(race, owners))
    (winner,) = [owner for owner, (won, _) in zip(owners, results) if won]
    assert all(owner == winner for _, owner in results)
    reopened = Storage(db)
    assert reopened.list_tasks("s10") == [{"task": "poisson", "owner": winner}]
    assert reopened.claim_task("other", "poisson", "@other") == (True, "@other")
    assert reopened.handoff_task("s10", "poisson", "@other", "@next") == (False, winner)
    assert reopened.handoff_task("s10", "poisson", winner, "@next") == (True, "@next")
    assert reopened.claim_task("s10", "poisson", winner) == (False, "@next")


def test_busy_launcher_holds_batch_but_injects_urgent(monkeypatch):
    launcher = AgentLauncher("codex", "@agent", idle_timeout=2)
    monkeypatch.setattr("acp.launcher.time.monotonic", lambda: 20)
    launcher.last_output_time = 20
    inbox = queue.SimpleQueue()
    prompts = []
    monkeypatch.setattr(launcher, "inject_prompt", prompts.append)
    for i, mode in enumerate(("broadcast", "broadcast", "urgent")):
        msg = {"id": i, "sender": "@peer", "content": str(i), "mode": mode}
        launcher._receive_broadcast({"event": "broadcast", "message": msg}, inbox)
    launcher._flush_broadcasts(inbox, 21)
    launcher._inject_broadcast(inbox.get_nowait())
    assert prompts == ["[Urgent broadcast] #2 @peer: 2"] and inbox.empty()
    launcher._flush_broadcasts(inbox, 22)
    launcher._inject_broadcast(inbox.get_nowait())
    launcher._flush_broadcasts(inbox, 23)
    assert prompts[1:] == ["[Broadcast] #0 @peer: 0\n#1 @peer: 1"] and inbox.empty()


@pytest.mark.dt
async def test_cli_broadcast_fanout_and_competing_claims(tmp_path):
    hub = ACPHub(port=0, db_path=str(tmp_path / "hub.db"))
    await hub.start()
    uri = f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}"
    agents = [ACPClient(uri, name) for name in ("one", "two", "three")]
    console = ACPClient(uri, "observer")
    seen = {}

    async def command(handle, *args):
        env = {**os.environ, "ACP_HUB_URI": uri, "ACP_AGENT_HANDLE": handle}
        return await asyncio.to_thread(subprocess.run, ["acp-chat", *args], env=env, capture_output=True,
                                       text=True, timeout=30)

    try:
        for client in [*agents, console]:
            seen[client.handle] = []
            client.on(EventType.BROADCAST.value, seen[client.handle].append)
            await client.connect(role="console" if client is console else "agent")
        normal = await command("@one", "broadcast", "normal")
        assert normal.returncode == 0 and "2 online" in normal.stdout
        urgent = await command("@one", "broadcast", "--urgent", "urgent")
        assert urgent.returncode == 0 and "3 online" in urgent.stdout
        # Reading back on each socket also puts a barrier behind the notifications.
        for client in [*agents, console]:
            history = await client.read_board(unread_only=False)
            assert [m["content"] for m in history] == ["normal", "urgent"]
            expected = ["urgent"] if client is agents[0] else ["normal", "urgent"]
            assert [e["message"]["content"] for e in seen[client.handle]] == expected
        contenders = await asyncio.gather(*(command(c.handle, "task", "claim", "poisson") for c in agents))
        (winner,) = [c.handle for c, result in zip(agents, contenders) if result.returncode == 0]
        for result in contenders:
            assert winner in result.stdout + result.stderr
        listing = await command("@observer", "task", "list")
        assert listing.returncode == 0 and listing.stdout.strip() == f"poisson\t{winner}"
    finally:
        await asyncio.gather(*(client.close() for client in [*agents, console]))
        await hub.stop()
