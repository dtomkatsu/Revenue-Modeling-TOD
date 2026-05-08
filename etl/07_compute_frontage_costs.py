"""Step 07 — compute frontage-based infrastructure cost per parcel.

Implements the "Urban3 classic" frontage proration described in
[METHODOLOGY.md §4](../METHODOLOGY.md):

* Reproject parcels and line networks to **EPSG:2783** (HI State Plane Z3,
  US-survey-feet) so length comes out in true feet without unit conversion.
* For each unique parcel, ``parcel.buffer(5_ft)`` is the snap tolerance for
  "adjacent". Frontage along each utility = sum of intersection lengths of
  that buffer with the relevant LineStrings.
* Citywide per-foot rates are derived once:

  ``rate_$/ft = budget_total_$ / total_centerline_ft_citywide``

  with the budget totals extracted in step 03 and the denominators from the
  ArcGIS line layers fetched in step 01 (after filters).

Filters applied to ``data/raw/road_centerlines.geojson`` before any rate /
frontage computation:

1. **Skyline guideway exclusion** — load
   ``data/raw/rail_transit_guideway_alignment_line.geojson``, buffer by 10 ft,
   and drop any road centerline whose intersection with that buffer covers
   ≥50% of its length. (A perpendicular crossing should keep its length; a
   centerline that's mostly co-located with the guideway should not.)
2. **State-owned freeway exclusion** — drop centerlines whose ownership /
   jurisdiction field flags them as state-maintained (HDOT, freeway,
   interstate). The actual field name on the centerlines layer is auto-
   discovered from a fixed candidate list; failure to find one is logged.

Sewer / water fallback (per METHODOLOGY.md §4.3): if the BWS line layer is
not present (only facilities are published, not network linework), road
centerlines are used as a proxy and the same rate denominator is used.

Landlocked parcels (``frontage_road_ft < 10``) are flagged with
``landlocked=true`` for downstream inspection.

Output: ``data/processed/parcels_costs.geojson`` with one row **per unique
TMK** (frontage doesn't depend on which station the parcel sits in;
step 08 broadcasts these values onto each parcel-station row).

Idempotent: skipped if the output and manifest exist. Pass ``--force`` to rebuild.

Usage::

    python etl/07_compute_frontage_costs.py
    python etl/07_compute_frontage_costs.py --force
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.strtree import STRtree

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import write_manifest  # noqa: E402


SCRIPT_NAME = "etl/07_compute_frontage_costs.py"

PARCELS_IN_PATH = _ROOT / "data" / "processed" / "parcels_in_walksheds.geojson"
BUDGET_PATH     = _ROOT / "data" / "processed" / "budget_totals.json"
ROADS_PATH      = _ROOT / "data" / "raw"       / "road_centerlines.geojson"
GUIDEWAY_PATH   = _ROOT / "data" / "raw"       / "rail_transit_guideway_alignment_line.geojson"
SEWER_PATH      = _ROOT / "data" / "raw"       / "sewer_mains.geojson"
WATER_PATH      = _ROOT / "data" / "raw"       / "water_mains.geojson"
OUTPUT_PATH     = _ROOT / "data" / "processed" / "parcels_costs.geojson"

WGS84            = 4326
UTM_4N           = 32604
HI_STATE_PLANE_3 = 2783        # US-survey-feet
SQ_M_PER_AC      = 4046.8564224

BUFFER_FT             = 50.0
GUIDEWAY_BUFFER_FT    = 10.0
GUIDEWAY_OVERLAP_FRAC = 0.5
LANDLOCKED_FT         = 10.0

# Candidate field names (lowercased) for road ownership / jurisdiction.
ROAD_OWNER_CANDIDATES = (
    "ownership", "jurisdiction", "owner", "owner_type", "ownertype",
    "maintained_by", "maintain_by", "maintainer", "owned_by", "agency",
    "juris", "fclass", "func_class", "funcclass",
)
# Substrings that indicate state / DOT ownership in the value of that field.
STATE_OWNER_TOKENS = (
    "state", "hdot", "hi-dot", "hi dot", "hidot",
    "department of transportation", "dot",
    "freeway", "interstate", "fwy",
    "h-1", "h-2", "h-3", "h1", "h2", "h3",
)


def _load_lines_proj(path: Path) -> gpd.GeoDataFrame | None:
    """Read a GeoJSON of lines, set/convert CRS, reproject to HI State Plane Z3."""
    if not path.exists():
        return None
    gdf = gpd.read_file(path)
    if gdf.crs is None:
        gdf = gdf.set_crs(WGS84)
    return gdf.to_crs(HI_STATE_PLANE_3)


def _filter_guideway(roads: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    """Drop road centerlines whose ≥50% length sits within 10 ft of the
    Skyline guideway. No-op (with warning) if the guideway file is missing."""
    if not GUIDEWAY_PATH.exists():
        print(f"[warn] {GUIDEWAY_PATH.relative_to(_ROOT)} not found; "
              "Skyline guideway will NOT be filtered out of road centerlines")
        return roads, 0

    guideway = gpd.read_file(GUIDEWAY_PATH)
    if guideway.crs is None:
        guideway = guideway.set_crs(WGS84)
    guideway = guideway.to_crs(HI_STATE_PLANE_3)

    g_buf = guideway.geometry.unary_union.buffer(GUIDEWAY_BUFFER_FT)

    candidates_mask = roads.geometry.intersects(g_buf)
    candidates = roads[candidates_mask]
    if candidates.empty:
        return roads, 0

    overlap = candidates.geometry.apply(
        lambda g: g.intersection(g_buf).length / max(g.length, 1e-9)
    )
    drop_idx = candidates.index[overlap >= GUIDEWAY_OVERLAP_FRAC]
    filtered = roads.drop(index=drop_idx)
    print(f"[filter] dropped {len(drop_idx)} road centerlines overlapping the "
          f"Skyline guideway ≥{GUIDEWAY_OVERLAP_FRAC:.0%} (within "
          f"{GUIDEWAY_BUFFER_FT:.0f}-ft buffer)")
    return filtered, len(drop_idx)


def _filter_state_owned(
    roads: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, str | None, int]:
    """Drop centerlines whose owner/jurisdiction column flags them as
    state-maintained. Returns the filtered frame plus the field name we used."""
    cols_lower = {c.lower(): c for c in roads.columns}
    owner_col = next(
        (cols_lower[c] for c in ROAD_OWNER_CANDIDATES if c in cols_lower),
        None,
    )
    if owner_col is None:
        print(f"[warn] road centerlines have no ownership/jurisdiction field; "
              f"DOT freeways will NOT be filtered. Searched: {ROAD_OWNER_CANDIDATES}")
        return roads, None, 0

    vals = roads[owner_col].astype(str).str.lower().str.strip()
    is_state = vals.apply(lambda v: any(tok in v for tok in STATE_OWNER_TOKENS))
    n_dropped = int(is_state.sum())
    filtered = roads[~is_state].copy()
    print(f"[filter] dropped {n_dropped} state-owned roads on field {owner_col!r}")
    return filtered, owner_col, n_dropped


def _frontage(parcel_buf, geoms_arr, tree: STRtree) -> float:
    """Sum the lengths of (parcel_buf ∩ line) over every line in ``tree`` that
    intersects ``parcel_buf``. Result in the input CRS's units (feet)."""
    idxs = tree.query(parcel_buf, predicate="intersects")
    if len(idxs) == 0:
        return 0.0
    total = 0.0
    for j in idxs:
        total += parcel_buf.intersection(geoms_arr[j]).length
    return total


def _rate(num, denom_ft) -> float | None:
    if num is None or denom_ft is None or denom_ft <= 0:
        return None
    return float(num) / float(denom_ft)


def compute_frontage_costs(*, force: bool) -> int:
    if not PARCELS_IN_PATH.exists():
        raise FileNotFoundError(
            f"Missing {PARCELS_IN_PATH}. Run `python etl/05_join_parcels.py` first."
        )
    if not ROADS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {ROADS_PATH}. Run "
            f"`python etl/01_fetch_arcgis.py road_centerlines` first."
        )
    if not BUDGET_PATH.exists():
        raise FileNotFoundError(
            f"Missing {BUDGET_PATH}. Run "
            f"`python etl/03_extract_budget_totals.py` first."
        )

    manifest_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".manifest.json")
    if not force and OUTPUT_PATH.exists() and manifest_path.exists():
        print(f"[skip] {OUTPUT_PATH.name} (cached)")
        return 0

    budget = json.loads(BUDGET_PATH.read_text())

    print(f"[read] {PARCELS_IN_PATH.relative_to(_ROOT)}")
    parcels = gpd.read_file(PARCELS_IN_PATH)
    if parcels.crs is None:
        parcels = parcels.set_crs(WGS84)
    elif parcels.crs.to_epsg() != WGS84:
        parcels = parcels.to_crs(WGS84)
    if "tmk" not in parcels.columns:
        raise KeyError(f"{PARCELS_IN_PATH.name} missing 'tmk' field")

    n_parcel_rows = len(parcels)
    parcels_unique = parcels.drop_duplicates("tmk").reset_index(drop=True)
    n_unique = len(parcels_unique)
    print(f"[dedupe] {n_unique} unique parcels (from {n_parcel_rows} parcel-station rows)")

    print(f"[area] reproject to EPSG:{UTM_4N} for true m²")
    parcels_unique["area_ac"] = (
        parcels_unique.to_crs(UTM_4N).geometry.area / SQ_M_PER_AC
    )

    print(f"[reproj] parcels to EPSG:{HI_STATE_PLANE_3} (US-survey-feet)")
    parcels_ft = parcels_unique.to_crs(HI_STATE_PLANE_3)
    parcel_bufs = parcels_ft.geometry.buffer(BUFFER_FT)

    # ---- Roads: load → filter guideway → filter state-owned --------------
    print(f"[read] {ROADS_PATH.relative_to(_ROOT)}")
    roads = _load_lines_proj(ROADS_PATH)
    n_road_in = len(roads)
    roads, n_dropped_guideway = _filter_guideway(roads)
    roads, road_owner_field, n_dropped_state = _filter_state_owned(roads)
    n_road_kept = len(roads)
    print(f"[roads] kept {n_road_kept}/{n_road_in} centerlines after filters")

    road_geoms = roads.geometry.to_numpy()
    road_tree  = STRtree(road_geoms)
    total_road_ft = float(roads.geometry.length.sum())

    # ---- Sewer: actual layer if present, else fallback to roads ---------
    sewer_proj = _load_lines_proj(SEWER_PATH)
    if sewer_proj is None:
        print(f"[note] {SEWER_PATH.name} not found; using filtered road "
              "centerlines as sewer proxy (per METHODOLOGY §4.3)")
        sewer_geoms, sewer_tree = road_geoms, road_tree
        total_sewer_ft = total_road_ft
        sewer_source   = "road_centerlines (proxy)"
    else:
        sewer_geoms = sewer_proj.geometry.to_numpy()
        sewer_tree  = STRtree(sewer_geoms)
        total_sewer_ft = float(sewer_proj.geometry.length.sum())
        sewer_source   = str(SEWER_PATH.relative_to(_ROOT))

    water_proj = _load_lines_proj(WATER_PATH)
    if water_proj is None:
        print(f"[note] {WATER_PATH.name} not found; using filtered road "
              "centerlines as water proxy (per METHODOLOGY §4.3)")
        water_geoms, water_tree = road_geoms, road_tree
        total_water_ft = total_road_ft
        water_source   = "road_centerlines (proxy)"
    else:
        water_geoms = water_proj.geometry.to_numpy()
        water_tree  = STRtree(water_geoms)
        total_water_ft = float(water_proj.geometry.length.sum())
        water_source   = str(WATER_PATH.relative_to(_ROOT))

    road_rate  = _rate(budget.get("road_om_total_usd"),  total_road_ft)
    sewer_rate = _rate(budget.get("sewer_om_total_usd"), total_sewer_ft)
    water_rate = _rate(budget.get("water_om_total_usd"), total_water_ft)

    def _fmt_rate(r): return f"${r:,.4f}/ft" if r is not None else "n/a"
    print(f"[rates] road  = {_fmt_rate(road_rate)}  "
          f"({budget.get('road_om_total_usd')!r} / {total_road_ft:,.0f} ft)")
    print(f"[rates] sewer = {_fmt_rate(sewer_rate)} "
          f"({budget.get('sewer_om_total_usd')!r} / {total_sewer_ft:,.0f} ft)")
    print(f"[rates] water = {_fmt_rate(water_rate)} "
          f"({budget.get('water_om_total_usd')!r} / {total_water_ft:,.0f} ft)")

    # ---- Per-parcel frontage --------------------------------------------
    print(f"[frontage] computing for {n_unique} parcels...")
    road_ft  = np.zeros(n_unique)
    sewer_ft = np.zeros(n_unique)
    water_ft = np.zeros(n_unique)
    sewer_is_road = sewer_geoms is road_geoms
    water_is_road = water_geoms is road_geoms

    for i, buf in enumerate(parcel_bufs):
        if (i + 1) % 5000 == 0:
            print(f"  ... {i + 1}/{n_unique}")
        road_ft[i]  = _frontage(buf, road_geoms, road_tree)
        sewer_ft[i] = road_ft[i] if sewer_is_road else _frontage(buf, sewer_geoms, sewer_tree)
        water_ft[i] = road_ft[i] if water_is_road else _frontage(buf, water_geoms, water_tree)

    parcels_unique["frontage_road_ft"]  = road_ft
    parcels_unique["frontage_sewer_ft"] = sewer_ft
    parcels_unique["frontage_water_ft"] = water_ft
    parcels_unique["landlocked"]        = road_ft < LANDLOCKED_FT

    nan_arr = np.full(n_unique, np.nan)
    cost_road  = road_ft  * road_rate  if road_rate  is not None else nan_arr
    cost_sewer = sewer_ft * sewer_rate if sewer_rate is not None else nan_arr
    cost_water = water_ft * water_rate if water_rate is not None else nan_arr

    parcels_unique["cost_road_usd"]  = cost_road
    parcels_unique["cost_sewer_usd"] = cost_sewer
    parcels_unique["cost_water_usd"] = cost_water

    stacked = np.stack([cost_road, cost_sewer, cost_water])
    cost_total = np.nansum(stacked, axis=0)
    cost_total[np.all(np.isnan(stacked), axis=0)] = np.nan
    parcels_unique["cost_total_usd"] = cost_total

    area = parcels_unique["area_ac"].to_numpy()
    parcels_unique["cost_per_ac"] = np.where(area > 0, cost_total / area, np.nan)

    keep_cols = [
        "tmk", "area_ac",
        "frontage_road_ft", "frontage_sewer_ft", "frontage_water_ft",
        "cost_road_usd", "cost_sewer_usd", "cost_water_usd",
        "cost_total_usd", "cost_per_ac", "landlocked",
        "geometry",
    ]
    parcels_out = parcels_unique[keep_cols].set_geometry("geometry")
    if parcels_out.crs is None or parcels_out.crs.to_epsg() != WGS84:
        parcels_out = parcels_out.to_crs(WGS84)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT_PATH.exists():
        OUTPUT_PATH.unlink()
    parcels_out.to_file(OUTPUT_PATH, driver="GeoJSON")

    n_landlocked = int(parcels_unique["landlocked"].sum())
    write_manifest(
        OUTPUT_PATH,
        source_url=f"file://{PARCELS_IN_PATH}",
        row_count=n_unique,
        script=SCRIPT_NAME,
        extras={
            "frontage_crs":              f"EPSG:{HI_STATE_PLANE_3}",
            "area_crs":                  f"EPSG:{UTM_4N}",
            "sq_m_per_acre":             SQ_M_PER_AC,
            "parcel_buffer_ft":          BUFFER_FT,
            "guideway_buffer_ft":        GUIDEWAY_BUFFER_FT,
            "guideway_overlap_threshold":GUIDEWAY_OVERLAP_FRAC,
            "landlocked_threshold_ft":   LANDLOCKED_FT,
            "roads_input":               str(ROADS_PATH.relative_to(_ROOT)),
            "roads_input_count":         int(n_road_in),
            "roads_dropped_guideway":    int(n_dropped_guideway),
            "roads_dropped_state_owned": int(n_dropped_state),
            "road_owner_field":          road_owner_field,
            "roads_kept":                int(n_road_kept),
            "sewer_source":              sewer_source,
            "water_source":              water_source,
            "total_road_centerline_ft":  total_road_ft,
            "total_sewer_main_ft":       total_sewer_ft,
            "total_water_main_ft":       total_water_ft,
            "rate_road_usd_per_ft":      road_rate,
            "rate_sewer_usd_per_ft":     sewer_rate,
            "rate_water_usd_per_ft":     water_rate,
            "budget_road_usd":           budget.get("road_om_total_usd"),
            "budget_sewer_usd":          budget.get("sewer_om_total_usd"),
            "budget_water_usd":          budget.get("water_om_total_usd"),
            "parcels_input_rows":        int(n_parcel_rows),
            "unique_parcels":            int(n_unique),
            "landlocked_parcels":        n_landlocked,
            "sum_road_frontage_ft":      float(road_ft.sum()),
            "sum_sewer_frontage_ft":     float(sewer_ft.sum()),
            "sum_water_frontage_ft":     float(water_ft.sum()),
        },
    )

    print(f"[done] {OUTPUT_PATH.relative_to(_ROOT)} "
          f"({n_unique} parcels, {n_landlocked} landlocked)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the cache exists.")
    args = ap.parse_args(argv)
    return compute_frontage_costs(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
