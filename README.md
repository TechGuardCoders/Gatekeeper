# Gatekeeper

**CI/CD regression gate for AI benchmarks — evals that block deploys, not dashboards.**

Gatekeeper turns benchmark baselines into a pass/fail verdict a GitHub Action can enforce:

- **FAIL** when task success drops more than 2 points, or p95 latency regresses more than 15%
- **SKIP** when there's too little evidence to judge (below min samples)
- **PASS** otherwise — with hysteresis so one flaky run doesn't block a merge

## The gate contract

```python
from gatekeeper import Baseline, check

baseline = Baseline.load("baseline.json")     # numbers main was measured at
current  = Baseline.load("current.json")      # this PR's numbers

result = check(baseline, current, state_path="gate-state.json")
result.verdict   # 'pass' | 'fail' | 'skip'
result.reasons   # human-readable, for the PR comment
sys.exit(0 if result.verdict == "pass" else 1)
```

## CLI

```bash
# snapshot a baseline from a benchmark run
python gatekeeper.py baseline --results results.json --out baseline.json

# the gate (exit 0 = pass, 1 = fail, 2 = skip)
python gatekeeper.py check --baseline baseline.json --current current.json
```

## Design decisions

- **Hysteresis with state**: a failing check needs N consecutive failures to block (default 2) — one flaky run warns, a real regression blocks. State is a small JSON file; in CI, pass it between steps via artifacts. Stateless mode (no `state_path`) blocks on any breach — right for one-shot gates.
- **Absolute success drop, relative latency regression**: success is a percentage-point contract ("never below 93%"); latency is a ratio ("no more than 15% worse than main").
- **Skip, don't guess**: below min samples the gate refuses to judge rather than passing or failing on noise.
- **Every reason is a sentence**: the PR comment is the output.

## GitHub Action

```yaml
- name: Regression gate
  run: |
    python gatekeeper.py check --baseline baseline.json --current current.json --state gate-state.json
```

Non-zero exit blocks the merge.

## Part of the TechGuardCoders portfolio

Extends [Batcher](https://github.com/TechGuardCoders/Batcher) and [Spinal](https://github.com/TechGuardCoders/Spinal): their benchmark output becomes gate input. Baselines are snapshotted from main; PRs run the same harness and Gatekeeper decides.

## Tests

```bash
python -m pytest tests/ -q   # 14 tests
```

The suite encodes the gate contract: real regressions block, healthy runs pass, flaky failures need a streak, insufficient evidence skips.
