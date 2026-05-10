"""Step 01 — fetch ArcGIS layers from the Honolulu open-data Hub.

Resolves each Hub dataset slug → ArcGIS REST FeatureServer URL via the org's
DCAT-US 1.1 feed, then paginates the layer to ``data/raw/<name>.geojson``
with a ``.manifest.json`` sidecar.

Idempotent: a layer is skipped if both the GeoJSON and its manifest already
exist. Pass ``--force`` to refetch everything (or list specific layer names).

Usage::

    python etl/01_fetch_arcgis.py
    python etl/01_fetch_arcgis.py --force
    python etl/01_fetch_arcgis.py parcels_tax road_centerlines
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `import common.*` work whether this is run as a script or via -m.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.arcgis_client import fetch_layer, resolve_slug  # noqa: E402
from common.manifest      import write_manifest             # noqa: E402


HUB = "https://honolulu-cchnl.opendata.arcgis.com"

# Logical name → Hub dataset slug. The FeatureServer URL + layer id are
# resolved at fetch time from the Hub's DCAT feed.
LAYERS: dict[str, str] = {
    "parcels_tax":                          "cchnl::parcels-tax",
    "rail_transit_station_points":          "cchnl::rail-transit-station-points",
    "rail_transit_station_footprint":       "cchnl::rail-transit-station-footprint",
    "rail_transit_guideway_alignment_line": "cchnl::rail-transit-guideway-alignment-line",
    "land_use_oahu":                        "cchnl::2017_lud_oahu",
    "road_centerlines":                     "cchnl::oahu-street-centerlines",
    "address_points":                       "cchnl::address-points-1",
    # All 16 special districts (Chinatown, Diamond Head, Haleiwa, etc.) —
    # the 6 TOD records get filtered out downstream in etl/04.
    "zoning_special_districts":             "cchnl::zoning-special-district",
}

# BWS sewer/water — try a few likely slugs; published Honolulu BWS layers
# are mostly facilities (hydrants, pumps), not network linework, so these
# may all fail. Methodology falls back to road centerlines as a proxy when
# no main-line layer is available.
OPTIONAL_LAYERS: dict[str, list[str]] = {
    "sewer_mains":  [
        "cchnl::sewer-mains",
        "cchnl::wastewater-mains",
        "cchnl::sewers",
    ],
    "water_mains":  [
        "cchnl::water-mains",
        "cchnl::bws-water-mains",
        "cchnl::potable-water-mains",
    ],
}

DATA_DIR    = _ROOT / "data" / "raw"
SCRIPT_NAME = "etl/01_fetch_arcgis.py"


def _cache_paths(name: str) -> tuple[Path, Path]:
    out = DATA_DIR / f"{name}.geojson"
    return out, out.with_suffix(out.suffix + ".manifest.json")


def fetch_one(name: str, slug: str, *, force: bool) -> bool:
    """Fetch a single layer. Returns True on success (or cache hit)."""
    out_path, manifest_path = _cache_paths(name)
    if not force and out_path.exists() and manifest_path.exists():
        print(f"[skip] {name} (cached)")
        return True

    try:
        service_url, layer_id = resolve_slug(HUB, slug)
    except (LookupError, ValueError) as e:
        print(f"[error] {name}: cannot resolve slug {slug!r}: {e}")
        return False

    source = f"{service_url}/{layer_id}"
    print(f"[fetch] {name} <- {source}")
    try:
        fc = fetch_layer(service_url, layer_id, out_path)
    except Exception as e:
        print(f"[error] {name}: fetch failed: {e}")
        return False

    n = len(fc.get("features") or [])
    write_manifest(
        out_path,
        source_url=source,
        row_count=n,
        script=SCRIPT_NAME,
        extras={"slug": slug, "hub": HUB},
    )
    print(f"[done] {name} ({n} features)")
    return True


def fetch_optional(name: str, candidates: list[str], *, force: bool) -> None:
    """Try each candidate slug; first success wins. Failure is non-fatal."""
    out_path, manifest_path = _cache_paths(name)
    if not force and out_path.exists() and manifest_path.exists():
        print(f"[skip] {name} (cached)")
        return

    for slug in candidates:
        try:
            service_url, layer_id = resolve_slug(HUB, slug)
        except (LookupError, ValueError):
            continue
        source = f"{service_url}/{layer_id}"
        print(f"[fetch] {name} <- {source}  (slug: {slug})")
        try:
            fc = fetch_layer(service_url, layer_id, out_path)
        except Exception as e:
            print(f"[warn]  {name}: fetch via {slug!r} failed: {e}")
            continue
        n = len(fc.get("features") or [])
        write_manifest(
            out_path,
            source_url=source,
            row_count=n,
            script=SCRIPT_NAME,
            extras={"slug": slug, "hub": HUB, "optional": True},
        )
        print(f"[done] {name} ({n} features, slug={slug})")
        return

    print(f"[note] {name}: no queryable line layer found "
          f"(tried {candidates}); pipeline will fall back to road centerlines.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Refetch even if the cache exists.")
    ap.add_argument("layers", nargs="*",
                    help="Subset of layer names to fetch (default: all).")
    args = ap.parse_args(argv)

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    all_names = list(LAYERS) + list(OPTIONAL_LAYERS)
    if args.layers:
        unknown = [n for n in args.layers if n not in all_names]
        if unknown:
            print(f"[error] unknown layer(s): {unknown}", file=sys.stderr)
            print(f"        known: {all_names}",          file=sys.stderr)
            return 2
        wanted = set(args.layers)
    else:
        wanted = set(all_names)

    failures: list[str] = []
    for name, slug in LAYERS.items():
        if name not in wanted:
            continue
        if not fetch_one(name, slug, force=args.force):
            failures.append(name)

    for name, candidates in OPTIONAL_LAYERS.items():
        if name not in wanted:
            continue
        fetch_optional(name, candidates, force=args.force)

    if failures:
        print(f"[fail] {len(failures)} required layer(s) failed: {failures}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
