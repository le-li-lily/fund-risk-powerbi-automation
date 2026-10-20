import numpy as np
import pandas as pd
import pytest

from src.risk_engine import (
    build_risk_mart,
    covariance_by_date,
    normalise_dates,
    parametric_var,
    screen_inputs,
    validate_schema,
)

FACTORS = ["equity", "rates"]


def make_config(**risk):
    settings = {
        "var_method": "parametric", "confidence_level": 0.99, "holding_period_days": 1,
        "covariance_lookback_days": 3, "stress_horizon_days": 10, "stressed_volatility_multiplier": 2.0,
    }
    settings.update(risk)
    return {
        "run": {"as_of_date": "2026-01-08", "base_currency": "USD"},
        "risk": settings,
        "stress_scenarios": [{"scenario_id": "EQ", "scenario_name": "Equity down 10%", "equity": -0.10}],
        "data_quality": {"reconciliation_tolerance_usd": 1.0, "max_quarantine_pct": 0.5},
    }


def make_positions(rows):
    frame = pd.DataFrame(rows)
    defaults = {"fund_name": "Fund One", "asset_class": "Equity", "currency": "USD"}
    for column, value in defaults.items():
        frame[column] = frame.get(column, value)
    return frame


def make_returns(dates):
    """Equity vol 10%, rates vol 5%, zero correlation by construction."""
    pattern = np.array([[0.1, 0.05], [-0.1, -0.05], [0.1, -0.05], [-0.1, 0.05]])
    history = np.resize(pattern, (len(dates), 2))
    return pd.DataFrame({"date": dates, "equity": history[:, 0], "rates": history[:, 1]})


def test_parametric_var_matches_closed_form():
    covariance = np.diag([0.04, 0.01])
    var = parametric_var(np.array([1_000_000.0, 2_000_000.0]), covariance, 2.326, 1)
    # sigma = sqrt(1e6^2*0.04 + 2e6^2*0.01) = 282,842.7
    assert var == pytest.approx(2.326 * 282_842.712, rel=1e-6)


def test_parametric_var_scales_with_square_root_of_time():
    covariance = np.diag([0.04, 0.01])
    exposures = np.array([1_000_000.0, 2_000_000.0])
    one_day = parametric_var(exposures, covariance, 2.326, 1)
    ten_day = parametric_var(exposures, covariance, 2.326, 10)
    assert ten_day == pytest.approx(one_day * np.sqrt(10))


def test_offsetting_exposures_reduce_var():
    covariance = np.array([[0.04, 0.04], [0.04, 0.04]])
    assert parametric_var(np.array([1e6, -1e6]), covariance, 2.326, 1) == pytest.approx(0.0, abs=1e-6)


def test_covariance_window_rejects_short_history():
    returns = make_returns(["2026-01-05", "2026-01-06"])
    with pytest.raises(ValueError, match="covariance_lookback_days"):
        covariance_by_date(returns, FACTORS, ["2026-01-06"], 3)


def test_covariance_uses_only_returns_up_to_the_date():
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    returns = make_returns(dates)
    matrices = covariance_by_date(returns, FACTORS, ["2026-01-07", "2026-01-08"], 3)
    expected = returns.loc[returns["date"] <= "2026-01-07", FACTORS].tail(3).cov().to_numpy()
    assert np.allclose(matrices["2026-01-07"], expected)
    assert not np.allclose(matrices["2026-01-07"], matrices["2026-01-08"])


def test_validate_schema_names_the_missing_column():
    positions = make_positions([{"as_of_date": "2026-01-08", "fund_id": "F1", "portfolio_id": "P1",
                                 "instrument_id": "I1", "market_value_usd": 1.0}])
    sensitivities = pd.DataFrame(columns=["as_of_date", "instrument_id", "risk_factor", "factor_exposure_usd"])
    limits = pd.DataFrame(columns=["as_of_date", "fund_id", "var_limit_usd", "stressed_var_limit_usd"])
    with pytest.raises(ValueError, match="gross_exposure_limit_usd"):
        validate_schema(positions, sensitivities, limits)


def test_screen_inputs_quarantines_without_raising():
    positions = make_positions([
        {"as_of_date": "2026-01-08", "fund_id": "F1", "portfolio_id": "P1", "instrument_id": "OK", "market_value_usd": 100.0},
        {"as_of_date": "2026-01-08", "fund_id": "F1", "portfolio_id": "P1", "instrument_id": "NOPRICE", "market_value_usd": None},
        {"as_of_date": "2026-01-08", "fund_id": "F1", "portfolio_id": "P1", "instrument_id": "NOSENS", "market_value_usd": 100.0},
        {"as_of_date": "2026-01-08", "fund_id": "F1", "portfolio_id": "P1", "instrument_id": "BADFACTOR", "market_value_usd": 100.0},
    ])
    sensitivities = pd.DataFrame([
        {"as_of_date": "2026-01-08", "instrument_id": "OK", "risk_factor": "equity", "factor_exposure_usd": 100.0},
        {"as_of_date": "2026-01-08", "instrument_id": "NOPRICE", "risk_factor": "equity", "factor_exposure_usd": 100.0},
        {"as_of_date": "2026-01-08", "instrument_id": "BADFACTOR", "risk_factor": "crypto", "factor_exposure_usd": 100.0},
    ])

    reportable, usable_sensitivities, exceptions = screen_inputs(positions, sensitivities, FACTORS)

    assert reportable["instrument_id"].tolist() == ["OK"]
    assert usable_sensitivities["instrument_id"].tolist() == ["OK"]
    rules = set(exceptions["rule"])
    assert rules == {"null_market_value", "unmapped_risk_factor", "position_not_risk_reportable"}
    assert set(exceptions.loc[exceptions["rule"] == "position_not_risk_reportable", "instrument_id"]) == {"NOSENS", "BADFACTOR"}
    assert (exceptions["as_of_date"] == "2026-01-08").all()
    # Every exception must carry the fund so Power BI can slice it by DimFund.
    assert (exceptions["fund_id"] == "F1").all()
    assert (exceptions["portfolio_id"] == "P1").all()


def test_a_bad_sensitivity_for_an_unheld_instrument_does_not_block_the_release():
    positions = make_positions([{"as_of_date": "2026-01-08", "fund_id": "F1", "portfolio_id": "P1",
                                 "instrument_id": "HELD", "market_value_usd": 100.0}])
    sensitivities = pd.DataFrame([
        {"as_of_date": "2026-01-08", "instrument_id": "HELD", "risk_factor": "equity", "factor_exposure_usd": 100.0},
        {"as_of_date": "2026-01-08", "instrument_id": "UNHELD", "risk_factor": "crypto", "factor_exposure_usd": None},
    ])

    reportable, usable_sensitivities, exceptions = screen_inputs(positions, sensitivities, FACTORS)

    assert reportable["instrument_id"].tolist() == ["HELD"]
    assert usable_sensitivities["instrument_id"].tolist() == ["HELD"]
    assert exceptions.empty


def test_screen_inputs_quarantines_both_sides_of_a_duplicate():
    row = {"as_of_date": "2026-01-08", "fund_id": "F1", "portfolio_id": "P1", "instrument_id": "I1", "market_value_usd": 100.0}
    positions = make_positions([row, row])
    sensitivities = pd.DataFrame([
        {"as_of_date": "2026-01-08", "instrument_id": "I1", "risk_factor": "equity", "factor_exposure_usd": 100.0}])

    reportable, _, exceptions = screen_inputs(positions, sensitivities, FACTORS)

    assert reportable.empty
    assert (exceptions["rule"] == "duplicate_position_key").sum() == 2


def build_two_fund_mart(limit_overrides=None):
    dates = ["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    returns = make_returns(dates)
    rows, sensitivity_rows = [], []
    for date in dates[-2:]:
        for fund_id, exposure in [("F1", 1_000_000.0), ("F2", -800_000.0)]:
            rows.append({"as_of_date": date, "fund_id": fund_id, "fund_name": f"Fund {fund_id}",
                         "portfolio_id": f"{fund_id}-P1", "instrument_id": f"{fund_id}-I1",
                         "market_value_usd": exposure})
            sensitivity_rows.append({"as_of_date": date, "instrument_id": f"{fund_id}-I1",
                                     "risk_factor": "equity", "factor_exposure_usd": exposure})
    limits = pd.DataFrame([
        {"as_of_date": date, "fund_id": fund_id,
         "var_limit_usd": 1_000_000.0, "stressed_var_limit_usd": 1_000_000.0,
         "gross_exposure_limit_usd": 1_000_000.0}
        for date in dates[-2:] for fund_id in ["F1", "F2"]])
    if limit_overrides is not None:
        limits = limit_overrides(limits)
    return build_risk_mart(make_positions(rows), pd.DataFrame(sensitivity_rows), returns, limits, make_config())


def test_mart_publishes_every_grain_and_a_full_calendar():
    tables = build_two_fund_mart()
    assert len(tables["FactRiskTotalDaily"]) == 2
    assert len(tables["FactRiskFundDaily"]) == 4
    assert "fund_id" not in tables["FactRiskTotalDaily"].columns
    assert "fund_name" not in tables["FactRiskFundDaily"].columns
    calendar = tables["DimDate"]
    assert len(calendar) == 365
    assert calendar["is_reporting_date"].sum() == 2
    assert {"year", "quarter", "month_name", "iso_week", "is_business_day"} <= set(calendar.columns)


def test_firm_var_is_below_the_sum_of_fund_var_when_funds_offset():
    tables = build_two_fund_mart()
    total = tables["FactRiskTotalDaily"]
    assert (total["var_usd"] < total["sum_of_fund_var_usd"]).all()
    assert (total["diversification_benefit_usd"] > 0).all()


def test_all_three_limits_drive_breach_flags():
    def shrink_stressed_and_gross(limits):
        limits = limits.copy()
        limits["var_limit_usd"] = 1e12
        limits["stressed_var_limit_usd"] = 1.0
        limits["gross_exposure_limit_usd"] = 1.0
        return limits

    fund = build_two_fund_mart(shrink_stressed_and_gross)["FactRiskFundDaily"]
    assert not fund["var_breach_flag"].any()
    assert fund["stressed_var_breach_flag"].all()
    assert fund["gross_exposure_breach_flag"].all()
    assert (fund["breach_count"] == 2).all()
    assert fund["breach_flag"].all()


def test_missing_limit_is_flagged_rather_than_defaulted():
    def drop_one_fund(limits):
        return limits.loc[limits["fund_id"] != "F2"]

    fund = build_two_fund_mart(drop_one_fund)["FactRiskFundDaily"]
    assert fund.loc[fund["fund_id"] == "F2", "limit_missing_flag"].all()
    assert not fund.loc[fund["fund_id"] == "F1", "limit_missing_flag"].any()
    assert not fund.loc[fund["fund_id"] == "F2", "breach_flag"].any()


def test_stressed_var_scales_from_the_one_day_sigma_not_from_var():
    tables = build_two_fund_mart()
    fund = tables["FactRiskFundDaily"]
    expected = fund["var_usd"] * np.sqrt(10) * 2.0
    assert np.allclose(fund["stressed_var_usd"], expected)


def test_stress_pnl_is_additive_from_portfolio_to_fund():
    tables = build_two_fund_mart()
    stress = tables["FactStressTest"]
    f1 = stress.loc[(stress["fund_id"] == "F1") & (stress["as_of_date"] == "2026-01-08"), "stress_pnl_usd"].sum()
    assert f1 == pytest.approx(1_000_000.0 * -0.10)
    assert set(stress.columns) >= {"as_of_date", "fund_id", "portfolio_id", "scenario_id", "stress_pnl_usd"}


def test_methodology_is_published_instead_of_encoded_in_column_names():
    methodology = build_two_fund_mart()["RiskMethodology"].iloc[0]
    assert methodology["confidence_level"] == 0.99
    assert methodology["stress_horizon_days"] == 10
    assert "99.0%" in methodology["var_label"]


@pytest.mark.parametrize("values", [
    ["2026-01-08", "2026-01-09"],
    ["08-Jan-2026", "09-Jan-2026"],
    pd.to_datetime(["2026-01-08", "2026-01-09"]),
])
def test_normalise_dates_produces_iso_strings(values):
    assert normalise_dates(pd.DataFrame({"as_of_date": values}))["as_of_date"].tolist() == ["2026-01-08", "2026-01-09"]
