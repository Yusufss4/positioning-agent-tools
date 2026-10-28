from __future__ import annotations

import json

import pytest

from positioning_agent_tools.engine import EngineError, deep_merge


def test_deep_merge_is_recursive_and_non_destructive():
    base = {"ble": {"noise_db": 4, "dead_beacons": ["A"]}, "seed": 1}
    out = deep_merge(base, {"ble": {"noise_db": 6, "dead_beacons": ["B"]}, "seed": None})
    assert out == {"ble": {"noise_db": 6, "dead_beacons": ["B"]}}
    assert base["ble"]["noise_db"] == 4


def test_list_scenarios_has_descriptions(tools):
    scenarios = tools.list_scenarios()
    names = {s["name"] for s in scenarios}
    assert {"office_nominal", "office_field_issue", "airport_hall_sparse"} <= names
    assert all(s["description"] for s in scenarios)


def test_describe_venue(tools):
    v = tools.describe_venue("office_floor")
    assert v["size_m"] == [40, 24]
    assert any(b["id"] == "S3" for b in v["beacons"])


def test_unknown_scenario_lists_alternatives(tools):
    with pytest.raises(EngineError, match="office_nominal"):
        tools.run_replay(scenario="nope")


def test_engine_config_patch_and_typo(tools):
    cfg = tools.engine_config("pf", {"pf": {"num_particles": 123}})
    assert cfg["pf"]["num_particles"] == 123
    with pytest.raises(EngineError, match="unknown config key"):
        tools.engine_config("pf", {"pf": {"num_particle": 1}})


def test_simulate_with_override_silences_beacon(tools):
    summary = tools.simulate_scenario("office_nominal", seed=3, overrides={"ble": {"dead_beacons": ["C1", "C2"]}})
    assert summary["wall_violations"] == 0
    trace = open(summary["trace"]).read()
    assert ",C1," not in trace and ",C2," not in trace
    assert ",C3," in trace


def test_replay_and_breakdown_find_the_moved_beacon(tools):
    # Pre-fix configuration that is known to jump to the moved beacon's mapped position.
    before = {"pf": {"beacon_integrity": False, "recovery_alpha_fast": 0.3, "rssi_offset_sigma": 5,
                     "step_scale_sigma": 0.1}}
    run = tools.run_replay(scenario="office_field_issue", filter="pf", config_patch=before)
    assert run["metrics"]["cep95_m"] > 0
    bd = tools.error_breakdown(run["run_id"], threshold_m=8.0)
    assert bd["windows"], "expected a sustained error window"
    worst = max(bd["windows"], key=lambda w: w["peak_error_m"])
    assert worst["beacons_near_estimate"][0].startswith("S3")


def test_replay_is_deterministic(tools):
    a = tools.run_replay(scenario="office_nominal", filter="ekf", seed=7)
    b = tools.run_replay(scenario="office_nominal", filter="ekf", seed=7)
    assert a["run_id"] == b["run_id"]
    assert a["metrics"]["cep50_m"] == b["metrics"]["cep50_m"]


def test_plot_run_returns_png(tools):
    run = tools.run_replay(scenario="office_nominal", filter="wcl")
    png = tools.plot_run(run["run_id"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_compare_filters_table(tools):
    res = tools.compare_filters("office_device_offset", ["pf", "ekf"], seeds=2)
    assert set(res["results"]) == {"pf", "ekf"}
    assert res["results"]["pf"]["cep50_m"] < res["results"]["ekf"]["cep50_m"]
    assert "| pf |" in res["table"]


def test_ab_test_detects_a_harmful_change(tools):
    res = tools.ab_test({"pf": {"num_particles": 40}}, presets=["pf"], seeds=2,
                        scenarios=["office_nominal", "office_device_offset"])
    assert len(res["rows"]) == 2
    assert "worse" in {r["verdict"] for r in res["rows"]}
    json.dumps(res)  # must be serialisable for MCP


def test_regression_gate_passes(tools):
    res = tools.run_regression()
    assert res["passed"], res["findings"]
