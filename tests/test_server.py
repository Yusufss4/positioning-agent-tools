from __future__ import annotations

import asyncio
import json

import pytest

from positioning_agent_tools import server

EXPECTED_TOOLS = {
    "list_scenarios", "describe_venue", "engine_config", "simulate_scenario", "run_replay", "error_breakdown",
    "plot_run", "compare_filters", "ab_test", "run_regression",
}


def test_tools_are_registered_with_descriptions():
    listed = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in listed}
    assert EXPECTED_TOOLS <= names
    for t in listed:
        assert t.description and len(t.description) > 30, t.name


def test_resources_are_registered():
    uris = {str(r.uri) for r in asyncio.run(server.mcp.list_resources())}
    assert {"ipe://docs/agents", "ipe://docs/formats", "ipe://results/latest"} <= uris


@pytest.fixture
def wired(tools, monkeypatch):
    monkeypatch.setattr(server, "_tools", tools)


def _text(result) -> str:
    return "".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def test_call_tool_through_mcp(wired):
    result = asyncio.run(server.mcp.call_tool("list_scenarios", {}))
    assert "office_field_issue" in _text(result)


def test_errors_are_returned_not_raised(wired):
    result = asyncio.run(server.mcp.call_tool("run_replay", {"scenario": "does_not_exist"}))
    assert "unknown scenario" in _text(result)


def test_plot_run_returns_image_content(wired, tools):
    run = tools.run_replay(scenario="office_nominal", filter="wcl")
    result = asyncio.run(server.mcp.call_tool("plot_run", {"run_id": run["run_id"]}))
    assert any(getattr(c, "type", "") == "image" for c in result.content)


def test_resource_read(wired):
    contents = list(asyncio.run(server.mcp.read_resource("ipe://results/latest")))
    assert json.loads(contents[0].content)["results"]
