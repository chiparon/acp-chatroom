import pytest

import asyncio
import os
import tempfile
from acp.hub import ACPHub
from acp.client import ACPClient
from acp.protocol import MessageMode, EventType


pytestmark = pytest.mark.dt


async def test_handle_owner_survives_conflicts_and_can_reconnect(tmp_path):
    hub = ACPHub(port=0, db_path=str(tmp_path / "hub.db"))
    await hub.start()
    uri = f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}"
    owner = ACPClient(uri, "@gpt1")
    duplicate = ACPClient(uri, "@GPT1")
    other = ACPClient(uri, "@gpt2")
    inbox = asyncio.Queue()
    owner.on(EventType.EXPLICIT_MESSAGE.value, inbox.put_nowait)
    try:
        await asyncio.gather(owner.connect(), other.connect())
        with pytest.raises(ValueError, match="already online"):
            await duplicate.connect()
        assert duplicate.ws is None
        with pytest.raises(ValueError, match="already registered"):
            await other.register("@gpt3")
        assert other.handle == "@gpt2"
        denied = await other.request({"action": "presence", "handle": "@gpt1", "status": "offline"})
        assert denied["status"] == "error"
        assert (await other.send_message("@GPT1", "original", MessageMode.EXPLICIT))["delivered"]
        assert (await asyncio.wait_for(inbox.get(), 3))["message"]["content"] == "original"
        await owner.close()
        # Once cleanup completes the exact handle can be claimed by a new process.
        async def wait_offline():
            while "@gpt1" in hub.clients:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_offline(), 3)
        await owner.connect()
        assert (await other.send_message("@gpt1", "reconnected", MessageMode.EXPLICIT))["delivered"]
        assert (await asyncio.wait_for(inbox.get(), 3))["message"]["content"] == "reconnected"
    finally:
        await asyncio.gather(owner.close(), duplicate.close(), other.close())
        await hub.stop()


async def test_explicit_message_isolation():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    hub = ACPHub(host="127.0.0.1", port=0, db_path=db_path)
    await hub.start()

    uri = f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}"

    # 1. Start Claude agent client
    claude = ACPClient(uri=uri, handle="@claudecode")
    await claude.connect(role="agent")

    # 2. Start GPT agent client
    gpt = ACPClient(uri=uri, handle="@gpt")
    await gpt.connect(role="agent")

    # 3. Start Human Console client
    console = ACPClient(uri=uri, handle="@user")
    await console.connect(role="console")

    claude_received = []
    gpt_received = []
    console_received = []

    claude.on(EventType.EXPLICIT_MESSAGE.value, lambda d: claude_received.append(d))
    gpt.on(EventType.EXPLICIT_MESSAGE.value, lambda d: gpt_received.append(d))
    console.on(EventType.EXPLICIT_MESSAGE.value, lambda d: console_received.append(d))

    try:
        # Sender client
        sender = ACPClient(uri=uri, handle="@tester")
        await sender.connect()

        # Send an explicit message meant ONLY for @gpt
        print("Sending explicit message to @gpt ...")
        resp = await sender.send_message(
            target="@gpt",
            content="Hello GPT, this is for you ONLY!",
            mode=MessageMode.EXPLICIT,
            topic="general",
        )
        assert resp.get("status") == "ok"
        assert resp.get("delivered") is True

        # Wait a moment for network dispatch
        await asyncio.sleep(0.5)

        # Assertions
        assert len(gpt_received) == 1, f"Expected GPT to receive 1 message, got {len(gpt_received)}"
        assert gpt_received[0]["message"]["content"] == "Hello GPT, this is for you ONLY!"

        assert len(console_received) == 1, f"Expected Console to receive 1 message for observation, got {len(console_received)}"

        assert len(claude_received) == 0, f"BUG DETECTED: Claude received {len(claude_received)} message(s) meant for GPT: {claude_received}!"

        print("✅ SUCCESS: Explicit message isolation verified! Claude did NOT receive GPT's message.")

        await sender.close()
        await claude.close()
        await gpt.close()
        await console.close()
    finally:
        await hub.stop()
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(test_explicit_message_isolation())
