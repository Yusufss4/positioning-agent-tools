"""End to end: spawn `positioning-mcp` as an agent would and talk to it over stdio."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _server_command() -> str:
    exe = Path(sys.executable).parent / "positioning-mcp"
    return str(exe) if exe.exists() else (shutil.which("positioning-mcp") or pytest.skip("not installed"))


async def _session(paths, body):
    env = dict(os.environ, IPE_ROOT=str(paths.root), IPE_BUILD=str(paths.build), IPE_WORKDIR=str(paths.workdir))
    params = StdioServerParameters(command=_server_command(), env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        return init, await body(session)


def test_stdio_round_trip(paths):
    async def body(session: ClientSession):
        tools = await session.list_tools()
        result = await session.call_tool(
            "run_replay", {"scenario": "office_nominal", "filter": "pf", "seed": 1})
        return tools, result

    init, (tools, result) = asyncio.run(_session(paths, body))
    assert init.server_info.name == "positioning-agent-tools"
    assert "ab_test" in {t.name for t in tools.tools}
    text = "".join(c.text for c in result.content if getattr(c, "type", "") == "text")
    metrics = json.loads(text)["metrics"]
    assert metrics["cep95_m"] < 3.0
