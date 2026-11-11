import os
import tempfile
from acp.protocol import Message, MessageMode, format_explicit_prompt
from acp.storage import Storage


def test_storage_basic():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
        db_path = tf.name

    try:
        storage = Storage(db_path)

        # 1. Save messages
        msg1 = Message(
            sender="@antigravity",
            target="#general",
            mode=MessageMode.SILENT,
            topic="general",
            content="Hello world",
        )
        saved1 = storage.save_message(msg1)
        assert saved1.id is not None
        assert saved1.id == 1

        msg2 = Message(
            sender="@antigravity",
            target="@codex",
            mode=MessageMode.EXPLICIT,
            topic="general",
            content="Please review this PR",
        )
        saved2 = storage.save_message(msg2)
        assert saved2.id == 2

        # 2. Test reading topic messages and cursor
        msgs = storage.read_topic_messages(topic="general", reader="@codex", unread_only=True)
        assert len(msgs) == 2
        # Cursor should now be 2
        cursor = storage.get_cursor("@codex", "general")
        assert cursor == 2

        # Reading again unread_only should yield 0
        msgs_unread = storage.read_topic_messages(topic="general", reader="@codex", unread_only=True)
        assert len(msgs_unread) == 0

        # Post a 3rd message
        msg3 = Message(
            sender="@codex",
            target="#general",
            mode=MessageMode.SILENT,
            topic="general",
            content="Review completed",
        )
        storage.save_message(msg3)

        # Reading again unread_only should yield 1 message (id=3)
        msgs_new = storage.read_topic_messages(topic="general", reader="@codex", unread_only=True)
        assert len(msgs_new) == 1
        assert msgs_new[0].content == "Review completed"

        # 3. Test roster
        storage.update_roster("@codex", "busy")
        roster = storage.get_roster()
        assert len(roster) == 1
        assert roster[0]["handle"] == "@codex"
        assert roster[0]["status"] == "busy"

        # 4. Test explicit prompt formatting
        prompt = format_explicit_prompt("@codex", "Please review")
        assert "@codex" in prompt
        assert "Please review" in prompt
        print("All storage tests passed!")
    finally:
        try:
            if os.path.exists(db_path):
                os.remove(db_path)
        except Exception:
            pass


if __name__ == "__main__":
    test_storage_basic()
