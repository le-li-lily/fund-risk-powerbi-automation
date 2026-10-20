import os
from pathlib import Path

import pandas as pd

CONTROL_COLUMNS = ["control_name", "control_value"]


def _rename_to_canonical(frame: pd.DataFrame, mapping: dict) -> pd.DataFrame:
    reverse = {source: canonical for canonical, source in mapping.items()}
    return frame.rename(columns=reverse)


def _read_controls(path: Path) -> pd.DataFrame:
    if path is None or not path.exists():
        return pd.DataFrame(columns=CONTROL_COLUMNS)
    controls = pd.read_csv(path)
    missing = set(CONTROL_COLUMNS) - set(controls.columns)
    if missing:
        raise ValueError(f"Source control file {path} is missing columns: {sorted(missing)}")
    return controls


def load_inputs(project_root: Path, config: dict):
    """Return positions, sensitivities, factor returns, fund limits and source controls."""
    source = config["input"]
    returns = pd.read_csv(project_root / source["returns_csv"])

    if source["mode"] == "csv":
        positions = _rename_to_canonical(pd.read_csv(project_root / source["positions_csv"]), source["column_mapping"])
        sensitivities = pd.read_csv(project_root / source["sensitivities_csv"])
        limits = pd.read_csv(project_root / source["limits_csv"])
        controls = _read_controls(project_root / source["controls_csv"] if source.get("controls_csv") else None)
        return positions, sensitivities, returns, limits, controls

    if source["mode"] == "sql":
        from sqlalchemy import create_engine, text

        settings = source["sql"]
        as_of = {"as_of_date": config["run"]["as_of_date"]}
        with create_engine(os.environ[settings["connection_env"]]).connect() as connection:
            def query(key):
                statement = (project_root / settings[key]).read_text(encoding="utf-8")
                return pd.read_sql(text(statement), connection, params=as_of)

            positions = query("query_file")
            sensitivities = query("sensitivity_query_file")
            limits = query("limit_query_file")
            controls = query("control_query_file")
        return (_rename_to_canonical(positions, source["column_mapping"]),
                sensitivities, returns, limits, controls)

    raise ValueError(f"Unsupported input mode {source['mode']!r}. Use simulate, csv or sql.")
