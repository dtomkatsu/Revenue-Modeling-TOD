"""Quick-fix revenue script: joins RPAD asmtgis to parcels_in_walksheds
and computes rev_per_ac using tnettaxval * FALLBACK_MILLAGE / 1000.

Run from ~/repos/Revenue-Modeling-TOD/
"""
import sys
from pathlib import Path
import pandas as pd
import geopandas as gpd
import numpy as np

ROOT = Path(__file__).resolve().parent
SQ_M_PER_AC = 4046.8564224
UTM_4N = 32604
WGS84 = 4326
FALLBACK_MILLAGE = 5.70   # $/1000 net taxable value (see METHODOLOGY §3)

parcels_path = ROOT / "data/processed/parcels_in_walksheds.geojson"
rpad_path    = ROOT / "data/raw/rpad/asmtgis.csv"
out_path     = ROOT / "data/processed/parcels_revenue.geojson"

print("[read] parcels_in_walksheds.geojson")
gdf = gpd.read_file(parcels_path)
if gdf.crs is None:
    gdf = gdf.set_crs(WGS84)
elif gdf.crs.to_epsg() != WGS84:
    gdf = gdf.to_crs(WGS84)

print("[area] reprojecting to UTM 4N for true acreage")
gdf["area_ac"] = gdf.to_crs(UTM_4N).geometry.area / SQ_M_PER_AC

# Normalize TMK to 8-digit zero-padded string
def norm_tmk(v):
    try:
        s = str(int(float(v))).zfill(8)
        return s if len(s) <= 9 else s[-8:]
    except:
        return None

gdf["_tmk8"] = gdf["tmk"].map(norm_tmk)

print("[read] RPAD asmtgis.csv")
rpad = pd.read_csv(rpad_path, low_memory=False)
rpad.columns = [c.lstrip('﻿') for c in rpad.columns]
rpad = rpad[rpad["taxyr"] == 2026]
rpad["_tmk8"] = rpad["tmk"].map(norm_tmk)
rpad = rpad.dropna(subset=["_tmk8"]).drop_duplicates("_tmk8", keep="last")

print(f"[rpad] {len(rpad)} FY26 parcels in RPAD")

merged = gdf.merge(
    rpad[["_tmk8", "tnettaxval", "buildingvalue", "landvalue",
          "buildingexemption", "landexemption"]],
    on="_tmk8", how="left"
)
n_matched = merged["tnettaxval"].notna().sum()
print(f"[join] {n_matched}/{len(merged)} rows matched RPAD")

merged["annual_property_tax"] = merged["tnettaxval"].astype(float) * FALLBACK_MILLAGE / 1000.0
merged["rev_per_ac"] = np.where(
    merged["area_ac"] > 0,
    merged["annual_property_tax"] / merged["area_ac"],
    np.nan
)
merged["assessed_value"] = (
    merged["buildingvalue"].fillna(0) + merged["landvalue"].fillna(0)
).astype(float)

keep = [
    "tmk", "STATION_ID", "STATION_NAME", "area_ac",
    "annual_property_tax", "rev_per_ac", "assessed_value",
    "geometry"
]
out = merged[keep].copy()
print(f"[stats] rev_per_ac  min={out['rev_per_ac'].min():.0f}  "
      f"median={out['rev_per_ac'].median():.0f}  "
      f"max={out['rev_per_ac'].max():.0f}  "
      f"nulls={out['rev_per_ac'].isna().sum()}")

if out_path.exists():
    out_path.unlink()
out.to_file(out_path, driver="GeoJSON")

# Write a minimal manifest
import json
from datetime import datetime, timezone
m = {
    "output": out_path.name,
    "source_url": str(rpad_path),
    "row_count": len(out),
    "script": "fix_revenue.py",
    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "value_field": "tnettaxval",
    "class_field": None,
    "millage_rate": FALLBACK_MILLAGE,
    "note": "Flat fallback millage — no class field in RPAD asmtgis data"
}
(out_path.with_suffix(out_path.suffix + ".manifest.json")).write_text(
    json.dumps(m, indent=2) + "\n"
)
print(f"[done] {out_path.name} ({len(out)} rows)")
