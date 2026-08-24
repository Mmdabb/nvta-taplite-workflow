from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import numpy as np

from ..resources.congestion_t_node_pair_lookup.load_node_pair_boundaries import (
    BOUNDARY_FIELDS as LOOKUP_BOUNDARY_FIELDS,
    load_period,
    lookup,
    period_path,
)
from ..resources.observed_link_t2_lookup.load_observed_link_t2 import (
    BOUNDARY_FIELDS as OBSERVED_BOUNDARY_FIELDS,
    load_lookup as load_observed_t2_lookup,
    lookup as lookup_observed_t2,
    lookup_path as observed_t2_lookup_path,
)


PROFILE_MODE_FIELD = "qvdf_profile_mode"
DEFAULT_QVDF_PROFILE_MODE = 2
BOUNDARY_FIELDS = [*LOOKUP_BOUNDARY_FIELDS, PROFILE_MODE_FIELD]


def _link_is_explicitly_closed(link) -> bool:
    value = link.other_attrs.get("allowed_use")
    if value is None:
        return False
    return str(value).strip().lower() == "closed"


def apply_congestion_boundaries(
    network,
    period: str,
    lookup_directory: Optional[Union[str, Path]] = None,
    observed_t2_lookup_directory: Optional[Union[str, Path]] = None,
):
    """Add period-specific observed T0/T2/T3 values to converted links."""
    period_name = str(period).lower()
    links = list(network.link_dict.values())
    eligible = np.fromiter(
        (not _link_is_explicitly_closed(link) for link in links),
        dtype=bool,
        count=len(links),
    )
    eligible_link_count = int(np.count_nonzero(eligible))
    closed_link_count = len(links) - eligible_link_count

    # Keep one stable link.csv schema across periods and parallel chunks.
    # A blank time value tells the kernel to use its existing analytical
    # fallback. Profile mode is currently fixed to 2 for every converted link.
    for link in links:
        for field in LOOKUP_BOUNDARY_FIELDS:
            link.other_attrs[field] = ""
        link.other_attrs[PROFILE_MODE_FIELD] = DEFAULT_QVDF_PROFILE_MODE

    lookup_path = period_path(period_name, lookup_directory)
    if not lookup_path.is_file():
        print(
            f"Congestion boundary lookup {period_name.upper()}: no lookup table; "
            f"{', '.join(LOOKUP_BOUNDARY_FIELDS)} left blank for "
            f"{eligible_link_count:,} eligible links; skipped "
            f"{closed_link_count:,} explicitly closed links."
        )
        boundary_available = False
        matched = 0
    else:
        boundary_available = True
        table = load_period(period_name, lookup_directory)
        from_node_ids = np.fromiter(
            (link.from_node.node_id for link in links),
            dtype=np.uint64,
            count=len(links),
        )
        to_node_ids = np.fromiter(
            (link.to_node.node_id for link in links),
            dtype=np.uint64,
            count=len(links),
        )
        values, found = lookup(table, from_node_ids, to_node_ids)
        found = found & eligible

        for link_index in np.flatnonzero(found):
            link = links[int(link_index)]
            for field_index, field in enumerate(LOOKUP_BOUNDARY_FIELDS):
                value = values[link_index, field_index]
                link.other_attrs[field] = float(value) if np.isfinite(value) else ""

        matched = int(np.count_nonzero(found))
        print(
            f"Congestion boundary lookup {period_name.upper()}: matched "
            f"{matched:,} of {eligible_link_count:,} eligible converted links; "
            f"{eligible_link_count - matched:,} eligible links left blank; "
            f"skipped {closed_link_count:,} explicitly closed links."
        )

    from_node_ids = np.fromiter(
        (link.from_node.node_id for link in links),
        dtype=np.uint64,
        count=len(links),
    )
    to_node_ids = np.fromiter(
        (link.to_node.node_id for link in links),
        dtype=np.uint64,
        count=len(links),
    )
    observed_path = observed_t2_lookup_path(observed_t2_lookup_directory)
    observed_period_supported = period_name.upper() in OBSERVED_BOUNDARY_FIELDS
    observed_t2_available = observed_path.is_file() and observed_period_supported
    observed_t2_pair_matches = 0
    observed_t2_assigned = 0
    if observed_t2_available:
        observed_values, observed_found = lookup_observed_t2(
            load_observed_t2_lookup(observed_t2_lookup_directory),
            period_name,
            from_node_ids,
            to_node_ids,
        )
        observed_found = observed_found & eligible
        observed_t2_pair_matches = int(np.count_nonzero(observed_found))
        complete_observed = np.isfinite(observed_values).all(axis=-1)
        partial_observed = np.isfinite(observed_values).any(axis=-1) & ~complete_observed
        if np.any(observed_found & partial_observed):
            raise ValueError(
                f"Observed boundary lookup {period_name.upper()} contains "
                "partial T0/T2/T3 triplets"
            )
        assign_observed = observed_found & complete_observed
        for link_index in np.flatnonzero(assign_observed):
            link = links[int(link_index)]
            for field_index, field in enumerate(LOOKUP_BOUNDARY_FIELDS):
                link.other_attrs[field] = float(
                    observed_values[link_index, field_index]
                )
        protect_no_observed_congestion = observed_found & ~complete_observed
        for link_index in np.flatnonzero(protect_no_observed_congestion):
            link = links[int(link_index)]
            for field in LOOKUP_BOUNDARY_FIELDS:
                link.other_attrs[field] = ""
        observed_t2_assigned = int(np.count_nonzero(assign_observed))
        observed_t2_cleared_no_episode = int(
            np.count_nonzero(protect_no_observed_congestion)
        )
        print(
            f"Observed boundary lookup {period_name.upper()}: assigned accepted "
            f"weekday-average T0/T2/T3 to {observed_t2_assigned:,} links from "
            f"{observed_t2_pair_matches:,} matched observed node pairs; "
            f"cleared all three fields on {observed_t2_cleared_no_episode:,} "
            "links with no accepted episode."
        )
    else:
        observed_t2_cleared_no_episode = 0
        if observed_path.is_file() and not observed_period_supported:
            print(
                f"Observed boundary lookup {period_name.upper()}: no observed "
                "calibration for this period; T0/T2/T3 left blank so the "
                "kernel uses its analytical defaults."
            )

    stats = {
        "period": period_name,
        "available": boundary_available,
        "lookup_path": str(lookup_path),
        "links": len(links),
        "eligible_links": eligible_link_count,
        "closed_links_skipped": closed_link_count,
        "matched": matched,
        "unmatched": eligible_link_count - matched,
        "observed_t2_available": observed_t2_available,
        "observed_t2_lookup_path": str(observed_path),
        "observed_t2_pair_matches": observed_t2_pair_matches,
        "observed_t2_assigned": observed_t2_assigned,
        "observed_t2_cleared_no_episode": observed_t2_cleared_no_episode,
    }
    network.congestion_boundary_stats = stats
    return stats
