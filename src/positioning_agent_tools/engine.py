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
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRESETS = ("pf", "pf-nomap", "ekf", "wcl")


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

    def _run(self, args: list[str], ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
        proc = subprocess.run([str(a) for a in args], capture_output=True, text=True)
        if proc.returncode not in ok_codes:
            raise EngineError(
                f"{Path(args[0]).name} failed (exit {proc.returncode}):\n{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc

    @property
    def scenario_dir(self) -> Path:
        return self.paths.root / "data" / "scenarios"

    def _scenario_path(self, scenario: str) -> Path:
        p = Path(scenario)
        if p.suffix == ".json" and p.exists():
            return p.resolve()
        candidate = self.scenario_dir / f"{scenario}.json"
        if not candidate.exists():
            names = ", ".join(s["name"] for s in self.list_scenarios())
            raise EngineError(f"unknown scenario '{scenario}'. Available: {names}")
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
        path = Path(venue)
        if not path.exists():
            path = self.paths.root / "data" / "venues" / f"{venue}.json"
        if not path.exists():
            raise EngineError(f"unknown venue '{venue}'")
        j = json.loads(path.read_text())
        x0, y0, x1, y1 = j["bounds"]
        return {
            "name": j.get("name"),
            "description": j.get("description", ""),
            "size_m": [x1 - x0, y1 - y0],
            "walls": len(j.get("walls", [])),
            "wall_attenuation_db_in_map": j.get("wall_attenuation_db", 3.0),
            "beacons": [{"id": b["id"], "x": b["x"], "y": b["y"]} for b in j["beacons"]],
        }

    def engine_config(self, preset: str = "pf", config_patch: dict | None = None) -> dict:
        args = [self.paths.tool("ipe-replay"), "--filter", preset, "--print-config"]
        if config_patch:
            args += ["--set", json.dumps(config_patch)]
        return json.loads(self._run(args).stdout)

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
                   scenario_overrides: dict | None = None) -> dict:
        if filter not in PRESETS:
            raise EngineError(f"unknown filter '{filter}', expected one of {PRESETS}")
        if not scenario and not trace:
            raise EngineError("give either a scenario name or a trace path (with venue)")

        key = {"scenario": scenario, "trace": trace, "venue": venue, "filter": filter, "seed": seed,
               "config_patch": config_patch, "overrides": scenario_overrides}
        run_id = f"{Path(scenario or trace).stem}-{filter}-{_short_hash(key)}"
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
            venue_path = str(Path(venue).resolve()) if Path(venue).exists() else str(
                self.paths.root / "data" / "venues" / f"{venue}.json")
            args += ["--trace", trace, "--venue", venue_path]
        if config_patch:
            args += ["--set", json.dumps(config_patch)]

        metrics = json.loads(self._run(args).stdout)
        (run_dir / "meta.json").write_text(json.dumps({**key, "venue_path": venue_path}, indent=2))
        return {"run_id": run_id, "metrics": metrics, "estimates_csv": str(run_dir / "estimates.csv")}

    def _load_run(self, run_id: str) -> tuple[list[dict], dict]:
        d = self._run_dir(run_id)
        with open(d / "estimates.csv", newline="") as f:
            rows = [{k: float(v) for k, v in r.items()} for r in csv.DictReader(f)]
        return rows, json.loads((d / "meta.json").read_text())

    def error_breakdown(self, run_id: str, threshold_m: float = 3.0, min_duration_s: float = 1.0,
                        warmup_s: float = 5.0) -> dict:
        """Finds time windows where the error exceeded a threshold and says where they happened."""
        rows, meta = self._load_run(run_id)
        venue = json.loads(Path(meta["venue_path"]).read_text())
        beacons = venue["beacons"]

        def nearest(x: float, y: float, k: int = 3) -> list[str]:
            ranked = sorted(beacons, key=lambda b: math.hypot(b["x"] - x, b["y"] - y))
            return [f"{b['id']} ({math.hypot(b['x'] - x, b['y'] - y):.1f} m)" for b in ranked[:k]]

        t0 = rows[0]["t"] if rows else 0.0
        windows: list[dict] = []
        current: list[dict] = []
        for r in rows + [None]:
            bad = r is not None and r["t"] - t0 >= warmup_s and r["has_fix"] and r["error_m"] > threshold_m
            if bad:
                current.append(r)
                continue
            if current and current[-1]["t"] - current[0]["t"] >= min_duration_s:
                peak = max(current, key=lambda q: q["error_m"])
                windows.append({
                    "start_s": round(current[0]["t"], 1),
                    "end_s": round(current[-1]["t"], 1),
                    "peak_error_m": round(peak["error_m"], 2),
                    "truth_at_peak": [round(peak["truth_x"], 1), round(peak["truth_y"], 1)],
                    "estimate_at_peak": [round(peak["est_x"], 1), round(peak["est_y"], 1)],
                    "beacons_near_truth": nearest(peak["truth_x"], peak["truth_y"]),
                    "beacons_near_estimate": nearest(peak["est_x"], peak["est_y"]),
                })
            current = []

        evaluated = [r for r in rows if r["t"] - t0 >= warmup_s and r["has_fix"]]
        bad_time = sum(w["end_s"] - w["start_s"] for w in windows)
        return {
            "run_id": run_id,
            "threshold_m": threshold_m,
            "windows": windows,
            "time_above_threshold_s": round(bad_time, 1),
            "evaluated_duration_s": round(evaluated[-1]["t"] - evaluated[0]["t"], 1) if evaluated else 0.0,
            "hint": ("A window whose estimate sits next to a beacon far from the truth suggests a moved or "
                     "mis-surveyed beacon; errors that grow steadily during walking suggest step length or "
                     "heading problems.") if windows else "no sustained errors above threshold",
        }

    def plot_run(self, run_id: str) -> bytes:
        """PNG with the floor plan, true and estimated tracks, and error over time."""
        import matplotlib

        matplotlib.use("Agg")
        import io

        import matplotlib.pyplot as plt

        rows, meta = self._load_run(run_id)
        venue = json.loads(Path(meta["venue_path"]).read_text())
        fig, (ax, ax2) = plt.subplots(2, 1, figsize=(9, 8), gridspec_kw={"height_ratios": [3, 1]})
        for w in venue.get("walls", []):
            ax.plot([w[0], w[2]], [w[1], w[3]], color="#555", lw=2)
        for b in venue["beacons"]:
            ax.scatter(b["x"], b["y"], marker="^", color="#7f3fbf")
            ax.annotate(b["id"], (b["x"], b["y"]), fontsize=7, xytext=(3, 3), textcoords="offset points")
        ax.plot([r["truth_x"] for r in rows], [r["truth_y"] for r in rows], color="#222", lw=2, label="truth")
        fix = [r for r in rows if r["has_fix"]]
        ax.plot([r["est_x"] for r in fix], [r["est_y"] for r in fix], color="#d62728", lw=1, label=meta["filter"])
        ax.set_aspect("equal")
        ax.legend(fontsize=8)
        ax.set_title(run_id)
        ax2.plot([r["t"] for r in fix], [r["error_m"] for r in fix], color="#d62728", lw=1)
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
                   config_patch: dict | None, scenario_overrides: dict | None = None) -> dict:
        with tempfile.TemporaryDirectory(dir=self.paths.workdir) as tmp:
            tmpdir = Path(tmp)
            names = scenarios or [s["name"] for s in self.list_scenarios()]
            for name in names:
                self._materialise_scenario(name, scenario_overrides, target_dir=tmpdir)
            out = tmpdir / "results.json"
            args = [self.paths.tool("ipe-regress"), "--scenarios", tmpdir, "--no-baseline", "--seeds", str(seeds),
                    "--presets", ",".join(presets), "--json", out]
            if config_patch:
                args += ["--set", json.dumps(config_patch)]
            self._run(args)
            return json.loads(out.read_text())

    def compare_filters(self, scenario: str, filters: list[str] | None = None, seeds: int = 5,
                        config_patch: dict | None = None, scenario_overrides: dict | None = None) -> dict:
        filters = filters or list(PRESETS)
        res = self._benchmark([scenario], filters, seeds, config_patch, scenario_overrides)["results"]
        (name, per_filter), = res.items()
        lines = ["| filter | mean [m] | CEP50 [m] | CEP95 [m] | converged [s] |", "|---|---:|---:|---:|---:|"]
        for f in filters:
            m = per_filter[f]
            conv = "never" if m["convergence_s"] < 0 else f"{m['convergence_s']:.1f}"
            lines.append(f"| {f} | {m['mean_m']:.2f} | {m['cep50_m']:.2f} | {m['cep95_m']:.2f} | {conv} |")
        return {"scenario": name, "seeds": seeds, "results": per_filter, "table": "\n".join(lines)}

    def ab_test(self, config_patch: dict, presets: list[str] | None = None, seeds: int = 5,
                scenarios: list[str] | None = None, min_delta_m: float = 0.05) -> dict:
        """Runs every scenario with and without `config_patch` on identical seeds and reports deltas."""
        presets = presets or ["pf"]
        base = self._benchmark(scenarios, presets, seeds, None)["results"]
        cand = self._benchmark(scenarios, presets, seeds, config_patch)["results"]

        rows = []
        counts = {"better": 0, "worse": 0, "mixed": 0, "same": 0}
        lines = ["| scenario | filter | CEP50 A -> B | CEP95 A -> B | B is |", "|---|---|---|---|---|"]
        for scen in sorted(base):
            for p in presets:
                a, b = base[scen][p], cand[scen][p]
                deltas = (b["cep50_m"] - a["cep50_m"], b["cep95_m"] - a["cep95_m"])
                improved = any(d < -min_delta_m for d in deltas)
                degraded = any(d > min_delta_m for d in deltas)
                verdict = ("mixed" if improved and degraded else "better" if improved
                           else "worse" if degraded else "same")
                counts[verdict] += 1
                rows.append({"scenario": scen, "filter": p, "a": a, "b": b, "verdict": verdict})
                lines.append(f"| {scen} | {p} | {a['cep50_m']:.2f} -> {b['cep50_m']:.2f} | "
                             f"{a['cep95_m']:.2f} -> {b['cep95_m']:.2f} | {verdict} |")
        summary = (f"B vs A: {counts['better']} better, {counts['worse']} worse, {counts['mixed']} mixed, "
                   f"{counts['same']} unchanged (|delta| <= {min_delta_m} m, {seeds} seeds)")
        return {"config_patch": config_patch, "seeds": seeds, "summary": summary, "rows": rows,
                "table": "\n".join(lines)}

    def run_regression(self, seeds: int = 3) -> dict:
        baseline = self.paths.root / "tests" / "regression" / "baseline.json"
        proc = self._run([self.paths.tool("ipe-regress"), "--scenarios", self.scenario_dir, "--baseline", baseline,
                          "--seeds", str(seeds)], ok_codes=(0, 1))
        findings = [line for line in proc.stdout.splitlines()
                    if line.split(" ")[0] in {"REGRESSED", "improved", "missing", "new"}]
        return {
            "passed": proc.returncode == 0,
            "findings": findings,
            "note": "Baseline uses 3 seeds; other seed counts are not comparable." if seeds != 3 else "",
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.paths.workdir, ignore_errors=True)
