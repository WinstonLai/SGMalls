# Mall Amenity Data Pipeline

Builds a per-mall amenity summary for Singapore shopping malls (clinics, gyms,
bike racks, playgrounds, healthier-dining outlets) and flags which malls
satisfy the current criteria set. `mall_dashboard.py` is a Streamlit app that
explores that summary.

## Files

| File | Role |
|---|---|
| `mall_transformation_pipeline.py` | builds `mall_locations_summary.csv` from the raw files in `_data/` |
| `mall_dashboard.py` | Streamlit dashboard over the summary (see [Dashboard](#dashboard)) |
| `mall_common.py` | logic shared by both: the `satisfy` criteria and HDP outlet de-duplication |
| `requirements.txt` / `requirements-pipeline.txt` | dashboard dependencies / extra pipeline dependencies (includes the first) |
| `_data/` | raw inputs (table below) |
| `mall_locations_summary.csv` | pipeline output, also the dashboard's sample data |

## Pipeline order

```
openv5.py  ->  full_df.csv  ->  mall_transformation_pipeline.py  ->  mall_locations_summary.csv
```

1. **`openv5.py`** (run separately, first; **not included in this repo**)
   Geocodes the raw mall name list via the OneMap API (with pagination and a
   manual override table for known multi-postal-code malls) and writes
   `_data/full_df.csv`. The committed `_data/full_df.csv` is its last output;
   without `openv5.py` you can't regenerate it, so fix bad rows by hand (or
   add them to `COORD_OVERRIDES` in the pipeline, see Notes).

2. **`mall_transformation_pipeline.py`** (needs `pip install -r requirements-pipeline.txt`;
   run from anywhere: `python mall_transformation_pipeline.py`, add `-v` to also
   log the full DataFrame dumps)
   - `build_amenity_summary()` — joins `full_df.csv` against every amenity
     source (postal-code merges for clinics/gyms/HDP outlets, a 200m spatial
     buffer join for bike racks, a name match for playgrounds) and produces
     a per-mall summary with amenity counts.
   - `apply_datamart_and_satisfy()` — merges in `datamart_summary.csv` and
     computes the `satisfy` flag with `mall_common.recompute_satisfy()` (bike
     rack or playground, at least one HPB event, at least 3 HDP outlets, at
     least one supermarket).
   - Writes `mall_locations_summary.csv` next to the script.

## Required input files

All expected under `_data/` next to the script (`DATA_DIR` at the top of
`mall_transformation_pipeline.py`):

| File | Source | Used for |
|---|---|---|
| `full_df.csv` | output of `openv5.py` | master mall list (name, postal, lat/lon, HPM) |
| `CHASClinics.geojson` | data.gov.sg | CHAS clinic locations |
| `GymsSGGEOJSON.geojson` | data.gov.sg | gym locations (geojson source) |
| `LTABicycleRackGEOJSON.geojson` | data.gov.sg | bike rack locations (spatial join, 200m) |
| `mall_playgrounds.csv` | manual/compiled | playground presence by mall name |
| `clean_hdp_FINAL.csv` | data.gov.sg + manually checked | Healthier Dining Partner outlets |
| `SportFacilities.csv` | data.gov.sg | gyms (CSV source, `Gymnasium` and `Fitness Corner` rows only) |
| `clinic_df.csv` | data.gov.sg | polyclinic/PHPC clinic locations |
| `datamart_summary.csv` | internal datamart | HPB event counts, supermarket counts, etc. |

`_data/List of shopping malls in SG - List of malls.csv` (`name`, `name2`, `HPM`)
is not read by any code here; it looks like the mall list `openv5.py` geocodes.

## Output

- **`mall_locations_summary.csv`** — one row per mall with amenity counts by
  category, `has_bike_rack`, `has_playground`, coordinates, HPM flag,
  datamart fields, and the final `satisfy` column.

## Dashboard

```
pip install -r requirements.txt
streamlit run mall_dashboard.py
```

Tick **Use sample data** (loads `mall_locations_summary.csv`) or upload a CSV
with the same columns as that file; a missing required column is reported and
the app stops. If both are set, the sample data wins and a warning says so.

- **Sidebar** — bike rack distance slider, satisfy criteria (each threshold
  adjustable, with a reset button), and filters (name search, current HPMs,
  malls that satisfy the criteria).
- **Live recomputation** — `has_bike_rack` is recomputed from the raw bike rack
  GeoJSON for the chosen distance, and `HDP Outlet` from `_data/full_df.csv` +
  `_data/clean_hdp_FINAL.csv`. These replace the CSV's values for this session
  only. Malls in an uploaded CSV that aren't in `full_df.csv` keep their own
  `HDP Outlet` value (the sidebar says how many).
- **Criteria table tab** — raw counts behind the criteria, plus a list of
  current HPM malls that fail them and which group they fail.
- **Weighted HPM Index tab** — five categories (A healthy dining, B active
  living, C HPB/H365 engagement, D healthcare, E population), each the average
  of its fields' `value / max across all malls`, combined with adjustable
  weights that always total 100. Filters don't change the scores.
- **Map** — blue = satisfies the criteria, orange = does not, dark ring = current
  HPM mall. The tooltip spells out the status and the counts.

## Notes

- Postal codes are normalized to zero-padded 6-digit strings throughout.
- Bike racks are matched by proximity (200m buffer, SVY21/EPSG:3414), not
  exact postal code, since racks usually sit just outside a mall's own
  postal boundary.
- **`satisfy`** (`mall_common.recompute_satisfy()`, used by both the pipeline and
  the dashboard, so the CSV column equals the dashboard's default): (bike rack
  OR playground) AND (≥1 HPB event) AND (≥3 HDP outlets) AND (≥1 supermarket).
  Gyms are not part of it. A mall with no datamart row fails.
- **HDP outlets** — an outlet listed several times under different categories
  counts once (unique on postal + brand + outlet name).
- **Gyms** — `Gym/Sports (CSV)` counts one per venue for `Gymnasium` and
  `Fitness Corner` rows only (pools, courts and studios are ignored). Where the
  GeoJSON gym list already has a gym at that postal code, the CSV row is
  dropped so it isn't counted twice.
- **Mall names** are stripped of surrounding whitespace on load; the pipeline
  warns about any mall with no datamart row.
- **Coordinates and postals** — `full_df.csv` is the geocoder's output with a few
  hand corrections: wrong postal rows removed (e.g. Nexus @One-North and
  Nexus International School had been attached to Nex, the Greenwich Drive
  logistics hub to Singpost Centre) and mall coordinates fixed (Nex, IMM, KAP,
  Singpost Centre, Sun Plaza), all checked against OneMap by postal code.
  Re-apply them if `full_df.csv` is regenerated. The pipeline warns when a
  mall's coordinates are more than 1 km from the datamart's `LAT`/`LNG`,
  except for the malls in `COORDS_VERIFIED`, where the datamart is the wrong
  one. Still unchecked: Sembawang Point 758440, The Central 239897/239899.
