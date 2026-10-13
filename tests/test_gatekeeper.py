"""Gatekeeper tests — the gate must block regressions and pass improvements."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from gatekeeper import DEFAULT_METRICS, evaluate, extract_metrics, gate, load_bench


def batcher_bench(conc_results):
    return {"results": [{"concurrency": c, "aggregate_tok_s": agg,
                         "completion_tok_s": comp, "per_stream_tok_s": ps,
                         "wall_p50_s": p50, "wall_p95_s": p95, "failed": f}
                        for c, agg, comp, ps, p50, p95, f in conc_results]}


def spinal_bench(ttfts, totals, n_failed=0):
    traces = [{"ttftMs": t, "totalMs": s, "failed": False} for t, s in zip(ttfts, totals)]
    traces += [{"failed": True} for _ in range(n_failed)]
    return {"traces": traces}


# ---------------------------------------------------------------- extraction

def test_extract_batcher_shape():
    bench = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    m = extract_metrics(bench)
    assert m["c1_aggregate_tok_s"] == 43.8
    assert m["c1_failed"] == 0


def test_extract_spinal_shape():
    bench = spinal_bench([300, 400, 500, 600, 700], [4000, 5000, 6000, 7000, 8000])
    m = extract_metrics(bench)
    assert m["ttft_p50_ms"] == 500
    assert m["ttft_p95_ms"] == 700
    assert m["failed"] == 0


def test_extract_unknown_shape_raises():
    with pytest.raises(ValueError):
        extract_metrics({"nope": 1})


# ---------------------------------------------------------------- gate verdicts

def test_identical_benchmarks_pass():
    b = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    r = evaluate(b, b)
    assert r.verdict == "pass"
    assert r.exit_code() == 0


def test_improvement_passes():
    base = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    cand = batcher_bench([(1, 50.0, 40.0, 40.0, 2.5, 4.5, 0)])
    r = evaluate(base, cand)
    assert r.verdict == "pass"
    assert r.improvements  # aggregate improved


def test_throughput_regression_blocks():
    base = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    cand = batcher_bench([(1, 35.0, 28.0, 28.0, 2.9, 5.3, 0)])  # -20% throughput
    r = evaluate(base, cand)
    assert r.verdict == "regress"
    assert r.exit_code() == 2
    assert any("aggregate_tok_s" in x for x in r.regressions)


def test_latency_regression_blocks():
    base = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    cand = batcher_bench([(1, 43.8, 35.6, 35.6, 4.5, 8.0, 0)])  # p50 +55%
    r = evaluate(base, cand)
    assert r.verdict == "regress"
    assert any("wall_p50_s" in x for x in r.regressions)


def test_new_failure_always_blocks():
    base = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    cand = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 1)])  # 1 failure
    r = evaluate(base, cand)
    assert r.verdict == "regress"
    assert any("failed" in x for x in r.regressions)


def test_small_noise_within_tolerance_passes():
    base = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    cand = batcher_bench([(1, 42.5, 34.5, 34.5, 3.0, 5.4, 0)])  # ~3% noise
    r = evaluate(base, cand)
    assert r.verdict == "pass"


def test_custom_tolerance_tightens():
    base = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    cand = batcher_bench([(1, 42.5, 34.5, 34.5, 2.9, 5.3, 0)])  # -3% throughput
    r_default = evaluate(base, cand)  # 5% default → pass
    r_tight = evaluate(base, cand, tolerances={"c1_aggregate_tok_s": 0.01})
    assert r_default.verdict == "pass"
    assert r_tight.verdict == "regress"


def test_spinal_ttft_regression_blocks():
    base = spinal_bench([300, 320, 340, 360, 380], [4000] * 5)
    cand = spinal_bench([500, 520, 540, 560, 580], [4000] * 5)  # ttft +60%
    r = evaluate(base, cand)
    assert r.verdict == "regress"
    assert any("ttft" in x for x in r.regressions)


# ---------------------------------------------------------------- errors

def test_missing_baseline_is_error():
    r = gate("/nonexistent/base.json", "/nonexistent/cand.json")
    assert r.verdict == "error"
    assert r.exit_code() == 1


def test_malformed_candidate_is_error():
    base = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    r = evaluate(base, {"unrecognized": True})
    assert r.verdict == "error"


# ---------------------------------------------------------------- shape skew

def test_shape_skew_ignores_missing_metrics():
    """Baseline batcher vs candidate spinal: common metrics only (failed)."""
    base = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    cand = spinal_bench([300, 400], [4000, 5000])
    r = evaluate(base, cand)
    # 'failed' is the only shared metric; 0 == 0 → no regression
    assert r.verdict == "pass"


# ---------------------------------------------------------------- file roundtrip

def test_gate_end_to_end(tmp_path):
    base_f = tmp_path / "base.json"
    cand_f = tmp_path / "cand.json"
    base_f.write_text(json.dumps(batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])))
    cand_f.write_text(json.dumps(batcher_bench([(1, 30.0, 25.0, 25.0, 4.0, 7.0, 0)])))
    r = gate(str(base_f), str(cand_f))
    assert r.verdict == "regress"
    assert len(r.regressions) >= 2


def test_result_serializable():
    base = batcher_bench([(1, 43.8, 35.6, 35.6, 2.9, 5.3, 0)])
    cand = batcher_bench([(1, 30.0, 25.0, 25.0, 4.0, 7.0, 0)])
    r = evaluate(base, cand)
    json.dumps(r.to_dict())  # must not raise
