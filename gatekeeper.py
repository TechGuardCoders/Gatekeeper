"""
Gatekeeper — CI/CD regression gate for AI benchmarks.

Evals that do not block deploys are reports, not gates. Gatekeeper turns
Batcher/Spinal-style benchmark JSON into a merge decision:

    pass    — metrics within tolerance vs the baseline file
    regress — any tracked metric worse than baseline beyond tolerance
    error   — baseline missing/unreadable, or candidate malformed

Design:
- Compares a CANDIDATE benchmark file against a BASELINE benchmark file
  (both the batcher_*.json / spinal_*.json shape).
- Per-metric tolerance, with hysteresis: a metric must regress beyond
  tolerance on the WHOLE file, not on one sample.
- Exit code 0 = pass (merge allowed), 2 = regress (block), 1 = error.
  CI-native: the exit code IS the gate.

Tracked metrics (batcher): aggregate_tok_s (higher better),
completion_tok_s (higher), per_stream_tok_s (higher),
wall_p50_s / wall_p95_s (lower better), failed (lower).
Tracked metrics (spinal): ttftMs p50/p95 (lower), totalMs p50 (lower).

Public API:
    load_bench(path) -> dict
    extract_metrics(bench) -> dict[str, float]
    evaluate(baseline, candidate, tolerances) -> GateResult
    GateResult (verdict, regressions, improvements, details)
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

__version__ = "0.1.0"

# metric -> (direction, default_tolerance_fraction)
# direction: "higher" = higher is better; "lower" = lower is better
# NOTE: c<N>_ prefixed metrics are generated per-concurrency; map their suffixes.
DEFAULT_METRICS: dict[str, tuple[str, float]] = {
    "aggregate_tok_s": ("higher", 0.05),   # 5% tolerance
    "completion_tok_s": ("higher", 0.05),
    "per_stream_tok_s": ("higher", 0.10),  # per-stream is noisier
    "wall_p50_s": ("lower", 0.10),
    "wall_p95_s": ("lower", 0.15),
    "ttft_p50_ms": ("lower", 0.15),
    "ttft_p95_ms": ("lower", 0.20),
    "total_p50_ms": ("lower", 0.10),
    "failed": ("lower", 0.0),              # any new failure regresses
}


def _metric_direction(metric: str) -> tuple[str, float]:
    """Look up direction/tolerance for a metric, stripping c<N>_ prefixes."""
    if metric in DEFAULT_METRICS:
        return DEFAULT_METRICS[metric]
    for suffix, spec in DEFAULT_METRICS.items():
        if metric.endswith("_" + suffix) or metric.startswith("c") and metric.endswith(suffix):
            return spec
    return ("lower", 0.05)  # conservative default


# ---------------------------------------------------------------- loading

def load_bench(path: str | Path) -> dict:
    """Load a batcher/spinal benchmark JSON file."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"benchmark file not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def _percentile(values: list[float], frac: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(frac * len(s)))
    return s[idx]


def extract_metrics(bench: dict) -> dict[str, float]:
    """Flatten a benchmark file into a flat metric dict.

    Handles both shapes:
    - batcher: results[] per concurrency -> aggregate_tok_s etc (use best level
      by aggregate throughput? No — compare per concurrency: emit c<N>_ prefixed)
    - spinal: traces[] -> ttftMs/totalMs percentiles
    """
    out: dict[str, float] = {}

    if "results" in bench:  # batcher shape
        for r in bench["results"]:
            c = r["concurrency"]
            out[f"c{c}_aggregate_tok_s"] = float(r.get("aggregate_tok_s", 0))
            out[f"c{c}_completion_tok_s"] = float(r.get("completion_tok_s", 0))
            out[f"c{c}_per_stream_tok_s"] = float(r.get("per_stream_tok_s", 0))
            out[f"c{c}_wall_p50_s"] = float(r.get("wall_p50_s", 0))
            out[f"c{c}_wall_p95_s"] = float(r.get("wall_p95_s", 0))
            out[f"c{c}_failed"] = float(r.get("failed", 0))
    elif "traces" in bench:  # spinal shape
        ok = [t for t in bench["traces"] if not t.get("failed")]
        ttfts = [float(t["ttftMs"]) for t in ok if "ttftMs" in t]
        totals = [float(t["totalMs"]) for t in ok if "totalMs" in t]
        out["ttft_p50_ms"] = _percentile(ttfts, 0.5)
        out["ttft_p95_ms"] = _percentile(ttfts, 0.95)
        out["total_p50_ms"] = _percentile(totals, 0.5)
        out["failed"] = float(len(bench["traces"]) - len(ok))
    else:
        raise ValueError("unrecognized benchmark shape (no 'results' or 'traces')")

    return out


# ---------------------------------------------------------------- gate

@dataclass
class GateResult:
    verdict: str                    # pass | regress | error
    regressions: list[str] = field(default_factory=list)
    improvements: list[str] = field(default_factory=list)
    details: dict[str, dict] = field(default_factory=dict)  # metric -> {base, cand, tol, delta}
    error: str = ""

    def exit_code(self) -> int:
        return {"pass": 0, "regress": 2, "error": 1}[self.verdict]

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "regressions": self.regressions,
            "improvements": self.improvements,
            "details": self.details,
            "error": self.error,
        }


def evaluate(baseline: dict, candidate: dict,
             tolerances: dict[str, float] | None = None) -> GateResult:
    """Compare candidate metrics against baseline. Returns a GateResult.

    tolerances: metric -> fraction (0.05 = 5%). Falls back to DEFAULT_METRICS.
    A metric regresses if it's worse than baseline by more than tolerance.
    Metrics present in only one file are ignored (shape/version skew).
    """
    try:
        base_m = extract_metrics(baseline)
        cand_m = extract_metrics(candidate)
    except (ValueError, KeyError) as e:
        return GateResult(verdict="error", error=str(e))

    tolerances = tolerances or {}
    result = GateResult(verdict="pass")

    for metric, base_val in base_m.items():
        if metric not in cand_m:
            continue  # shape skew — ignore
        direction, default_tol = _metric_direction(metric)
        tol = tolerances.get(metric, default_tol)
        cand_val = cand_m[metric]

        if direction == "higher":
            if base_val == 0:
                delta = 0.0 if cand_val == 0 else float("inf")
                regressed = cand_val < base_val
            else:
                delta = (cand_val - base_val) / base_val
                regressed = delta < -tol
        else:
            if base_val == 0:
                # zero baseline: any nonzero candidate regresses (e.g. failed 0 -> 1)
                delta = 0.0 if cand_val == 0 else float("inf")
                regressed = cand_val > base_val
            else:
                delta = (cand_val - base_val) / base_val
                regressed = delta > tol

        result.details[metric] = {
            "baseline": round(base_val, 4), "candidate": round(cand_val, 4),
            "delta_pct": round(delta * 100, 2), "tolerance_pct": round(tol * 100, 1),
            "direction": direction,
        }
        if regressed:
            result.regressions.append(
                f"{metric}: {base_val} -> {cand_val} ({delta * 100:+.1f}% vs "
                f"tolerance {tol * 100:.0f}%)")
            result.verdict = "regress"
        elif abs(delta) > tol:
            result.improvements.append(f"{metric}: {delta * 100:+.1f}%")

    return result


def gate(baseline_path: str, candidate_path: str,
         tolerances: dict[str, float] | None = None) -> GateResult:
    """One-call gate: load both files, evaluate, return result."""
    try:
        baseline = load_bench(baseline_path)
        candidate = load_bench(candidate_path)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return GateResult(verdict="error", error=str(e))
    return evaluate(baseline, candidate, tolerances)


# ---------------------------------------------------------------- CLI

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Gatekeeper — benchmark regression gate")
    p.add_argument("baseline", help="baseline benchmark JSON")
    p.add_argument("candidate", help="candidate benchmark JSON")
    p.add_argument("--tolerance", action="append", default=[],
                   help="metric=fraction, e.g. aggregate_tok_s=0.03 (repeatable)")
    args = p.parse_args()

    tolerances = {}
    for t in args.tolerance:
        k, v = t.split("=", 1)
        tolerances[k] = float(v)

    result = gate(args.baseline, args.candidate, tolerances)
    print(json.dumps(result.to_dict(), indent=2))
    sys.exit(result.exit_code())


if __name__ == "__main__":
    main()
