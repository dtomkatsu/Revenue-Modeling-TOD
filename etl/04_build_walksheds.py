"""Step 04 — build half-mile walksheds around the 13 operating Skyline stations.

Reads ``data/raw/rail_transit_station_points.geojson`` (fetched by step 01),
filters to Segments 1+2 (the currently operating stations: IDs 1–13), buffers
each station point by 1.0 mi (1609.344 m) in EPSG:32604 (UTM Zone 4N) so the
buffer is in true meters, and reprojects the resulting polygons back to
WGS84 for the output ``data/processed/walksheds.geojson``.

Each output feature carries ``STATION_ID`` (the source ``ID`` int) and
``STATION_NAME`` (the source ``STATION`` string, with diacritics preserved).

Idempotent: skipped if the output and its manifest already exist. Pass
``--force`` to rebuild.

Usage::

    python etl/04_build_walksheds.py
    python etl/04_build_walksheds.py --force
"""

from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

import geopandas as gpd

# Make `import common.*` work whether this is run as a script or via -m.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import write_manifest  # noqa: E402


SCRIPT_NAME = "etl/04_build_walksheds.py"

INPUT_PATH  = _ROOT / "data" / "raw"       / "rail_transit_station_points.geojson"
OUTPUT_PATH = _ROOT / "data" / "processed" / "walksheds.geojson"

# Half-mile buffer in meters (1 mi = 1609.344 m).
BUFFER_METERS = 1.0 * 1609.344  # 1609.344 m (1.0 mi)

UTM_4N = 32604  # meters, true-area CRS for Oʻahu
WGS84  = 4326

# Currently operating stations: Segments 1 (East Kapolei → Aloha Stadium)
# and 2 (Pearl Harbor → Middle Street). Their ``ID`` values in the source
# layer are 1–13. Names are the Hawaiian-language official names from the
# source ``STATION`` field; the ASCII-folded form is what we match against
# (the source uses ʻokina + kahakō which won't survive copy/paste reliably).
EXPECTED_STATIONS: dict[int, str] = {
    1:  "kualakai station",
    2:  "keoneae station",
    3:  "honouliuli station",
    4:  "hoaeae station",
    5:  "pouhala station",
    6:  "halaulani station",
    7:  "waiawa station",
    8:  "kalauao station",
    9:  "halawa station",
    10: "makalapa station",
    11: "lelepaua station",
    12: "ahua station",
    13: "kahauiki station",  # aka Middle Street
}


def _ascii_fold(s: str) -> str:
    """Lowercase, strip Hawaiian ʻokina / kahakō, collapse whitespace."""
    if s is None:
        return ""
    # NFKD decomposes accented chars; then drop combining marks and ʻokina.
    decomposed = unicodedata.normalize("NFKD", s)
    cleaned = "".join(
        ch for ch in decomposed
        if unicodedata.category(ch) != "Mn" and ch not in "ʻ'’`"
    )
    return " ".join(cleaned.lower().split())


def build_walksheds(*, force: bool) -> int:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(
            f"Missing {INPUT_PATH}. Run "
            f"`python etl/01_fetch_arcgis.py rail_transit_station_points` first."
        )

    manifest_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".manifest.json")
    if not force and OUTPUT_PATH.exists() and manifest_path.exists():
        print(f"[skip] {OUTPUT_PATH.name} (cached)")
        return 0

    stations = gpd.read_file(INPUT_PATH)
    if stations.crs is None:
        stations = stations.set_crs(WGS84)
    elif stations.crs.to_epsg() != WGS84:
        stations = stations.to_crs(WGS84)

    if "ID" not in stations.columns or "STATION" not in stations.columns:
        raise KeyError(
            f"Expected fields 'ID' and 'STATION' in {INPUT_PATH.name}; "
            f"got {list(stations.columns)}"
        )

    operating = stations[stations["ID"].isin(EXPECTED_STATIONS)].copy()
    operating = operating.sort_values("ID").reset_index(drop=True)

    # Verify the IDs we got match the names we expect. If the upstream layer
    # ever renumbers stations, this is the trip-wire that catches it.
    missing = sorted(set(EXPECTED_STATIONS) - set(operating["ID"].tolist()))
    if missing:
        raise ValueError(f"Source layer is missing expected station IDs: {missing}")

    mismatches: list[str] = []
    for _, row in operating.iterrows():
        sid     = int(row["ID"])
        got     = _ascii_fold(row["STATION"])
        want    = EXPECTED_STATIONS[sid]
        if got != want:
            mismatches.append(f"  ID {sid}: expected {want!r}, got {got!r} (raw: {row['STATION']!r})")
    if mismatches:
        raise ValueError(
            "Station-name mismatch between source and EXPECTED_STATIONS:\n"
            + "\n".join(mismatches)
        )

    # Buffer in true meters, then reproject back to WGS84 for the output.
    buffered = operating.to_crs(UTM_4N).copy()
    buffered["geometry"] = buffered.geometry.buffer(BUFFER_METERS)
    walksheds = buffered.to_crs(WGS84)

    walksheds = walksheds[["ID", "STATION", "geometry"]].rename(
        columns={"ID": "STATION_ID", "STATION": "STATION_NAME"}
    )
    walksheds["STATION_ID"] = walksheds["STATION_ID"].astype(int)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    walksheds.to_file(OUTPUT_PATH, driver="GeoJSON")

    write_manifest(
        OUTPUT_PATH,
        source_url=f"file://{INPUT_PATH}",
        row_count=len(walksheds),
        script=SCRIPT_NAME,
        extras={
            "buffer_meters":  BUFFER_METERS,
            "buffer_crs":     f"EPSG:{UTM_4N}",
            "output_crs":     f"EPSG:{WGS84}",
            "station_count":  len(walksheds),
        },
    )

    print(f"[done] {OUTPUT_PATH.relative_to(_ROOT)} ({len(walksheds)} walksheds, "
          f"{BUFFER_METERS:.3f} m buffer in EPSG:{UTM_4N})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the cache exists.")
    args = ap.parse_args(argv)
    return build_walksheds(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
