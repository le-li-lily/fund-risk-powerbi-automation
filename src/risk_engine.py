"""Parametric (delta-normal) market risk and construction of the Power BI star schema.

Column names are deliberately independent of the configured confidence level and
horizon. The parameters that were used are published in the `RiskMethodology`
table instead, so changing them never silently invalidates a report label.
"""
from statistics import NormalDist

import numpy as np
import pandas as pd

POSITION_REQUIRED = {
    "as_of_date", "fund_id", "fund_name", "portfolio_id",
    "instrument_id", "asset_class", "currency", "market_value_usd",
}
SENSITIVITY_REQUIRED = {"as_of_date", "instrument_id", "risk_factor", "factor_exposure_usd"}
LIMIT_REQUIRED = {"as_of_date", "fund_id", "var_limit_usd", "stressed_var_limit_usd", "gross_exposure_limit_usd"}

POSITION_KEY = ["as_of_date", "fund_id", "portfolio_id", "instrument_id"]
SENSITIVITY_KEY = ["as_of_date", "instrument_id", "risk_factor"]

TOTAL_GRAIN = ["as_of_date"]
FUND_GRAIN = ["as_of_date", "fund_id"]
PORTFOLIO_GRAIN = ["as_of_date", "fund_id", "portfolio_id"]

EXCEPTION_COLUMNS = ["as_of_date", "fund_id", "portfolio_id", "instrument_id", "rule", "severity", "detail"]
LIMIT_COLUMNS = ["var_limit_usd", "stressed_var_limit_usd", "gross_exposure_limit_usd"]


def validate_schema(positions, sensitivities, limits):
    """Mapping-level check. A missing column is a configuration fault, not a data exception."""
    for label, frame, required in (
        ("position", positions, POSITION_REQUIRED),
        ("sensitivity", sensitivities, SENSITIVITY_REQUIRED),
        ("limit", limits, LIMIT_REQUIRED),
    ):
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Missing {label} columns: {sorted(missing)}")


def normalise_dates(frame, column="as_of_date"):
    normalised = frame.copy()
    normalised[column] = pd.to_datetime(normalised[column]).dt.strftime("%Y-%m-%d")
    return normalised


def parametric_var(exposures, covariance, z_score, horizon_days):
    """Delta-normal VaR for one vector of factor exposures."""
    variance = float(np.asarray(exposures) @ covariance @ np.asarray(exposures))
    return z_score * np.sqrt(max(variance, 0.0)) * np.sqrt(horizon_days)


def z_score_for(config):
    return NormalDist().inv_cdf(config["risk"]["confidence_level"])


def covariance_by_date(returns, factors, dates, lookback_days):
    """One covariance matrix per reporting date, estimated from that date's trailing window.

    A single matrix for the whole run would make every day's VaR move only with
    exposure, which hides volatility regime changes in the trend charts.
    """
    history = returns.sort_values("date")
    matrices = {}
    for date in dates:
        window = history.loc[history["date"] <= date].tail(lookback_days)
        if len(window) < lookback_days:
            raise ValueError(
                f"{date} has {len(window)} factor returns on or before it but "
                f"covariance_lookback_days is {lookback_days}. Extend the return history."
            )
        matrices[date] = window[factors].cov().to_numpy()
    return matrices


def screen_inputs(positions, sensitivities, factors):
    """Flag unreportable records and quarantine the positions they affect.

    Returns the reportable slices plus one exception row per problem. Quarantining
    instead of raising means a failed run still produces the audit evidence that
    operations need to fix the source data.

    Only sensitivities of held instruments are screened. The sensitivity table covers
    the whole instrument master, so screening all of it would block a release over rows
    that carry no risk.
    """
    exceptions = []
    holdings = positions[["as_of_date", "instrument_id", "fund_id", "portfolio_id"]].drop_duplicates()
    held = sensitivities.merge(
        holdings[["as_of_date", "instrument_id"]].drop_duplicates(),
        on=["as_of_date", "instrument_id"], how="inner",
    ).reset_index(drop=True)

    def flag_positions(mask, rule, detail, severity="ERROR"):
        mask = np.asarray(mask)
        if not mask.any():
            return
        subset = positions.loc[mask].assign(rule=rule, severity=severity, detail=detail)
        exceptions.append(subset[EXCEPTION_COLUMNS])

    def flag_sensitivities(mask, rule, detail, severity="ERROR"):
        """Resolved onto the holding positions so the exception is sliceable by fund."""
        mask = np.asarray(mask)
        if not mask.any():
            return
        subset = held.loc[mask, ["as_of_date", "instrument_id", "risk_factor"]].merge(
            holdings, on=["as_of_date", "instrument_id"], how="inner")
        subset["rule"] = rule
        subset["severity"] = severity
        subset["detail"] = subset["risk_factor"].astype(str) + ": " + detail
        exceptions.append(subset[EXCEPTION_COLUMNS])

    null_value = positions["market_value_usd"].isna()
    duplicate_position = positions.duplicated(POSITION_KEY, keep=False)
    flag_positions(null_value, "null_market_value", "market_value_usd is null, usually a missing price or FX rate")
    flag_positions(duplicate_position, "duplicate_position_key", "more than one row at the position grain")

    null_exposure = held["factor_exposure_usd"].isna()
    unmapped_factor = ~held["risk_factor"].isin(factors)
    duplicate_sensitivity = held.duplicated(SENSITIVITY_KEY, keep=False)
    flag_sensitivities(null_exposure, "null_factor_exposure", "factor_exposure_usd is null")
    flag_sensitivities(unmapped_factor, "unmapped_risk_factor", "risk factor has no return history")
    flag_sensitivities(duplicate_sensitivity, "duplicate_sensitivity_key", "more than one row at the sensitivity grain")

    usable_sensitivity = ~(null_exposure | unmapped_factor | duplicate_sensitivity)
    usable_keys = held.loc[usable_sensitivity, ["as_of_date", "instrument_id"]].drop_duplicates()
    matched = positions[["as_of_date", "instrument_id"]].merge(
        usable_keys, on=["as_of_date", "instrument_id"], how="left", indicator=True
    )
    unsensitised = (matched["_merge"] == "left_only").to_numpy()
    flag_positions(unsensitised, "position_not_risk_reportable", "no approved sensitivity for this position")

    quarantined = null_value.to_numpy() | duplicate_position.to_numpy() | unsensitised
    reportable_positions = positions.loc[~quarantined].reset_index(drop=True)
    reportable_sensitivities = held.loc[usable_sensitivity].merge(
        reportable_positions[["as_of_date", "instrument_id"]].drop_duplicates(),
        on=["as_of_date", "instrument_id"], how="inner",
    ).reset_index(drop=True)

    frame = pd.concat(exceptions, ignore_index=True) if exceptions else pd.DataFrame(columns=EXCEPTION_COLUMNS)
    return reportable_positions, reportable_sensitivities, frame[EXCEPTION_COLUMNS]


def _risk_at_grain(detail, grain, factors, covariances, z_score, risk):
    exposure = (
        detail.groupby(grain + ["risk_factor"], dropna=False)["factor_exposure_usd"].sum()
        .unstack("risk_factor").reindex(columns=factors).fillna(0.0)
    )
    rows = []
    for keys, vector in exposure.iterrows():
        keys = keys if isinstance(keys, tuple) else (keys,)
        covariance = covariances[keys[0]]
        values = vector.to_numpy(dtype=float)
        rows.append(dict(zip(grain, keys)) | {
            "var_usd": parametric_var(values, covariance, z_score, risk["holding_period_days"]),
            "stressed_var_usd": parametric_var(values, covariance, z_score, risk["stress_horizon_days"])
                                * risk["stressed_volatility_multiplier"],
        })
    return pd.DataFrame(rows)


def _exposure_at_grain(positions, grain):
    return positions.groupby(grain, as_index=False, dropna=False).agg(
        gross_exposure_usd=("market_value_usd", lambda values: values.abs().sum()),
        net_exposure_usd=("market_value_usd", "sum"),
    )


def _dim_date(dates):
    """Contiguous calendar covering whole years, so Power BI time intelligence works."""
    reporting = pd.to_datetime(pd.Index(dates))
    calendar = pd.date_range(f"{reporting.min().year}-01-01", f"{reporting.max().year}-12-31", freq="D")
    frame = pd.DataFrame({"date": calendar})
    frame["date_key"] = frame["date"].dt.strftime("%Y%m%d").astype(int)
    frame["year"] = frame["date"].dt.year
    frame["quarter"] = "Q" + frame["date"].dt.quarter.astype(str)
    frame["month"] = frame["date"].dt.month
    frame["month_name"] = frame["date"].dt.strftime("%b")
    frame["year_month"] = frame["date"].dt.strftime("%Y-%m")
    frame["iso_week"] = frame["date"].dt.isocalendar().week.astype(int)
    frame["day_name"] = frame["date"].dt.strftime("%a")
    frame["is_business_day"] = frame["date"].dt.dayofweek < 5
    frame["is_month_end"] = frame["date"].dt.is_month_end
    frame["is_reporting_date"] = frame["date"].isin(reporting)
    frame["date"] = frame["date"].dt.strftime("%Y-%m-%d")
    return frame


def _stress_results(detail, config, factors):
    base = detail.groupby(PORTFOLIO_GRAIN + ["risk_factor"], as_index=False)["factor_exposure_usd"].sum()
    frames = []
    for scenario in config["stress_scenarios"]:
        shocks = {factor: scenario.get(factor, 0.0) for factor in factors}
        part = base.copy()
        part["scenario_id"] = scenario["scenario_id"]
        part["scenario_name"] = scenario["scenario_name"]
        part["stress_pnl_usd"] = part["factor_exposure_usd"] * part["risk_factor"].map(shocks).fillna(0.0)
        frames.append(
            part.groupby(PORTFOLIO_GRAIN + ["scenario_id", "scenario_name"], as_index=False)["stress_pnl_usd"].sum()
        )
    return pd.concat(frames, ignore_index=True)


def build_risk_mart(positions, sensitivities, returns, limits, config):
    risk = config["risk"]
    factors = [column for column in returns.columns if column != "date"]
    dates = sorted(positions["as_of_date"].unique())
    covariances = covariance_by_date(returns, factors, dates, risk["covariance_lookback_days"])
    z_score = z_score_for(config)

    detail = positions[POSITION_KEY].merge(
        sensitivities[SENSITIVITY_KEY + ["factor_exposure_usd"]],
        on=["as_of_date", "instrument_id"], how="inner", validate="one_to_many",
    )

    portfolio = _risk_at_grain(detail, PORTFOLIO_GRAIN, factors, covariances, z_score, risk).merge(
        _exposure_at_grain(positions, PORTFOLIO_GRAIN), on=PORTFOLIO_GRAIN, validate="one_to_one")
    fund = _risk_at_grain(detail, FUND_GRAIN, factors, covariances, z_score, risk).merge(
        _exposure_at_grain(positions, FUND_GRAIN), on=FUND_GRAIN, validate="one_to_one")
    total = _risk_at_grain(detail, TOTAL_GRAIN, factors, covariances, z_score, risk).merge(
        _exposure_at_grain(positions, TOTAL_GRAIN), on=TOTAL_GRAIN, validate="one_to_one")

    fund = fund.merge(limits[FUND_GRAIN + LIMIT_COLUMNS], on=FUND_GRAIN, how="left", validate="one_to_one")
    fund["var_utilisation"] = fund["var_usd"] / fund["var_limit_usd"]
    fund["stressed_var_utilisation"] = fund["stressed_var_usd"] / fund["stressed_var_limit_usd"]
    fund["gross_exposure_utilisation"] = fund["gross_exposure_usd"] / fund["gross_exposure_limit_usd"]
    breaches = ["var_breach_flag", "stressed_var_breach_flag", "gross_exposure_breach_flag"]
    for flag_column, utilisation in zip(breaches, ["var_utilisation", "stressed_var_utilisation", "gross_exposure_utilisation"]):
        fund[flag_column] = fund[utilisation] > 1
    fund["breach_count"] = fund[breaches].sum(axis=1)
    fund["breach_flag"] = fund["breach_count"] > 0
    fund["limit_missing_flag"] = fund[LIMIT_COLUMNS].isna().any(axis=1)

    # Published so that the report can show, rather than hide, the fact that VaR is not additive.
    total = total.merge(
        fund.groupby("as_of_date", as_index=False).agg(sum_of_fund_var_usd=("var_usd", "sum")),
        on="as_of_date", validate="one_to_one")
    total["diversification_benefit_usd"] = total["sum_of_fund_var_usd"] - total["var_usd"]

    methodology = pd.DataFrame([{
        "as_of_date": config["run"]["as_of_date"],
        "var_method": risk["var_method"],
        "confidence_level": risk["confidence_level"],
        "holding_period_days": risk["holding_period_days"],
        "stress_horizon_days": risk["stress_horizon_days"],
        "stressed_volatility_multiplier": risk["stressed_volatility_multiplier"],
        "covariance_lookback_days": risk["covariance_lookback_days"],
        "base_currency": config["run"]["base_currency"],
        "var_label": f"{risk['confidence_level']:.1%} / {risk['holding_period_days']}-day {risk['var_method']} VaR",
    }])

    return {
        "FactRiskTotalDaily": total,
        "FactRiskFundDaily": fund,
        "FactRiskPortfolioDaily": portfolio,
        "FactRiskLimit": limits[FUND_GRAIN + LIMIT_COLUMNS].copy(),
        "FactRiskFactor": detail.groupby(PORTFOLIO_GRAIN + ["risk_factor"], as_index=False)["factor_exposure_usd"].sum(),
        "FactStressTest": _stress_results(detail, config, factors),
        "DimFund": positions[["fund_id", "fund_name"]].drop_duplicates().sort_values("fund_id", ignore_index=True),
        "DimDate": _dim_date(dates),
        "RiskMethodology": methodology,
    }
