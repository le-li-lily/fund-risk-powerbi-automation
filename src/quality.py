"""Layered reconciliation evidence for the SQL to Python to Power BI chain.

A control that cannot be evaluated is reported as FAIL or WARN, never PASS. Silently
defaulting a missing control to "agrees" is the failure mode that makes a release
gate useless.
"""
import pandas as pd

CHECK_COLUMNS = ["as_of_date", "check_layer", "check_name", "source_value", "target_value",
                 "difference", "tolerance", "status", "evidence_quality", "detail"]
RAW_POSITION_CONTROL = "SOURCE_RAW_POSITION_ROW_COUNT"


def control_totals(positions):
    """Control totals measured from a position frame."""
    def flag_total(name):
        column = positions[name] if name in positions else pd.Series(0, index=positions.index)
        return float(column.fillna(0).sum())

    return {
        "SOURCE_ROW_COUNT": float(len(positions)),
        "SOURCE_GROSS_MARKET_VALUE_USD": float(positions["market_value_usd"].abs().sum()),
        "SOURCE_NET_MARKET_VALUE_USD": float(positions["market_value_usd"].sum()),
        "SOURCE_MISSING_PRICE_COUNT": flag_total("missing_price_flag"),
        "SOURCE_MISSING_FX_COUNT": flag_total("missing_fx_flag"),
    }


def simulated_source_controls(positions):
    """Stand-in controls for simulate mode, marked as non-independent.

    They are derived from the same frame the pipeline reads, so they can only ever
    agree. Flagging that keeps the demo evidence from looking like a real control.
    """
    frame = pd.DataFrame(
        [{"control_name": name, "control_value": value} for name, value in control_totals(positions).items()])
    frame["is_independent"] = False
    return frame


def _row(layer, name, source, target, tolerance, status=None, detail="", evidence="INDEPENDENT"):
    difference = None if source is None or target is None else float(target) - float(source)
    if status is None:
        status = "PASS" if difference is not None and abs(difference) <= tolerance else "FAIL"
    return {
        "check_layer": layer, "check_name": name,
        "source_value": None if source is None else float(source),
        "target_value": None if target is None else float(target),
        "difference": difference, "tolerance": float(tolerance),
        "status": status, "evidence_quality": evidence, "detail": detail,
    }


def reconciliation_checks(raw_positions, reportable_positions, exceptions, source_controls, tables, config):
    quality = config["data_quality"]
    tolerance = quality["reconciliation_tolerance_usd"]
    measured = control_totals(raw_positions)
    raw_controls = {}
    independent = False
    if not source_controls.empty:
        raw_controls = source_controls.set_index("control_name")["control_value"].astype(float).to_dict()
        independent = bool(source_controls.get("is_independent", pd.Series(True, index=source_controls.index)).all())
    evidence = "INDEPENDENT" if independent else "NOT_INDEPENDENT"

    rows = [_row(
        "SOURCE", "SOURCE_CONTROL_INDEPENDENCE", None, None, 0,
        status="PASS" if independent else "WARN", evidence=evidence,
        detail="Controls come from the source system" if independent
               else "Controls were derived from the same extract the pipeline read",
    )]

    if RAW_POSITION_CONTROL in raw_controls:
        rows.append(_row(
            "SOURCE", "SOURCE_JOIN_COMPLETENESS", raw_controls[RAW_POSITION_CONTROL],
            raw_controls.get("SOURCE_ROW_COUNT"), 0, evidence=evidence,
            detail="Front-office rows versus rows surviving the reference-data joins",
        ))
    else:
        rows.append(_row(
            "SOURCE", "SOURCE_JOIN_COMPLETENESS", None, None, 0, status="WARN", evidence=evidence,
            detail=f"{RAW_POSITION_CONTROL} was not supplied, so positions dropped by a join cannot be detected",
        ))

    for name, target in measured.items():
        if name not in raw_controls:
            rows.append(_row("SQL_TO_PYTHON", name, None, target, 0, status="FAIL", evidence=evidence,
                             detail="No matching source control was supplied"))
            continue
        rows.append(_row("SQL_TO_PYTHON", name, raw_controls[name], target,
                         tolerance if "VALUE" in name else 0, evidence=evidence))

    rule_counts = exceptions["rule"].value_counts().to_dict() if not exceptions.empty else {}
    quarantined = len(raw_positions) - len(reportable_positions)
    quarantine_rate = quarantined / max(len(raw_positions), 1)
    rows.extend([
        _row("PYTHON", "DUPLICATE_POSITION_KEYS", 0, rule_counts.get("duplicate_position_key", 0), 0),
        _row("PYTHON", "DUPLICATE_SENSITIVITY_KEYS", 0, rule_counts.get("duplicate_sensitivity_key", 0), 0),
        _row("PYTHON", "POSITIONS_WITHOUT_SENSITIVITY", 0, rule_counts.get("position_not_risk_reportable", 0), 0,
             detail="A position with no approved sensitivity cannot be risk reported"),
        _row("PYTHON", "POSITION_COMPLETENESS", len(raw_positions), len(reportable_positions) + quarantined, 0,
             detail="Every extracted position is either reported or quarantined"),
        _row("PYTHON", "QUARANTINE_RATE", 0, quarantine_rate, quality["max_quarantine_pct"],
             detail="Share of extracted positions excluded from the risk result"),
        _row("PYTHON", "FUNDS_WITHOUT_LIMITS", 0, int(tables["FactRiskFundDaily"]["limit_missing_flag"].sum()), 0,
             detail="A reported fund/date with no approved limit cannot be limit monitored"),
    ])

    fund = tables["FactRiskFundDaily"]
    total = tables["FactRiskTotalDaily"]
    reportable_gross = reportable_positions["market_value_usd"].abs().sum()
    worst_benefit = float(total["diversification_benefit_usd"].min())
    rows.extend([
        _row("PYTHON_TO_POWERBI", "POWERBI_FUND_GROSS_EXPOSURE", reportable_gross,
             fund["gross_exposure_usd"].sum(), tolerance),
        _row("PYTHON_TO_POWERBI", "POWERBI_GRAIN_CONSISTENCY", fund["gross_exposure_usd"].sum(),
             total["gross_exposure_usd"].sum(), tolerance,
             detail="Fund grain and total grain describe the same book"),
        _row("PYTHON_TO_POWERBI", "VAR_SUBADDITIVITY", 0, worst_benefit, tolerance,
             status="PASS" if worst_benefit >= -tolerance else "FAIL",
             detail="Firm VaR must not exceed the sum of fund VaR"),
    ])

    checks = pd.DataFrame(rows)
    checks.insert(0, "as_of_date", config["run"]["as_of_date"])
    return checks[CHECK_COLUMNS]
