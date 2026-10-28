#!/usr/bin/env python3
"""The tool sequence an agent follows to investigate office_field_issue, scripted for reproducibility.

    IPE_ROOT=../indoor-positioning-engine python examples/investigate_field_issue.py
"""

from __future__ import annotations

import json

from positioning_agent_tools.engine import EngineTools

# The particle filter configuration before the fix described in the engine's docs/experiments.md.
BEFORE = {"pf": {"beacon_integrity": False, "recovery_alpha_fast": 0.3, "rssi_offset_sigma": 5,
                 "step_scale_sigma": 0.1}}


def show(title: str, value) -> None:
    print(f"\n### {title}\n")
    print(value if isinstance(value, str) else json.dumps(value, indent=2))


def main() -> None:
    t = EngineTools()

    scen = next(s for s in t.list_scenarios() if s["name"] == "office_field_issue")
    show("1. list_scenarios -> office_field_issue", scen["description"])

    show("2. compare_filters with the pre-fix config: the map-aware PF loses to the map-free one",
         t.compare_filters("office_field_issue", ["pf", "pf-nomap", "ekf"], seeds=5, config_patch=BEFORE)["table"])

    run = t.run_replay(scenario="office_field_issue", filter="pf", config_patch=BEFORE)
    bd = t.error_breakdown(run["run_id"], threshold_m=5.0)
    show("3. error_breakdown: where does it fail?", {"windows": bd["windows"], "hint": bd["hint"]})

    show("4. hypothesis: the moved beacon S3 causes it -> put S3 back where the map says",
         t.compare_filters("office_field_issue", ["pf", "pf-nomap"], seeds=5, config_patch=BEFORE,
                           scenario_overrides={"ble": {"moved_beacons": None}})["table"])

    ab = t.ab_test(BEFORE, presets=["pf"], seeds=5)
    show("5. ab_test: A = current defaults (after the fix), B = pre-fix config", ab["table"] + "\n\n" + ab["summary"])


if __name__ == "__main__":
    main()
