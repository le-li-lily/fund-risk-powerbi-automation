"""Deterministic sample data: a stable instrument universe observed over many business days."""
import math
from pathlib import Path

import numpy as np
import pandas as pd

from .risk_engine import covariance_by_date, parametric_var, z_score_for

FACTORS = ["equity", "rates", "fx", "credit", "commodity"]
ASSET_CLASS_FACTOR = {
    "Equity": "equity",
    "Government Bond": "rates",
    "Corporate Bond": "credit",
    "FX Forward": "fx",
    "Commodity Future": "commodity",
    "Equity Option": "equity",
}
FACTOR_BETA = {"equity": 1.0, "rates": 0.65, "fx": 0.35, "credit": 0.80, "commodity": 0.70}
CURRENCIES = ["USD", "EUR", "GBP", "JPY", "CNY"]
PORTFOLIOS_PER_FUND = 4

# Each fund gets the asset mix its mandate implies. Drawing every fund from one global mix
# would make the funds statistically identical, leaving no diversification between them and
# nothing for the firm-wide VaR grain to show.
MANDATES = {
    "F001": {
        "fund_name": "Global Macro Fund", "share": 0.30, "notional_usd": 1_200_000, "long_only": False,
        "weights": {"Equity": 0.10, "Government Bond": 0.34, "Corporate Bond": 0.06,
                    "FX Forward": 0.26, "Commodity Future": 0.18, "Equity Option": 0.06},
    },
    "F002": {
        "fund_name": "Asia Equity Long Short", "share": 0.40, "notional_usd": 900_000, "long_only": False,
        "weights": {"Equity": 0.58, "Government Bond": 0.02, "Corporate Bond": 0.04,
                    "FX Forward": 0.08, "Commodity Future": 0.02, "Equity Option": 0.26},
    },
    "F003": {
        "fund_name": "Multi Asset Income Fund", "share": 0.30, "notional_usd": 1_100_000, "long_only": True,
        "weights": {"Equity": 0.18, "Government Bond": 0.30, "Corporate Bond": 0.42,
                    "FX Forward": 0.04, "Commodity Future": 0.02, "Equity Option": 0.04},
    },
}

FACTOR_VOLS = np.array([0.012, 0.004, 0.006, 0.007, 0.015])
FACTOR_CORR = np.array([
    [1.00, 0.15, 0.18, 0.35, 0.22],
    [0.15, 1.00, 0.08, 0.30, 0.05],
    [0.18, 0.08, 1.00, 0.10, 0.12],
    [0.35, 0.30, 0.10, 1.00, 0.15],
    [0.22, 0.05, 0.12, 0.15, 1.00],
])
# Utilisation each fund is calibrated to run at on a typical day. They differ because a
# limit framework reflects each mandate, and F002 is deliberately left running hot so the
# sample data exercises the breach logic rather than only the happy path.
TARGET_UTILISATION = {
    "F001": {"var": 0.58, "stressed_var": 0.62, "gross_exposure": 0.72},
    "F002": {"var": 0.90, "stressed_var": 0.92, "gross_exposure": 0.84},
    "F003": {"var": 0.66, "stressed_var": 0.70, "gross_exposure": 0.76},
}


def _instrument_universe(rng, count):
    """Each instrument keeps one fund, portfolio and asset class for the whole history."""
    asset_classes = list(ASSET_CLASS_FACTOR)
    records = []
    index = 0
    for fund_id, mandate in MANDATES.items():
        weights = [mandate["weights"][asset_class] for asset_class in asset_classes]
        for _ in range(round(count * mandate["share"])):
            index += 1
            asset_class = str(rng.choice(asset_classes, p=weights))
            primary = ASSET_CLASS_FACTOR[asset_class]
            secondary = str(rng.choice([f for f in FACTORS if f != primary])) if rng.random() < 0.28 else None
            value = float(rng.normal(0, mandate["notional_usd"]))
            records.append({
                "instrument_id": f"INS-{index:06d}",
                "fund_id": fund_id,
                "fund_name": mandate["fund_name"],
                "portfolio_id": f"{fund_id}-P{int(rng.integers(1, PORTFOLIOS_PER_FUND + 1)):02d}",
                "asset_class": asset_class,
                "currency": str(rng.choice(CURRENCIES)),
                "opening_value_usd": abs(value) if mandate["long_only"] else value,
                "primary_factor": primary,
                "secondary_factor": secondary,
            })
    return pd.DataFrame(records)


def _factor_returns(rng, as_of, days):
    covariance = np.outer(FACTOR_VOLS, FACTOR_VOLS) * FACTOR_CORR
    history = rng.multivariate_normal(np.zeros(len(FACTORS)), covariance, size=days)
    frame = pd.DataFrame(history, columns=FACTORS)
    frame.insert(0, "date", pd.bdate_range(end=as_of, periods=days).strftime("%Y-%m-%d"))
    return frame


def _positions(rng, universe, dates):
    """Each book moves with its own market first, then every position moves on its own.

    Without the common move, 1,200 independent random walks cancel out and fund-level risk
    is almost flat for the whole history, which is not what a risk trend looks like.
    """
    attributes = ["instrument_id", "fund_id", "fund_name", "portfolio_id", "asset_class", "currency"]
    value = universe["opening_value_usd"].to_numpy(dtype=float).copy()
    _, fund_index = np.unique(universe["fund_id"].to_numpy(), return_inverse=True)
    frames = []
    for date in dates:
        common = rng.normal(0.0, 0.012, size=len(MANDATES))[fund_index]
        value = value * (1.0 + common + rng.normal(0.0, 0.015, size=len(universe)))
        frame = universe[attributes].copy()
        frame.insert(0, "as_of_date", date)
        frame["market_value_usd"] = np.round(value, 2)
        frame["missing_price_flag"] = 0
        frame["missing_fx_flag"] = 0
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def _sensitivities(rng, positions, universe):
    attributes = universe.set_index("instrument_id")[["primary_factor", "secondary_factor"]]
    merged = positions[["as_of_date", "instrument_id", "market_value_usd"]].merge(
        attributes, left_on="instrument_id", right_index=True)
    frames = []
    for column in ["primary_factor", "secondary_factor"]:
        part = merged.loc[merged[column].notna()].copy()
        part["risk_factor"] = part[column]
        noise = rng.normal(1.0, 0.12, size=len(part))
        part["factor_exposure_usd"] = (
            part["market_value_usd"] * part["risk_factor"].map(FACTOR_BETA) * noise).round(2)
        frames.append(part[["as_of_date", "instrument_id", "risk_factor", "factor_exposure_usd"]])
    return pd.concat(frames, ignore_index=True).sort_values(
        ["as_of_date", "instrument_id", "risk_factor"], ignore_index=True)


def _round_up(value, significant=2):
    """Round up to a readable number of significant figures.

    A fixed rounding step cannot serve limits that span two orders of magnitude: rounding
    660,000 up to the nearest 500,000 would inflate it by half and break the calibration.
    """
    if value <= 0:
        return 0.0
    magnitude = 10 ** (math.floor(math.log10(value)) - (significant - 1))
    return float(math.ceil(value / magnitude) * magnitude)


def _measured_fund_risk(positions, sensitivities, returns, dates, config):
    risk = config["risk"]
    covariances = covariance_by_date(returns, FACTORS, dates, risk["covariance_lookback_days"])
    z_score = z_score_for(config)
    detail = positions[["as_of_date", "fund_id", "instrument_id", "market_value_usd"]].merge(
        sensitivities, on=["as_of_date", "instrument_id"])

    rows = []
    for (date, fund_id), group in detail.groupby(["as_of_date", "fund_id"]):
        exposures = (group.groupby("risk_factor")["factor_exposure_usd"].sum()
                     .reindex(FACTORS, fill_value=0.0).to_numpy(dtype=float))
        rows.append({
            "as_of_date": date, "fund_id": fund_id,
            "var": parametric_var(exposures, covariances[date], z_score, risk["holding_period_days"]),
            "stressed_var": parametric_var(exposures, covariances[date], z_score, risk["stress_horizon_days"])
                            * risk["stressed_volatility_multiplier"],
        })
    measured = pd.DataFrame(rows)
    gross = positions.groupby(["as_of_date", "fund_id"], as_index=False).agg(
        gross_exposure=("market_value_usd", lambda values: values.abs().sum()))
    return measured.merge(gross, on=["as_of_date", "fund_id"])


def _fund_limits(positions, sensitivities, returns, dates, config):
    """Approve one limit set per fund, calibrated so a typical day sits at target utilisation.

    Limits are held constant across the history, as a real limit framework would be, so
    utilisation and breaches are driven by risk moving rather than by the limit moving.
    """
    typical = _measured_fund_risk(positions, sensitivities, returns, dates, config).groupby("fund_id").median(
        numeric_only=True)
    rows = []
    for fund_id, measured in typical.iterrows():
        targets = TARGET_UTILISATION[fund_id]
        rows.append({"fund_id": fund_id} | {
            f"{measure}_limit_usd": _round_up(measured[measure] / target)
            for measure, target in targets.items()
        })
    limits = pd.DataFrame(rows)
    return pd.concat([limits.assign(as_of_date=date) for date in dates], ignore_index=True)[
        ["as_of_date", "fund_id", "var_limit_usd", "stressed_var_limit_usd", "gross_exposure_limit_usd"]]


def create_sample_data(target: Path, config: dict):
    target.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(config["run"]["random_seed"])
    simulation = config["simulation"]
    as_of = pd.Timestamp(config["run"]["as_of_date"])

    universe = _instrument_universe(rng, simulation["instrument_count"])
    returns = _factor_returns(rng, as_of, simulation["returns_history_days"])
    dates = list(pd.bdate_range(end=as_of, periods=simulation["reporting_days"]).strftime("%Y-%m-%d"))
    positions = _positions(rng, universe, dates)
    sensitivities = _sensitivities(rng, positions, universe)
    limits = _fund_limits(positions, sensitivities, returns, dates, config)

    positions.to_csv(target / "positions.csv", index=False)
    sensitivities.to_csv(target / "instrument_sensitivities.csv", index=False)
    returns.to_csv(target / "factor_returns.csv", index=False)
    limits.to_csv(target / "fund_limits.csv", index=False)
    return positions, sensitivities, returns, limits
