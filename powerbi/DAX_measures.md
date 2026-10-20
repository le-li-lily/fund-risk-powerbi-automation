# Power BI semantic model

## Model relationships

Mark `DimDate` as the date table on `DimDate[date]`, and set `date` and every fact
`as_of_date` to the Date type in Power Query. The CSVs carry ISO strings, which Power BI
imports as text by default, and a text date key disables all time intelligence.

| From | To | On |
|---|---|---|
| `DimDate[date]` (1) | `FactRiskTotalDaily`, `FactRiskFundDaily`, `FactRiskPortfolioDaily`, `FactRiskLimit`, `FactRiskFactor`, `FactStressTest`, `DataQualityExceptions`, `ReconciliationChecks` | `as_of_date` |
| `DimFund[fund_id]` (1) | `FactRiskFundDaily`, `FactRiskPortfolioDaily`, `FactRiskLimit`, `FactRiskFactor`, `FactStressTest`, `DataQualityExceptions` | `fund_id` |

`FactRiskTotalDaily` has no `fund_id`: it is the firm-wide grain and must stay outside the
fund relationship so a fund slicer cannot filter it into a wrong number.
`ReconciliationChecks` and `RiskMethodology` have no `fund_id` either, so relate them on
date only. Keep all relationships single-direction from dimension to fact.

## Exposure, limits and breaches

Exposure and stress P&L are additive, so `SUM` is correct for them at every grain.

```DAX
Gross Exposure = SUM(FactRiskFundDaily[gross_exposure_usd])

Net Exposure = SUM(FactRiskFundDaily[net_exposure_usd])

Stress P&L = SUM(FactStressTest[stress_pnl_usd])

Worst Scenario Loss = MINX(VALUES(FactStressTest[scenario_id]), [Stress P&L])

VaR Limit = SUM(FactRiskLimit[var_limit_usd])

Stressed VaR Limit = SUM(FactRiskLimit[stressed_var_limit_usd])

Gross Exposure Limit = SUM(FactRiskLimit[gross_exposure_limit_usd])
```

Utilisation is already calculated per fund in the mart. Aggregate it with `MAX`, not by
dividing summed numerators by summed denominators, because the ratio of the totals is not
the utilisation of any actual limit.

```DAX
Peak VaR Utilisation = MAX(FactRiskFundDaily[var_utilisation])

Peak Stressed VaR Utilisation = MAX(FactRiskFundDaily[stressed_var_utilisation])

Peak Gross Exposure Utilisation = MAX(FactRiskFundDaily[gross_exposure_utilisation])

Limit Breaches = SUM(FactRiskFundDaily[breach_count])

VaR Breaches = CALCULATE(COUNTROWS(FactRiskFundDaily), FactRiskFundDaily[var_breach_flag] = TRUE())

Stressed VaR Breaches = CALCULATE(COUNTROWS(FactRiskFundDaily), FactRiskFundDaily[stressed_var_breach_flag] = TRUE())

Gross Exposure Breaches = CALCULATE(COUNTROWS(FactRiskFundDaily), FactRiskFundDaily[gross_exposure_breach_flag] = TRUE())

Funds Without Limits = CALCULATE(COUNTROWS(FactRiskFundDaily), FactRiskFundDaily[limit_missing_flag] = TRUE())
```

## VaR: never aggregate it with SUM

VaR is sub-additive. Adding fund VaR together assumes every fund is perfectly correlated
and overstates risk. The pipeline therefore publishes VaR precalculated at the total, fund
and portfolio grains, and the measure below picks the grain that the current selection
actually has a correct number for.

```DAX
Firm VaR = SUM(FactRiskTotalDaily[var_usd])

Firm Stressed VaR = SUM(FactRiskTotalDaily[stressed_var_usd])

Funds In Scope = DISTINCTCOUNT(FactRiskFundDaily[fund_id])

Funds In Model = CALCULATE(DISTINCTCOUNT(FactRiskFundDaily[fund_id]), REMOVEFILTERS(DimFund))

VaR =
SWITCH(
    TRUE(),
    [Funds In Scope] = 1, SUM(FactRiskFundDaily[var_usd]),
    [Funds In Scope] = [Funds In Model], [Firm VaR],
    BLANK()
)

Stressed VaR =
SWITCH(
    TRUE(),
    [Funds In Scope] = 1, SUM(FactRiskFundDaily[stressed_var_usd]),
    [Funds In Scope] = [Funds In Model], [Firm Stressed VaR],
    BLANK()
)

VaR Scope Note =
IF(
    ISBLANK([VaR]) && [Funds In Scope] > 0,
    "VaR is not additive. Select one fund, or all funds, to see a calculated VaR.",
    BLANK()
)
```

Put `VaR Scope Note` in a card next to every VaR visual. A blank VaR with no explanation
looks like missing data, which invites someone to "fix" it with a `SUM`.

Make the diversification effect visible rather than leaving it implicit:

```DAX
Sum Of Fund VaR (not a VaR) = SUM(FactRiskTotalDaily[sum_of_fund_var_usd])

Diversification Benefit = SUM(FactRiskTotalDaily[diversification_benefit_usd])
```

## Release status banner

```DAX
Failed Reconciliation Checks = CALCULATE(COUNTROWS(ReconciliationChecks), ReconciliationChecks[status] = "FAIL")

Warned Reconciliation Checks = CALCULATE(COUNTROWS(ReconciliationChecks), ReconciliationChecks[status] = "WARN")

Data Quality Exceptions = COUNTROWS(DataQualityExceptions)

Run Ids Loaded =
COUNTROWS(
    DISTINCT(
        UNION(
            VALUES(FactRiskTotalDaily[run_id]),
            VALUES(FactRiskFundDaily[run_id]),
            VALUES(FactRiskLimit[run_id]),
            VALUES(ReconciliationChecks[run_id])
        )
    )
)

Risk Data Release Status =
SWITCH(
    TRUE(),
    [Run Ids Loaded] > 1, "MIXED RUNS - DO NOT RELEASE",
    [Failed Reconciliation Checks] > 0, "DO NOT RELEASE",
    [Warned Reconciliation Checks] > 0, "RECONCILED WITH WARNINGS",
    "RECONCILED"
)
```

`Run Ids Loaded` must be evaluated with no date filter to be meaningful, so pin it to a
card outside the report's date slicer. More than one `run_id` means the refresh caught a
partially replaced folder and the numbers on screen mix two runs.

## Method labels

Read the VaR basis from the data instead of typing it into a title, so the label cannot
drift from the configuration that produced the numbers.

```DAX
VaR Basis = SELECTEDVALUE(RiskMethodology[var_label])

Stress Horizon = SELECTEDVALUE(RiskMethodology[stress_horizon_days]) & "-day"
```

Format currency measures in USD and every utilisation measure as a percentage.
