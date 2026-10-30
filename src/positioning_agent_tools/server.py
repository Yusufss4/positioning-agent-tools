"""MCP server: lets Claude Code, Cursor or Codex run positioning experiments instead of guessing.

Run with `positioning-mcp` (stdio transport). Configure with IPE_ROOT / IPE_BUILD / IPE_WORKDIR.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import Image, MCPServer

from .engine import EngineError, EngineTools

INSTRUCTIONS = """\
Tools for evaluating the indoor-positioning-engine: pedestrian dead reckoning fused with BLE or Wi-Fi RSSI and
an optional barometer, on single- or multi-floor venues, by a particle filter (pf, pf-nomap), an EKF and a
weighted centroid (wcl). Use them to measure, not to guess:

- Start with list_scenarios; each scenario description says which real-world fault it injects. Scenarios on
  office_two_floor add stairs, a lift, barometer drift and floor bleed.
- Never judge an algorithm change from one seed. Use ab_test (>= 5 seeds, all scenarios) before claiming
  an improvement, and report regressions in other scenarios too.
- On multi-floor venues, CEP only counts fixes on the correct floor; floor_accuracy reports the rest. A change
  that improves CEP while losing floor accuracy is a trade-off, not a win. coverage95 says whether the
  reported uncertainty is honest (~0.95); lower means overconfident.
- To investigate a bad run: run_replay, then error_breakdown to see when and where it failed (including
  wrong-floor windows), then plot_run.
- Separate model limitations (wrong path-loss/wall/slab model, step length, heading) from data problems (dead or
  moved beacons, magnetic anomalies, barometer drift) before proposing a fix. simulate_scenario overrides let
  you add or remove a single fault to test a hypothesis.
- Simulated results are optimistic: the simulator and the filters share one radio model. Check real phone
  walks (list_recordings, run_replay(recording=...), benchmark_recordings) before claiming field accuracy.
  Real walks are Wi-Fi positioning and need config_file="msil_wifi"; ground truth is only at waypoints.
- Choose settings on the 'tune' manifest and report results on 'holdout'; never tune on holdout.
- run_regression is the CI gate (suite simulated, real or all); never suggest regenerating either baseline
  (tests/regression/baseline.json, msil_baseline.json) to make it pass.
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
    """Size, walls, levels, connectors (stairs, lifts), mapped wall/slab attenuation and transmitter positions.

    Takes a venue name ('office_floor', 'office_two_floor', 'airport_hall'), a real recording's floor
    ('site1_F2', see list_recordings) or a venue file path.
    """
    return _guard(tools().describe_venue, venue)


@mcp.tool()
def engine_config(preset: str = "pf", config_patch: dict | None = None, config_file: str | None = None) -> dict:
    """Effective engine configuration for a preset (pf, pf-nomap, ekf, wcl).

    `config_file` (a name from list_configs, e.g. 'msil_wifi', or a path) is applied first, then the
    `config_patch` JSON merge patch. Use this to discover tunable parameter names before writing a patch.
    """
    return _guard(tools().engine_config, preset, config_patch, config_file)


@mcp.tool()
def list_configs() -> list[dict]:
    """Config patch files shipped with the engine (data/configs), e.g. msil_wifi for real Wi-Fi walks."""
    return _guard(tools().list_configs)


@mcp.tool()
def list_recordings() -> dict:
    """Real phone walks (Microsoft Indoor Location dataset, two shopping malls) that can be replayed.

    Lists the walks committed with the engine (for run_replay(recording=...)) and, if the full converted
    dataset is present, the tune/holdout manifests for benchmark_recordings.
    """
    return _guard(tools().list_recordings)


@mcp.tool()
def simulate_scenario(scenario: str, seed: int | None = None, overrides: dict | None = None) -> dict:
    """Generate a synthetic trace (IMU, BLE, barometer if the scenario has one, ground truth) for a scenario.

    `overrides` is merged into the scenario JSON, e.g. {"ble": {"dead_beacons": ["C3"]}},
    {"ble": {"device_offset_db": -6}} or {"baro": {"drift_hpa_per_hour": 0}}, to test one hypothesis at a time.
    """
    return _guard(tools().simulate_scenario, scenario, seed, overrides)


@mcp.tool()
def run_replay(scenario: str | None = None, filter: str = "pf", seed: int | None = None,
               config_patch: dict | None = None, scenario_overrides: dict | None = None,
               recording: str | None = None, config_file: str | None = None,
               trace: str | None = None, venue: str | None = None) -> dict:
    """Run one scenario (simulated in memory), a real recording, or a trace file through a filter.

    Give exactly one of `scenario`, `recording` (an id from list_recordings; its recommended config_file is
    used unless you pass one, or 'none') or `trace` with `venue`. Returns accuracy metrics (CEP50/CEP95/mean,
    convergence, floor_accuracy, coverage95) and a run_id for error_breakdown and plot_run.
    One run is one sample: use ab_test, compare_filters or benchmark_recordings for conclusions.
    """
    return _guard(tools().run_replay, scenario=scenario, trace=trace, venue=venue, filter=filter, seed=seed,
                  config_patch=config_patch, scenario_overrides=scenario_overrides, recording=recording,
                  config_file=config_file)


@mcp.tool()
def error_breakdown(run_id: str, threshold_m: float = 3.0, min_duration_s: float = 1.0) -> dict:
    """Time windows where the error stayed above a threshold or the estimate was on the wrong floor, with the
    true and estimated position (and level) at the peak and the beacons nearest to each on that level.
    Useful for spotting moved beacons, divergence and floor-detection failures."""
    return _guard(tools().error_breakdown, run_id, threshold_m, min_duration_s)


@mcp.tool()
def plot_run(run_id: str) -> Image | dict:
    """Render the floor plan of each visited level with the true and estimated tracks, plus error over time
    (wrong-floor intervals shaded), as a PNG."""
    try:
        return Image(data=tools().plot_run(run_id), format="png")
    except EngineError as exc:
        return {"error": str(exc)}


@mcp.tool()
def compare_filters(scenario: str, filters: list[str] | None = None, seeds: int = 5,
                    config_patch: dict | None = None, scenario_overrides: dict | None = None,
                    config_file: str | None = None) -> dict:
    """Mean CEP50/CEP95/convergence (plus floor accuracy and coverage95 where they apply) of several filters
    on one scenario, averaged over seeds.

    `scenario_overrides` (JSON merge patch on the scenario; null deletes a key) lets you remove or add a
    single fault to test a hypothesis, e.g. {"ble": {"moved_beacons": null}}.
    """
    return _guard(tools().compare_filters, scenario, filters, seeds, config_patch, scenario_overrides, config_file)


@mcp.tool()
def ab_test(config_patch: dict, presets: list[str] | None = None, seeds: int = 5,
            scenarios: list[str] | None = None, config_file: str | None = None) -> dict:
    """A/B test a configuration change across scenarios on identical seeds.

    A = defaults (+ config_file if given), B = A + config_patch (JSON merge patch, e.g.
    {"pf": {"num_particles": 500}}). Returns per-scenario CEP and floor-accuracy deltas with a verdict for B
    (better, worse, mixed or same) and a summary. "mixed" means the metrics moved in opposite directions
    (e.g. CEP95 better but floor accuracy worse); treat it as a trade-off, not a win.
    """
    return _guard(tools().ab_test, config_patch, presets, seeds, scenarios, config_file=config_file)


@mcp.tool()
def benchmark_recordings(manifest: str = "holdout", presets: list[str] | None = None,
                         config_file: str | None = "msil_wifi", config_patch: dict | None = None) -> dict:
    """Replay real phone walks through each preset (default pf, wcl); results per floor plus 'all'.

    `manifest`: 'holdout' (report results here), 'tune' (choose settings here), 'all', 'demo' (only the walks
    committed in data/real; works without the full dataset) or a manifest path. Medians over walks are the
    headline numbers. The full holdout set takes under a minute.
    """
    return _guard(tools().benchmark_recordings, manifest, presets, config_file, config_patch)


@mcp.tool()
def run_regression(seeds: int = 3, suite: str = "simulated", config_file: str | None = None) -> dict:
    """Run the CI accuracy gates.

    suite='simulated': every scenario and preset against tests/regression/baseline.json (CEP50, CEP95, floor
    accuracy). suite='real': held-out real walks with msil_wifi against tests/regression/msil_baseline.json
    (skipped if the dataset is not converted). suite='all': both. `config_file` (simulated suite only) shows
    what would change if that config became the default.
    """
    return _guard(tools().run_regression, seeds, suite, config_file)


@mcp.resource("ipe://docs/agents", mime_type="text/markdown")
def agents_md() -> str:
    """The engine repository's AGENTS.md: build commands and evaluation rules."""
    return (tools().paths.root / "AGENTS.md").read_text()


@mcp.resource("ipe://docs/formats", mime_type="text/markdown")
def formats_md() -> str:
    """Venue (incl. levels and connectors), scenario, trace, config, manifest and replay output formats."""
    return (tools().paths.root / "docs" / "formats.md").read_text()


@mcp.resource("ipe://docs/design", mime_type="text/markdown")
def design_md() -> str:
    """Engine design: filters, sensor models, multi-floor handling and known model limitations."""
    return (tools().paths.root / "docs" / "design.md").read_text()


@mcp.resource("ipe://docs/experiments", mime_type="text/markdown")
def experiments_md() -> str:
    """Experiment log: what was tried, with numbers, and which changes were adopted or rejected."""
    return (tools().paths.root / "docs" / "experiments.md").read_text()


@mcp.resource("ipe://docs/real_data", mime_type="text/markdown")
def real_data_md() -> str:
    """Real phone traces: dataset, conversion, tune/holdout protocol and results."""
    return (tools().paths.root / "docs" / "real_data.md").read_text()


@mcp.resource("ipe://results/latest", mime_type="application/json")
def latest_results() -> str:
    """Last published benchmark (docs/results.json in the engine repo)."""
    return json.dumps(json.loads((tools().paths.root / "docs" / "results.json").read_text()), indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
