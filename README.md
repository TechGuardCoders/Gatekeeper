# Gatekeeper

**CI/CD regression gate for AI benchmarks — evals that block deploys, not just report them.**

Gatekeeper compares a candidate benchmark file against a baseline and returns a merge decision:

| Verdict | Exit code | Meaning |
|---|---|---|
| `pass` | 0 | metrics within tolerance — merge allowed |
| `regress` | 2 | any tracked metric worse than baseline beyond tolerance — **block** |
| `error` | 1 | baseline missing/unreadable or candidate malformed |

The exit code IS the gate: wire it into GitHub Actions and a throughput drop or latency spike blocks the merge.

## What it tracks

**Batcher shape** (`results[]` per concurrency): `aggregate_tok_s`, `completion_tok_s`, `per_stream_tok_s` (higher-better), `wall_p50_s`, `wall_p95_s` (lower-better), `failed` (any new failure regresses).

**Spinal shape** (`traces[]`): `ttft_p50_ms`, `ttft_p95_ms`, `total_p50_ms` (lower-better).

Per-metric tolerances with sensible defaults (5% throughput, 10-20% latency, 0% failures), overridable per metric. `c<N>_`-prefixed metrics inherit their suffix's direction automatically.

## Quick start

```bash
# CLI — exit code drives the gate
python gatekeeper.py baseline.json candidate.json

# custom tolerance: block if aggregate throughput drops >3%
python gatekeeper.py baseline.json candidate.json --tolerance c1_aggregate_tok_s=0.03
```

```python
from gatekeeper import gate

r = gate("baseline.json", "candidate.json")
r.verdict        # 'regress'
r.exit_code()    # 2
r.regressions    # ['c1_aggregate_tok_s: 43.8 -> 30.0 (-31.5% vs tolerance 5%)']
r.to_dict()      # full machine-readable result
```

## GitHub Actions

```yaml
- name: Benchmark regression gate
  run: python gatekeeper.py baselines/main.json candidate.json
```

Non-zero exit fails the step → the merge blocks.

## Design decisions

- **Hysteresis by tolerance, not by sample**: a metric must regress beyond tolerance on the whole file — one noisy sample can't block.
- **Zero-baseline handling**: `failed: 0 → 1` regresses even though percentage math is undefined (any nonzero candidate on a zero baseline is a regression).
- **Shape skew tolerated**: metrics present in only one file are ignored, so batcher-vs-spinal comparisons don't false-alarm.
- **Fail-closed on error**: a missing baseline is `error` (exit 1), not silent pass.

## Part of the TechGuardCoders portfolio

Extends [Batcher](https://github.com/TechGuardCoders/Batcher) and [Spinal](https://github.com/TechGuardCoders/Spinal): their benchmark files are the inputs. The gate closes the loop — benchmarks measure, Gatekeeper enforces.

## Tests

```bash
python -m pytest tests/ -q   # 16 tests
```

Every verdict path has a test: regressions block, improvements pass, noise within tolerance passes, new failures always block, custom tolerances tighten, shape skew is ignored, and errors fail closed.
