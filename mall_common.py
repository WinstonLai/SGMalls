"""
Logic shared by mall_transformation_pipeline.py and mall_dashboard.py, so
the 'satisfy' flag and the HDP outlet counts can't drift apart between the
committed CSV and what the dashboard shows.
"""

import pandas as pd

# A mall must satisfy all of the following:
#   - bike rack OR playground
#   - HPB events (on its own)
#   - HDP outlets (on its own)
#   - supermarkets (on its own, only if that column is present)
DEFAULT_INCLUDE_BIKE_RACK = True
DEFAULT_INCLUDE_PLAYGROUND = True
DEFAULT_MIN_HPB_EVENTS = 1
DEFAULT_MIN_HDP_OUTLETS = 3
DEFAULT_MIN_SUPERMARKET = 1

# One HDP outlet can be listed several times under different Category values
# (e.g. Burger King as both "Western" and "Fast Food"); this is what makes a
# row a distinct outlet.
HDP_OUTLET_KEY = ["postal", "Brand_Name", "Name_of_Outlet"]


def dedupe_hdp_outlets(hdp_df):
    """Drops repeat listings of the same outlet, keeping the first."""
    return hdp_df.drop_duplicates(subset=HDP_OUTLET_KEY)


def compute_satisfy_groups(
    df,
    include_bike_rack=DEFAULT_INCLUDE_BIKE_RACK,
    include_playground=DEFAULT_INCLUDE_PLAYGROUND,
    min_hpb_events=DEFAULT_MIN_HPB_EVENTS,
    min_hdp_outlets=DEFAULT_MIN_HDP_OUTLETS,
    min_supermarket=DEFAULT_MIN_SUPERMARKET,
):
    """The satisfy-criteria groups, as individual boolean Series rather
    than one combined flag — shared by recompute_satisfy() (which just ANDs
    them together) and by anything that wants to show *which* group(s) a
    mall is failing. A mall must meet all of:
      - bike rack (if include_bike_rack), or playground (if
        include_playground) — if neither is enabled, this group is treated
        as satisfied automatically (the check is effectively switched off)
      - hpb_event_count >= min_hpb_events (on its own)
      - HDP Outlet >= min_hdp_outlets (on its own)
      - supermarket_outlet_count >= min_supermarket, as a SEPARATE
        requirement from HDP outlets (both must independently pass) —
        only checked if that column is loaded, otherwise treated as
        satisfied automatically
    A missing (NaN) count fails its group.
    """
    if not include_bike_rack and not include_playground:
        active_living_ok = pd.Series(True, index=df.index)
    else:
        active_living_ok = pd.Series(False, index=df.index)
        if include_bike_rack:
            active_living_ok = active_living_ok | (df["has_bike_rack"] >= 1)
        if include_playground:
            active_living_ok = active_living_ok | (df["has_playground"] >= 1)

    events_ok = df["hpb_event_count"] >= min_hpb_events

    hdp_ok = df["HDP Outlet"] >= min_hdp_outlets

    if "supermarket_outlet_count" in df.columns:
        supermarket_ok = df["supermarket_outlet_count"] >= min_supermarket
    else:
        supermarket_ok = pd.Series(True, index=df.index)

    return active_living_ok, events_ok, hdp_ok, supermarket_ok


def recompute_satisfy(
    df,
    include_bike_rack=DEFAULT_INCLUDE_BIKE_RACK,
    include_playground=DEFAULT_INCLUDE_PLAYGROUND,
    min_hpb_events=DEFAULT_MIN_HPB_EVENTS,
    min_hdp_outlets=DEFAULT_MIN_HDP_OUTLETS,
    min_supermarket=DEFAULT_MIN_SUPERMARKET,
):
    """Applies the 'satisfy' criteria with every threshold configurable
    instead of hardcoded. Gyms/sports facilities are not part of this
    criteria. See compute_satisfy_groups() for what the groups are — a mall
    must meet all of them. Returns a 0/1 int Series."""
    active_living_ok, events_ok, hdp_ok, supermarket_ok = compute_satisfy_groups(
        df,
        include_bike_rack=include_bike_rack,
        include_playground=include_playground,
        min_hpb_events=min_hpb_events,
        min_hdp_outlets=min_hdp_outlets,
        min_supermarket=min_supermarket,
    )
    return (active_living_ok & events_ok & hdp_ok & supermarket_ok).astype(int)
