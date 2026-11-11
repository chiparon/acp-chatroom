import asyncio
import json
import os
import shlex
import sys
from uuid import uuid4

import pytest

from acp.client import ACPClient
from acp.hub import ACPHub


pytestmark = pytest.mark.dt


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.03)
    await asyncio.wait_for(poll(), 20)


async def test_resume_cli_contention_and_restart(tmp_path):
    # The native stand-in records every invocation. A duplicate launch is visible
    # even if that process exits before it could appear in the Hub roster.
    script = tmp_path / "agent.py"
    script.write_text(
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['ACP_TEST_DIR'])\n"
        "native_id = sys.argv[-1]\n"
        "if native_id == os.environ['BAD_SESSION']:\n"
        "    sys.exit(23)\n"
        "record = root / f'{native_id}.{os.getpid()}.json'\n"
        "record.with_suffix('.tmp').write_text(json.dumps([os.environ['ACP_AGENT_HANDLE'], sys.argv[1:]]))\n"
        "record.with_suffix('.tmp').replace(record)\n"
        "while not (root / ('stop-' + native_id)).exists() and not (root / 'stop-all').exists():\n"
        "    time.sleep(.03)\n", "utf-8",
    )
    if os.name == "nt":
        (tmp_path / "codex.cmd").write_text(f'@"{sys.executable}" "{script}" %*\n', "utf-8")
    else:
        shim = tmp_path / "codex"
        shim.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
        shim.chmod(0o755)
    original, second, third, bad = (str(uuid4()) for _ in range(4))
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "ACP_TEST_DIR": str(tmp_path), "CODEX_HOME": str(tmp_path / "config"), "BAD_SESSION": bad,
           "PYTHONUTF8": "1"}
    processes = []
    hub = ACPHub(port=0, db_path=str(tmp_path / "hub.db"))
    await hub.start()

    async def run(native_id, *options):
        uri = f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "acp.cli", "resume", "codex", native_id, *options,
            "--non-interactive", "--uri", uri, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        processes.append(proc)
        return proc

    def records(native_id):
        return [json.loads(p.read_text()) for p in tmp_path.glob(f"{native_id}.*.json")]

    occupant = None
    try:
        proc = await run(original, "--as", "gpt1")
        await until(lambda: "@gpt1" in hub.clients and records(original))
        (tmp_path / ("stop-" + original)).touch()
        assert await asyncio.wait_for(proc.wait(), 15) == 0
        await hub.stop()

        # Only historical bindings survive a restart. A free old name is reclaimed.
        hub = ACPHub(port=0, db_path=str(tmp_path / "hub.db"))
        await hub.start()
        proc = await run(original)
        assert await asyncio.wait_for(proc.wait(), 15) == 0
        assert [r[0] for r in records(original)] == ["@gpt1", "@gpt1"]
        (tmp_path / ("stop-" + original)).unlink()
        await until(lambda: not hub.clients)
        occupant = ACPClient(f"ws://127.0.0.1:{hub.server.sockets[0].getsockname()[1]}", "@GPT1")
        await occupant.connect()

        # Two simultaneous recalls of one session, two other sessions, one occupied name.
        one, two, _, _ = await asyncio.gather(run(original), run(original), run(second, "--as", "gpt1"),
                                               run(third, "--as", "gpt1"))
        await until(lambda: len(hub.clients) == 4 and len(records(original)) == 3
                    and records(second) and records(third))
        current = [r for r in records(original) if r[0] != "@gpt1"]
        current += records(second) + records(third)
        assert {r[0] for r in current} == {"@codex1", "@codex2", "@codex3"}
        assert {r[1][-1] for r in current} == {original, second, third}
        assert all(r[1][-2] == "resume" for r in current)
        assert "@gpt1" in hub.clients
        await until(lambda: one.returncode is not None or two.returncode is not None)
        loser = one if one.returncode is not None else two
        output, _ = await loser.communicate()
        assert loser.returncode == 0 and b"is already " in output

        duplicate = await run(original, "--as", "different")
        output, _ = await asyncio.wait_for(duplicate.communicate(), 15)
        handle = next(r[0] for r in current if r[1][-1] == original)
        assert duplicate.returncode == 0 and f"{handle} is already online" in output.decode()
        assert len(records(original)) == 3

        failed = await run(bad)
        output, _ = await asyncio.wait_for(failed.communicate(), 15)
        assert failed.returncode != 0 and b"23" in output
        await until(lambda: len(hub.claims) == 3)
    finally:
        (tmp_path / "stop-all").touch()
        for proc in processes:
            try:
                await asyncio.wait_for(proc.wait(), 15)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        if occupant:
            await occupant.close()
        await hub.stop()
