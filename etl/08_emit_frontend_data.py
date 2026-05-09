"""Step 08 — emit final frontend data files (parcels + station markers).

Merges revenue (step 06) + cost (step 07) into ``data/parcels_tod.geojson``
with the minimum properties the MapLibre frontend (``script.js``) needs:

* ``tmk``               — parcel key (zero-padded 8-digit string)
* ``station_id``        — int 1–13, the operating Skyline station
* ``area_ac``           — parcel acreage (UTM-4N derived; see METHODOLOGY §2)
* ``rev_per_ac``        — annual property tax / acres
* ``cost_om_per_ac``    — frontage-prorated O&M only / acres
* ``cip_per_ac``        — frontage-prorated CIP only / acres (annualized 6yr-avg)
* ``cost_per_ac``       — frontage-prorated total infrastructure (O&M + CIP) / acres
* ``net_per_ac``        — rev_per_ac − cost_per_ac (i.e. rev − (O&M + CIP))
* ``frontage_road_ft``  — feet of road centerline within 5 ft of parcel
* ``frontage_sewer_ft`` — feet of sewer main (or road proxy) within 5 ft
* ``frontage_water_ft`` — feet of water main (or road proxy) within 5 ft
* ``assessed_value``    — RPAD net taxable / total assessed value (USD)
* ``land_use``          — RPAD class label that drives the millage rate
* ``landlocked``        — bool, ``frontage_road_ft < 10``

Also writes ``data/stations.geojson`` — point markers for the 13 operating
stations with ``id`` + ``name`` properties (as expected by ``script.js``).

Both outputs land in ``data/`` (NOT ``data/processed/``) so they're committed
to git and served directly by the static frontend; ``.gitignore`` only
excludes ``data/raw`` / ``data/cache`` / ``data/processed``.

The ``assessed_value`` / ``land_use`` source columns are looked up from
``parcels_revenue.geojson.manifest.json`` (written by step 06's auto-detect),
falling back to the same heuristic candidate lists if the manifest is silent.

Idempotent: skipped if both outputs and their manifests exist. Pass
``--force`` to rebuild.

Usage::

    python etl/08_emit_frontend_data.py
    python etl/08_emit_frontend_data.py --force
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import read_manifest, write_manifest  # noqa: E402


SCRIPT_NAME = "etl/08_emit_frontend_data.py"

REVENUE_PATH   = _ROOT / "data" / "processed" / "parcels_revenue.geojson"
COSTS_PATH     = _ROOT / "data" / "processed" / "parcels_costs.geojson"
STATIONS_PATH  = _ROOT / "data" / "raw"       / "rail_transit_station_points.geojson"
ADDRESSES_PATH = _ROOT / "data" / "raw"       / "address_points.geojson"
GUIDEWAY_PATH  = _ROOT / "data" / "raw"       / "rail_transit_guideway_alignment_line.geojson"

OUTPUT_PARCELS  = _ROOT / "data" / "parcels_tod.geojson"
OUTPUT_STATIONS = _ROOT / "data" / "stations.geojson"
OUTPUT_RAIL     = _ROOT / "data" / "rail_line.geojson"

WGS84 = 4326
# Hawaii Zone 3, US-survey-feet — same projected CRS as step 07 frontage work.
# Used to buffer the guideway centerline by a fixed-foot half-width so the
# resulting ribbon is uniform on the ground (lat/lng buffering distorts).
HI_FEET = 2783

# Skyline guideway is mostly a viaduct ~30 ft wide at the deck. We render it
# as a translucent fill-extrusion ribbon, so the geometry is a half-width
# buffer of the centerline. 18 ft total width (±9) reads cleanly at z14–15
# without overpowering the parcel extrusions.
RAIL_HALFWIDTH_FT = 9.0

# Guideway feature_name values that are currently operating (Segments 1+2:
# West Oahu/Farrington + Kamehameha Highway opened 2023-06; Airport opened
# 2025-10). City Center Section is still under construction as of 2026-05
# and is excluded so the ribbon ends at Kahauiki/Middle St where service
# actually ends.
OPERATING_RAIL_SECTIONS = (
    "West Oahu/Farrington Highway Section",
    "Kamehameha Highway Section",
    "Airport Section",
)

# IDs of the 13 currently operating Skyline stations (Segments 1+2). Mirrors
# etl/04_build_walksheds.EXPECTED_STATIONS keys.
OPERATING_STATION_IDS = list(range(1, 14))

# Same heuristics as step 06 — used as a fallback if the revenue manifest
# doesn't record the actual columns.
VALUE_FIELD_CANDIDATES = (
    "net_taxable_value", "nettaxablevalue", "taxable_value",
    "total_assessed_value", "totalassessed", "assessed_value", "totalvalue",
)
CLASS_FIELD_CANDIDATES = (
    "land_use",
    "taxratecode", "taxrateclass", "tax_rate_code",
    "tax_class", "property_class", "class_code", "land_use_class",
    "rpa_class", "puc", "class",
)


def _first_present(cols_lower: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    for c in candidates:
        if c in cols_lower:
            return cols_lower[c]
    return None


def _resolve_field(
    df: pd.DataFrame,
    rev_manifest: dict | None,
    manifest_key: str,
    candidates: tuple[str, ...],
) -> str | None:
    """Prefer the column step 06 recorded; fall back to candidate list."""
    if rev_manifest:
        name = rev_manifest.get(manifest_key)
        if name and name in df.columns:
            return name
    cols_lower = {c.lower(): c for c in df.columns}
    return _first_present(cols_lower, candidates)


def _coerce_number(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    cleaned = s.astype(str).str.replace(r"[\$,\s]", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def _round_coords(obj, decimals: int):
    """Recursively round float coordinates in a parsed GeoJSON object so tiny
    cross-machine float drift (different geos/proj versions) doesn't churn
    the committed artifact. ~7 decimals = 1 cm at Honolulu's latitude."""
    if isinstance(obj, list):
        if obj and all(isinstance(c, (int, float)) for c in obj):
            return [round(c, decimals) if isinstance(c, float) else c for c in obj]
        return [_round_coords(x, decimals) for x in obj]
    if isinstance(obj, dict):
        return {k: _round_coords(v, decimals) for k, v in obj.items()}
    return obj


def _serialize_fc(obj) -> str:
    """Serialize a FeatureCollection with each top-level field on its own line
    and each feature on its own line. Compact within features (no whitespace)
    so file size stays small while git diffs remain feature-scoped."""
    if obj.get("type") != "FeatureCollection":
        return json.dumps(obj, separators=(",", ":")) + "\n"
    parts = ['{']
    for k, v in obj.items():
        if k == "features":
            continue
        parts.append(f'"{k}":{json.dumps(v, separators=(",", ":"))},')
    parts.append('"features":[')
    feats = obj.get("features", [])
    for i, f in enumerate(feats):
        sep = "," if i < len(feats) - 1 else ""
        parts.append(json.dumps(f, separators=(",", ":")) + sep)
    parts.append(']}')
    return "\n".join(parts) + "\n"


def _emit_geojson_idempotent(
    gdf: gpd.GeoDataFrame, output_path: Path, *, decimals: int | None = None
) -> bool:
    """Write *gdf* as GeoJSON to *output_path*, but skip the rewrite if the
    resulting (optionally coord-rounded) content is byte-identical to the
    existing file. Returns True if the file was rewritten, False if skipped.

    Companion to a stable manifest: the caller should also gate the
    write_manifest call on the return value, so unchanged content keeps
    its original ``fetched_at``.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    if tmp.exists():
        tmp.unlink()
    gdf.to_file(tmp, driver="GeoJSON")
    obj = json.loads(tmp.read_text())
    tmp.unlink()
    # fiona derives the FeatureCollection ``name`` from the path it wrote to,
    # so the temp file leaves it as ``<stem>.geojson``. Pin it to the final
    # output's stem to match the prior on-disk convention.
    if isinstance(obj, dict) and "name" in obj:
        obj["name"] = output_path.stem
    if decimals is not None:
        obj = _round_coords(obj, decimals)
    new_text = _serialize_fc(obj)
    if output_path.exists() and output_path.read_text() == new_text:
        return False
    output_path.write_text(new_text)
    return True


def emit_frontend(*, force: bool) -> int:
    for p, hint in (
        (REVENUE_PATH,  "python etl/06_compute_revenue.py"),
        (COSTS_PATH,    "python etl/07_compute_frontage_costs.py"),
        (STATIONS_PATH, "python etl/01_fetch_arcgis.py rail_transit_station_points"),
    ):
        if not p.exists():
            raise FileNotFoundError(f"Missing {p}. Run `{hint}` first.")

    p_manifest = OUTPUT_PARCELS.with_suffix(OUTPUT_PARCELS.suffix + ".manifest.json")
    s_manifest = OUTPUT_STATIONS.with_suffix(OUTPUT_STATIONS.suffix + ".manifest.json")
    r_manifest = OUTPUT_RAIL.with_suffix(OUTPUT_RAIL.suffix + ".manifest.json")
    if (not force
            and OUTPUT_PARCELS.exists()  and p_manifest.exists()
            and OUTPUT_STATIONS.exists() and s_manifest.exists()
            and OUTPUT_RAIL.exists()     and r_manifest.exists()):
        print(f"[skip] {OUTPUT_PARCELS.name}, {OUTPUT_STATIONS.name}, "
              f"{OUTPUT_RAIL.name} (cached)")
        return 0

    rev_manifest = read_manifest(REVENUE_PATH)

    print(f"[read] {REVENUE_PATH.relative_to(_ROOT)}")
    rev = gpd.read_file(REVENUE_PATH)
    if rev.crs is None:
        rev = rev.set_crs(WGS84)
    elif rev.crs.to_epsg() != WGS84:
        rev = rev.to_crs(WGS84)
    if "tmk" not in rev.columns:
        raise KeyError(f"{REVENUE_PATH.name} missing 'tmk' field")
    if "STATION_ID" not in rev.columns:
        raise KeyError(f"{REVENUE_PATH.name} missing 'STATION_ID' field")

    print(f"[read] {COSTS_PATH.relative_to(_ROOT)}")
    cost = gpd.read_file(COSTS_PATH)
    if "tmk" not in cost.columns:
        raise KeyError(f"{COSTS_PATH.name} missing 'tmk' field")

    cost_attrs = pd.DataFrame(
        cost[[
            "tmk",
            "frontage_road_ft", "frontage_sewer_ft", "frontage_water_ft",
            "cost_om_per_ac", "cip_per_ac", "cost_per_ac", "landlocked",
        ]]
    ).drop_duplicates("tmk", keep="last")

    merged = rev.merge(cost_attrs, on="tmk", how="left")
    n_unmatched = int(merged["cost_per_ac"].isna().sum())
    if n_unmatched:
        print(f"[warn] {n_unmatched}/{len(merged)} revenue rows missing cost data after join")

    value_field = _resolve_field(merged, rev_manifest, "value_field", VALUE_FIELD_CANDIDATES)
    class_field = _resolve_field(merged, rev_manifest, "class_field", CLASS_FIELD_CANDIDATES)

    if value_field:
        merged["assessed_value"] = _coerce_number(merged[value_field])
        print(f"[field] assessed_value <- {value_field!r}")
    else:
        print("[warn] no assessed-value column found; assessed_value will be null")
        merged["assessed_value"] = pd.NA

    if class_field:
        col = merged[class_field]
        merged["land_use"] = col.where(col.notna(), None).astype("object")
        print(f"[field] land_use       <- {class_field!r}")
    else:
        print("[warn] no land-use class column found; land_use will be null")
        merged["land_use"] = None

    # ---- Addresses (modal address per TMK from address_points layer) ----
    address_by_tmk: dict[str, str] = {}
    if ADDRESSES_PATH.exists():
        print(f"[read] {ADDRESSES_PATH.relative_to(_ROOT)}")
        addr = gpd.read_file(ADDRESSES_PATH)
        # geocodeadd is the human-readable concatenated address; fall back to
        # composing one from house number + street.
        addr_cols = {c.lower(): c for c in addr.columns}
        tmk_col   = addr_cols.get("tmk")
        full_col  = addr_cols.get("geocodeadd") or addr_cols.get("full_number")
        if tmk_col and full_col:
            df = pd.DataFrame(addr[[tmk_col, full_col]]).dropna(subset=[tmk_col, full_col])
            df.columns = ["tmk_raw", "address"]
            df["tmk"] = df["tmk_raw"].astype(str).str.replace(r"\D", "", regex=True).str.zfill(8).str[-8:]
            # If multiple addresses share a TMK, keep the most common (modal)
            modal = df.groupby("tmk")["address"].agg(
                lambda s: s.mode().iloc[0] if not s.mode().empty else s.iloc[0]
            )
            address_by_tmk = modal.to_dict()
            print(f"[addr]  joined {len(address_by_tmk)} unique TMK addresses")
        else:
            print(f"[warn] address_points has no tmk/geocodeadd cols; address omitted")
    else:
        print(f"[note] {ADDRESSES_PATH.name} not present; address omitted "
              "(run `python etl/01_fetch_arcgis.py address_points` to fetch)")

    merged_tmk_str = merged["tmk"].astype(str).str.zfill(8)
    merged["address"] = merged_tmk_str.map(address_by_tmk).where(
        lambda s: s.notna(), None
    ).astype("object")

    merged["station_id"] = merged["STATION_ID"].astype(int)
    merged["net_per_ac"] = merged["rev_per_ac"] - merged["cost_per_ac"]

    # Dedupe by TMK. The spatial join in step 05 emits one row per (parcel,
    # walkshed) pair — large parcels touching multiple stations end up
    # duplicated. The frontend uses promoteId='tmk' for feature-state, so
    # duplicates would all enter hover state at once and z-fight (visible
    # flicker on cyan-on-hover). Collapse to one row per TMK with the set
    # of station_ids preserved as an array; the frontend filter uses an
    # 'in' membership test against that array.
    parcel_props = [
        "tmk", "area_ac", "rev_per_ac",
        "cost_om_per_ac", "cip_per_ac", "cost_per_ac", "net_per_ac",
        "frontage_road_ft", "frontage_sewer_ft", "frontage_water_ft",
        "assessed_value", "land_use", "address", "landlocked",
    ]
    station_lists = (
        merged.groupby("tmk")["station_id"]
              .apply(lambda s: sorted(set(int(x) for x in s)))
              .reset_index(name="station_ids")
    )
    n_before = len(merged)
    deduped = merged.drop_duplicates(subset="tmk", keep="first")
    deduped = deduped.merge(station_lists, on="tmk", how="left")
    print(f"[dedup] {n_before} rows → {len(deduped)} unique tmks "
          f"(-{n_before - len(deduped)} duplicates collapsed)")

    keep = parcel_props + ["station_ids", "geometry"]
    out_parcels = deduped[keep].copy()

    changed = _emit_geojson_idempotent(out_parcels, OUTPUT_PARCELS)
    all_stations = sorted({sid for ids in out_parcels["station_ids"] for sid in ids})
    if changed:
        write_manifest(
            OUTPUT_PARCELS,
            source_url=f"{REVENUE_PATH.relative_to(_ROOT).as_posix()} + "
                       f"{COSTS_PATH.relative_to(_ROOT).as_posix()}",
            row_count=len(out_parcels),
            script=SCRIPT_NAME,
            extras={
                "value_field":          value_field,
                "class_field":          class_field,
                "rows_unmatched_cost":  n_unmatched,
                "crs":                  f"EPSG:{WGS84}",
                "stations_represented": all_stations,
                "deduped_by_tmk":       True,
                "rows_collapsed":       n_before - len(deduped),
            },
        )
    suffix = "" if changed else " (unchanged, skipped)"
    print(f"[done] {OUTPUT_PARCELS.relative_to(_ROOT)} ({len(out_parcels)} rows){suffix}")

    # ---- Stations -------------------------------------------------------
    print(f"[read] {STATIONS_PATH.relative_to(_ROOT)}")
    stations = gpd.read_file(STATIONS_PATH)
    if stations.crs is None:
        stations = stations.set_crs(WGS84)
    elif stations.crs.to_epsg() != WGS84:
        stations = stations.to_crs(WGS84)
    if "ID" not in stations.columns or "STATION" not in stations.columns:
        raise KeyError(f"{STATIONS_PATH.name} missing ID/STATION fields")

    operating = stations[stations["ID"].isin(OPERATING_STATION_IDS)].copy()
    operating = operating.sort_values("ID").reset_index(drop=True)
    operating = operating.rename(columns={"ID": "id", "STATION": "name"})
    operating = operating[["id", "name", "geometry"]]
    operating["id"] = operating["id"].astype(int)

    changed = _emit_geojson_idempotent(operating, OUTPUT_STATIONS, decimals=7)
    if changed:
        write_manifest(
            OUTPUT_STATIONS,
            source_url=STATIONS_PATH.relative_to(_ROOT).as_posix(),
            row_count=len(operating),
            script=SCRIPT_NAME,
            extras={
                "operating_only": True,
                "station_ids":    sorted(operating["id"].tolist()),
                "crs":            f"EPSG:{WGS84}",
            },
        )
    suffix = "" if changed else " (unchanged, skipped)"
    print(f"[done] {OUTPUT_STATIONS.relative_to(_ROOT)} ({len(operating)} stations){suffix}")

    # ---- Rail line ribbon ----------------------------------------------
    # Buffer the operating Skyline guideway centerlines into a thin polygon
    # ribbon. The frontend extrudes this with fill-extrusion-base ~10 m and
    # height ~14 m, producing a translucent cyan band that sits at viaduct
    # elevation above the parcel bars (which start at ground).
    if not GUIDEWAY_PATH.exists():
        print(f"[warn] {GUIDEWAY_PATH.name} not present; skipping rail ribbon. "
              f"Run `python etl/01_fetch_arcgis.py "
              f"rail_transit_guideway_alignment_line` to fetch it.")
    else:
        print(f"[read] {GUIDEWAY_PATH.relative_to(_ROOT)}")
        guideway = gpd.read_file(GUIDEWAY_PATH)
        # ArcGIS publishes Center, Eastbound, and Westbound alignments per
        # section. Use only the Center to avoid a triple-thick ribbon.
        keep = (
            guideway["feature_name"].isin(OPERATING_RAIL_SECTIONS)
            & (guideway["feature_desc"] == "Center Alignment")
        )
        center = guideway[keep].copy()
        if center.empty:
            raise ValueError(
                "no rail-guideway features matched OPERATING_RAIL_SECTIONS; "
                "check feature_name/desc values upstream"
            )
        # Buffer in projected feet for a uniform ribbon, then dissolve so
        # the frontend gets a single MultiPolygon feature.
        ribbon = center.to_crs(HI_FEET)
        ribbon["geometry"] = ribbon.geometry.buffer(RAIL_HALFWIDTH_FT)
        dissolved = ribbon.dissolve()
        dissolved = dissolved.to_crs(WGS84)
        rail_out = gpd.GeoDataFrame(
            {"name": ["Skyline guideway"]},
            geometry=dissolved.geometry.values,
            crs=f"EPSG:{WGS84}",
        )

        changed = _emit_geojson_idempotent(rail_out, OUTPUT_RAIL, decimals=7)
        if changed:
            write_manifest(
                OUTPUT_RAIL,
                source_url=GUIDEWAY_PATH.relative_to(_ROOT).as_posix(),
                row_count=len(rail_out),
                script=SCRIPT_NAME,
                extras={
                    "halfwidth_ft":   RAIL_HALFWIDTH_FT,
                    "buffer_crs":     f"EPSG:{HI_FEET}",
                    "operating_only": True,
                    "sections":       list(OPERATING_RAIL_SECTIONS),
                    "crs":            f"EPSG:{WGS84}",
                },
            )
        suffix = "" if changed else " (unchanged, skipped)"
        print(f"[done] {OUTPUT_RAIL.relative_to(_ROOT)} "
              f"({len(center)} centerlines → 1 dissolved ribbon){suffix}")

    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the cache exists.")
    args = ap.parse_args(argv)
    return emit_frontend(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
