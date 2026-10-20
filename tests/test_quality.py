import pandas as pd
import pytest

from src.quality import control_totals, reconciliation_checks, simulated_source_controls

CONFIG = {
    "run": {"as_of_date": "2026-01-08"},
    "data_quality": {"reconciliation_tolerance_usd": 1.0, "max_quarantine_pct": 0.10},
}


def make_positions(count=10, value=100.0):
    return pd.DataFrame({
        "as_of_date": ["2026-01-08"] * count,
        "fund_id": ["F1"] * count,
        "instrument_id": [f"I{index}" for index in range(count)],
        "market_value_usd": [value] * count,
        "missing_price_flag": [0] * count,
        "missing_fx_flag": [0] * count,
    })


def make_tables(positions, diversification_benefit=5.0):
    gross = positions["market_value_usd"].abs().sum()
    return {
        "FactRiskFundDaily": pd.DataFrame([{
            "as_of_date": "2026-01-08", "fund_id": "F1", "var_usd": 10.0,
            "gross_exposure_usd": gross, "limit_missing_flag": False}]),
        "FactRiskTotalDaily": pd.DataFrame([{
            "as_of_date": "2026-01-08", "var_usd": 10.0, "gross_exposure_usd": gross,
            "sum_of_fund_var_usd": 10.0 + diversification_benefit,
            "diversification_benefit_usd": diversification_benefit}]),
    }


def run_checks(positions, reportable=None, exceptions=None, controls=None, tables=None):
    reportable = positions if reportable is None else reportable
    exceptions = pd.DataFrame(columns=["rule"]) if exceptions is None else exceptions
    controls = simulated_source_controls(positions) if controls is None else controls
    tables = make_tables(reportable) if tables is None else tables
    checks = reconciliation_checks(positions, reportable, exceptions, controls, tables, CONFIG)
    return checks.set_index("check_name")


def test_a_missing_source_control_fails_instead_of_passing():
    positions = make_positions()
    controls = simulated_source_controls(positions)
    partial = controls.loc[controls["control_name"] != "SOURCE_GROSS_MARKET_VALUE_USD"]

    checks = run_checks(positions, controls=partial)

    assert checks.loc["SOURCE_GROSS_MARKET_VALUE_USD", "status"] == "FAIL"
    assert checks.loc["SOURCE_ROW_COUNT", "status"] == "PASS"


def test_no_source_control_at_all_fails_every_control():
    checks = run_checks(make_positions(), controls=pd.DataFrame(columns=["control_name", "control_value"]))
    sql_layer = checks.loc[checks["check_layer"] == "SQL_TO_PYTHON"]
    assert (sql_layer["status"] == "FAIL").all()


def test_control_disagreement_beyond_tolerance_fails():
    positions = make_positions()
    controls = simulated_source_controls(positions).copy()
    controls.loc[controls["control_name"] == "SOURCE_GROSS_MARKET_VALUE_USD", "control_value"] += 100.0

    checks = run_checks(positions, controls=controls)

    assert checks.loc["SOURCE_GROSS_MARKET_VALUE_USD", "status"] == "FAIL"
    assert checks.loc["SOURCE_GROSS_MARKET_VALUE_USD", "difference"] == pytest.approx(-100.0)


def test_simulated_controls_are_reported_as_not_independent():
    checks = run_checks(make_positions())
    assert checks.loc["SOURCE_CONTROL_INDEPENDENCE", "status"] == "WARN"
    assert (checks.loc[checks["check_layer"] == "SQL_TO_PYTHON", "evidence_quality"] == "NOT_INDEPENDENT").all()


def test_independent_controls_pass_the_independence_check():
    positions = make_positions()
    controls = simulated_source_controls(positions).assign(is_independent=True)
    checks = run_checks(positions, controls=controls)
    assert checks.loc["SOURCE_CONTROL_INDEPENDENCE", "status"] == "PASS"


def test_join_completeness_warns_without_a_raw_position_count():
    checks = run_checks(make_positions())
    assert checks.loc["SOURCE_JOIN_COMPLETENESS", "status"] == "WARN"


def test_join_completeness_fails_when_rows_are_lost_in_a_join():
    positions = make_positions(count=10)
    controls = pd.concat([
        simulated_source_controls(positions),
        pd.DataFrame([{"control_name": "SOURCE_RAW_POSITION_ROW_COUNT", "control_value": 12.0, "is_independent": False}]),
    ], ignore_index=True)

    checks = run_checks(positions, controls=controls)

    assert checks.loc["SOURCE_JOIN_COMPLETENESS", "status"] == "FAIL"
    assert checks.loc["SOURCE_JOIN_COMPLETENESS", "difference"] == pytest.approx(-2.0)


def test_quarantine_within_tolerance_passes_but_is_still_counted():
    positions = make_positions(count=100)
    reportable = positions.iloc[:95]
    checks = run_checks(positions, reportable=reportable)
    assert checks.loc["QUARANTINE_RATE", "status"] == "PASS"
    assert checks.loc["QUARANTINE_RATE", "target_value"] == pytest.approx(0.05)
    assert checks.loc["POSITION_COMPLETENESS", "status"] == "PASS"


def test_quarantine_beyond_tolerance_fails():
    positions = make_positions(count=100)
    checks = run_checks(positions, reportable=positions.iloc[:80])
    assert checks.loc["QUARANTINE_RATE", "status"] == "FAIL"


def test_positions_without_sensitivity_always_fail():
    positions = make_positions(count=10)
    exceptions = pd.DataFrame([{"rule": "position_not_risk_reportable"}])
    checks = run_checks(positions, reportable=positions.iloc[:9], exceptions=exceptions)
    assert checks.loc["POSITIONS_WITHOUT_SENSITIVITY", "status"] == "FAIL"


def test_missing_limit_fails_the_release():
    positions = make_positions()
    tables = make_tables(positions)
    tables["FactRiskFundDaily"]["limit_missing_flag"] = True
    checks = run_checks(positions, tables=tables)
    assert checks.loc["FUNDS_WITHOUT_LIMITS", "status"] == "FAIL"


def test_superadditive_var_fails_subadditivity():
    positions = make_positions()
    tables = make_tables(positions, diversification_benefit=-50.0)
    checks = run_checks(positions, tables=tables)
    assert checks.loc["VAR_SUBADDITIVITY", "status"] == "FAIL"


def test_grain_inconsistency_fails():
    positions = make_positions()
    tables = make_tables(positions)
    tables["FactRiskTotalDaily"]["gross_exposure_usd"] = 1.0
    checks = run_checks(positions, tables=tables)
    assert checks.loc["POWERBI_GRAIN_CONSISTENCY", "status"] == "FAIL"


def test_control_totals_ignore_absent_operational_flags():
    positions = make_positions().drop(columns=["missing_price_flag", "missing_fx_flag"])
    totals = control_totals(positions)
    assert totals["SOURCE_MISSING_PRICE_COUNT"] == 0.0
    assert totals["SOURCE_GROSS_MARKET_VALUE_USD"] == pytest.approx(1000.0)
