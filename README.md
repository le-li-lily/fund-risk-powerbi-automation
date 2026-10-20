# Fund Risk → Power BI Automation

A reusable daily market-risk pipeline for an asset-management firm. It turns raw positions, instrument sensitivities, historical risk-factor returns and approved limits into Power BI-ready CSV tables, and refuses to publish anything that fails its reconciliation controls.

The included sample builds **1,200 instruments observed over 65 business days (78,000 position rows)** across three funds with different mandates. No simulated risk result is intended for investment decisions.

## What changes between implementations

Normally, only change `config/config.json`:

1. Set `input.mode` to `csv` or `sql` instead of `simulate`.
2. Adjust `input.column_mapping` to match source-system fields.
3. Point to the supplied CSV locations or modify the templates in `sql/`.
4. Set the confidence level, holding period, covariance lookback, stress scenarios and the report date.

Fund limits are **not** configuration. They are an input table sourced from the limit-management system, because a limit is an approved control with an owner and an effective date, not a pipeline parameter.

The output schema is stable — column names never encode methodology parameters — so one Power BI semantic model can be reused across funds, confidence levels and reporting periods. The parameters that produced a run are published separately in `RiskMethodology.csv`.

## Quick start

```bash
cd fund_risk_powerbi_automation
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run_pipeline.py
pytest
```

The first run creates the simulated input files in `data/raw/`. Power BI must load only `outputs/published/`; every attempt — released or blocked — is retained in `outputs/archive/<run_id>/`.

## Inputs

### Multi-table SQL extraction

The sample SQL is deliberately a production-style extraction rather than a single-table query. It deduplicates end-of-day positions and latest prices/FX rates with `ROW_NUMBER()`, then joins:

- Front-office end-of-day positions
- Fund and instrument master data (**LEFT JOIN** plus a `missing_*_master_flag`, so a position is never silently dropped because reference data is late)
- Security prices and FX rates
- Instrument-level risk-factor sensitivities

The CTE output is the single **canonical position/risk snapshot** consumed by Python. See [sql/positions.sql](sql/positions.sql). In production, materialise its `position_enriched` result into an immutable, date-partitioned staging table (`risk_stage.position_enriched`); [sql/source_controls.sql](sql/source_controls.sql) then calculates control totals from that same frozen snapshot. This avoids mismatches when source tables change mid-run.

### Required position columns (canonical names)

| Column | Description |
|---|---|
| `as_of_date` | Risk snapshot date |
| `fund_id`, `fund_name`, `portfolio_id` | Organisational hierarchy |
| `instrument_id`, `asset_class`, `currency` | Position attributes |
| `market_value_usd` | Base-currency market value |

Sensitivities are a **separate** input requiring `as_of_date`, `instrument_id`, `risk_factor` and `factor_exposure_usd`, where `risk_factor` is one of `equity`, `rates`, `fx`, `credit`, `commodity`. Keeping them separate prevents the market value of a multi-factor instrument from being counted once per sensitivity.

Optional operational fields `missing_price_flag` and `missing_fx_flag` are produced by the SQL template and checked during reconciliation.

### Approved limits

`fund_limits.csv` / [sql/fund_limits.sql](sql/fund_limits.sql) supply `as_of_date`, `fund_id`, `var_limit_usd`, `stressed_var_limit_usd` and `gross_exposure_limit_usd`. The SQL template selects the effective-dated `APPROVED` row per fund. A fund that reaches the mart without a limit is flagged (`limit_missing_flag`) and **fails** the release rather than silently reporting 0% utilisation.

### Historical factor returns

`factor_returns.csv` needs `date` plus one daily return column per risk factor. The pipeline estimates a **rolling** covariance matrix for each reporting date from the trailing `risk.covariance_lookback_days` observations, so risk moves as markets move instead of being frozen at one matrix for the whole run. A reporting date with insufficient history is a hard error, not a silently shortened window.

### Source control totals

`source_controls.csv` / `sql/source_controls.sql` supply independently-calculated row counts and value totals. Each row carries `is_independent`: in `simulate` mode the controls are derived from the same data they verify, so the pipeline publishes them as `NOT_INDEPENDENT` evidence and raises a WARN rather than claiming a verified extract. With `data_quality.require_source_controls = true`, a missing controls input stops the run with an actionable message.

## SQL mode

```bash
export RISK_SQL_CONNECTION='mssql+pyodbc://user:password@server/RiskDB?driver=ODBC+Driver+18+for+SQL+Server'
```

Set `"mode": "sql"` in the configuration. The pipeline binds `:as_of_date` safely into each template. In production, use a service principal/managed identity and a secret manager rather than storing credentials in files.

## Outputs for Power BI

| File | Grain | Purpose |
|---|---|---|
| `FactRiskTotalDaily.csv` | Date | Firm-wide VaR, diversification benefit vs the sum of fund VaRs |
| `FactRiskFundDaily.csv` | Date × fund | Fund VaR, stressed VaR, gross/net exposure, three utilisations and breach flags |
| `FactRiskPortfolioDaily.csv` | Date × fund × portfolio | Portfolio drill-down (not a limit decision table) |
| `FactRiskLimit.csv` | Date × fund | Approved VaR, stressed-VaR and gross-exposure limits |
| `FactRiskFactor.csv` | Date × fund × portfolio × factor | Risk-factor drill-down |
| `FactStressTest.csv` | Date × fund × portfolio × scenario | Scenario loss analysis |
| `DimFund.csv`, `DimDate.csv` | Dimension | Star-schema filters |
| `RiskMethodology.csv` | Metadata | Method, confidence level, holding period, lookback, stress parameters for the run |
| `DataQualityExceptions.csv` | Exception | Quarantined rows, sliceable by fund and portfolio |
| `ReconciliationChecks.csv` | Control | SQL → Python → Power BI release controls |

`FactRiskTotalDaily` exists because **VaR is not additive**. Summing fund VaR overstates firm risk by the diversification benefit — 32% in the sample data — so the firm-wide number is precomputed rather than left to a DAX `SUM`. The `VaR` measure in [powerbi/DAX_measures.md](powerbi/DAX_measures.md) is scope-aware: it returns the fund figure for a single fund, the precomputed firm figure when all funds are selected, and blanks for an arbitrary subset it cannot legitimately aggregate.

### Loading in Power BI Desktop

Use **Get Data → Folder** on `outputs/published/` but **do not use Combine Files** — the folder holds eleven different schemas, and combining them produces one meaningless wide table. Create a separate query per CSV. Then:

1. In `DimDate`, set `date` to type **Date** and use **Table tools → Mark as date table** on it, otherwise no DAX time intelligence will work.
2. Set `as_of_date` to type Date on every fact table.
3. Create the relationships documented in [powerbi/DAX_measures.md](powerbi/DAX_measures.md) — single-direction, dimension filters fact. `FactRiskTotalDaily` intentionally has no `fund_id` and therefore no `DimFund` relationship.

Never load `archive/`. For a production model, use a SQL risk-mart destination instead of CSV and configure incremental refresh on `as_of_date`.

## Automation schedule

```bash
python run_pipeline.py config/config.json
```

Use Azure Data Factory, Airflow, Control-M, a GitHub Actions runner or Windows Task Scheduler. Schedule it after trading, pricing and reference-data loads complete. Then trigger the Power BI semantic-model refresh with a service principal and the Power BI REST API. Persist run logs and alert on failures or `breach_flag = TRUE`.

## Data-quality and reconciliation gates

Bad input data is **quarantined and reported**, not raised on. A row with a null market value, an unknown risk factor or a duplicate key is excluded from the mart and written to `DataQualityExceptions.csv` with its fund and portfolio attached, so operations can see which book is affected. The run then fails the release only if quarantine exceeds `data_quality.max_quarantine_pct`.

Evidence is written to the archive **before** the release gate, so a blocked run still leaves a complete audit trail of why it was blocked.

| Control layer | Evidence checked |
|---|---|
| SQL → Python | Row count, gross and net market value, missing-price and missing-FX counts against `source_controls.sql`; raw-vs-enriched row count to catch rows lost in a join; control independence. A control that is **absent** fails — it does not default to pass |
| Python | Duplicate position and sensitivity keys, positions with no sensitivity, position completeness, quarantine rate, funds without limits |
| Python → Power BI | Fund gross exposure and total-vs-fund grain consistency reconcile to the staging data; VaR sub-additivity (firm VaR ≤ sum of fund VaR) |
| Power BI | Load `ReconciliationChecks.csv`; use the `Risk Data Release Status` DAX measure as a visible report banner and alert condition. It also detects a mixed-run load |

Publishing replaces the Power BI ingress directory **as a whole** via an atomic rename, then moves the `outputs/RELEASE_READY.json` pointer. Copying file by file can leave a refresh reading two runs at once and never removes tables from a retired schema. The release marker is deleted at the start of every run, so a failed run cannot leave stale output looking released.

A report is considered released only when the pipeline succeeds, `failed_reconciliations = 0`, and the Power BI refresh confirms the same `run_id` as `run_manifest.json`.

## Risk-method notes

- Variance-covariance (parametric) VaR: `z × √(eᵀΣe) × √horizon`, with Σ estimated from the rolling lookback window.
- Stressed VaR scales the **1-day** sigma to `risk.stress_horizon_days` and applies a transparent volatility multiplier. Replace it with your approved stressed-window methodology.
- Stress P&L is factor exposure × configured factor shock, and is additive across funds (unlike VaR).
- Production use requires independent validation, approved methodologies, correct sensitivities, FX conversion, back-testing and governance controls.

## Tests

`pytest` covers the risk engine and the control framework: closed-form VaR, √time scaling, offsetting exposures, covariance-window boundaries, quarantine behaviour, all three breach flags, missing limits, sub-additivity, and every reconciliation failure mode.
