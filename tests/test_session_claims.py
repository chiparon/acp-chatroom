from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from click.testing import CliRunner

from acp.agents import agent_registry
from acp.cli import cli
from acp.client import ACPClient
from acp.hub import ACPHub
from acp.launcher import AgentLauncher
from acp.protocol import SessionKey
from acp.storage import Storage


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


async def test_hub_clears_stale_roster_on_startup_and_disconnect(tmp_path):
    db_path = str(tmp_path / "room.db")
    key = SessionKey("host-a", "codex", "/home/a", str(uuid4()))
    pre = Storage(db_path)
    pre.update_roster("@codex1", "idle", key)
    pre.update_roster("@antig", "busy")

    hub = ACPHub(db_path=db_path)
    assert hub.storage.get_roster() == []
    assert hub.storage.previous_handle(key) == "@codex1"

    # Ad-hoc handle @antig is freed after disconnect; @codex1 stays reserved for its session.
    owner = Mock()
    assert (await hub.process_action(owner, "register", {"handle": "@antig"}))["status"] == "ok"
    assert [r["handle"] for r in hub.storage.get_roster()] == ["@antig"]
    await hub._cleanup_connection(owner)
    assert hub.storage.get_roster() == []

    # Claiming a new codex session skips @codex1 because session_handles still owns it.
    fresh = await hub.process_action(Mock(), "claim", {"harness": "codex"})
    assert fresh["handle"] == "@codex2"


async def test_hub_remove_and_prune_actions(tmp_path):
    hub = ACPHub(db_path=str(tmp_path / "room.db"))
    antig_ws, codex_ws, pending_ws, caller_ws = Mock(), Mock(), Mock(), Mock()
    antig_ws.close = AsyncMock()
    pending_ws.close = AsyncMock()

    assert (await hub.process_action(antig_ws, "register", {"handle": "@antig"}))["status"] == "ok"
    assert (await hub.process_action(codex_ws, "register", {"handle": "@codex"}))["status"] == "ok"
    assert (await hub.process_action(pending_ws, "claim", {"harness": "claude"}))["handle"] == "@claude1"
    hub.storage.update_roster("@stale", "offline")

    pruned = await hub.process_action(caller_ws, "prune", {})
    assert pruned == {"status": "ok", "action": "prune", "removed": ["@stale"], "count": 1}

    removed = await hub.process_action(caller_ws, "remove", {"target": "antig"})
    assert removed == {"status": "ok", "action": "remove", "handle": "@antig", "was_online": True}
    antig_ws.close.assert_awaited_once()
    assert "@antig" not in hub.clients
    assert [r["handle"] for r in hub.storage.get_roster()] == ["@codex"]

    removed_pending = await hub.process_action(caller_ws, "remove", {"target": "@claude1"})
    assert removed_pending == {"status": "ok", "action": "remove", "handle": "@claude1", "was_online": False}
    pending_ws.close.assert_awaited_once()
    assert pending_ws not in hub.claims

    missing = await hub.process_action(caller_ws, "remove", {"target": "@nobody"})
    assert missing["status"] == "error"


def test_cli_remove_and_prune_online_and_offline_fallback(tmp_path, monkeypatch):
    db_path = tmp_path / "room.db"
    key = SessionKey("host-a", "codex", "/home/a", str(uuid4()))
    storage = Storage(str(db_path))
    storage.update_roster("@antig", "idle")
    storage.update_roster("@codex", "busy", key)

    runner = CliRunner()

    # Offline fallback removes from local SQLite DB when Hub connection raises OSError.
    async def offline_request(self, payload, *, on_response=None):
        raise OSError("Hub offline")

    monkeypatch.setattr(ACPClient, "request", offline_request)
    res_rm = runner.invoke(cli, ["remove", "antig", "--db", str(db_path)])
    assert res_rm.exit_code == 0
    assert "Removed @antig from offline database" in res_rm.output
    assert [r["handle"] for r in storage.get_roster()] == ["@codex"]

    res_prune = runner.invoke(cli, ["prune", "--db", str(db_path)])
    assert res_prune.exit_code == 0
    assert "Cleared 1 stale entry(ies)" in res_prune.output
    assert storage.get_roster() == []
    assert storage.previous_handle(key) == "@codex"

    # Online Hub mode sends remove/prune actions and supports ACP_AGENT_HANDLE via leave/remove.
    requests = []

    async def online_request(self, payload, *, on_response=None):
        requests.append(payload)
        if payload["action"] == "remove":
            return {"status": "ok", "handle": payload["target"], "was_online": True}
        return {"status": "ok", "removed": ["@stale"], "count": 1}

    monkeypatch.setattr(ACPClient, "request", online_request)
    res_leave = runner.invoke(cli, ["leave"], env={"ACP_AGENT_HANDLE": "@claudecode"})
    assert res_leave.exit_code == 0
    assert "Removed @claudecode" in res_leave.output
    res_online_prune = runner.invoke(cli, ["prune"])
    assert res_online_prune.exit_code == 0
    assert "Pruned 1 offline agent(s): @stale" in res_online_prune.output
    assert [r["action"] for r in requests] == ["remove", "prune"]
