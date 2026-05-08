"""Step 05 — join parcels to Skyline-station walksheds.

Reads ``data/raw/parcels_tax.geojson`` and ``data/processed/walksheds.geojson``
(produced by steps 01 and 04), filters out right-of-way parcels
(``street_parcel == 1`` per the schema doc — no taxpayer of record), and
spatial-joins parcels to walksheds with ``predicate='intersects'``.

A parcel that touches multiple walksheds is **duplicated** in the output (one
row per (parcel, station) pair). Each output row carries ``STATION_ID`` and
``STATION_NAME`` from the walkshed it intersects.

Output: ``data/processed/parcels_in_walksheds.geojson`` (+ manifest sidecar).

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

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import write_manifest  # noqa: E402


SCRIPT_NAME = "etl/05_join_parcels.py"

PARCELS_PATH    = _ROOT / "data" / "raw"       / "parcels_tax.geojson"
WALKSHEDS_PATH  = _ROOT / "data" / "processed" / "walksheds.geojson"
OUTPUT_PATH     = _ROOT / "data" / "processed" / "parcels_in_walksheds.geojson"

WGS84 = 4326


def join_parcels(*, force: bool) -> int:
    if not PARCELS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {PARCELS_PATH}. Run "
            f"`python etl/01_fetch_arcgis.py parcels_tax` first."
        )
    if not WALKSHEDS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {WALKSHEDS_PATH}. Run "
            f"`python etl/04_build_walksheds.py` first."
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

    print(f"[read] {WALKSHEDS_PATH.relative_to(_ROOT)}")
    walksheds = gpd.read_file(WALKSHEDS_PATH)
    if walksheds.crs is None:
        walksheds = walksheds.set_crs(WGS84)
    elif walksheds.crs.to_epsg() != WGS84:
        walksheds = walksheds.to_crs(WGS84)

    n_parcels_total = len(parcels)
    if "street_parcel" in parcels.columns:
        rights_of_way = (parcels["street_parcel"] == 1).sum()
        parcels = parcels[parcels["street_parcel"] != 1].copy()
        print(f"[filter] dropped {rights_of_way} right-of-way parcels "
              f"(street_parcel == 1); {len(parcels)}/{n_parcels_total} remain")
    else:
        print("[warn] no 'street_parcel' field on parcels layer; skipping ROW filter")

    walksheds = walksheds[["STATION_ID", "STATION_NAME", "geometry"]]

    print(f"[sjoin] parcels ∩ walksheds  (predicate=intersects)")
    joined = gpd.sjoin(
        parcels,
        walksheds,
        how="inner",
        predicate="intersects",
    )
    joined = joined.drop(columns=["index_right"], errors="ignore")

    n_unique_parcels = joined["tmk"].nunique() if "tmk" in joined.columns else None
    n_rows           = len(joined)
    n_per_station    = joined.groupby("STATION_ID").size().to_dict()

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    joined.to_file(OUTPUT_PATH, driver="GeoJSON")

    write_manifest(
        OUTPUT_PATH,
        source_url=f"file://{PARCELS_PATH}",
        row_count=n_rows,
        script=SCRIPT_NAME,
        extras={
            "predicate":               "intersects",
            "parcels_input":           str(PARCELS_PATH.relative_to(_ROOT)),
            "walksheds_input":         str(WALKSHEDS_PATH.relative_to(_ROOT)),
            "parcels_input_total":     int(n_parcels_total),
            "parcels_after_row_filter": int(len(parcels)),
            "unique_parcels_in_join":  None if n_unique_parcels is None else int(n_unique_parcels),
            "rows_per_station":        {int(k): int(v) for k, v in n_per_station.items()},
            "filters":                 {"street_parcel != 1": True},
            "crs":                     f"EPSG:{WGS84}",
        },
    )

    print(f"[done] {OUTPUT_PATH.relative_to(_ROOT)} "
          f"({n_rows} rows; {n_unique_parcels} unique parcels across "
          f"{len(n_per_station)} stations)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the cache exists.")
    args = ap.parse_args(argv)
    return join_parcels(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
