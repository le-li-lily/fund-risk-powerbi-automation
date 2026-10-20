"""Run lifecycle: archive every attempt, publish only a complete and reconciled set."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pandas as pd


def start_run(project_root: Path, config: dict):
    """Drop the stale release marker first, so a failed run cannot look released."""
    root = project_root / config["output"]["directory"]
    marker = root / "RELEASE_READY.json"
    if marker.exists():
        marker.unlink()
    run_id = str(uuid4())
    archive = root / config["output"]["archive_directory"] / run_id
    archive.mkdir(parents=True, exist_ok=False)
    return run_id, archive


def _stamp(frame: pd.DataFrame, run_id: str) -> pd.DataFrame:
    stamped = frame.copy()
    stamped.insert(0, "run_id", run_id)
    return stamped


def write_evidence(archive: Path, run_id: str, exceptions: pd.DataFrame, checks: pd.DataFrame | None = None) -> None:
    """Written before any gate, so a blocked release still leaves an audit trail."""
    _stamp(exceptions, run_id).to_csv(archive / "DataQualityExceptions.csv", index=False)
    if checks is not None:
        _stamp(checks, run_id).to_csv(archive / "ReconciliationChecks.csv", index=False)


def write_tables(archive: Path, run_id: str, tables: dict[str, pd.DataFrame]) -> None:
    for name, table in tables.items():
        _stamp(table, run_id).to_csv(archive / f"{name}.csv", index=False)


def write_manifest(archive: Path, run_id: str, tables: dict[str, pd.DataFrame],
                   exceptions: pd.DataFrame, checks: pd.DataFrame, config: dict) -> dict:
    manifest = {
        "run_id": run_id,
        "as_of_date": config["run"]["as_of_date"],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "tables": {name: len(table) for name, table in tables.items()},
        "data_quality_exceptions": len(exceptions),
        "failed_reconciliations": int((checks["status"] == "FAIL").sum()),
        "warned_reconciliations": int((checks["status"] == "WARN").sum()),
    }
    (archive / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def publish(project_root: Path, archive: Path, manifest: dict, config: dict) -> Path:
    """Replace the Power BI ingress folder as a whole, then move the release pointer.

    Copying file by file can leave a refresh reading two different runs at once, and
    never removes tables from a retired schema. Swapping the directory avoids both.
    """
    root = project_root / config["output"]["directory"]
    published = root / config["output"]["published_directory"]
    staging = root / f".publish-{manifest['run_id']}"
    retired = root / f".retired-{manifest['run_id']}"
    for path in (staging, retired):
        if path.exists():
            shutil.rmtree(path)

    shutil.copytree(archive, staging)
    if published.exists():
        os.rename(published, retired)
    os.rename(staging, published)
    shutil.rmtree(retired, ignore_errors=True)

    temp_marker = root / "RELEASE_READY.tmp"
    temp_marker.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    os.replace(temp_marker, root / "RELEASE_READY.json")
    return published
