from __future__ import annotations

import json

import pytest

from positioning_agent_tools.engine import EngineError, EngineTools, Paths, deep_merge, verdict


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


def test_verdict_counts_floor_accuracy():
    a = {"cep50_m": 1.0, "cep95_m": 2.0, "floor_accuracy": 0.99}
    assert verdict(a, {**a, "floor_accuracy": 0.90}) == "worse"
    assert verdict(a, {**a, "floor_accuracy": 0.98}) == "same"
    assert verdict(a, {**a, "cep95_m": 1.5, "floor_accuracy": 0.90}) == "mixed"
    single = {"cep50_m": 1.0, "cep95_m": 2.0}
    assert verdict(single, {**single, "cep50_m": 0.8}) == "better"


def test_describe_multi_floor_venue(tools):
    v = tools.describe_venue("office_two_floor")
    assert [lv["ordinal"] for lv in v["levels"]] == [0, 1]
    assert v["walls"] > 0 and all(lv["walls"] > 0 for lv in v["levels"])
    assert {c["kind"] for c in v["connectors"]} == {"stairs", "elevator"}
    assert {b["level"] for b in v["beacons"]} == {0, 1}


def test_describe_real_venue_caps_transmitters(tools):
    v = tools.describe_venue("site1_F2")
    assert v["beacon_count"] > len(v["beacons"])
    assert v["transmitters"]["wifi"] > 100


def test_multi_floor_replay_reports_wrong_floor_windows(tools):
    run = tools.run_replay(scenario="office_floor_bleed", filter="wcl", seed=1)
    assert 0 < run["metrics"]["floor_accuracy"] < 1
    bd = tools.error_breakdown(run["run_id"])
    wrong = [w for w in bd["windows"] if w["kind"] == "wrong_floor"]
    assert wrong and all(w["truth_level"] != w["estimate_level"] for w in wrong)
    assert bd["time_on_wrong_floor_s"] > 0
    assert tools.plot_run(run["run_id"])[:4] == b"\x89PNG"


def test_compare_filters_shows_floor_accuracy(tools):
    res = tools.compare_filters("office_elevator", ["pf", "wcl"], seeds=1)
    assert "floor acc" in res["table"]
    assert "floor_accuracy" in res["results"]["pf"]


def test_config_file(tools):
    assert {"msil_wifi", "open_venue"} <= {c["name"] for c in tools.list_configs()}
    cfg = tools.engine_config("pf", config_file="msil_wifi")
    assert cfg["pf"]["num_particles"] == 5000
    assert tools.engine_config("pf", {"pf": {"num_particles": 7}}, config_file="msil_wifi")["pf"]["num_particles"] == 7
    with pytest.raises(EngineError, match="msil_wifi"):
        tools.engine_config("pf", config_file="nope")


def test_real_recording_uses_its_config(tools):
    rec = next(r for r in tools.list_recordings()["recordings"] if r["venue"] == "site1_F2")
    assert rec["config_file"] == "msil_wifi"
    tuned = tools.run_replay(recording="site1_F2", filter="pf")
    default = tools.run_replay(recording="site1_F2", filter="pf", config_file="none")
    assert tuned["config_file"].endswith("msil_wifi.json")
    assert tuned["metrics"]["cep95_m"] < default["metrics"]["cep95_m"]
    bd = tools.error_breakdown(tuned["run_id"], threshold_m=1.0)
    assert bd["sparse_ground_truth"]
    assert tools.plot_run(tuned["run_id"])[:4] == b"\x89PNG"


def test_benchmark_demo_recordings(tools):
    res = tools.benchmark_recordings("demo", presets=["wcl"])
    assert "all" in res["results"]
    assert res["results"]["all"]["wcl"]["traces"] == len(tools.list_recordings()["recordings"])
    assert "median_cep95_m" in res["results"]["all"]["wcl"]
    assert "| all | wcl |" in res["table"]


def test_real_regression_is_skipped_without_dataset(paths, tmp_path):
    bare = EngineTools(Paths(root=tmp_path, build=paths.build, workdir=tmp_path / "work"))
    res = bare.run_regression(suite="real")
    assert res["passed"] and res["suites"]["real"]["status"] == "skipped"
    assert "fetch.sh" in res["note"]
    with pytest.raises(EngineError, match="demo"):
        bare.benchmark_recordings("holdout")
