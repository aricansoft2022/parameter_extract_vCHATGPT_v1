# Final ccbot deployment export

This is the final serialization stage of the research pipeline. It does **not** search,
retune, drop, reorder or re-leverage strategies. It turns an already frozen selected set
into the exact CSV contract understood by the audited live bot, and writes a provenance
manifest beside it.

## Preconditions

The exporter requires the complete final artifact chain:

- the exact `selection-result.json` whose selected-set fingerprint was carried through
  holdout and risk evaluation;
- the exact `risk-result.json` produced from that selection lineage;
- an `exchange-risk-result.json` with `status: EXCHANGE_RISK_PASS`,
  `exchange_liquidation_validated: true` and `teams_export_ready: true`.

`deployment.json` pins the selection and exchange-risk files by SHA-256. The exporter also
requires the supplied risk-result file to match the exact SHA stored inside exchange-risk,
and the risk result must in turn pin the supplied selection-result SHA. This closes the
selection -> risk -> exchange-risk artifact lineage instead of merely checking that the
strategy parameters happen to be equal.

The selected-set fingerprint, symbol and slot count must remain consistent through the
chain.

## Audited live-bot contract

V1 intentionally supports only:

- repository: `aricansoft2022/cryptobot_vCLUADE_v5`
- commit: `0ab6aa532cb22f399bc94393280c604cb6756d66`

The live-bot CSV fields audited at that commit are:

```text
id,enabled,priority,symbol,rsi_period,rsi_entry,adx_min,adx_max,exit_mode,rsi_exit,tp_price_pct,leverage
```

The exporter also mirrors the audited live `Team.validate()` constraints: positive IDs,
priority 0..10000, RSI period 14..19, valid RSI/ADX bounds, exit-mode-specific values and
leverage 1..125.

If the live bot changes, do not merely edit the commit SHA in a deployment file. Update
and re-audit the exporter contract first.

## deployment.json

Example:

```json
{
  "schema_version": 1,
  "name": "BTC production candidate set",
  "source_selection_result_sha256": "COPY_SHA256_OF_SELECTION_RESULT",
  "source_exchange_risk_result_sha256": "COPY_SHA256_OF_EXCHANGE_RISK_RESULT",
  "target_ccbot_repository": "aricansoft2022/cryptobot_vCLUADE_v5",
  "target_ccbot_commit_sha": "0ab6aa532cb22f399bc94393280c604cb6756d66",
  "first_team_id": 101,
  "enabled": false
}
```

`first_team_id` is explicit because the research engine does not know what IDs already
exist in a running ccbot database. IDs are assigned sequentially from that number.

`enabled` is also explicit. `false` is the safer handoff default because importing a CSV
should not silently become an authorization to open new positions. If an operator chooses
`true`, that choice is recorded in the deployment fingerprint and manifest.

## Export

```bash
pextract-deploy \
  --selection-result selection-result.json \
  --risk-result risk-result.json \
  --exchange-risk-result exchange-risk-result.json \
  --deployment deployment.json \
  --teams-csv teams.csv \
  --manifest deployment-manifest.json
```

The CSV preserves the selected set's compact priority order `1..N`. It does not search
priority permutations. Every row uses the one leverage value already approved by the
risk and exchange-risk gates. For TP teams `rsi_exit` is blank; for RSI-exit teams
`tp_price_pct` is blank.

## Deployment manifest

`deployment-manifest.json` stores enough information to audit the serialization step:

- exact source selection, risk and exchange-risk SHA-256 values;
- source holdout, study and dataset lineage inherited from the verified risk result;
- selected-set and source-portfolio fingerprint lineage;
- exchange snapshot fingerprint;
- exact audited ccbot repository and commit;
- audited CSV contract fingerprint and field order;
- slot/allocation/reserve/leverage assumptions;
- the frozen selected rows and exact ccbot rows;
- SHA-256 and byte size of `teams.csv`;
- a self-verifying deployment-manifest fingerprint.

It also records:

```text
complete_artifact_lineage_checked: true
strategy_parameters_retuned: false
selected_set_changed: false
priority_reoptimized: false
leverage_optimized: false
exchange_liquidation_validated: true
teams_export_ready: true
existing_live_team_id_collisions_checked: false
import_requires_ccbot_dry_run: true
```

## Live import procedure

The exporter deliberately does not connect to the live bot and does not modify its
repository or database.

Before applying the CSV on the audited ccbot checkout, run the bot's own validation-only
import first:

```bash
ccbot import-teams teams.csv
```

Only after that dry run succeeds should an operator consciously decide whether to apply:

```bash
ccbot import-teams teams.csv --apply
```

If IDs collide with existing teams, choose a different `first_team_id`, regenerate both
artifacts, and keep the new deployment manifest with the CSV. Do not hand-edit `teams.csv`:
that would break its recorded SHA-256 and provenance.

Export approval is a serialization/integrity statement, not a guarantee of trading
profit or a substitute for checking current exchange/account state before deployment.
