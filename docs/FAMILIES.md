# Strategy-family clustering

The family stage prevents a final team list from being filled with many versions of the same
behavior. It operates only on **existing ROBUST frozen centers**. It does not create new
parameters and it does not inspect holdout.

## Why parameter distance alone is insufficient

Two parameter sets can be numerically close but trade very differently around a threshold.
Conversely, two somewhat different sets can repeatedly produce the same entries and occupy a
slot at the same times. V1 therefore compares four signals of similarity:

1. **Raw signal Dice** — one-to-one matching of raw signal timestamps within a configured
   tolerance.
2. **Accepted-entry Dice** — the same comparison after each team's one-position occupancy
   rule has removed signals that could not become positions.
3. **Exposure Jaccard** — intersection / union of the actual time intervals in which each
   candidate held a position.
4. **Normalized parameter distance** — RMS distance after dividing each parameter difference
   by an explicit scale from the family contract.

TP and RSI-exit strategies are never collapsed into the same V1 family. Their exit semantics
are considered materially different even when entry signals happen to coincide.

## Complete-link, not chain-link

V1 uses a conservative complete-link rule. A new candidate can join an existing family only
when it passes the family thresholds against **every existing member**.

This avoids the classic single-link failure:

```text
A is similar to B
B is similar to C
A is not similar to C
```

A single-link cluster would still merge A/B/C. This engine refuses that bridge.

## Representative selection

A representative is never synthesized. It is one of the already ROBUST frozen centers.
Candidates are ordered deterministically using evidence already produced by robustness:

- higher validation-neighbor survival;
- higher discovery-neighbor stability;
- better worst neighbor validation return;
- smaller center-vs-neighbor spike;
- higher center validation return;
- fingerprint as the final deterministic tie break.

The first/best center in a family remains its representative. No parameter is retuned.

## Family contract

```json
{
  "schema_version": 1,
  "name": "btc robust families",
  "source_robustness_result_sha256": "SHA256_OF_ROBUSTNESS_RESULT_JSON",
  "thresholds": {
    "signal_tolerance_minutes": 1.0,
    "min_raw_signal_dice": 0.70,
    "min_accepted_signal_dice": 0.70,
    "min_exposure_jaccard": 0.60,
    "max_parameter_distance": 1.25
  },
  "parameter_scales": {
    "rsi_period": 1.0,
    "rsi_entry": 1.0,
    "adx_min": 2.0,
    "adx_max": 2.0,
    "tp_price_pct": 0.25,
    "rsi_exit": 2.0
  },
  "max_pair_evaluations": 20000
}
```

The thresholds are research assumptions and should be declared before inspecting the family
output. They are not universal truths.

## Integrity and phase gates

Before clustering, the engine verifies the robustness-result structure, embedded robustness
contract fingerprint, robust/fragile counts, center strategy fingerprints and stored neighbor
fingerprints. The family contract also pins the exact robustness-result file by SHA-256.

Behavioral evidence comes from discovery + validation only. The dedicated evidence API does
not have a holdout reveal option: requesting holdout is rejected.

## Run

```bash
pextract families \
  --study study.json \
  --robustness-result robustness-result.json \
  --families families.json \
  --data-directory . \
  --output families-result.json
```

The result contains the complete pairwise evidence, complete-link families and one frozen
representative per family. It records:

- `parameters_retuned: false`;
- `representatives_are_existing_robust_centers: true`;
- `discovery_accessed: true`;
- `validation_accessed: true`;
- `holdout_accessed: false`.

## What this stage does not prove

Family deduplication is not portfolio optimization. Two different families can still compete
for the same limited slots or produce highly correlated PnL. The next stage must replay the
representatives together under the live bot's capital, slot and priority rules. Holdout should
remain sealed until that portfolio construction policy is frozen.
