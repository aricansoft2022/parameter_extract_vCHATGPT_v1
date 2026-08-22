# Fail-closed scale calibration

`pextract-scale` is an operational calibration layer for deciding what candidate budget has
actually been demonstrated on a specific machine, dataset and set of search contracts.
It does not optimize strategy parameters and it does not change a search file.

## Why calibration is explicit

The repository intentionally does not contain representative production candle/study
artifacts. Synthetic test timings are not a basis for claiming that 50k, 100k or one million
candidates are operationally safe.

A calibration contract therefore names every scale stage explicitly and pins the exact
study, dataset and search fingerprints used to exercise it.

## Contract

Example shape only — the resource limits below are placeholders, **not recommended values**:

```json
{
  "schema_version": 1,
  "name": "BTC research workstation ladder",
  "study_file": "study.json",
  "study_fingerprint_sha256": "<64-hex-study-fingerprint>",
  "dataset_fingerprint_sha256": "<64-hex-dataset-fingerprint>",
  "stages": [
    {
      "name": "stage-50k",
      "search_file": "search-50k.json",
      "search_fingerprint_sha256": "<64-hex-search-fingerprint>",
      "expected_max_candidates": 50000,
      "min_evaluated_candidates": 40000,
      "max_elapsed_seconds": 600.0,
      "max_peak_python_heap_mb": 2048.0
    },
    {
      "name": "stage-100k",
      "search_file": "search-100k.json",
      "search_fingerprint_sha256": "<64-hex-search-fingerprint>",
      "expected_max_candidates": 100000,
      "min_evaluated_candidates": 80000,
      "max_elapsed_seconds": 1200.0,
      "max_peak_python_heap_mb": 3072.0
    }
  ]
}
```

Candidate budgets must strictly increase. Each stage's `expected_max_candidates` must equal
the `refinement.max_candidates` in its exact search contract.

`min_evaluated_candidates` prevents a nominally large cap from passing without actually
exercising comparable scale. Choose it from the intended calibration design; the runner does
not invent a fraction for you.

The study/search paths must be relative to the calibration file and cannot escape its
directory. This keeps the contract's artifact bundle self-contained.

## Run

```bash
pextract-scale calibrate \
  --calibration scale-calibration.json \
  --data-directory /path/to/data \
  --output scale-calibration-result.json
```

Every stage uses `bulk_entry_membership_exact_v1`, which already runs the full runtime parity
chain back to truth replay.

A stage fails for any of the following classes of reasons:

- result/engine/provenance mismatch;
- validation or holdout access;
- runtime parity failure;
- bulk entry cache fallback miss;
- too few evaluated candidates to exercise the declared scale;
- evaluated candidates exceeding the declared budget;
- wall-clock limit exceeded;
- peak Python heap limit exceeded;
- inconsistent deterministic work profile;
- engine exception.

The ladder stops at the first failed stage. `safe_max_candidates` is only the last passing
stage's declared budget. If the first stage fails it is `null`.

The runner records but never applies this number:

```text
auto_raises_candidate_cap: false
```

Changing a real search cap remains an explicit research-contract change.

## Resource measurement

Elapsed time is measured with `time.perf_counter()` around the complete bulk-search stage.
Peak memory is measured with `tracemalloc`, so the stored number is **peak Python heap**, not
total process RSS or system memory. Machine/Python metadata is recorded with the result.

OS filesystem cache, CPU load, thermal behavior and other machine effects can change elapsed
time. A calibration result is evidence for that machine/run context, not a universal
benchmark claim.

## Work evidence

Each successful stage also carries:

- bulk entry event visits;
- ADX band-membership checks;
- event-scan reduction fraction;
- deterministic query work profile;
- coarse/refined/evaluated/Pareto candidate counts.

Use those counters with elapsed time to explain scaling rather than treating wall-clock time
alone as a performance model.

## Verify a stored result

```bash
pextract-scale verify --result scale-calibration-result.json
```

The verifier checks:

- the embedded calibration contract and fingerprint;
- exact stage order;
- stage evidence against the stored limits;
- first-failure stopping semantics;
- `safe_max_candidates` consistency;
- `all_stages_passed` consistency;
- a fingerprint over the complete calibration result.

Mutating a stage measurement, status or safe-cap value breaks verification.

## Recommended operating procedure

1. Create representative study/data artifacts outside Git and pin their fingerprints.
2. Create several explicit search contracts whose grids genuinely exercise increasing
   candidate counts.
3. Choose resource limits appropriate to the machine and the research workflow.
4. Run the ladder once under reasonably controlled machine load.
5. Inspect both wall-clock/resource evidence and logical work counters.
6. If a stage fails, keep the last passing cap; do not reinterpret the failed stage as a pass.
7. If the next scale requires code optimization, change the engine first and recalibrate with
   a new result artifact.

Do not use sealed holdout performance to decide a computational candidate cap. Scale
calibration is an operational property of the discovery engine and should remain separate
from strategy-selection evidence.
