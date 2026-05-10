"""Step 04 — extract Honolulu's adopted TOD Special District boundaries.

Reads ``data/raw/zoning_special_districts.geojson`` (fetched by step 01 from
``cchnl::zoning-special-district`` — 16 records covering all of Honolulu's
Special Districts), filters to the 6 records named
``Transit-Oriented Development Special District``, and emits the union as
``data/processed/tod_areas.geojson``.

Also reads ``data/raw/rail_transit_station_points.geojson`` to build a
``STATION_IDS`` array on each TOD polygon, listing the operating Skyline
stations (IDs 1–13) that fall inside that polygon. Stations that don't sit
inside any adopted TOD Special District (e.g. Hālawa is bordered by but
not inside a TOD area) are warned about; the pipeline still continues.

The previous behavior — buffering each station by 1 mi to form a circular
walkshed — is deprecated. The legacy ``etl/04_build_walksheds.py`` remains
for reference but is no longer wired into ``pipeline_run.py``.

Output: ``data/processed/tod_areas.geojson`` (+ manifest sidecar). Each
feature has fields:

- ``TOD_ID`` (int, 1-based, sorted by westmost x of the polygon centroid)
- ``STATION_IDS`` (list[int], stations 1–13 contained in the polygon)
- ``STATION_NAMES`` (list[str], parallel to STATION_IDS)

Idempotent: skipped if the output and its manifest already exist. Pass
``--force`` to rebuild.

Usage::

    python etl/04_build_tod_areas.py
    python etl/04_build_tod_areas.py --force
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import geopandas as gpd
from shapely.geometry import shape

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import write_manifest  # noqa: E402

SCRIPT_NAME = "etl/04_build_tod_areas.py"

DISTRICTS_PATH = _ROOT / "data" / "raw"       / "zoning_special_districts.geojson"
STATIONS_PATH  = _ROOT / "data" / "raw"       / "rail_transit_station_points.geojson"
OUTPUT_PATH    = _ROOT / "data" / "processed" / "tod_areas.geojson"

WGS84  = 4326
UTM_4N = 32604  # for centroid sorting in true meters

TOD_NAME = "Transit-Oriented Development Special District"

# Mirror EXPECTED_STATIONS from the deprecated walkshed builder so the
# trip-wire still catches upstream station-id renumbering.
EXPECTED_STATION_IDS: set[int] = set(range(1, 14))  # 1..13


def build_tod_areas(*, force: bool) -> int:
    if not DISTRICTS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {DISTRICTS_PATH}. Run "
            f"`python etl/01_fetch_arcgis.py zoning_special_districts` first."
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

    print(f"[read] {DISTRICTS_PATH.relative_to(_ROOT)}")
    sd = gpd.read_file(DISTRICTS_PATH)
    if sd.crs is None:
        sd = sd.set_crs(WGS84)
    elif sd.crs.to_epsg() != WGS84:
        sd = sd.to_crs(WGS84)

    name_field = next(
        (c for c in sd.columns if c.lower() == "special_district"),
        None,
    )
    if name_field is None:
        raise KeyError(
            f"Expected a 'special_district' field in {DISTRICTS_PATH.name}; "
            f"got {list(sd.columns)}"
        )

    tod = sd[sd[name_field] == TOD_NAME].copy()
    if tod.empty:
        raise ValueError(
            f"No '{TOD_NAME}' polygons in {DISTRICTS_PATH.name}. "
            f"Did the upstream layer rename the district?"
        )
    print(f"[filter] {len(tod)}/{len(sd)} polygons named {TOD_NAME!r}")

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
    operating = stations[stations["ID"].isin(EXPECTED_STATION_IDS)].copy()

    # Sort TOD polygons west → east by centroid x in UTM (true meters).
    tod_utm = tod.to_crs(UTM_4N)
    tod = tod.assign(_cx=tod_utm.geometry.centroid.x).sort_values("_cx").reset_index(drop=True)
    tod["TOD_ID"] = range(1, len(tod) + 1)

    # For each TOD polygon, list which operating stations sit inside it.
    station_ids_per_tod: list[list[int]] = []
    station_names_per_tod: list[list[str]] = []
    matched: set[int] = set()
    for poly in tod.geometry:
        ids: list[int] = []
        names: list[str] = []
        for _, row in operating.iterrows():
            if row.geometry.within(poly):
                ids.append(int(row["ID"]))
                names.append(str(row["STATION"]))
                matched.add(int(row["ID"]))
        station_ids_per_tod.append(sorted(ids))
        station_names_per_tod.append([n for _, n in sorted(zip(ids, names))])
    tod["STATION_IDS"]   = station_ids_per_tod
    tod["STATION_NAMES"] = station_names_per_tod

    unmatched = sorted(EXPECTED_STATION_IDS - matched)
    if unmatched:
        print(f"[warn] stations not inside any TOD Special District polygon: {unmatched}")
        print("       These stations contribute parcels via walking-distance only,")
        print("       since their containing area was not adopted as a TOD district.")

    out = tod[["TOD_ID", "STATION_IDS", "STATION_NAMES", "geometry"]].copy()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_file(OUTPUT_PATH, driver="GeoJSON")

    write_manifest(
        OUTPUT_PATH,
        source_url=f"file://{DISTRICTS_PATH}",
        row_count=len(out),
        script=SCRIPT_NAME,
        extras={
            "districts_input":   str(DISTRICTS_PATH.relative_to(_ROOT)),
            "stations_input":    str(STATIONS_PATH.relative_to(_ROOT)),
            "tod_name_filter":   TOD_NAME,
            "stations_inside":   sorted(matched),
            "stations_outside":  unmatched,
            "output_crs":        f"EPSG:{WGS84}",
        },
    )

    print(f"[done] {OUTPUT_PATH.relative_to(_ROOT)} "
          f"({len(out)} TOD polygons covering stations {sorted(matched)})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the cache exists.")
    args = ap.parse_args(argv)
    return build_tod_areas(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
