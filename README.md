# positioning-agent-tools

An [MCP](https://modelcontextprotocol.io) server that lets AI coding agents (Claude Code, Cursor, Codex)
run experiments on the [indoor-positioning-engine](https://github.com/Yusufss4/indoor-positioning-engine)
instead of reasoning about positioning accuracy from code alone.

An agent asked "why is the blue dot jumping in this venue?" can simulate the venue, replay it through
each filter, find the time window where the error spikes, see which beacons sit next to the wrong
estimate, change one fault in the scenario to test a hypothesis, and A/B test a fix across every
scenario on identical seeds. Every tool returns numbers that come from the engine's own replay and
metrics code, so the agent's claims can be checked.

## Tools

| Tool | What it does |
|---|---|
| `list_scenarios` | Scenarios, their venue, and the real-world fault each one injects |
| `describe_venue` | Floor size, walls, mapped wall loss and beacon positions |
| `engine_config` | Effective config of a preset (`pf`, `pf-nomap`, `ekf`, `wcl`) with an optional JSON merge patch |
| `simulate_scenario` | Synthetic IMU/BLE/ground-truth trace, with scenario overrides (e.g. kill one beacon) |
| `run_replay` | One scenario or recorded trace through one filter; metrics plus a `run_id` |
| `error_breakdown` | Windows where error exceeded a threshold, with the beacons nearest the truth and the estimate |
| `plot_run` | PNG of the floor plan, true and estimated tracks, and error over time |
| `compare_filters` | Several filters on one scenario, averaged over seeds |
| `ab_test` | Config change vs defaults on every scenario, same seeds; verdict per scenario: better, worse, mixed or same |
| `run_regression` | The engine's CI accuracy gate against its stored baseline |

Resources: `ipe://docs/agents` (the engine's `AGENTS.md`), `ipe://docs/formats`, `ipe://results/latest`.

The server instructions tell the agent how to use them: never judge a change from one seed, report
regressions in other scenarios, separate model problems from data problems, and never regenerate the
regression baseline to make the gate pass.

## Setup

Build the engine's command-line tools (unit tests are not needed here):

```bash
git clone https://github.com/Yusufss4/indoor-positioning-engine
cmake -S indoor-positioning-engine -B indoor-positioning-engine/build -DCMAKE_BUILD_TYPE=Release -DIPE_BUILD_TESTS=OFF
cmake --build indoor-positioning-engine/build -j

git clone https://github.com/Yusufss4/positioning-agent-tools
cd positioning-agent-tools
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

If the two repositories are checked out side by side, the server finds the engine on its own. Otherwise
set `IPE_ROOT` (engine checkout), and optionally `IPE_BUILD` (default `$IPE_ROOT/build`) and
`IPE_WORKDIR` (scratch space for traces and runs, default `~/.cache/positioning-agent-tools`).

**Claude Code**

```bash
claude mcp add positioning -e IPE_ROOT=/path/to/indoor-positioning-engine -- /path/to/positioning-agent-tools/.venv/bin/positioning-mcp
```

**Cursor** (`.cursor/mcp.json` in the engine checkout, or `~/.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "positioning": {
      "command": "/path/to/positioning-agent-tools/.venv/bin/positioning-mcp",
      "env": { "IPE_ROOT": "/path/to/indoor-positioning-engine" }
    }
  }
}
```

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.positioning]
command = "/path/to/positioning-agent-tools/.venv/bin/positioning-mcp"
env = { IPE_ROOT = "/path/to/indoor-positioning-engine" }
```

## Example: investigating a field issue

[`examples/investigate_field_issue.py`](examples/investigate_field_issue.py) scripts the sequence of tool
calls an agent makes on `office_field_issue` (two dead beacons, one installed in the wrong room), using
the particle filter configuration from before the engine fix. Output, abridged:

**`compare_filters`** (5 seeds): the map-aware particle filter is worse than the map-free one, which
should not happen.

| filter | mean [m] | CEP50 [m] | CEP95 [m] | converged [s] |
|---|---:|---:|---:|---:|
| pf | 1.35 | 0.80 | 5.41 | 5.9 |
| pf-nomap | 1.17 | 0.97 | 2.42 | 3.5 |
| ekf | 1.27 | 0.86 | 4.09 | 16.1 |

**`error_breakdown`** finds one window, t = 31.0-39.4 s, peak error 18 m. The truth is 1.9 m from
beacon N3; the estimate is 4.2 m from beacon S3. That points to S3 being somewhere other than where the
map says.

**`compare_filters` with `scenario_overrides={"ble": {"moved_beacons": null}}`** puts S3 back. The
map-aware filter's CEP95 drops from 5.41 to 2.88 m, which confirms the hypothesis.

**`ab_test`** of the pre-fix config (B) against the current defaults (A) shows the engine fix is a
trade-off, not a free win:

| scenario | filter | CEP50 A -> B | CEP95 A -> B | B is |
|---|---|---|---|---|
| airport_hall_sparse | pf | 1.38 -> 1.55 | 8.26 -> 6.58 | mixed |
| office_device_offset | pf | 0.65 -> 0.75 | 1.82 -> 2.40 | worse |
| office_field_issue | pf | 0.76 -> 0.80 | 2.42 -> 5.41 | worse |
| office_mag_disturbance | pf | 1.06 -> 0.98 | 2.65 -> 2.51 | better |
| office_nlos_heavy | pf | 0.64 -> 0.66 | 2.23 -> 2.03 | better |
| office_nominal | pf | 0.60 -> 0.61 | 1.37 -> 1.37 | same |
| office_step_scale | pf | 0.62 -> 0.55 | 1.75 -> 1.51 | better |

With 5 seeds several of these deltas are within noise. The engine's
[experiment log](https://github.com/Yusufss4/indoor-positioning-engine/blob/main/docs/experiments.md)
repeats the comparison with 20 seeds.

## Development

```bash
.venv/bin/pytest -q
```

The tests call the real engine binaries (they are skipped if the engine is not built). They exercise the
tools directly, through the server's MCP dispatch, and end to end by spawning `positioning-mcp` and
talking to it over stdio with the MCP client, as an agent would. CI builds the engine from source and
runs them on every push.

## License

MIT
