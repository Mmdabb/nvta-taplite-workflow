"""Stage existing assignment outputs and verify every exported link/period total."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from nvta_taplite_workflow.postprocessing.runner import preprocess_and_summarize_scenario
from nvta_taplite_workflow.postprocessing.pipeline.time_dependent_speeds import minute_is_in_period


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        raise ValueError("Use a separate validation output directory.")
    if args.output.exists():
        raise FileExistsError("Use a new validation output directory.")
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    periods = ["am", "md", "pm", "nt"]
    ranges = ["0600_0900", "0900_1500", "1500_1900", "1900_0600"]
    before = {}
    start = time.perf_counter()
    for period in periods:
        (args.output / period).mkdir(parents=True)
        for filename in ["link.csv", "link_performance.csv"]:
            source = args.source / period / filename
            before[f"{period}/{filename}"] = digest(source)
            shutil.copy2(source, args.output / period / filename)
    preprocess_and_summarize_scenario(
        scenario_dir=args.output, time_periods=periods, period_range_list=ranges,
        time_duration_dict={"am": 3, "md": 6, "pm": 4, "nt": 11},
    )
    results = {"source": str(args.source), "output": str(args.output), "scopes": {}}
    for name in ["link_speed_volume", "link_speed_volume_regional"]:
        wide = pd.read_csv(args.output / f"{name}.csv", low_memory=False)
        audit = pd.read_csv(args.output / f"{name}_audit.csv", low_memory=False)
        assert not wide.duplicated(["from_node_id", "to_node_id"]).any()
        assert np.isfinite(wide.filter(like="approx_volume_").fillna(0)).all().all()
        assert (wide.filter(like="approx_volume_").fillna(0) >= 0).all().all()
        period_checks = []
        for period, time_range in zip(periods, ranges):
            cols = [c for c in wide if c.startswith("approx_volume_") and minute_is_in_period(
                int(c[-5:-3]) * 60 + int(c[-2:]), time_range)]
            actual = wide[["from_node_id", "to_node_id"]].copy()
            actual["actual"] = wide[cols].sum(axis=1)
            expected = audit[audit.time_period == period].merge(actual, on=["from_node_id", "to_node_id"], validate="one_to_one")
            assert np.allclose(expected.actual, expected.assigned_volume, rtol=1e-12, atol=1e-8)
            period_checks.append({
                "period": period, "links": len(expected), "intervals": len(cols),
                "assigned_total": float(expected.assigned_volume.sum()),
                "exported_total": float(expected.actual.sum()),
                "max_absolute_error": float((expected.actual - expected.assigned_volume).abs().max()),
            })
        results["scopes"][name] = {
            "unique_links": len(wide), "columns": len(wide.columns),
            "speed_columns": len([c for c in wide if c.startswith("spd_mph_")]),
            "volume_columns": len([c for c in wide if c.startswith("approx_volume_")]),
            "status_counts": audit.status.value_counts().to_dict(),
            "capacity_sources": audit.capacity_source.value_counts().to_dict(),
            "free_speed_sources": audit.free_speed_source.value_counts().to_dict(),
            "critical_speed_sources": audit.critical_speed_source.value_counts().to_dict(),
            "invalid_speed_intervals": int(audit.invalid_speed_intervals.sum()),
            "imputed_speed_intervals": int(audit.imputed_speed_intervals.sum()),
            "above_capacity_intervals": int(audit.above_capacity_intervals.sum()),
            "period_checks": period_checks,
            "period_varying_capacity_links": int((audit.groupby(["from_node_id", "to_node_id"]).capacity.nunique() > 1).sum()),
        }
        # A representative full-day row plus its period audit for the PDF example.
        representative = audit[(audit.time_period == "am") & (audit.assigned_volume > 1000)].iloc[0]
        example = wide[(wide.from_node_id == representative.from_node_id) & (wide.to_node_id == representative.to_node_id)].iloc[0]
        results["scopes"][name]["example_row"] = example.to_dict()
        results["scopes"][name]["example_audit"] = audit[(audit.from_node_id == representative.from_node_id) & (audit.to_node_id == representative.to_node_id)].to_dict(orient="records")
        wide.head(10).to_csv(args.output / f"{name}_sample.csv", index=False)
    results["source_hashes_unchanged"] = all(digest(args.source / key) == value for key, value in before.items())
    results["source_sha256"] = before
    results["elapsed_seconds"] = time.perf_counter() - start
    with (args.output / "validation_summary.json").open("w", encoding="utf-8") as stream:
        json.dump(results, stream, indent=2)
    print(json.dumps({k: v for k, v in results.items() if k != "scopes"}, indent=2), flush=True)
    for name, result in results["scopes"].items():
        print(name, {k: v for k, v in result.items() if not k.startswith("example")}, flush=True)


if __name__ == "__main__":
    main()
