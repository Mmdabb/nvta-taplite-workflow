from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


PERIODS = {
    "am": (6.0, 9.0),
    "md": (9.0, 15.0),
    "pm": (15.0, 19.0),
}


def _resource_frame(path: Path) -> pd.DataFrame:
    records = np.load(path, mmap_mode="r", allow_pickle=False)
    return pd.DataFrame.from_records(records)


def audit_assignment(assignment_dir: Path, resource_dir: Path) -> tuple[pd.DataFrame, dict]:
    t2 = _resource_frame(resource_dir / "observed_link_t2_lookup" / "observed_link_t2.npy")
    speed = _resource_frame(
        resource_dir
        / "observed_link_speed_boundary_lookup"
        / "observed_link_speed_boundaries.npy"
    )
    keys = ["packed_key", "from_node_id", "to_node_id"]
    resources = t2.merge(speed, on=keys, how="outer", validate="one_to_one")
    audit_parts: list[pd.DataFrame] = []
    period_summary: dict[str, dict[str, int]] = {}

    for period, (period_start, period_end) in PERIODS.items():
        link_path = assignment_dir / period / "link.csv"
        link = pd.read_csv(link_path, low_memory=False)
        link["packed_key"] = (
            link["from_node_id"].astype("uint64") << np.uint64(32)
        ) | link["to_node_id"].astype("uint64")
        matched = resources.merge(
            link,
            on=["packed_key", "from_node_id", "to_node_id"],
            how="left",
            indicator=True,
        )
        expected_boundaries = {
            boundary: pd.to_numeric(
                matched[f"observed_{boundary}_hour_{period}"], errors="coerce"
            )
            for boundary in ("t0", "t2", "t3")
        }
        expected_t2 = expected_boundaries["t2"]
        start_expected = pd.to_numeric(
            matched[f"qvdf_start_speed_mph_{period}"], errors="coerce"
        )
        end_expected = pd.to_numeric(
            matched[f"qvdf_end_speed_mph_{period}"], errors="coerce"
        )
        t0 = pd.to_numeric(matched.get("t0_hour"), errors="coerce")
        t2_actual = pd.to_numeric(matched.get("t2_hour"), errors="coerce")
        t3 = pd.to_numeric(matched.get("t3_hour"), errors="coerce")
        start_actual = pd.to_numeric(
            matched.get("qvdf_start_speed_mph"), errors="coerce"
        )
        end_actual = pd.to_numeric(
            matched.get("qvdf_end_speed_mph"), errors="coerce"
        )

        has_congestion = expected_t2.notna()
        all_boundaries = t0.notna() & t2_actual.notna() & t3.notna()
        any_boundaries = t0.notna() | t2_actual.notna() | t3.notna()
        matched_link = matched["_merge"].eq("both")
        ordered = (
            t0.ge(0.0)
            & t0.lt(t2_actual)
            & t2_actual.lt(t3)
            & t3.le(24.0)
            & t2_actual.ge(period_start)
            & t2_actual.lt(period_end)
        )
        boundary_matches = {
            "t0": np.isclose(t0, expected_boundaries["t0"], atol=1e-4, equal_nan=False),
            "t2": np.isclose(t2_actual, expected_boundaries["t2"], atol=1e-4, equal_nan=False),
            "t3": np.isclose(t3, expected_boundaries["t3"], atol=1e-4, equal_nan=False),
        }
        start_matches = np.isclose(
            start_actual, start_expected, atol=1e-4, equal_nan=False
        )
        end_matches = np.isclose(end_actual, end_expected, atol=1e-4, equal_nan=False)

        matched["period"] = period.upper()
        matched["expected_congestion"] = has_congestion
        matched["missing_converted_link"] = ~matched_link
        matched["missing_required_boundaries"] = has_congestion & ~all_boundaries
        matched["invalid_boundary_order_or_range"] = has_congestion & all_boundaries & ~ordered
        matched["observed_boundary_mismatch"] = (
            has_congestion
            & all_boundaries
            & ~(boundary_matches["t0"] & boundary_matches["t2"] & boundary_matches["t3"])
        )
        matched["unexpected_boundaries_without_congestion"] = ~has_congestion & any_boundaries
        matched["missing_or_mismatched_start_speed"] = ~start_matches
        matched["missing_or_mismatched_end_speed"] = ~end_matches
        violation_fields = [
            "missing_converted_link",
            "missing_required_boundaries",
            "invalid_boundary_order_or_range",
            "observed_boundary_mismatch",
            "unexpected_boundaries_without_congestion",
            "missing_or_mismatched_start_speed",
            "missing_or_mismatched_end_speed",
        ]
        matched["audit_pass"] = ~matched[violation_fields].any(axis=1)
        output_fields = [
            "period",
            "packed_key",
            "from_node_id",
            "to_node_id",
            "link_id",
            f"observed_t0_hour_{period}",
            f"observed_t2_hour_{period}",
            f"observed_t3_hour_{period}",
            "t0_hour",
            "t2_hour",
            "t3_hour",
            f"qvdf_start_speed_mph_{period}",
            "qvdf_start_speed_mph",
            f"qvdf_end_speed_mph_{period}",
            "qvdf_end_speed_mph",
            "expected_congestion",
            *violation_fields,
            "audit_pass",
        ]
        audit_parts.append(matched.reindex(columns=output_fields))
        period_summary[period.upper()] = {
            "tmc_matched_node_pairs": int(len(matched)),
            "expected_congestion_pairs": int(has_congestion.sum()),
            "expected_no_congestion_pairs": int((~has_congestion).sum()),
            "missing_converted_links": int((~matched_link).sum()),
            "missing_required_boundaries": int(
                matched["missing_required_boundaries"].sum()
            ),
            "invalid_boundary_order_or_range": int(
                matched["invalid_boundary_order_or_range"].sum()
            ),
            "observed_boundary_mismatch": int(
                matched["observed_boundary_mismatch"].sum()
            ),
            "unexpected_boundaries_without_congestion": int(
                matched["unexpected_boundaries_without_congestion"].sum()
            ),
            "missing_or_mismatched_start_speed": int(
                matched["missing_or_mismatched_start_speed"].sum()
            ),
            "missing_or_mismatched_end_speed": int(
                matched["missing_or_mismatched_end_speed"].sum()
            ),
            "passed_pairs": int(matched["audit_pass"].sum()),
        }

    audit = pd.concat(audit_parts, ignore_index=True)
    summary = {
        "assignment_dir": str(assignment_dir.resolve()),
        "resource_dir": str(resource_dir.resolve()),
        "periods": period_summary,
        "total_rows": int(len(audit)),
        "total_failures": int((~audit["audit_pass"]).sum()),
        "status": "PASS" if bool(audit["audit_pass"].all()) else "FAIL",
    }
    return audit, summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit TMC-matched QVDF boundary and speed inputs in converted link.csv files."
    )
    parser.add_argument("assignment_dir", type=Path)
    parser.add_argument(
        "--resource-dir",
        type=Path,
        default=Path(__file__).resolve().parent
        / "src"
        / "dtalite4cube"
        / "resources",
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    output_dir = args.output_dir or args.assignment_dir / "audit" / "tmc-qvdf-inputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    audit, summary = audit_assignment(args.assignment_dir, args.resource_dir)
    audit.to_csv(output_dir / "tmc_qvdf_input_audit.csv", index=False)
    failures = audit.loc[~audit["audit_pass"]]
    failures.to_csv(output_dir / "tmc_qvdf_input_failures.csv", index=False)
    (output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
