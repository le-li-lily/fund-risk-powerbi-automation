"""Run: python run_pipeline.py [config/config.json]"""
import sys
from pathlib import Path

from src.config import load_config
from src.extract import load_inputs
from src.output import publish, start_run, write_evidence, write_manifest, write_tables
from src.quality import reconciliation_checks, simulated_source_controls
from src.risk_engine import build_risk_mart, normalise_dates, screen_inputs, validate_schema
from src.simulation import create_sample_data


def _collect_inputs(root: Path, config: dict):
    if config["input"]["mode"] == "simulate":
        positions, sensitivities, returns, limits = create_sample_data(root / "data/raw", config)
        return positions, sensitivities, returns, limits, simulated_source_controls(positions)

    positions, sensitivities, returns, limits, controls = load_inputs(root, config)
    if controls.empty and config["data_quality"]["require_source_controls"]:
        raise RuntimeError(
            "No source controls were supplied. Point input.controls_csv at the control totals "
            "produced by the source system, or set data_quality.require_source_controls to false "
            "to accept an unverified extract."
        )
    return positions, sensitivities, returns, limits, controls


def main() -> None:
    root = Path(__file__).resolve().parent
    config = load_config(root / (sys.argv[1] if len(sys.argv) > 1 else "config/config.json"))
    run_id, archive = start_run(root, config)

    positions, sensitivities, returns, limits, source_controls = _collect_inputs(root, config)
    validate_schema(positions, sensitivities, limits)
    positions, sensitivities, limits = (normalise_dates(frame) for frame in (positions, sensitivities, limits))
    returns = normalise_dates(returns, "date")

    factors = [column for column in returns.columns if column != "date"]
    reportable_positions, reportable_sensitivities, exceptions = screen_inputs(positions, sensitivities, factors)
    write_evidence(archive, run_id, exceptions)
    if reportable_positions.empty:
        raise RuntimeError(f"No position is risk reportable. Exceptions: {archive / 'DataQualityExceptions.csv'}")

    tables = build_risk_mart(reportable_positions, reportable_sensitivities, returns, limits, config)
    checks = reconciliation_checks(positions, reportable_positions, exceptions, source_controls, tables, config)
    write_evidence(archive, run_id, exceptions, checks)
    write_tables(archive, run_id, tables)
    manifest = write_manifest(archive, run_id, tables, exceptions, checks, config)

    failures = checks.loc[checks["status"] == "FAIL", "check_name"].tolist()
    if failures and config["data_quality"]["fail_on_error"]:
        raise RuntimeError(f"Reconciliation failed, nothing published. Checks: {failures}. Evidence: {archive}")

    published = publish(root, archive, manifest, config)
    breaches = int(tables["FactRiskFundDaily"]["breach_count"].sum())
    print(f"Released {run_id} for {config['run']['as_of_date']}: "
          f"{len(reportable_positions):,} reportable positions, {len(exceptions):,} exceptions, "
          f"{manifest['warned_reconciliations']} warnings, {breaches} limit breaches -> {published}")


if __name__ == "__main__":
    main()
