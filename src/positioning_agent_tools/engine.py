"""Thin, testable wrapper around the indoor-positioning-engine command line tools.

Everything here is plain Python returning JSON-serialisable values; `server.py` only adds the MCP layer.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import os
import shutil
import statistics
import subprocess
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRESETS = ("pf", "pf-nomap", "ekf", "wcl")
# Same as Tolerance::floor_accuracy in the engine's eval/include/ipe/regression.hpp.
FLOOR_ACCURACY_TOLERANCE = 0.03
MANIFESTS = {"holdout": "manifest_holdout.json", "tune": "manifest_tune.json", "all": "manifest.json"}
MAX_LISTED_BEACONS = 60
FETCH_HINT = ("Fetch and convert the Microsoft Indoor Location sample in the engine checkout: "
              "datasets/msil/fetch.sh && python3 datasets/msil/convert.py --wifi --scans --out datasets/msil/out_scans "
              "(see docs/real_data.md).")


class EngineError(RuntimeError):
    """Raised when an engine tool fails; the message is meant to be shown to the agent."""


def deep_merge(base: dict, patch: dict) -> dict:
    """RFC 7386-style merge: dicts merge recursively, everything else replaces."""
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        elif value is None:
            out.pop(key, None)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _short_hash(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:8]


def _levels(venue: dict) -> list[dict]:
    """Levels sorted by ordinal; position in this list is the level index used in replay outputs."""
    if "levels" not in venue:
        return [{"ordinal": 0, "name": "", "elevation_m": 0.0, "walls": venue.get("walls", [])}]
    out = []
    for lv in sorted(venue["levels"], key=lambda lv: lv["ordinal"]):
        height = lv.get("height_m", 4.0)
        out.append({"ordinal": lv["ordinal"], "name": lv.get("name", ""),
                    "elevation_m": lv.get("elevation_m", lv["ordinal"] * height), "walls": lv.get("walls", [])})
    return out


def _venue_walls(venue: dict, level: int | None = None) -> list[list[float]]:
    """Walls of one level (by index), or of every level."""
    levels = _levels(venue)
    if level is not None:
        return levels[level]["walls"] if 0 <= level < len(levels) else []
    return [w for lv in levels for w in lv["walls"]]


def _beacon_level(venue: dict, beacon: dict) -> int:
    ordinals = [lv["ordinal"] for lv in _levels(venue)]
    ordinal = beacon.get("level", 0)
    return ordinals.index(ordinal) if ordinal in ordinals else 0


def _transmitter_kind(beacon_id: str) -> str:
    return "wifi" if beacon_id.startswith("w:") else "ble"


def _median_truth_interval(rows: list[dict]) -> float:
    gaps = [b["t"] - a["t"] for a, b in zip(rows, rows[1:])]
    return statistics.median(gaps) if gaps else 0.0


def _config_args(config_file: Path | None, config_patch: dict | None) -> list[Any]:
    """`--config` before `--set`, the order the engine applies them in."""
    args: list[Any] = []
    if config_file:
        args += ["--config", config_file]
    if config_patch:
        args += ["--set", json.dumps(config_patch)]
    return args


def verdict(a: dict, b: dict, min_delta_m: float = 0.05) -> str:
    """B against A: better, worse, mixed (a trade-off) or same, from CEP50/CEP95 and floor accuracy."""
    deltas = (b["cep50_m"] - a["cep50_m"], b["cep95_m"] - a["cep95_m"])
    improved = any(d < -min_delta_m for d in deltas)
    degraded = any(d > min_delta_m for d in deltas)
    if "floor_accuracy" in a and "floor_accuracy" in b:
        floor_delta = b["floor_accuracy"] - a["floor_accuracy"]
        improved |= floor_delta > FLOOR_ACCURACY_TOLERANCE
        degraded |= floor_delta < -FLOOR_ACCURACY_TOLERANCE
    return "mixed" if improved and degraded else "better" if improved else "worse" if degraded else "same"


def _fmt(value: float | None, pct: bool = False) -> str:
    if value is None or value < 0:
        return "-"
    return f"{100 * value:.0f}%" if pct else f"{value:.2f}"


@dataclass
class Paths:
    root: Path
    build: Path
    workdir: Path

    @staticmethod
    def from_env() -> "Paths":
        # Editable installs default to a sibling checkout: <dir>/positioning-agent-tools/src/... ->
        # <dir>/indoor-positioning-engine. Set IPE_ROOT otherwise.
        here = Path(__file__).resolve()
        default_root = here.parents[3] / "indoor-positioning-engine"
        root = Path(os.environ.get("IPE_ROOT", default_root)).expanduser().resolve()
        build = Path(os.environ.get("IPE_BUILD", root / "build")).expanduser().resolve()
        workdir = Path(
            os.environ.get("IPE_WORKDIR", Path.home() / ".cache" / "positioning-agent-tools")
        ).expanduser()
        return Paths(root, build, workdir)

    def tool(self, name: str) -> Path:
        path = self.build / "tools" / name
        if not path.exists():
            raise EngineError(
                f"{path} not found. Build the engine first: cmake -S {self.root} -B {self.build} "
                f"-DCMAKE_BUILD_TYPE=Release && cmake --build {self.build} -j"
            )
        return path


class EngineTools:
    def __init__(self, paths: Paths | None = None):
        self.paths = paths or Paths.from_env()
        self.paths.workdir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ helpers

    def _run(self, args: list[Any], ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        proc = subprocess.run([str(a) for a in args], capture_output=True, text=True)
        if proc.returncode not in ok_codes:
            raise EngineError(
                f"{Path(args[0]).name} failed (exit {proc.returncode}):\n{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    def _existing(self, path: str) -> Path | None:
        """A path as given, or relative to the engine checkout."""
        for p in (Path(path).expanduser(), self.paths.root / path):
            if p.exists():
                return p.resolve()
        return None

    @property
    def scenario_dir(self) -> Path:
        return self.paths.root / "data" / "scenarios"

    @property
    def config_dir(self) -> Path:
        return self.paths.root / "data" / "configs"

    @property
    def real_dir(self) -> Path:
        return self.paths.root / "data" / "real"

    @property
    def msil_dir(self) -> Path:
        return self.paths.root / "datasets" / "msil" / "out_scans"

    def _scenario_path(self, scenario: str) -> Path:
        p = Path(scenario)
        if p.suffix == ".json" and p.exists():
            return p.resolve()
        candidate = self.scenario_dir / f"{scenario}.json"
        if not candidate.exists():
            names = ", ".join(s["name"] for s in self.list_scenarios())
            raise EngineError(f"unknown scenario '{scenario}'. Available: {names}")
        return candidate

    def _venue_path(self, venue: str) -> Path:
        if venue.endswith(".json") and (found := self._existing(venue)):
            return found
        for candidate in (self.paths.root / "data" / "venues" / f"{venue}.json", self.real_dir / venue / "venue.json"):
            if candidate.exists():
                return candidate
        known = [p.stem for p in sorted((self.paths.root / "data" / "venues").glob("*.json"))]
        known += [p.parent.name for p in sorted(self.real_dir.glob("*/venue.json"))]
        raise EngineError(f"unknown venue '{venue}'. Available: {', '.join(known)}")

    def _config_path(self, config_file: str | None) -> Path | None:
        if not config_file or config_file == "none":
            return None
        if config_file.endswith(".json") and (found := self._existing(config_file)):
            return found
        candidate = self.config_dir / f"{config_file}.json"
        if not candidate.exists():
            names = ", ".join(c["name"] for c in self.list_configs())
            raise EngineError(f"unknown config_file '{config_file}'. Available: {names}")
        return candidate

    def _load_scenario(self, scenario: str) -> tuple[dict, Path]:
        path = self._scenario_path(scenario)
        data = json.loads(path.read_text())
        data["venue"] = str((path.parent / data["venue"]).resolve())
        return data, path

    def _materialise_scenario(self, scenario: str, overrides: dict | None, target_dir: Path | None = None) -> Path:
        """Writes the scenario (with overrides and an absolute venue path) to the work directory."""
        data, _ = self._load_scenario(scenario)
        if overrides:
            data = deep_merge(data, overrides)
            data["name"] = f"{data['name']}+{_short_hash(overrides)}"
        target_dir = target_dir or self.paths.workdir / "scenarios"
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / f"{data['name']}.json"
        out.write_text(json.dumps(data, indent=2))
        return out

    def _run_dir(self, run_id: str) -> Path:
        d = self.paths.workdir / "runs" / run_id
        if not d.exists():
            raise EngineError(f"unknown run_id '{run_id}'. Use run_replay first.")
        return d

    # -------------------------------------------------------------- discovery

    def list_scenarios(self) -> list[dict]:
        out = []
        for f in sorted(self.scenario_dir.glob("*.json")):
            j = json.loads(f.read_text())
            out.append({
                "name": j.get("name", f.stem),
                "venue": Path(j.get("venue", "")).stem,
                "description": j.get("description", ""),
            })
        return out

    def describe_venue(self, venue: str) -> dict:
        j = json.loads(self._venue_path(venue).read_text())
        x0, y0, x1, y1 = j["bounds"]
        beacons = j.get("beacons", [])
        levels = _levels(j)
        listed = [{"id": b["id"], "x": b["x"], "y": b["y"], "level": b.get("level", 0)}
                  for b in beacons[:MAX_LISTED_BEACONS]]
        out = {
            "name": j.get("name"),
            "description": j.get("description", ""),
            "size_m": [x1 - x0, y1 - y0],
            "walls": len(_venue_walls(j)),
            "wall_attenuation_db_in_map": j.get("wall_attenuation_db", 3.0),
            "levels": [{"ordinal": lv["ordinal"], "name": lv["name"], "elevation_m": lv["elevation_m"],
                        "walls": len(lv["walls"])} for lv in levels],
            "connectors": [{"id": c.get("id"), "kind": c.get("kind"), "levels": c.get("levels", [])}
                           for c in j.get("connectors", [])],
            "beacon_count": len(beacons),
            "transmitters": dict(Counter(_transmitter_kind(b["id"]) for b in beacons)),
            "beacons": listed,
        }
        if len(levels) > 1:
            out["floor_attenuation_db_in_map"] = j.get("floor_attenuation_db", 15.0)
        if len(beacons) > MAX_LISTED_BEACONS:
            out["note"] = (f"first {MAX_LISTED_BEACONS} of {len(beacons)} transmitters listed; ids starting with "
                           "'w:' are Wi-Fi access points, 'b:' iBeacons, fitted from training walks")
        return out

    def engine_config(self, preset: str = "pf", config_patch: dict | None = None,
                      config_file: str | None = None) -> dict:
        args = [self.paths.tool("ipe-replay"), "--filter", preset, "--print-config"]
        args += _config_args(self._config_path(config_file), config_patch)
        return json.loads(self._run(args).stdout)

    def list_configs(self) -> list[dict]:
        return [{"name": f.stem, "path": str(f), "patch": json.loads(f.read_text())}
                for f in sorted(self.config_dir.glob("*.json"))]

    def _demos(self) -> list[dict]:
        path = self.real_dir / "demos.json"
        return json.loads(path.read_text())["demos"] if path.exists() else []

    def _recording(self, recording: str) -> dict:
        demos = self._demos()
        matches = [d for d in demos if d["id"] == recording] or [d for d in demos if d["id"].startswith(recording)]
        if not matches:
            raise EngineError(f"unknown recording '{recording}'. Available: {', '.join(d['id'] for d in demos)}")
        return matches[0]

    def list_recordings(self) -> dict:
        """Real phone walks: the ones committed with the engine, plus the converted dataset if present."""
        recordings = [{
            "id": d["id"],
            "title": d.get("title", ""),
            "venue": Path(d["venue"]).parent.name,
            "trace": str(self.real_dir / d["trace"]),
            "duration_s": d.get("duration_s"),
            "waypoints": d.get("waypoints"),
            "config_file": Path(d["config"]).stem if d.get("config") else None,
        } for d in self._demos()]
        manifests = {}
        for name, file in MANIFESTS.items():
            path = self.msil_dir / file
            if path.exists():
                traces = json.loads(path.read_text())["traces"]
                manifests[name] = {"path": str(path), "traces": len(traces),
                                   "groups": dict(sorted(Counter(t["group"] for t in traces).items()))}
        out = {"recordings": recordings, "manifests": manifests,
               "note": ("Ground truth is the surveyor's waypoints only. Replay these with their config_file "
                        "(the default for run_replay(recording=...)). Choose settings on the 'tune' manifest and "
                        "report results on 'holdout'.")}
        if not manifests:
            out["manifests_missing"] = FETCH_HINT
        return out

    # ----------------------------------------------------------- simulation

    def simulate_scenario(self, scenario: str, seed: int | None = None, overrides: dict | None = None) -> dict:
        scen_path = self._materialise_scenario(scenario, overrides)
        name = scen_path.stem
        trace = self.paths.workdir / "traces" / f"{name}_seed{seed if seed is not None else 'default'}.csv"
        trace.parent.mkdir(parents=True, exist_ok=True)
        args = [self.paths.tool("ipe-sim"), "--scenario", scen_path, "--out", trace]
        if seed is not None:
            args += ["--seed", str(seed)]
        proc = self._run(args, ok_codes=(0, 3))
        summary = json.loads(proc.stdout)
        summary["scenario_file"] = str(scen_path)
        if proc.returncode == 3:
            summary["warning"] = "ground-truth path crosses walls; fix the waypoints before trusting results"
        return summary

    # ---------------------------------------------------------------- replay

    def run_replay(self, scenario: str | None = None, trace: str | None = None, venue: str | None = None,
                   filter: str = "pf", seed: int | None = None, config_patch: dict | None = None,
                   scenario_overrides: dict | None = None, recording: str | None = None,
                   config_file: str | None = None) -> dict:
        if filter not in PRESETS:
            raise EngineError(f"unknown filter '{filter}', expected one of {PRESETS}")
        if sum(x is not None for x in (scenario, trace, recording)) != 1:
            raise EngineError("give exactly one of: a scenario name, a recording id, or a trace path (with venue)")

        if recording:
            demo = self._recording(recording)
            recording = demo["id"]
            trace = str(self.real_dir / demo["trace"])
            venue = str(self.real_dir / demo["venue"])
            if config_file is None and demo.get("config"):
                config_file = str((self.real_dir / demo["config"]).resolve())
        cfg_path = self._config_path(config_file)

        key = {"scenario": scenario, "trace": trace, "venue": venue, "recording": recording, "filter": filter,
               "seed": seed, "config_patch": config_patch, "config_file": str(cfg_path) if cfg_path else None,
               "overrides": scenario_overrides}
        run_id = f"{Path(scenario or recording or trace).stem}-{filter}-{_short_hash(key)}"
        run_dir = self.paths.workdir / "runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        args = [self.paths.tool("ipe-replay"), "--filter", filter, "--out", run_dir / "estimates.csv",
                "--metrics", run_dir / "metrics.json"]
        if scenario:
            scen_path = self._materialise_scenario(scenario, scenario_overrides)
            venue_path = json.loads(scen_path.read_text())["venue"]
            args += ["--scenario", scen_path]
            if seed is not None:
                args += ["--seed", str(seed)]
        else:
            if not venue:
                raise EngineError("replaying a trace file requires the venue")
            trace_path = self._existing(trace)
            if not trace_path:
                raise EngineError(f"trace file '{trace}' not found")
            venue_path = str(self._venue_path(venue))
            args += ["--trace", trace_path, "--venue", venue_path]
        args += _config_args(cfg_path, config_patch)

        metrics = json.loads(self._run(args).stdout)
        (run_dir / "meta.json").write_text(json.dumps({**key, "venue_path": venue_path}, indent=2))
        out = {"run_id": run_id, "metrics": metrics, "estimates_csv": str(run_dir / "estimates.csv")}
        if cfg_path:
            out["config_file"] = str(cfg_path)
        return out

    def _load_run(self, run_id: str) -> tuple[list[dict], dict]:
        d = self._run_dir(run_id)
        with open(d / "estimates.csv", newline="") as f:
            rows = [{k: float(v) for k, v in r.items()} for r in csv.DictReader(f)]
        for r in rows:
            r.setdefault("truth_level", 0.0)
            r.setdefault("est_level", r["truth_level"])
            r["truth_level"], r["est_level"] = int(r["truth_level"]), int(r["est_level"])
        return rows, json.loads((d / "meta.json").read_text())

    def error_breakdown(self, run_id: str, threshold_m: float = 3.0, min_duration_s: float = 1.0,
                        warmup_s: float = 5.0) -> dict:
        """Finds time windows where the error exceeded a threshold or the floor was wrong, and says where."""
        rows, meta = self._load_run(run_id)
        venue = json.loads(Path(meta["venue_path"]).read_text())
        levels = _levels(venue)
        multi_level = len(levels) > 1
        beacons = [(b, _beacon_level(venue, b)) for b in venue.get("beacons", [])]

        def ordinal(index: int) -> int:
            return levels[index]["ordinal"] if 0 <= index < len(levels) else index

        def nearest(x: float, y: float, level: int, k: int = 3) -> list[str]:
            on_level = [b for b, lv in beacons if lv == level] or [b for b, _ in beacons]
            ranked = sorted(on_level, key=lambda b: math.hypot(b["x"] - x, b["y"] - y))
            return [f"{b['id']} ({math.hypot(b['x'] - x, b['y'] - y):.1f} m)" for b in ranked[:k]]

        # Recorded traces only have ground truth at the surveyor's waypoints, seconds apart: one bad
        # waypoint is then a window on its own.
        truth_interval = _median_truth_interval(rows)
        sparse = truth_interval > min_duration_s

        def kind(r: dict | None) -> str | None:
            if r is None or r["t"] - t0 < warmup_s or not r["has_fix"]:
                return None
            if r["est_level"] != r["truth_level"]:
                return "wrong_floor"
            return "horizontal" if r["error_m"] > threshold_m else None

        t0 = rows[0]["t"] if rows else 0.0
        windows: list[dict] = []
        current: list[dict] = []
        current_kind = None
        for r in rows + [None]:
            k = kind(r)
            if k is not None and k == current_kind:
                current.append(r)
                continue
            if current and (sparse or current[-1]["t"] - current[0]["t"] >= min_duration_s):
                peak = max(current, key=lambda q: q["error_m"])
                window = {
                    "kind": current_kind,
                    "start_s": round(current[0]["t"], 1),
                    "end_s": round(current[-1]["t"], 1),
                    "samples": len(current),
                    "peak_error_m": round(peak["error_m"], 2),
                    "truth_at_peak": [round(peak["truth_x"], 1), round(peak["truth_y"], 1)],
                    "estimate_at_peak": [round(peak["est_x"], 1), round(peak["est_y"], 1)],
                    "beacons_near_truth": nearest(peak["truth_x"], peak["truth_y"], peak["truth_level"]),
                    "beacons_near_estimate": nearest(peak["est_x"], peak["est_y"], peak["est_level"]),
                }
                if multi_level:
                    window["truth_level"] = ordinal(peak["truth_level"])
                    window["estimate_level"] = ordinal(peak["est_level"])
                windows.append(window)
            current, current_kind = ([r], k) if k is not None else ([], None)

        evaluated = [r for r in rows if r["t"] - t0 >= warmup_s and r["has_fix"]]
        horizontal = [w for w in windows if w["kind"] == "horizontal"]
        wrong_floor = [w for w in windows if w["kind"] == "wrong_floor"]
        hints = []
        if horizontal:
            hints.append("A window whose estimate sits next to a beacon far from the truth suggests a moved or "
                         "mis-surveyed beacon; errors that grow steadily during walking suggest step length or "
                         "heading problems.")
        if wrong_floor:
            hints.append("Wrong-floor windows are not counted in CEP (see floor_accuracy). Check the barometer "
                         "(drift, offset, spikes), the slab loss of the map vs reality (floor bleed), and lift rides, "
                         "where no steps are detected.")
        if sparse:
            hints.append(f"Ground truth exists only at waypoints (median spacing {truth_interval:.1f} s), so a "
                         "window can be a single waypoint and the track between waypoints is unknown.")
        out = {
            "run_id": run_id,
            "threshold_m": threshold_m,
            "windows": windows,
            "time_above_threshold_s": round(sum(w["end_s"] - w["start_s"] for w in horizontal), 1),
            "evaluated_duration_s": round(evaluated[-1]["t"] - evaluated[0]["t"], 1) if evaluated else 0.0,
            "hint": " ".join(hints) if windows else "no sustained errors above threshold",
        }
        if multi_level:
            out["time_on_wrong_floor_s"] = round(sum(w["end_s"] - w["start_s"] for w in wrong_floor), 1)
        if sparse:
            out["sparse_ground_truth"] = True
        return out

    def plot_run(self, run_id: str) -> bytes:
        """PNG with one floor plan per visited level (true and estimated tracks), and error over time."""
        import matplotlib

        matplotlib.use("Agg")
        import io

        import matplotlib.pyplot as plt

        rows, meta = self._load_run(run_id)
        venue = json.loads(Path(meta["venue_path"]).read_text())
        levels = _levels(venue)
        fix = [r for r in rows if r["has_fix"]]
        shown = sorted({r["truth_level"] for r in rows} | {r["est_level"] for r in fix})
        shown = [i for i in shown if 0 <= i < len(levels)] or [0]
        beacons = [(b, _beacon_level(venue, b)) for b in venue.get("beacons", [])]
        many_beacons = len(beacons) > MAX_LISTED_BEACONS
        sparse = _median_truth_interval(rows) > 1.0
        x0, y0, x1, y1 = venue["bounds"]
        nan = float("nan")

        n = len(shown)
        fig = plt.figure(figsize=(max(9.0, 6.0 * n), 8 if n == 1 else 6.5))
        grid = fig.add_gridspec(2, n, height_ratios=[3, 1])
        for col, li in enumerate(shown):
            ax = fig.add_subplot(grid[0, col])
            level = levels[li]
            wall_lw = 0.6 if len(level["walls"]) >= 600 else 2
            for w in level["walls"]:
                ax.plot([w[0], w[2]], [w[1], w[3]], color="#555", lw=wall_lw)
            for c in venue.get("connectors", []):
                if level["ordinal"] in c.get("levels", []) and c.get("area"):
                    ax.fill([p[0] for p in c["area"]], [p[1] for p in c["area"]], color="#1f77b4", alpha=0.25)
                    ax.annotate(c.get("kind", ""), tuple(c["area"][0]), fontsize=7, color="#1f77b4")
            on_level = [b for b, lv in beacons if lv == li]
            ax.scatter([b["x"] for b in on_level], [b["y"] for b in on_level], marker="^", color="#7f3fbf",
                       s=6 if many_beacons else 30)
            if not many_beacons:
                for b in on_level:
                    ax.annotate(b["id"], (b["x"], b["y"]), fontsize=7, xytext=(3, 3), textcoords="offset points")
            truth = [(r["truth_x"], r["truth_y"]) if r["truth_level"] == li else (nan, nan) for r in rows]
            est = [(r["est_x"], r["est_y"]) if r["est_level"] == li else (nan, nan) for r in fix]
            ax.plot(*zip(*truth), color="#222", lw=2, marker="o" if sparse else None, ms=3, label="truth")
            if est:
                ax.plot(*zip(*est), color="#d62728", lw=1, marker="." if sparse else None, label=meta["filter"])
            # Large floors (malls): zoom to the walk.
            pts = [p for p in truth + est if not math.isnan(p[0])]
            if pts and max(x1 - x0, y1 - y0) > 80:
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                ax.set_xlim(max(x0, min(xs) - 15), min(x1, max(xs) + 15))
                ax.set_ylim(max(y0, min(ys) - 15), min(y1, max(ys) + 15))
            ax.set_aspect("equal")
            ax.legend(fontsize=8)
            ax.set_title(run_id if n == 1 else f"level {level['ordinal']} {level['name']}".strip(), fontsize=9)
        if n > 1:
            fig.suptitle(run_id)

        ax2 = fig.add_subplot(grid[1, :])
        ax2.plot([r["t"] for r in fix], [r["error_m"] for r in fix], color="#d62728", lw=1,
                 marker="." if sparse else None)
        spans: list[list[float]] = []
        for prev, r in zip([None] + fix, fix):
            if r["est_level"] == r["truth_level"]:
                continue
            if spans and prev is not None and prev["est_level"] != prev["truth_level"]:
                spans[-1][1] = r["t"]
            else:
                spans.append([r["t"], r["t"]])
        for start, end in spans:
            ax2.axvspan(start - 0.1, end + 0.1, color="#ff7f0e", alpha=0.3, lw=0)
        if spans:
            ax2.plot([], [], color="#ff7f0e", alpha=0.5, lw=6, label="wrong floor")
            ax2.legend(fontsize=8)
        ax2.set_xlabel("time [s]")
        ax2.set_ylabel("error [m]")
        ax2.grid(alpha=0.3)
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=90)
        plt.close(fig)
        return buf.getvalue()

    # ------------------------------------------------------------- benchmarks

    def _benchmark(self, scenarios: list[str] | None, presets: list[str], seeds: int,
                   config_patch: dict | None, scenario_overrides: dict | None = None,
                   config_file: str | None = None) -> dict:
        cfg_path = self._config_path(config_file)
        with tempfile.TemporaryDirectory(dir=self.paths.workdir) as tmp:
            tmpdir = Path(tmp)
            names = scenarios or [s["name"] for s in self.list_scenarios()]
            for name in names:
                self._materialise_scenario(name, scenario_overrides, target_dir=tmpdir)
            out = tmpdir / "results.json"
            args = [self.paths.tool("ipe-regress"), "--scenarios", tmpdir, "--no-baseline", "--seeds", str(seeds),
                    "--presets", ",".join(presets), "--json", out]
            args += _config_args(cfg_path, config_patch)
            self._run(args)
            return json.loads(out.read_text())

    def compare_filters(self, scenario: str, filters: list[str] | None = None, seeds: int = 5,
                        config_patch: dict | None = None, scenario_overrides: dict | None = None,
                        config_file: str | None = None) -> dict:
        filters = filters or list(PRESETS)
        res = self._benchmark([scenario], filters, seeds, config_patch, scenario_overrides, config_file)["results"]
        (name, per_filter), = res.items()
        has_floor = any("floor_accuracy" in m for m in per_filter.values())
        has_cov = any("coverage95" in m for m in per_filter.values())
        header = ["filter", "mean [m]", "CEP50 [m]", "CEP95 [m]", "converged [s]"]
        header += ["floor acc"] * has_floor + ["cov95"] * has_cov
        lines = ["| " + " | ".join(header) + " |", "|---|" + "---:|" * (len(header) - 1)]
        for f in filters:
            m = per_filter[f]
            conv = "never" if m["convergence_s"] < 0 else f"{m['convergence_s']:.1f}"
            cells = [f, _fmt(m["mean_m"]), _fmt(m["cep50_m"]), _fmt(m["cep95_m"]), conv]
            if has_floor:
                cells.append(_fmt(m.get("floor_accuracy"), pct=True))
            if has_cov:
                cells.append(_fmt(m.get("coverage95"), pct=True))
            lines.append("| " + " | ".join(cells) + " |")
        out = {"scenario": name, "seeds": seeds, "results": per_filter, "table": "\n".join(lines)}
        if has_cov:
            out["note"] = "cov95: share of fixes inside the reported 95% ellipse; an honest filter scores ~95%."
        return out

    def ab_test(self, config_patch: dict, presets: list[str] | None = None, seeds: int = 5,
                scenarios: list[str] | None = None, min_delta_m: float = 0.05,
                config_file: str | None = None) -> dict:
        """Runs every scenario with and without `config_patch` on identical seeds and reports deltas.

        `config_file` is applied to both sides, so B differs from A only by `config_patch`.
        """
        presets = presets or ["pf"]
        base = self._benchmark(scenarios, presets, seeds, None, config_file=config_file)["results"]
        cand = self._benchmark(scenarios, presets, seeds, config_patch, config_file=config_file)["results"]

        rows = []
        counts = {"better": 0, "worse": 0, "mixed": 0, "same": 0}
        has_floor = any("floor_accuracy" in m for per in base.values() for m in per.values())
        header = "| scenario | filter | CEP50 A -> B | CEP95 A -> B |" + (" floor acc A -> B |" if has_floor else "")
        lines = [header + " B is |", "|---|---|---|---|" + "---|" * has_floor + "---|"]
        for scen in sorted(base):
            for p in presets:
                a, b = base[scen][p], cand[scen][p]
                floor_cell = ""
                if "floor_accuracy" in a and "floor_accuracy" in b:
                    floor_cell = f" {_fmt(a['floor_accuracy'], True)} -> {_fmt(b['floor_accuracy'], True)} |"
                elif has_floor:
                    floor_cell = " - |"
                v = verdict(a, b, min_delta_m)
                counts[v] += 1
                rows.append({"scenario": scen, "filter": p, "a": a, "b": b, "verdict": v})
                lines.append(f"| {scen} | {p} | {a['cep50_m']:.2f} -> {b['cep50_m']:.2f} | "
                             f"{a['cep95_m']:.2f} -> {b['cep95_m']:.2f} |{floor_cell} {v} |")
        floor_rule = (f", floor accuracy |delta| <= {100 * FLOOR_ACCURACY_TOLERANCE:.0f} pp" if has_floor else "")
        summary = (f"B vs A: {counts['better']} better, {counts['worse']} worse, {counts['mixed']} mixed, "
                   f"{counts['same']} unchanged (CEP |delta| <= {min_delta_m} m{floor_rule}, {seeds} seeds)")
        return {"config_patch": config_patch, "config_file": config_file, "seeds": seeds, "summary": summary,
                "rows": rows, "table": "\n".join(lines)}

    def _manifest_path(self, manifest: str) -> Path:
        if manifest == "demo":
            return self._demo_manifest()
        if manifest in MANIFESTS:
            path = self.msil_dir / MANIFESTS[manifest]
            if not path.exists():
                raise EngineError(f"{path} not found. {FETCH_HINT} manifest='demo' replays the walks "
                                  "committed in data/real instead.")
            return path
        found = self._existing(manifest)
        if not found:
            raise EngineError(f"unknown manifest '{manifest}': use holdout, tune, all, demo or a manifest path")
        return found

    def _demo_manifest(self) -> Path:
        """A trace manifest over the walks in data/real, for a quick check without the full dataset."""
        out = self.paths.workdir / "manifests" / "demo.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        traces = [{"name": d["id"], "group": Path(d["venue"]).parent.name,
                   "venue": str(self.real_dir / d["venue"]), "trace": str(self.real_dir / d["trace"])}
                  for d in self._demos()]
        if not traces:
            raise EngineError(f"no recordings in {self.real_dir / 'demos.json'}")
        out.write_text(json.dumps({"traces": traces}, indent=2))
        return out

    def benchmark_recordings(self, manifest: str = "holdout", presets: list[str] | None = None,
                             config_file: str | None = "msil_wifi", config_patch: dict | None = None) -> dict:
        """Replays recorded walks (one run each) through each preset; results per group plus 'all'."""
        presets = presets or ["pf", "wcl"]
        path = self._manifest_path(manifest)
        with tempfile.TemporaryDirectory(dir=self.paths.workdir) as tmp:
            out = Path(tmp) / "results.json"
            args = [self.paths.tool("ipe-regress"), "--traces", path, "--no-baseline", "--presets", ",".join(presets),
                    "--json", out]
            args += _config_args(self._config_path(config_file), config_patch)
            self._run(args)
            results = json.loads(out.read_text())["results"]

        lines = ["| group | filter | walks | median CEP50 [m] | median CEP95 [m] | mean CEP50 [m] | mean CEP95 [m] "
                 "| median cov95 |", "|---|---|---:|---:|---:|---:|---:|---:|"]
        for group in sorted(results, key=lambda g: (g != "all", g)):
            for p in presets:
                m = results[group].get(p)
                if not m:
                    continue
                lines.append(f"| {group} | {p} | {m.get('traces', '-')} | {_fmt(m.get('median_cep50_m'))} | "
                             f"{_fmt(m.get('median_cep95_m'))} | {_fmt(m['cep50_m'])} | {_fmt(m['cep95_m'])} | "
                             f"{_fmt(m.get('median_coverage95'), pct=True)} |")
        notes = ["Each walk is one run; the medians over walks are the headline numbers (a few short walks can "
                 "dominate the means)."]
        if manifest == "tune":
            notes.append("These are the tuning walks: use them to choose settings, never as evidence. Report the "
                         "result on manifest='holdout'.")
        elif manifest == "demo":
            notes.append(f"Only the {len(self._demos())} walks committed in data/real, one per floor: a smoke test, "
                         "not a benchmark.")
        return {"manifest": str(path), "config_file": config_file, "config_patch": config_patch,
                "results": results, "table": "\n".join(lines), "note": " ".join(notes)}

    def _regression_suite(self, suite: str, seeds: int, config_file: str | None) -> dict:
        if suite == "simulated":
            baseline = self.paths.root / "tests" / "regression" / "baseline.json"
            args = [self.paths.tool("ipe-regress"), "--scenarios", self.scenario_dir, "--baseline", baseline,
                    "--seeds", str(seeds)]
            args += _config_args(self._config_path(config_file), None)
            notes = []
            if seeds != 3:
                notes.append("Baseline uses 3 seeds; other seed counts are not comparable.")
            if config_file:
                notes.append("The baseline was recorded with default settings: findings show what would change if "
                             "this config became the default.")
        else:
            manifest = self.msil_dir / "manifest_holdout.json"
            if not manifest.exists():
                return {"status": "skipped", "findings": [], "note": f"{manifest} not found. {FETCH_HINT}"}
            baseline = self.paths.root / "tests" / "regression" / "msil_baseline.json"
            args = [self.paths.tool("ipe-regress"), "--traces", manifest, "--presets", "pf,wcl", "--config",
                    self.config_dir / "msil_wifi.json", "--baseline", baseline]
            notes = ["Held-out real walks with data/configs/msil_wifi.json, as the real_data_regression CTest."]
        proc = self._run(args, ok_codes=(0, 1))
        findings = [line for line in proc.stdout.splitlines()
                    if line.split(" ")[0] in {"REGRESSED", "improved", "missing", "new"}]
        return {"status": "passed" if proc.returncode == 0 else "failed", "findings": findings,
                "note": " ".join(notes)}

    def run_regression(self, seeds: int = 3, suite: str = "simulated", config_file: str | None = None) -> dict:
        """The engine's accuracy gates: 'simulated' (scenarios), 'real' (held-out phone walks) or 'all'."""
        if suite not in ("simulated", "real", "all"):
            raise EngineError(f"unknown suite '{suite}', expected simulated, real or all")
        names = ["simulated", "real"] if suite == "all" else [suite]
        suites = {name: self._regression_suite(name, seeds, config_file if name == "simulated" else None)
                  for name in names}
        findings = [f"[{name}] {f}" if len(names) > 1 else f for name, s in suites.items() for f in s["findings"]]
        return {
            "passed": all(s["status"] != "failed" for s in suites.values()),
            "findings": findings,
            "suites": suites,
            "note": " ".join(s["note"] for s in suites.values() if s["note"]),
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.paths.workdir, ignore_errors=True)
