"""Gatekeeper tests — the gate must block real regressions and pass healthy runs."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
from gatekeeper import Baseline, GateResult, check


def base(**kw) -> Baseline:
    d = {"success_rate": 0.95, "latency_p95_s": 2.0, "samples": 50}
    d.update(kw)
    return Baseline(**d)


# ---------------------------------------------------------------- pass cases

def test_healthy_run_passes():
    r = check(base(), base(), state_path=None)
    assert r.verdict == "pass"
    assert not any("consecutive failures" in x for x in r.reasons)


def test_tiny_success_drop_within_tolerance_passes():
    r = check(base(), base(success_rate=0.94), state_path=None)  # 1pt drop < 2pt max
    assert r.verdict == "pass"


def test_small_latency_regression_within_tolerance_passes():
    r = check(base(), base(latency_p95_s=2.2), state_path=None)  # 10% < 15% max
    assert r.verdict == "pass"


# ---------------------------------------------------------------- fail cases

def test_success_drop_blocks():
    r = check(base(), base(success_rate=0.90), state_path=None)  # 5pt drop > 2pt
    assert r.verdict == "fail"
    assert any("success dropped" in x for x in r.reasons)


def test_latency_regression_blocks():
    r = check(base(), base(latency_p95_s=2.5), state_path=None)  # 25% > 15%
    assert r.verdict == "fail"
    assert any("latency regressed" in x for x in r.reasons)


def test_both_failing_blocks():
    r = check(base(), base(success_rate=0.90, latency_p95_s=2.5), state_path=None)
    assert r.verdict == "fail"
    assert len([x for x in r.reasons if "consecutive" in x]) == 2


# ---------------------------------------------------------------- skip cases

def test_few_samples_skips():
    r = check(base(), base(samples=5), state_path=None)
    assert r.verdict == "skip"
    assert any("samples" in x for x in r.reasons)


# ---------------------------------------------------------------- hysteresis

def test_flaky_failure_needs_streak_to_block(tmp_path):
    state = tmp_path / "state.json"
    # first failure: warning only
    r1 = check(base(), base(success_rate=0.90), state_path=state)
    assert r1.verdict == "pass"
    assert any("warning only" in x for x in r1.reasons)
    # second consecutive failure: blocks
    r2 = check(base(), base(success_rate=0.90), state_path=state)
    assert r2.verdict == "fail"


def test_streak_resets_on_pass(tmp_path):
    state = tmp_path / "state.json"
    check(base(), base(success_rate=0.90), state_path=state)   # fail 1
    check(base(), base(), state_path=state)                    # pass resets
    r3 = check(base(), base(success_rate=0.90), state_path=state)  # fail 1 again
    assert r3.verdict == "pass"  # streak was reset


def test_hysteresis_one_blocks_immediately(tmp_path):
    state = tmp_path / "state.json"
    r = check(base(), base(success_rate=0.90), hysteresis=1, state_path=state)
    assert r.verdict == "fail"


# ---------------------------------------------------------------- baseline io

def test_baseline_roundtrip(tmp_path):
    bl = base(meta={"model": "glm-5.3-flash"})
    p = tmp_path / "baseline.json"
    bl.save(p)
    bl2 = Baseline.load(p)
    assert bl2.success_rate == bl.success_rate
    assert bl2.latency_p95_s == bl.latency_p95_s
    assert bl2.meta.get("model") == "glm-5.3-flash"


def test_baseline_from_ok_failed():
    bl = Baseline.from_results({"ok": 95, "failed": 5, "latency_p95_s": 2.0, "requests": 100})
    assert bl.success_rate == 0.95
    assert bl.samples == 100


def test_baseline_requires_metrics():
    with pytest.raises(ValueError):
        Baseline.from_results({"foo": 1})


# ---------------------------------------------------------------- serialization

def test_gate_result_serializable():
    r = check(base(), base(success_rate=0.90), state_path=None)
    json.dumps(r.to_dict())  # must not raise
    assert r.to_dict()["verdict"] == "fail"
