"""Step 05 — join parcels to the TOD envelope.

The envelope is the **union** of two source polygons:

1. ``data/processed/tod_areas.geojson`` — Honolulu's adopted TOD Special
   District polygons (from step 04).
2. A 1.6-mi straight-line buffer around each of the 13 operating stations
   (in ``data/raw/rail_transit_station_points.geojson``). 1.6 mi is chosen
   so the dataset envelope safely contains every parcel that could fall
   within the frontend slider's max walking distance (1.5 mi), accounting
   for typical walking-route detour factors of ~1.05-1.20×.

A parcel is included iff it intersects this union. The output carries:

- ``in_tod_area`` (bool): True if the parcel intersects any TOD polygon.
- ``STATION_ID`` (int): the **straight-line** nearest station. This is a
  preliminary attribution; ``etl/05b_walking_distances.py`` re-computes the
  nearest station and adds ``walk_dist_ft`` using the actual road network.
- ``STATION_NAME`` (str): name of that nearest station.

Right-of-way parcels (``street_parcel == 1``) are filtered out before the
join, same as before.

Output: ``data/processed/parcels_in_walksheds.geojson`` (path retained for
backward compatibility with downstream steps; +manifest sidecar).

Idempotent: skipped if the output and its manifest already exist. Pass
``--force`` to rebuild.

Usage::

    python etl/05_join_parcels.py
    python etl/05_join_parcels.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import write_manifest  # noqa: E402

SCRIPT_NAME = "etl/05_join_parcels.py"

PARCELS_PATH    = _ROOT / "data" / "raw"       / "parcels_tax.geojson"
TOD_AREAS_PATH  = _ROOT / "data" / "processed" / "tod_areas.geojson"
STATIONS_PATH   = _ROOT / "data" / "raw"       / "rail_transit_station_points.geojson"
OUTPUT_PATH     = _ROOT / "data" / "processed" / "parcels_in_walksheds.geojson"

WGS84  = 4326
UTM_4N = 32604

# Straight-line envelope buffer in meters. 1.6 mi gives padding above the
# slider's 1.5-mi walking-distance max for typical walk-route detour ratios.
ENVELOPE_BUFFER_M = 1.6 * 1609.344  # 2574.95 m

OPERATING_STATION_IDS: set[int] = set(range(1, 14))  # 1..13


def join_parcels(*, force: bool) -> int:
    if not PARCELS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {PARCELS_PATH}. Run "
            f"`python etl/01_fetch_arcgis.py parcels_tax` first."
        )
    if not TOD_AREAS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {TOD_AREAS_PATH}. Run `python etl/04_build_tod_areas.py` first."
        )
    if not STATIONS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {STATIONS_PATH}. Run "
            f"`python etl/01_fetch_arcgis.py rail_transit_station_points` first."
        )

    manifest_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".manifest.json")
    if not force and OUTPUT_PATH.exists() and manifest_path.exists():
        print(f"[skip] {OUTPUT_PATH.name} (cached)")
        return 0

    print(f"[read] {PARCELS_PATH.relative_to(_ROOT)}")
    parcels = gpd.read_file(PARCELS_PATH)
    if parcels.crs is None:
        parcels = parcels.set_crs(WGS84)
    elif parcels.crs.to_epsg() != WGS84:
        parcels = parcels.to_crs(WGS84)

    print(f"[read] {TOD_AREAS_PATH.relative_to(_ROOT)}")
    tod_areas = gpd.read_file(TOD_AREAS_PATH)
    if tod_areas.crs is None:
        tod_areas = tod_areas.set_crs(WGS84)
    elif tod_areas.crs.to_epsg() != WGS84:
        tod_areas = tod_areas.to_crs(WGS84)

    print(f"[read] {STATIONS_PATH.relative_to(_ROOT)}")
    stations = gpd.read_file(STATIONS_PATH)
    if stations.crs is None:
        stations = stations.set_crs(WGS84)
    elif stations.crs.to_epsg() != WGS84:
        stations = stations.to_crs(WGS84)

    if "ID" not in stations.columns or "STATION" not in stations.columns:
        raise KeyError(
            f"Expected fields 'ID' and 'STATION' in {STATIONS_PATH.name}; "
            f"got {list(stations.columns)}"
        )
    operating = stations[stations["ID"].isin(OPERATING_STATION_IDS)].copy()
    operating["ID"] = operating["ID"].astype(int)

    n_parcels_total = len(parcels)
    if "street_parcel" in parcels.columns:
        rights_of_way = (parcels["street_parcel"] == 1).sum()
        parcels = parcels[parcels["street_parcel"] != 1].copy()
        print(f"[filter] dropped {rights_of_way} right-of-way parcels "
              f"(street_parcel == 1); {len(parcels)}/{n_parcels_total} remain")
    else:
        print("[warn] no 'street_parcel' field on parcels layer; skipping ROW filter")

    # Build envelope = union(TOD areas) ∪ buffer around each station.
    # Work in UTM so the buffer is in true meters.
    print(f"[envelope] building TOD ∪ {ENVELOPE_BUFFER_M:.0f}-m station buffers")
    tod_utm = tod_areas.to_crs(UTM_4N)
    op_utm  = operating.to_crs(UTM_4N)
    station_buffers_utm = op_utm.copy()
    station_buffers_utm["geometry"] = op_utm.geometry.buffer(ENVELOPE_BUFFER_M)

    envelope_utm = unary_union(
        list(tod_utm.geometry) + list(station_buffers_utm.geometry)
    )
    envelope = gpd.GeoSeries([envelope_utm], crs=UTM_4N).to_crs(WGS84).iloc[0]

    # Spatial filter: parcels that intersect the envelope.
    print("[sjoin] parcels ∩ envelope")
    in_env_mask = parcels.geometry.intersects(envelope)
    in_env = parcels[in_env_mask].copy()
    print(f"[envelope] {len(in_env)} parcels in TOD envelope")

    # Flag in_tod_area: parcel intersects ANY TOD polygon.
    tod_union = unary_union(list(tod_areas.geometry))
    in_env["in_tod_area"] = in_env.geometry.intersects(tod_union)
    print(f"[in_tod_area] {int(in_env['in_tod_area'].sum())} parcels inside an adopted TOD district")

    # Straight-line nearest station — a preliminary attribution. The
    # walking-distance step (etl/05b) can override this if the actual road
    # network ranks stations differently.
    print("[nearest] straight-line nearest station per parcel (preliminary)")
    parcels_utm = in_env.to_crs(UTM_4N).copy()
    parcels_utm["_centroid"] = parcels_utm.geometry.centroid

    op_pts = op_utm[["ID", "STATION", "geometry"]].copy()
    op_pts.rename(columns={"ID": "STATION_ID", "STATION": "STATION_NAME"}, inplace=True)
    # GeoPandas sjoin_nearest gives us per-parcel nearest station in one pass.
    centroids = gpd.GeoDataFrame(
        parcels_utm.drop(columns=["geometry"]).rename(columns={"_centroid": "geometry"}),
        geometry="geometry",
        crs=UTM_4N,
    )
    nearest = gpd.sjoin_nearest(
        centroids,
        op_pts,
        how="left",
        distance_col="_dist_m",
    ).drop(columns=["index_right"], errors="ignore")
    # Some parcels can match multiple stations at exactly equal distance;
    # keep the lowest STATION_ID for determinism.
    nearest = nearest.sort_values(["tmk", "STATION_ID"]).groupby("tmk", as_index=False).first() if "tmk" in nearest.columns else nearest

    # Merge attribution back onto in_env (keeping original parcel polygon geometry).
    attr_cols = [c for c in ("tmk", "STATION_ID", "STATION_NAME") if c in nearest.columns]
    if "tmk" in in_env.columns and "tmk" in nearest.columns:
        in_env = in_env.merge(nearest[attr_cols], on="tmk", how="left")
    else:
        # Fallback: positional merge if no tmk join key.
        in_env = pd.concat(
            [in_env.reset_index(drop=True),
             nearest[attr_cols].reset_index(drop=True)],
            axis=1,
        )

    in_env["STATION_ID"] = in_env["STATION_ID"].astype("Int64")

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    in_env.to_file(OUTPUT_PATH, driver="GeoJSON")

    rows_per_station = (
        in_env.dropna(subset=["STATION_ID"]).groupby("STATION_ID").size().to_dict()
    )

    write_manifest(
        OUTPUT_PATH,
        source_url=f"file://{PARCELS_PATH}",
        row_count=len(in_env),
        script=SCRIPT_NAME,
        extras={
            "predicate":               "intersects (envelope)",
            "envelope":                "TOD areas ∪ station buffers",
            "envelope_buffer_meters":  ENVELOPE_BUFFER_M,
            "envelope_crs_for_buffer": f"EPSG:{UTM_4N}",
            "tod_areas_input":         str(TOD_AREAS_PATH.relative_to(_ROOT)),
            "stations_input":          str(STATIONS_PATH.relative_to(_ROOT)),
            "parcels_input":           str(PARCELS_PATH.relative_to(_ROOT)),
            "parcels_input_total":     int(n_parcels_total),
            "parcels_after_row_filter": int(len(parcels)),
            "parcels_in_envelope":     int(len(in_env)),
            "parcels_in_tod_area":     int(in_env["in_tod_area"].sum()),
            "rows_per_station":        {int(k): int(v) for k, v in rows_per_station.items()},
            "filters":                 {"street_parcel != 1": True},
            "crs":                     f"EPSG:{WGS84}",
        },
    )

    print(f"[done] {OUTPUT_PATH.relative_to(_ROOT)} "
          f"({len(in_env)} parcels in envelope, "
          f"{int(in_env['in_tod_area'].sum())} inside TOD areas)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the cache exists.")
    args = ap.parse_args(argv)
    return join_parcels(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
