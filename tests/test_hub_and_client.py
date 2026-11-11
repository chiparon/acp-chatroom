import asyncio
import os
import tempfile
from acp.hub import ACPHub
from acp.client import ACPClient
from acp.protocol import MessageMode, AgentStatus, EventType


async def run_hub_and_client_test():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    hub = ACPHub(host="127.0.0.1", port=18765, db_path=db_path)
    await hub.start()

    try:
        uri = "ws://127.0.0.1:18765"

        # Client 1: @antigravity
        c1 = ACPClient(uri=uri, handle="@antigravity")
        await c1.connect()

        # Client 2: @codex
        c2 = ACPClient(uri=uri, handle="@codex")
        await c2.connect()

        # Check roster
        roster = await c1.get_roster()
        handles = [r["handle"] for r in roster]
        assert "@antigravity" in handles
        assert "@codex" in handles

        # Prepare listener queue on c2 for incoming events
        received_explicit = asyncio.Queue()

        def on_explicit(data):
            received_explicit.put_nowait(data)

        c2.on(EventType.EXPLICIT_MESSAGE.value, on_explicit)

        # 1. c1 sends silent message
        res_silent = await c1.send_message(
            target="#general",
            content="Hello silent world",
            mode=MessageMode.SILENT,
            topic="general",
        )
        assert res_silent.get("status") == "ok"

        # 2. c2 reads topic general
        msgs = await c2.read_board(topic="general", unread_only=True)
        assert len(msgs) == 1
        assert msgs[0]["content"] == "Hello silent world"

        # 3. c2 reads again (unread_only), should be 0
        msgs2 = await c2.read_board(topic="general", unread_only=True)
        assert len(msgs2) == 0

        # 4. c1 sends explicit message to @codex
        res_exp = await c1.send_message(
            target="@codex",
            content="Please write unit tests",
            mode=MessageMode.EXPLICIT,
            topic="general",
        )
        assert res_exp.get("status") == "ok"
        assert res_exp.get("delivered") is True

        # 5. Wait for c2 to receive the explicit message event
        event = await asyncio.wait_for(received_explicit.get(), timeout=3.0)
        assert event["event"] == "explicit_message"
        assert event["message"]["sender"] == "@antigravity"
        assert event["message"]["target"] == "@codex"
        assert event["message"]["content"] == "Please write unit tests"

        # 6. Test presence update
        await c2.set_presence(AgentStatus.BUSY)
        roster_after = await c1.get_roster()
        codex_status = next(r["status"] for r in roster_after if r["handle"] == "@codex")
        assert codex_status == "busy"

        await c1.close()
        await c2.close()
        print("All Hub and Client integration tests passed successfully!")

    finally:
        await hub.stop()
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(run_hub_and_client_test())
