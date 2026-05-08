"""Fix revenue: joins RPAD asmtgis to parcels_in_walksheds and computes
rev_per_ac using class-specific FY2026 Honolulu millage rates.

Tax class detection tries a list of candidate column names (including
'taxratecode', the RPAD standard). Falls back to a flat rate if the class
column is absent, printing a warning.

Residential A tier 2 (tnettaxval > $1M) applies $11.40 on the excess.

Run from ~/repos/Revenue-Modeling-TOD/
"""
import re
import sys
from pathlib import Path
import pandas as pd
import geopandas as gpd
import numpy as np

ROOT = Path(__file__).resolve().parent
SQ_M_PER_AC = 4046.8564224
UTM_4N  = 32604
WGS84   = 4326

PARCELS_IN = ROOT / "data/processed/parcels_in_walksheds.geojson"
RPAD_PATH  = ROOT / "data/raw/rpad/asmtgis.csv"
OUT_PATH   = ROOT / "data/processed/parcels_revenue.geojson"

# FY2026 Honolulu RPT rates $/1,000 net taxable value.
# Source: City & County Honolulu FY2026 RPT ordinance.
MILLAGE: dict[str, float] = {
    # Single-letter RPAD tax rate codes
    "a":    3.50,   # Residential (homeowner with exemption)
    "aa":   4.50,   # Residential A (non-owner; tier-2 handled separately)
    "b":   11.40,   # Apartment
    "c":   12.40,   # Commercial
    "d":   12.40,   # Industrial
    "e":    5.70,   # Agricultural
    "f":    5.70,   # Preservation
    "g":   13.90,   # Hotel and Resort
    "h":    9.85,   # Vacation Rental / Transient Accommodation Rental
    "i":    9.35,   # Residential Investor
    "j":    6.50,   # Bed and Breakfast
    "x":    0.00,   # Exempt / Public Service
    "p":    0.00,   # Public Service (alias)
    # Text equivalents for asmtpitt-style class names
    "residential":                3.50,
    "residential a":              4.50,
    "apartment":                 11.40,
    "commercial":                12.40,
    "industrial":                12.40,
    "agricultural":               5.70,
    "preservation":               5.70,
    "hotel and resort":          13.90,
    "hotel/resort":              13.90,
    "vacation rental":            9.85,
    "transient accommodations":   9.85,
    "transient accommodations rental": 9.85,
    "tar":                        9.85,
    "bed and breakfast":          6.50,
    "residential investor":       9.35,
    "exempt":                     0.00,
    "public service":             0.00,
}

# Residential A tier-2 kicks in above this net taxable value
RA_TIER2_THRESHOLD = 1_000_000.0
RA_TIER2_RATE      = 11.40   # $/1k on the excess

# Candidate column names (lowercased) to detect the tax class
CLASS_CANDIDATES = (
    "taxratecode", "taxrateclass", "tax_rate_code", "tax_class",
    "taxclass", "property_class", "class_code", "land_use_class",
    "rpa_class", "puc", "class", "class1", "luc", "lucode",
    "struc_class", "property_type",
)

FALLBACK_MILLAGE = 4.50  # median residential-A rate if class unknown
FALLBACK_NOTE    = ("No tax class column found in RPAD; used flat "
                    f"${FALLBACK_MILLAGE}/1k (Residential A tier-1 fallback). "
                    "Inspect RPAD columns and add the class field name to "
                    "CLASS_CANDIDATES in fix_revenue.py.")


def norm_code(v):
    """Lowercase, strip punctuation and extra whitespace."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def norm_tmk(v):
    """8-digit zero-padded TMK; strip county prefix (1) if 9-digit."""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    digits = re.sub(r"\D", "", str(v))
    if not digits:
        return None
    if len(digits) == 9 and digits.startswith("1"):
        digits = digits[1:]
    return digits.zfill(8) if digits else None


def compute_ra_tax(tnettaxval: pd.Series, class_code: pd.Series) -> pd.Series:
    """Compute tax applying Residential-A tier-2 premium above $1M."""
    base_rate = class_code.map(MILLAGE)
    tax = tnettaxval * base_rate / 1000.0
    ra_mask = class_code.isin(["aa", "residential a"])
    excess = (tnettaxval - RA_TIER2_THRESHOLD).clip(lower=0)
    tax_tier2 = excess * (RA_TIER2_RATE - MILLAGE.get("aa", 4.50)) / 1000.0
    tax = tax + np.where(ra_mask, tax_tier2, 0.0)
    return tax


print("[read] parcels_in_walksheds.geojson")
gdf = gpd.read_file(PARCELS_IN)
if gdf.crs is None:
    gdf = gdf.set_crs(WGS84)
elif gdf.crs.to_epsg() != WGS84:
    gdf = gdf.to_crs(WGS84)

print("[area] reprojecting to UTM 4N for true acreage")
gdf["area_ac"] = gdf.to_crs(UTM_4N).geometry.area / SQ_M_PER_AC
gdf["_tmk8"]   = gdf["tmk"].map(norm_tmk)

print("[read] RPAD asmtgis.csv")
rpad = pd.read_csv(RPAD_PATH, dtype=str, low_memory=False)
rpad.columns = [c.lstrip("﻿").strip() for c in rpad.columns]
print(f"  RPAD columns ({len(rpad.columns)}): {list(rpad.columns)}")

# Filter to FY2026
if "taxyr" in rpad.columns:
    rpad = rpad[rpad["taxyr"].astype(str).str.strip() == "2026"]

rpad["_tmk8"] = rpad["tmk"].map(norm_tmk)
rpad = rpad.dropna(subset=["_tmk8"]).drop_duplicates("_tmk8", keep="last")
print(f"  {len(rpad)} FY26 RPAD rows after dedup")

# Auto-detect class column
rpad_cols_lower = {c.lower(): c for c in rpad.columns}
class_col = next((rpad_cols_lower[c] for c in CLASS_CANDIDATES
                  if c in rpad_cols_lower), None)
if class_col:
    print(f"  Found tax class column: {class_col!r}")
    unique_codes = rpad[class_col].dropna().unique()[:20]
    print(f"  Unique class codes: {list(unique_codes)}")
else:
    print(f"[warn] {FALLBACK_NOTE}")

# Select columns to merge
keep_cols = ["_tmk8", "tnettaxval", "buildingvalue", "landvalue",
             "buildingexemption", "landexemption"]
if class_col:
    keep_cols.append(class_col)
keep_cols = [c for c in keep_cols if c in rpad.columns]
rpad_sub = rpad[keep_cols].copy()

merged = gdf.merge(rpad_sub, on="_tmk8", how="left")
n_matched = merged["tnettaxval"].notna().sum() if "tnettaxval" in merged.columns else 0
print(f"[join] {n_matched}/{len(merged)} rows matched RPAD")

# Numeric value
tnettaxval = pd.to_numeric(
    merged["tnettaxval"].astype(str).str.replace(r"[\$,\s]", "", regex=True),
    errors="coerce"
) if "tnettaxval" in merged.columns else pd.Series(np.nan, index=merged.index)

# Compute tax
if class_col:
    code_norm = merged[class_col].map(norm_code)
    unmatched = code_norm[code_norm.notna() & ~code_norm.isin(MILLAGE)].unique()
    if len(unmatched):
        print(f"[warn] {len(unmatched)} class code(s) not in MILLAGE table: {list(unmatched)}")
    annual_tax = compute_ra_tax(tnettaxval, code_norm)
    method = f"class-specific millage via {class_col}"
    land_use = code_norm.copy()
else:
    annual_tax = tnettaxval * FALLBACK_MILLAGE / 1000.0
    method = f"flat {FALLBACK_MILLAGE}/1k (no class field)"
    land_use = pd.Series(None, index=merged.index)

merged["annual_property_tax"] = annual_tax
merged["rev_per_ac"] = np.where(
    merged["area_ac"] > 0,
    merged["annual_property_tax"] / merged["area_ac"],
    np.nan,
)
merged["assessed_value"] = (
    pd.to_numeric(merged.get("buildingvalue", pd.Series(0, index=merged.index))
                  .astype(str).str.replace(r"[\$,\s]", "", regex=True), errors="coerce").fillna(0)
  + pd.to_numeric(merged.get("landvalue", pd.Series(0, index=merged.index))
                  .astype(str).str.replace(r"[\$,\s]", "", regex=True), errors="coerce").fillna(0)
)
merged["land_use"] = land_use

keep = ["tmk", "STATION_ID", "STATION_NAME", "area_ac",
        "annual_property_tax", "rev_per_ac", "assessed_value",
        "land_use", "geometry"]
out = merged[[c for c in keep if c in merged.columns]].copy()

print(f"[stats] method={method}")
print(f"  rev_per_ac: min={out['rev_per_ac'].min():.0f}  "
      f"median={out['rev_per_ac'].median():.0f}  "
      f"max={out['rev_per_ac'].max():.0f}  "
      f"nulls={out['rev_per_ac'].isna().sum()}")

if OUT_PATH.exists():
    OUT_PATH.unlink()
out.to_file(OUT_PATH, driver="GeoJSON")

import json
from datetime import datetime, timezone
manifest = {
    "output": OUT_PATH.name,
    "source_url": str(RPAD_PATH),
    "row_count": len(out),
    "script": "fix_revenue.py",
    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "method": method,
    "class_field": class_col,
    "fallback_millage": FALLBACK_MILLAGE if not class_col else None,
    "ra_tier2_threshold_usd": RA_TIER2_THRESHOLD,
    "ra_tier2_rate": RA_TIER2_RATE,
}
OUT_PATH.with_suffix(OUT_PATH.suffix + ".manifest.json").write_text(
    json.dumps(manifest, indent=2) + "\n"
)
print(f"[done] {OUT_PATH.name} ({len(out)} rows)")
