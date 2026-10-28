"""MCP server: lets Claude Code, Cursor or Codex run positioning experiments instead of guessing.

Run with `positioning-mcp` (stdio transport). Configure with IPE_ROOT / IPE_BUILD / IPE_WORKDIR.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from .engine import EngineError, EngineTools

INSTRUCTIONS = """\
Tools for evaluating the indoor-positioning-engine (particle filter, EKF and weighted centroid fusing PDR and
BLE RSSI). Use them to measure, not to guess:

- Start with list_scenarios; each scenario description says which real-world fault it injects.
- Never judge an algorithm change from one seed. Use ab_test (>= 5 seeds, all scenarios) before claiming
  an improvement, and report regressions in other scenarios too.
- To investigate a bad run: run_replay, then error_breakdown to see when and where it failed, then plot_run.
- Separate model limitations (wrong path-loss/wall model, step length, heading) from data problems (dead or
  moved beacons, magnetic anomalies) before proposing a fix. simulate_scenario overrides let you add or
  remove a single fault to test a hypothesis.
- run_regression is the CI gate; do not suggest regenerating the baseline to make it pass.
"""

mcp = MCPServer("positioning-agent-tools", instructions=INSTRUCTIONS)
_tools: EngineTools | None = None


def tools() -> EngineTools:
    global _tools
    if _tools is None:
        _tools = EngineTools()
    return _tools


def _guard(fn, *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except EngineError as exc:
        return {"error": str(exc)}


@mcp.tool()
def list_scenarios() -> list[dict]:
    """List simulation scenarios with the venue they use and the real-world effect each one injects."""
    return _guard(tools().list_scenarios)


@mcp.tool()
def describe_venue(venue: str) -> dict:
    """Floor size, wall count, mapped wall attenuation and beacon positions of a venue (e.g. 'office_floor')."""
    return _guard(tools().describe_venue, venue)


@mcp.tool()
def engine_config(preset: str = "pf", config_patch: dict | None = None) -> dict:
    """Effective engine configuration for a preset (pf, pf-nomap, ekf, wcl), optionally with a JSON merge patch.

    Use this to discover tunable parameter names before writing a config_patch.
    """
    return _guard(tools().engine_config, preset, config_patch)


@mcp.tool()
def simulate_scenario(scenario: str, seed: int | None = None, overrides: dict | None = None) -> dict:
    """Generate a synthetic trace (IMU, BLE, ground truth) for a scenario.

    `overrides` is merged into the scenario JSON, e.g. {"ble": {"dead_beacons": ["C3"]}} or
    {"ble": {"device_offset_db": -6}}, to test one hypothesis at a time.
    """
    return _guard(tools().simulate_scenario, scenario, seed, overrides)


@mcp.tool()
def run_replay(scenario: str | None = None, filter: str = "pf", seed: int | None = None,
               config_patch: dict | None = None, scenario_overrides: dict | None = None,
               trace: str | None = None, venue: str | None = None) -> dict:
    """Run one scenario (simulated in memory) or a recorded trace file through a filter.

    Returns accuracy metrics (CEP50/CEP95/mean/convergence) and a run_id for error_breakdown and plot_run.
    One run is one sample: use ab_test or compare_filters for conclusions.
    """
    return _guard(tools().run_replay, scenario=scenario, trace=trace, venue=venue, filter=filter, seed=seed,
                  config_patch=config_patch, scenario_overrides=scenario_overrides)


@mcp.tool()
def error_breakdown(run_id: str, threshold_m: float = 3.0, min_duration_s: float = 1.0) -> dict:
    """Time windows where the error stayed above a threshold, with the true and estimated position at the
    peak and the beacons nearest to each. Useful for spotting moved beacons and divergence."""
    return _guard(tools().error_breakdown, run_id, threshold_m, min_duration_s)


@mcp.tool()
def plot_run(run_id: str) -> Image | dict:
    """Render the floor plan with the true and estimated tracks, plus error over time, as a PNG."""
    try:
        return Image(data=tools().plot_run(run_id), format="png")
    except EngineError as exc:
        return {"error": str(exc)}


@mcp.tool()
def compare_filters(scenario: str, filters: list[str] | None = None, seeds: int = 5,
                    config_patch: dict | None = None, scenario_overrides: dict | None = None) -> dict:
    """Mean CEP50/CEP95/convergence of several filters on one scenario, averaged over seeds.

    `scenario_overrides` (JSON merge patch on the scenario; null deletes a key) lets you remove or add a
    single fault to test a hypothesis, e.g. {"ble": {"moved_beacons": null}}.
    """
    return _guard(tools().compare_filters, scenario, filters, seeds, config_patch, scenario_overrides)


@mcp.tool()
def ab_test(config_patch: dict, presets: list[str] | None = None, seeds: int = 5,
            scenarios: list[str] | None = None) -> dict:
    """A/B test a configuration change across scenarios on identical seeds.

    A = current defaults, B = defaults + config_patch (JSON merge patch, e.g. {"pf": {"num_particles": 500}}).
    Returns per-scenario CEP deltas with a verdict for B (better, worse, mixed or same) and a summary.
    "mixed" means CEP50 and CEP95 moved in opposite directions; treat it as a trade-off, not a win.
    """
    return _guard(tools().ab_test, config_patch, presets, seeds, scenarios)


@mcp.tool()
def run_regression(seeds: int = 3) -> dict:
    """Run the CI accuracy gate: every scenario and preset against tests/regression/baseline.json."""
    return _guard(tools().run_regression, seeds)


@mcp.resource("ipe://docs/agents", mime_type="text/markdown")
def agents_md() -> str:
    """The engine repository's AGENTS.md: build commands and evaluation rules."""
    return (tools().paths.root / "AGENTS.md").read_text()


@mcp.resource("ipe://docs/formats", mime_type="text/markdown")
def formats_md() -> str:
    """Venue, scenario, trace and replay output formats."""
    return (tools().paths.root / "docs" / "formats.md").read_text()


@mcp.resource("ipe://results/latest", mime_type="application/json")
def latest_results() -> str:
    """Last published benchmark (docs/results.json in the engine repo)."""
    return json.dumps(json.loads((tools().paths.root / "docs" / "results.json").read_text()), indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
