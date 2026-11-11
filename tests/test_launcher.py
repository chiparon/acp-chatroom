import asyncio
import os
import sys
import tempfile
import time
from acp.hub import ACPHub
from acp.client import ACPClient
from acp.protocol import MessageMode
from acp.launcher import AgentLauncher


async def test_launcher_prompt_injection():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    hub = ACPHub(host="127.0.0.1", port=18766, db_path=db_path)
    await hub.start()

    uri = "ws://127.0.0.1:18766"
    cmd = f"{sys.executable} -u tests/mock_agent.py"

    # Create launcher for mock agent
    launcher = AgentLauncher(
        command=cmd,
        handle="@mock_agent",
        uri=uri,
        idle_timeout=1.0,
        interactive=False,
    )

    launcher_task = asyncio.create_task(launcher.run())
    # Give launcher a second to connect and start process
    await asyncio.sleep(1.0)

    try:
        # Sender client
        sender = ACPClient(uri=uri, handle="@human_tester")
        await sender.connect()

        # Check roster
        roster = await sender.get_roster()
        handles = [r["handle"] for r in roster]
        assert "@mock_agent" in handles, f"Expected @mock_agent in {handles}"

        # Send explicit message to @mock_agent
        print("\nSending explicit message to @mock_agent ...")
        resp = await sender.send_message(
            target="@mock_agent",
            content="Please write code for feature X",
            mode=MessageMode.EXPLICIT,
            topic="general",
        )
        assert resp.get("status") == "ok"
        assert resp.get("delivered") is True

        # Wait for injection and processing
        await asyncio.sleep(1.5)

        # Send EXIT command
        launcher.write_input("EXIT\n")
        await asyncio.sleep(0.5)

        print("\nLauncher prompt injection test successful!")
    finally:
        launcher._running = False
        launcher_task.cancel()
        await hub.stop()
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(test_launcher_prompt_injection())
