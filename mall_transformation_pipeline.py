"""
Mall amenity data transformation pipeline.

Combines amenity merges + spatial joins (per-mall summary) with the
datamart merge + 'satisfy' flag computation (merging in
datamart_summary.csv and computing the 'satisfy' flag), which used to be
a separate step.

Upstream dependency (NOT included here, run separately):
  - openv5.py  -> produces full_df.csv via OneMap geocoding.
    Kept separate on purpose: it calls a live, paginated, rate-limited API
    and shouldn't be re-run every time the amenity/criteria logic changes.

Required input files (all under DATA_DIR):
  - full_df.csv                    (output of openv5.py)
  - CHASClinics.geojson
  - GymsSGGEOJSON.geojson
  - LTABicycleRackGEOJSON.geojson
  - mall_playgrounds.csv
  - clean_hdp_FINAL.csv
  - SportFacilities.csv
  - clinic_df.csv
  - datamart_summary.csv

Output:
  - mall_locations_summary.csv    (final per-mall summary with 'satisfy' flag)

Note: this no longer writes a separate pre-datamart-merge intermediate
summary or any debug side-files — everything is chained in memory and only
the final CSV (mall_locations_summary.csv) is written. If you still want
the intermediate amenity-summary CSV for debugging, uncomment the marked
line near the bottom of build_amenity_summary().
"""

import argparse
import logging
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from bs4 import BeautifulSoup

from mall_common import dedupe_hdp_outlets, recompute_satisfy

log = logging.getLogger("mall_pipeline")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
REPO_DIR = Path(__file__).resolve().parent
DATA_DIR = REPO_DIR / "_data"
OUTPUT_PATH = REPO_DIR / "mall_locations_summary.csv"
X_METERS = 200  # bike rack proximity threshold, in metres

# Malls whose full_df.csv coordinates were checked against OneMap by postal
# code, where the datamart's LAT/LNG is the wrong one (IMM, KAP, Nex) or
# differs for other reasons; the datamart check below skips them.
COORDS_VERIFIED = {"IMM", "KAP", "Nex", "Singpost Centre", "Sun Plaza", "Terminal 3"}
COORD_MISMATCH_KM = 1  # warn when full_df and datamart coords differ by more

# SportFacilities.csv facility types that count as a gym (lowercased).
GYM_FACILITY_TYPES = {"gymnasium", "fitness corner"}


# ---------------------------------------------------------------------------
# Shared geojson helpers (CHAS / gym / bike geojson sources only — the CSV
# sources don't need HTML parsing or a geometry column)
# ---------------------------------------------------------------------------
def parse_html_table(html_str):
    """Parse the ESRI-style HTML table stored in a geojson 'Description' field
    into a flat dict of {field_name: value}."""
    if not isinstance(html_str, str):
        return {}
    soup = BeautifulSoup(html_str, 'html.parser')
    data = {}
    for row in soup.find_all('tr'):
        th = row.find('th')
        td = row.find('td')
        if th and td:
            data[th.get_text(strip=True)] = td.get_text(strip=True)
    return data


def clean(df):
    """Expand the 'Description' HTML column into real columns, split geometry
    into longitude/latitude, and normalise any postal code column to a
    zero-padded 6-digit string."""
    parsed_data = df['Description'].apply(parse_html_table)
    extracted_df = pd.DataFrame(list(parsed_data))

    clean_gdf = pd.concat([df.drop(columns=['Description']), extracted_df], axis=1)
    clean_gdf['longitude'] = clean_gdf['geometry'].x
    clean_gdf['latitude'] = clean_gdf['geometry'].y
    clean_gdf = clean_gdf.drop(columns=['geometry'])

    postal_cols = ['POSTAL_CD', 'ADDRESSPOSTALCODE', 'POSTAL_CODE', 'PostalCode']
    for col in postal_cols:
        if col in clean_gdf.columns:
            clean_gdf[col] = (
                clean_gdf[col]
                .astype(str)
                .str.split('.').str[0]  # drop trailing '.0' from float parsing
                .str.zfill(6)            # pad back to 6 digits
            )

    log.info(f"There are {len(clean_gdf)} rows.")
    return clean_gdf


def check_coords_against_datamart(malls_df):
    """Warns about malls whose full_df.csv coordinates disagree with the
    datamart's LAT/LNG by more than COORD_MISMATCH_KM — one of the two is a
    bad geocode, and the bike rack flag and map both depend on them."""
    datamart = pd.read_csv(f"{DATA_DIR}/datamart_summary.csv")
    datamart['MALL_NAME'] = datamart['MALL_NAME'].str.strip()
    coords = malls_df[['mall_name', 'latitude', 'longitude']].drop_duplicates('mall_name')
    coords = coords.assign(mall_name=coords['mall_name'].str.strip())
    merged = coords.merge(datamart[['MALL_NAME', 'LAT', 'LNG']], left_on='mall_name', right_on='MALL_NAME')

    lat1, lon1, lat2, lon2 = (np.radians(merged[c]) for c in ['latitude', 'longitude', 'LAT', 'LNG'])
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    merged['km_apart'] = 2 * 6371 * np.arcsin(np.sqrt(a))

    far = merged[(merged['km_apart'] > COORD_MISMATCH_KM) & ~merged['mall_name'].isin(COORDS_VERIFIED)]
    if len(far):
        log.warning(
            "%d malls' full_df coordinates are >%skm from the datamart's:\n%s",
            len(far), COORD_MISMATCH_KM,
            far[['mall_name', 'latitude', 'longitude', 'LAT', 'LNG', 'km_apart']].round(3).to_string(index=False),
        )


def build_amenity_summary():
    """Runs the full amenity merge/spatial-join pipeline and returns the
    per-mall summary DataFrame (indexed by mall_name)."""

    # -----------------------------------------------------------------
    # Mall reference data — the ONE source every merge/spatial join uses.
    # -----------------------------------------------------------------
    malls_df = pd.read_csv(f"{DATA_DIR}/full_df.csv", dtype={'postal': str})
    log.debug("%s", malls_df.dtypes)

    malls_df['postal'] = malls_df['postal'].astype(str)
    malls_df = malls_df.rename(columns={'name': 'mall_name'})
    # A trailing space in a name (e.g. "People Park Complex ") silently breaks
    # every name-based merge below (datamart, playgrounds).
    malls_df['mall_name'] = malls_df['mall_name'].str.strip()

    check_coords_against_datamart(malls_df)

    check = malls_df[malls_df.duplicated(subset=['postal'], keep=False)]
    log.info("%d rows share a postal code with another row", len(check))
    log.debug("%s", check)

    malls_for_merge = malls_df[['postal', 'mall_name']]

    # -----------------------------------------------------------------
    # GEOJSON SOURCES — CHAS clinics, gyms, and bicycle racks
    # -----------------------------------------------------------------
    chas_gdf = clean(gpd.read_file(f"{DATA_DIR}/CHASClinics.geojson")).rename(columns={'POSTAL_CD': 'postal'})
    gym_geo_gdf = clean(gpd.read_file(f"{DATA_DIR}/GymsSGGEOJSON.geojson")).rename(columns={'ADDRESSPOSTALCODE': 'postal'})
    bike_gdf = clean(gpd.read_file(f"{DATA_DIR}/LTABicycleRackGEOJSON.geojson"))

    chas_malls_mapped = pd.merge(chas_gdf, malls_for_merge, how='left', on='postal')
    mapped_chas_clinics = chas_malls_mapped[chas_malls_mapped['mall_name'].notnull()].copy()
    log.info(f"{len(mapped_chas_clinics)}/{len(chas_gdf)} CHAS clinics mapped to malls")

    gym_geo_malls_mapped = pd.merge(gym_geo_gdf, malls_for_merge, how='left', on='postal')
    mapped_gyms_geo = gym_geo_malls_mapped[gym_geo_malls_mapped['mall_name'].notnull()].copy()
    log.info(f"{len(mapped_gyms_geo)}/{len(gym_geo_gdf)} geojson gyms mapped to malls")

    # Bike racks get a proximity flag (nearest X metres) rather than an exact
    # postal-code match, since racks usually sit just outside a mall's own
    # postal boundary.
    bike_spatial = gpd.GeoDataFrame(
        bike_gdf, geometry=gpd.points_from_xy(bike_gdf['longitude'], bike_gdf['latitude']), crs="EPSG:4326"
    ).to_crs(epsg=3414)

    malls_spatial = gpd.GeoDataFrame(
        malls_df, geometry=gpd.points_from_xy(malls_df['longitude'], malls_df['latitude']), crs="EPSG:4326"
    ).to_crs(epsg=3414)

    malls_buffered = malls_spatial.copy()
    malls_buffered['geometry'] = malls_buffered.geometry.buffer(X_METERS)

    joined = gpd.sjoin(malls_buffered, bike_spatial, how="left", predicate="intersects")
    match_counts = joined.groupby(joined.index)['index_right'].count()
    malls_df['has_bike_rack'] = (match_counts > 0).reindex(malls_df.index, fill_value=False).astype(int)
    log.info(f"{malls_df['has_bike_rack'].sum()} malls have a bike rack within {X_METERS}m")

    # -----------------------------------------------------------------
    # Playgrounds — matched directly on mall name (no postal/geometry
    # available), so this doesn't go through the postal merge or
    # bike-rack-style spatial join.
    # -----------------------------------------------------------------
    playground_df = pd.read_csv(f"{DATA_DIR}/mall_playgrounds.csv")
    playground_df = playground_df.rename(columns={'Mall / Location': 'mall_name', 'Playground': 'has_playground'})
    playground_df['mall_name'] = playground_df['mall_name'].str.strip()

    unmatched_playgrounds = playground_df[~playground_df['mall_name'].isin(malls_df['mall_name'])]
    if len(unmatched_playgrounds):
        log.warning(
            "Playground entries with no matching mall_name in malls_df (flag will be dropped for these): %s",
            unmatched_playgrounds['mall_name'].tolist(),
        )

    playground_by_mall = (
        playground_df[playground_df['mall_name'].isin(malls_df['mall_name'])]
        .drop_duplicates('mall_name')
        .set_index('mall_name')['has_playground']
    )
    log.info(f"{len(playground_by_mall)} malls matched to a playground flag")

    # -----------------------------------------------------------------
    # CSV SOURCES — HDP outlets, sports facilities, PHPC clinics
    # -----------------------------------------------------------------

    # --- Healthier Dining Partner (HDP) outlets ---
    hdp_df = pd.read_csv(f"{DATA_DIR}/clean_hdp_FINAL.csv", dtype={'postal': str})
    hdp_df['postal'] = hdp_df['postal'].astype(str).str.zfill(6)
    log.info(f"{len(hdp_df)} initial HDP outlets")
    # An outlet listed under several categories is still one outlet.
    hdp_df = dedupe_hdp_outlets(hdp_df)
    log.info(f"{len(hdp_df)} unique HDP outlets")

    log.info(f"Total null rows: {hdp_df['postal'].isnull().sum()}")
    hdp_df_clean = hdp_df[hdp_df['postal'].notnull()]

    stores_malls_mapped = pd.merge(hdp_df_clean, malls_for_merge, how='left', on='postal')
    mapped_stores = stores_malls_mapped[stores_malls_mapped['mall_name'].notnull()].copy()
    log.info(f"{len(mapped_stores)}/{len(hdp_df)} HDP stores mapped to malls")

    # --- Gyms from the sports facilities CSV ---
    # The file has one row per facility TYPE per venue (pools, courts,
    # studios, ...), so keep only gym-type rows and count each venue once.
    gym_csv_df = pd.read_csv(f"{DATA_DIR}/SportFacilities.csv", dtype={"PostalCode": str})
    gym_csv_df = gym_csv_df.rename(columns={"PostalCode": "postal"})
    log.info(f"{len(gym_csv_df)} initial sports facilities")
    gym_csv_df = gym_csv_df[gym_csv_df['SportsFacility'].str.strip().str.lower().isin(GYM_FACILITY_TYPES)]
    log.info(f"{len(gym_csv_df)} gym-type rows (of {', '.join(sorted(GYM_FACILITY_TYPES))})")
    log.info(f"Total null rows: {gym_csv_df['postal'].isnull().sum()}")
    gym_csv_df = gym_csv_df[gym_csv_df['postal'].notnull()]
    gym_csv_df = gym_csv_df.assign(_venue=gym_csv_df['VenueName'].str.strip().str.lower())
    gym_csv_df = gym_csv_df.drop_duplicates(subset=['postal', '_venue']).drop(columns='_venue')
    # The GeoJSON gym list overlaps this one; where it already has a gym at a
    # postal code, it wins and the CSV row is dropped so the gym isn't
    # counted twice.
    already_in_geojson = gym_csv_df['postal'].isin(gym_geo_gdf['postal'])
    log.info(f"{int(already_in_geojson.sum())} CSV gyms dropped as already in the GeoJSON gym list")
    gym_csv_df = gym_csv_df[~already_in_geojson]
    log.info(f"{len(gym_csv_df)} unique CSV gyms")

    gyms_csv_malls_mapped = pd.merge(gym_csv_df, malls_for_merge, how='left', on='postal')
    mapped_gyms_csv = gyms_csv_malls_mapped[gyms_csv_malls_mapped['mall_name'].notnull()].copy()
    log.info(f"{len(mapped_gyms_csv)}/{len(gym_csv_df)} CSV sports facilities mapped to malls")

    # --- PHPC clinics ---
    clinic_df = pd.read_csv(f"{DATA_DIR}/clinic_df.csv")
    clinic_df['postal'] = clinic_df['postal'].astype(str).str.zfill(6)
    # The postal column loses leading zeros when read as a number (padded
    # above) and some rows hold the wrong digits (e.g. a unit number), so
    # the postal written in the address, "Singapore NNNNNN", wins when
    # present.
    address_postal = clinic_df['address'].str.extract(r'Singapore\s*(\d{5,6})\b')[0].str.zfill(6)
    changed = address_postal.notnull() & (address_postal != clinic_df['postal'])
    if changed.any():
        log.warning("Using the address postal over the postal column for: %s",
                    clinic_df.loc[changed, 'name'].tolist())
    clinic_df['postal'] = address_postal.fillna(clinic_df['postal'])

    log.info(f"{len(clinic_df)} initial PHPC clinics")
    log.info(f"Total null rows: {clinic_df['postal'].isnull().sum()}")

    clinic_df = clinic_df[clinic_df['postal'].notnull()]
    clinic_malls_mapped = pd.merge(clinic_df, malls_for_merge, how='left', on='postal')
    mapped_clinics_phpc = clinic_malls_mapped[clinic_malls_mapped['mall_name'].notnull()].copy()
    log.info(f"{len(mapped_clinics_phpc)}/{len(clinic_df)} PHPC clinics mapped to malls")

    # -----------------------------------------------------------------
    # Combined per-mall summary across all five countable amenity sources
    # + the bike rack proximity flag
    # -----------------------------------------------------------------
    mapped_stores['category'] = 'HDP Outlet'
    mapped_gyms_csv['category'] = 'Gym/Sports (CSV)'
    mapped_clinics_phpc['category'] = 'Clinic (PHPC)'
    mapped_chas_clinics['category'] = 'Clinic (CHAS)'
    mapped_gyms_geo['category'] = 'Gym (GeoJSON)'

    all_mapped = pd.concat([
        mapped_stores[['mall_name', 'category']],
        mapped_gyms_csv[['mall_name', 'category']],
        mapped_clinics_phpc[['mall_name', 'category']],
        mapped_chas_clinics[['mall_name', 'category']],
        mapped_gyms_geo[['mall_name', 'category']],
    ], ignore_index=True)

    mall_summary = pd.crosstab(all_mapped['mall_name'], all_mapped['category'])
    mall_summary['Total'] = mall_summary.sum(axis=1)

    # Crosstab only keeps malls with >=1 mapped amenity — reindex against
    # the full mall list so malls with zero matches still appear (as
    # all-zero rows)
    mall_summary = mall_summary.reindex(malls_df['mall_name'].unique(), fill_value=0)
    mall_summary.index.name = 'mall_name'

    bike_by_mall = malls_df[['mall_name', 'has_bike_rack']].drop_duplicates('mall_name').set_index('mall_name')['has_bike_rack']
    mall_summary['has_bike_rack'] = bike_by_mall.reindex(mall_summary.index).fillna(0).astype(int)

    mall_summary['has_playground'] = playground_by_mall.reindex(mall_summary.index).fillna(0).astype(int)

    # Bring lat/lon/HPM back in (dropped by the crosstab) so the dashboard
    # can plot malls on a map
    coords_by_mall = malls_df[['mall_name', 'longitude', 'latitude', 'HPM']].drop_duplicates('mall_name').set_index('mall_name')
    mall_summary = mall_summary.join(coords_by_mall, how='left')

    mall_summary = mall_summary.sort_values(by='Total', ascending=False)

    log.info(f"""
{len(mapped_stores)} HDP stores out of {len(hdp_df)} mapped.
{len(mapped_gyms_csv)} CSV gyms/sports facilities out of {len(gym_csv_df)} mapped.
{len(mapped_clinics_phpc)} PHPC clinics out of {len(clinic_df)} mapped.
{len(mapped_chas_clinics)} CHAS clinics out of {len(chas_gdf)} mapped.
{len(mapped_gyms_geo)} geojson gyms out of {len(gym_geo_gdf)} mapped.
{malls_df['has_bike_rack'].sum()} malls have a bike rack within {X_METERS}m.
""")

    log.debug("Per-mall amenity summary:\n%s", mall_summary.to_string())

    # Uncomment if you want the old intermediate file for debugging:
    # mall_summary.to_csv("mall_locations_summary.csv")

    return mall_summary.reset_index()


def apply_datamart_and_satisfy(mall_locations_summary):
    """Merges in datamart_summary.csv and computes the final 'satisfy' flag."""

    mall_datamart_summary = pd.read_csv(f"{DATA_DIR}/datamart_summary.csv")
    mall_datamart_summary['MALL_NAME'] = mall_datamart_summary['MALL_NAME'].str.strip()
    mall_datamart_summary = mall_datamart_summary.rename(columns={'MALL_NAME': 'mall_name'}).drop(
        columns=['HPM', 'LAT', 'LNG']
    )

    unmatched = set(mall_locations_summary['mall_name']) - set(mall_datamart_summary['mall_name'])
    if unmatched:
        log.warning("%d malls have no datamart row (their datamart fields will be NaN): %s", len(unmatched), sorted(unmatched))

    total_summary = pd.merge(mall_locations_summary, mall_datamart_summary, how="left", on='mall_name')

    # 'satisfy' uses the same criteria (and defaults) as the dashboard — see
    # mall_common.recompute_satisfy().
    total_summary['satisfy'] = recompute_satisfy(total_summary)

    return total_summary


def main():
    mall_locations_summary = build_amenity_summary()
    total_summary = apply_datamart_and_satisfy(mall_locations_summary)

    total_summary.to_csv(OUTPUT_PATH, index=False)

    log.info("Wrote %d malls to %s (%d satisfy the criteria)", len(total_summary), OUTPUT_PATH, int(total_summary['satisfy'].sum()))
    log.debug("%s", total_summary.to_string())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("-v", "--verbose", action="store_true", help="also log full DataFrame dumps")
    logging.basicConfig(level=logging.DEBUG if parser.parse_args().verbose else logging.INFO, format="%(levelname)s %(message)s")
    main()
