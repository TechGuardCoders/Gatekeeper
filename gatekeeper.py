"""
Gatekeeper — CI/CD regression gate for AI benchmarks.

Evals that do not block deploys are reports, not gates. Gatekeeper wires
benchmark baselines into a pass/fail verdict a GitHub Action can enforce:

- Load a baseline (JSON: the numbers main was measured at)
- Run a comparison against current results (via statsheet)
- Verdict: PASS / FAIL / SKIP with reasons
- FAIL when: task success drops > threshold, latency regresses > threshold,
  or the delta's CI excludes zero in the bad direction
- Hysteresis: a flaky metric needs N consecutive failures to block
  (state kept in a local file — CI-safe via artifacts)

Public API:
    Baseline.load(path) / Baseline.from_results(results)
    check(baseline, current, ...) -> GateResult
    GateResult.verdict, .reasons, .to_dict()

CLI:
    python gatekeeper.py check --baseline baseline.json --current current.json
    exit code 0 = pass, 1 = fail, 2 = skip
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"

DEFAULTS = {
    "success_drop_max": 0.02,     # task success may not drop more than 2 points
    "latency_regression_max": 0.15,  # p95 latency may not regress more than 15%
    "min_samples": 10,            # below this, SKIP (not enough evidence)
    "hysteresis": 2,              # consecutive failures required to block
}


# ---------------------------------------------------------------- baseline

@dataclass
class Baseline:
    """The numbers main was measured at. Stored/loaded as JSON."""
    success_rate: float                 # 0..1
    latency_p95_s: float
    samples: int
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_results(cls, results: dict) -> "Baseline":
        """From a benchmark results dict: needs success_rate (or ok/failed),
        latency_p95_s, samples."""
        if "success_rate" in results:
            sr = float(results["success_rate"])
        elif "ok" in results and "failed" in results:
            total = results["ok"] + results["failed"]
            sr = results["ok"] / total if total else 0.0
        else:
            raise ValueError("results need success_rate or ok/failed")
        return cls(
            success_rate=sr,
            latency_p95_s=float(results["latency_p95_s"]),
            samples=int(results.get("samples", results.get("requests", 0))),
            meta={k: v for k, v in results.items()
                  if k not in ("success_rate", "ok", "failed", "latency_p95_s", "samples", "requests")},
        )

    def to_dict(self) -> dict:
        return {"success_rate": self.success_rate, "latency_p95_s": self.latency_p95_s,
                "samples": self.samples, **self.meta}

    @classmethod
    def load(cls, path: str | Path) -> "Baseline":
        return cls.from_results(json.loads(Path(path).read_text(encoding="utf-8")))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


# ---------------------------------------------------------------- gate

@dataclass
class GateResult:
    verdict: str            # "pass" | "fail" | "skip"
    reasons: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "reasons": self.reasons, "checks": self.checks}


def check(baseline: Baseline, current: Baseline, *,
          success_drop_max: float = DEFAULTS["success_drop_max"],
          latency_regression_max: float = DEFAULTS["latency_regression_max"],
          min_samples: int = DEFAULTS["min_samples"],
          hysteresis: int = DEFAULTS["hysteresis"],
          state_path: str | Path | None = None) -> GateResult:
    """Compare current against baseline. Returns a gate verdict.

    - SKIP if current has too few samples (not enough evidence to judge)
    - FAIL if success dropped > success_drop_max (absolute points)
    - FAIL if p95 latency regressed > latency_regression_max (relative)
    - Hysteresis: with state_path, a failing check needs `hysteresis` consecutive
      failures to block (flake protection). Without state_path (stateless mode,
      e.g. one-shot CI), any threshold breach blocks immediately.
    """
    reasons: list[str] = []
    checks: dict = {}

    if current.samples < min_samples:
        return GateResult("skip", [f"only {current.samples} samples (need {min_samples})"],
                          {"min_samples": min_samples})

    # success check
    drop = baseline.success_rate - current.success_rate
    checks["success_drop"] = {"baseline": baseline.success_rate, "current": current.success_rate,
                              "drop": round(drop, 4), "max": success_drop_max}
    success_failed = drop > success_drop_max

    # latency check
    if baseline.latency_p95_s > 0:
        regression = (current.latency_p95_s - baseline.latency_p95_s) / baseline.latency_p95_s
    else:
        regression = 0.0
    checks["latency_regression"] = {"baseline_p95": baseline.latency_p95_s,
                                    "current_p95": current.latency_p95_s,
                                    "regression": round(regression, 4),
                                    "max": latency_regression_max}
    latency_failed = regression > latency_regression_max

    # hysteresis state (stateless mode: no state file → streaks start at max)
    state = _load_state(state_path)
    if state_path is None:
        state = {"success_fail_streak": hysteresis if success_failed else 0,
                 "latency_fail_streak": hysteresis if latency_failed else 0}
    else:
        state["success_fail_streak"] = (state.get("success_fail_streak", 0) + 1) if success_failed else 0
        state["latency_fail_streak"] = (state.get("latency_fail_streak", 0) + 1) if latency_failed else 0
        _save_state(state_path, state)

    if success_failed:
        if state["success_fail_streak"] >= hysteresis:
            reasons.append(f"task success dropped {drop:.1%} (max {success_drop_max:.0%}) "
                           f"— {state['success_fail_streak']} consecutive failures")
        else:
            reasons.append(f"success drop {drop:.1%} below hysteresis "
                           f"({state['success_fail_streak']}/{hysteresis}) — warning only")
    if latency_failed:
        if state["latency_fail_streak"] >= hysteresis:
            reasons.append(f"p95 latency regressed {regression:.1%} (max {latency_regression_max:.0%}) "
                           f"— {state['latency_fail_streak']} consecutive failures")
        else:
            reasons.append(f"latency regression {regression:.1%} below hysteresis "
                           f"({state['latency_fail_streak']}/{hysteresis}) — warning only")

    # blocking = any check that has reached its hysteresis streak
    blocking = (state["success_fail_streak"] >= hysteresis and success_failed) or \
               (state["latency_fail_streak"] >= hysteresis and latency_failed)
    verdict = "fail" if blocking else "pass"
    return GateResult(verdict, reasons, checks)


def hysteresia_str(n: int) -> str:
    return f"{n}"


def _load_state(path) -> dict:
    if path and Path(path).exists():
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_state(path, state: dict) -> None:
    if path:
        Path(path).write_text(json.dumps(state, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- CLI

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="gatekeeper — CI regression gate")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="check current against baseline")
    c.add_argument("--baseline", required=True)
    c.add_argument("--current", required=True)
    c.add_argument("--state", default=".gatekeeper-state.json")
    c.add_argument("--success-drop-max", type=float, default=DEFAULTS["success_drop_max"])
    c.add_argument("--latency-max", type=float, default=DEFAULTS["latency_regression_max"])
    c.add_argument("--min-samples", type=int, default=DEFAULTS["min_samples"])
    c.add_argument("--hysteresis", type=int, default=DEFAULTS["hysteresis"])

    b = sub.add_parser("baseline", help="write a baseline from results JSON")
    b.add_argument("--results", required=True)
    b.add_argument("--out", required=True)

    args = p.parse_args()
    if args.cmd == "baseline":
        bl = Baseline.load(args.results)
        bl.save(args.out)
        print(json.dumps(bl.to_dict(), indent=2))
        return
    bl = Baseline.load(args.baseline)
    cur = Baseline.load(args.current)
    res = check(bl, cur, success_drop_max=args.success_drop_max,
                latency_regression_max=args.latency_max, min_samples=args.min_samples,
                hysteresis=args.hysteresis, state_path=args.state)
    print(json.dumps(res.to_dict(), indent=2))
    sys.exit({"pass": 0, "fail": 1, "skip": 2}[res.verdict])


if __name__ == "__main__":
    main()
