from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from acp.agents import agent_registry
from acp.hub import ACPHub
from acp.launcher import AgentLauncher
from acp.protocol import SessionKey


@pytest.mark.parametrize(("agent", "native_args", "canonical"), [
    ("codex", ["resume"], "codex"),
    ("claudecode", ["--resume"], "claude"),
    ("antigravity", ["--conversation"], "agy"),
])
def test_resume_profile(agent, native_args, canonical):
    session_id = uuid4()
    launcher = AgentLauncher(agent, None, session_id=str(session_id).upper())
    assert launcher.argv[-2:] == [*native_args, str(session_id)]
    assert launcher.session.harness == canonical
    assert launcher.session == agent_registry()[canonical].session_key(str(session_id))


def test_native_id_and_profile_scope(tmp_path, monkeypatch):
    profile = agent_registry()["codex"]
    with pytest.raises(ValueError, match="native session UUID"):
        profile.session_key("--last")
    with pytest.raises(ValueError, match="does not define"):
        replace(profile, resume_args=()).session_key(str(uuid4()))
    native_id = str(uuid4())
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "a"))
    a = profile.session_key(native_id)
    legacy = AgentLauncher(f"codex --no-daemon resume {native_id}", "gpt1")
    assert legacy.session == a and legacy.needs_claim
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "b"))
    assert a != profile.session_key(native_id)


async def test_pending_claim_excludes_other_registrations_and_releases_on_disconnect(tmp_path):
    hub = ACPHub(db_path=str(tmp_path / "room.db"))
    session = SessionKey("host", "codex", "scope", str(uuid4()))
    data = {"harness": "codex", "session": session._asdict()}
    owner, other = Mock(), Mock()
    first = await hub.process_action(owner, "claim", data)
    assert first["claimed"] and first["handle"] == "@codex1"
    assert await hub.process_action(other, "claim", data) == {
        "status": "ok", "claimed": False, "state": "starting", "handle": "@codex1",
    }
    assert (await hub.process_action(other, "register", {"handle": "@CODEX1"}))["status"] == "error"
    assert (await hub.process_action(owner, "register", {"handle": "@other"}))["status"] == "error"
    assert not hub.clients and not hub.storage.get_roster()
    assert hub.storage.previous_handle(session) is None
    await hub._cleanup_connection(owner)
    again = await hub.process_action(other, "claim", data)
    assert again == first


def test_distinct_hosts_and_profiles_do_not_share_history(tmp_path):
    hub = ACPHub(db_path=str(tmp_path / "room.db"))
    key = SessionKey("host-a", "codex", "/home/a", str(uuid4()))
    hub.storage.update_roster("@historical", "offline", key)
    for distinct in (key._replace(host="host-b"), key._replace(scope="/home/b"), key._replace(harness="agy")):
        assert hub.storage.previous_handle(distinct) is None
    assert hub.storage.previous_handle(key) == "@historical"


async def test_unnamed_launches_get_separate_names(tmp_path):
    hub = ACPHub(db_path=str(tmp_path / "room.db"))
    launcher = AgentLauncher("codex", None)
    assert launcher.needs_claim and launcher.session is None
    names = [await hub.process_action(Mock(), "claim", {"harness": "codex"}) for _ in range(2)]
    assert [result["handle"] for result in names] == ["@codex1", "@codex2"]
