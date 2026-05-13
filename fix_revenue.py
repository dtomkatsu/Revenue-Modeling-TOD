"""Fix revenue: joins RPAD assessment data to parcels_in_walksheds and
computes rev_per_ac using class-specific FY2026 Honolulu millage rates.

Tax class detection tries a list of candidate column names (including
'taxratecode', the RPAD standard) on data/raw/rpad/asmtgis.csv. If the
column is missing or fully empty (asmtgis.csv ships taxratecode but it
is NaN in every FY2026 row), we fall back to data/raw/rpad/asmtpitt.csv,
which carries the same schema with the class field populated.

Per-TMK aggregation: a TMK that holds N condo units appears as N suffix
rows in RPAD. We sum tnettaxval/buildingvalue/landvalue across the
suffixes and take the modal taxratecode/ovrclass — keeping a single row
per TMK is wrong for condos.

ovrclass=11 = Residential A (verified by tnettaxval distribution).
Residential A tier 2 ($11.40 on tnettaxval excess above $1M) is applied.

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

PARCELS_IN     = ROOT / "data/processed/parcels_in_walksheds.geojson"
RPAD_PRIMARY   = ROOT / "data/raw/rpad/asmtgis.csv"
RPAD_FALLBACK  = ROOT / "data/raw/rpad/asmtpitt.csv"
OUT_PATH       = ROOT / "data/processed/parcels_revenue.geojson"

# FY2026 Honolulu RPT rates $/1,000 net taxable value.
# Source: Honolulu Resolution 2575 / Ordinance 25-44 (FY July 1, 2025 –
# June 30, 2026). Honolulu has nine taxed classes; Preservation and Public
# Service are land-use designations on RPAD records but are not taxed.
RATE = {
    "Residential":              3.50,
    "Residential A":            4.00,   # tier-1; tier-2 above $1M handled separately
    "Commercial":              12.40,
    "Industrial":              12.40,
    "Agricultural":             5.70,
    "Vacant Agricultural":      8.50,
    "Hotel and Resort":        13.90,
    "Bed and Breakfast Home":   6.50,
    "Preservation":             0.00,   # land use only, not a taxed class
    "Public Service":           0.00,   # land use only, not a taxed class
}

# RPAD numeric taxratecode -> human-readable class. Authoritative mapping
# from the City & County of Honolulu RPAD "Land Use Codes" reference
# (https://realproperty.honolulu.gov/media/eombhyp2/zoning.pdf, retrieved
# 2026-05-12). Class/Tax Code is the second column of that table.
NUMERIC_CODE = {
    "0":  "Vacant Agricultural",
    "1":  "Residential",
    "3":  "Commercial",
    "4":  "Industrial",
    "5":  "Agricultural",
    "6":  "Preservation",
    "7":  "Hotel and Resort",
    "9":  "Public Service",
}

# RPAD ovrclass overrides (only well-established mappings — others fall back
# to the base taxratecode).
#   ovrclass=11 → Residential A (median tnettaxval ~$1.27M, all base code 1)
OVRCLASS_OVERRIDE = {
    "11": "Residential A",
}

# Letter-code shim (kept for forward compatibility if RPAD ever reverts to
# the older alpha codes — Honolulu's CAMA system has used both numeric and
# letter encodings historically). Only canonical, currently-real classes
# are mapped; obscure / unknown letters fall through to None.
LETTER_CODE = {
    "a":  "Residential",         "aa": "Residential A",
    "c":  "Commercial",          "d":  "Industrial",
    "e":  "Agricultural",        "f":  "Preservation",
    "g":  "Hotel and Resort",    "j":  "Bed and Breakfast Home",
    "v":  "Vacant Agricultural",
    "x":  "Public Service",      "p":  "Public Service",
}

# Residential A tier-2 kicks in above this net taxable value
RA_TIER2_THRESHOLD = 1_000_000.0
RA_TIER2_RATE      = 11.40   # $/1k on the excess

CLASS_CANDIDATES = (
    "taxratecode", "taxrateclass", "tax_rate_code", "tax_class",
    "taxclass", "property_class", "class_code", "land_use_class",
    "rpa_class", "puc", "class", "class1", "luc", "lucode",
    "struc_class", "property_type",
)


def norm_code(v):
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


def _to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(
        s.astype(str).str.replace(r"[\$,\s]", "", regex=True),
        errors="coerce",
    )


def _modal(s: pd.Series):
    s = s.dropna()
    if s.empty:
        return None
    m = s.mode()
    return m.iloc[0] if not m.empty else None


def load_rpad_with_class():
    """Return (DataFrame aggregated to one row per tmk, source_path,
    raw_class_col_name). Falls through to asmtpitt.csv if asmtgis.csv lacks
    the class column or it's all NaN."""
    for path in (RPAD_PRIMARY, RPAD_FALLBACK):
        if not path.exists():
            print(f"[skip] {path} not found")
            continue
        print(f"[read] {path.relative_to(ROOT)}")
        df = pd.read_csv(path, dtype=str, low_memory=False)
        df.columns = [c.lstrip("﻿").strip() for c in df.columns]
        if "taxyr" in df.columns:
            df = df[df["taxyr"].astype(str).str.strip() == "2026"]
        df["_tmk8"] = df["tmk"].map(norm_tmk)
        df = df.dropna(subset=["_tmk8"])

        cols_lower = {c.lower(): c for c in df.columns}
        class_col = next(
            (cols_lower[c] for c in CLASS_CANDIDATES if c in cols_lower),
            None,
        )
        has_class = (
            class_col is not None
            and df[class_col].notna().any()
        )
        if not has_class:
            print(f"  no populated class column on {path.name}; trying next source")
            continue

        print(f"  using class column: {class_col!r}")
        agg_dict = {
            "tnettaxval":    "sum",
            "buildingvalue": "sum",
            "landvalue":     "sum",
            class_col:       _modal,
        }
        for c in ("buildingexemption", "landexemption"):
            if c in df.columns:
                agg_dict[c] = "sum"
        if "ovrclass" in df.columns:
            agg_dict["ovrclass"] = _modal

        # Numeric coercion before sum
        for num_col in ("tnettaxval", "buildingvalue", "landvalue",
                        "buildingexemption", "landexemption"):
            if num_col in df.columns:
                df[num_col] = _to_num(df[num_col])

        agg = df.groupby("_tmk8", as_index=False).agg(agg_dict)
        print(f"  {len(agg)} aggregated TMK rows from {len(df)} suffix rows")
        return agg, path, class_col

    raise RuntimeError(
        "Neither asmtgis.csv nor asmtpitt.csv had a populated class column."
    )


def code_to_class(code, ovrclass):
    """Resolve final class label from numeric/letter code + ovrclass override."""
    if ovrclass is not None and not (isinstance(ovrclass, float) and pd.isna(ovrclass)):
        ovr = str(ovrclass).strip()
        if ovr in OVRCLASS_OVERRIDE:
            return OVRCLASS_OVERRIDE[ovr]
    if code is None or (isinstance(code, float) and pd.isna(code)):
        return None
    raw = str(code).strip()
    if raw in NUMERIC_CODE:
        return NUMERIC_CODE[raw]
    norm = norm_code(raw)
    if norm and norm in LETTER_CODE:
        return LETTER_CODE[norm]
    if norm and norm.title() in RATE:
        return norm.title()
    return None


def compute_tax(tnettaxval: pd.Series, land_use: pd.Series) -> pd.Series:
    base_rate = land_use.map(RATE)
    tax = tnettaxval * base_rate / 1000.0
    ra_mask = land_use == "Residential A"
    excess = (tnettaxval - RA_TIER2_THRESHOLD).clip(lower=0)
    tax_tier2 = excess * (RA_TIER2_RATE - RATE["Residential A"]) / 1000.0
    tax = tax + np.where(ra_mask, tax_tier2, 0.0)
    return tax


# ---------------------------------------------------------------------------

print("[read] parcels_in_walksheds.geojson")
gdf = gpd.read_file(PARCELS_IN)
if gdf.crs is None:
    gdf = gdf.set_crs(WGS84)
elif gdf.crs.to_epsg() != WGS84:
    gdf = gdf.to_crs(WGS84)

print("[area] reprojecting to UTM 4N for true acreage")
gdf["area_ac"] = gdf.to_crs(UTM_4N).geometry.area / SQ_M_PER_AC
gdf["_tmk8"]   = gdf["tmk"].map(norm_tmk)

rpad_agg, rpad_source, class_col = load_rpad_with_class()

merged = gdf.merge(rpad_agg, on="_tmk8", how="left")
n_matched = merged["tnettaxval"].notna().sum()
print(f"[join] {n_matched}/{len(merged)} parcel rows matched RPAD")

# Resolve land-use label from raw code + ovrclass override
ovrclass_series = merged["ovrclass"] if "ovrclass" in merged.columns else pd.Series(
    [None] * len(merged), index=merged.index
)
land_use = pd.Series(
    [code_to_class(c, o) for c, o in zip(merged[class_col], ovrclass_series)],
    index=merged.index,
)
unknown_mask = merged[class_col].notna() & land_use.isna()
if unknown_mask.any():
    unknown_codes = merged.loc[unknown_mask, class_col].value_counts().head(10).to_dict()
    print(f"[warn] {int(unknown_mask.sum())} rows with unmapped class code "
          f"(top: {unknown_codes}) — treated as unclassified (rate=0)")

annual_tax = compute_tax(merged["tnettaxval"].fillna(0), land_use)
merged["annual_property_tax"] = annual_tax
merged["rev_per_ac"] = np.where(
    merged["area_ac"] > 0,
    merged["annual_property_tax"] / merged["area_ac"],
    np.nan,
)
merged["assessed_value"] = (
    merged.get("buildingvalue", pd.Series(0, index=merged.index)).fillna(0)
  + merged.get("landvalue",     pd.Series(0, index=merged.index)).fillna(0)
)
merged["land_use"] = land_use

keep = ["tmk", "STATION_ID", "STATION_NAME", "area_ac",
        "annual_property_tax", "rev_per_ac", "assessed_value",
        "land_use", "geometry"]
out = merged[[c for c in keep if c in merged.columns]].copy()

method = (
    f"class-specific millage via {class_col!r} from "
    f"{rpad_source.relative_to(ROOT)} (ovrclass=11 -> Residential A)"
)
print(f"[stats] method={method}")
print(
    f"  rev_per_ac: min={out['rev_per_ac'].min():.0f}  "
    f"median={out['rev_per_ac'].median():.0f}  "
    f"max={out['rev_per_ac'].max():.0f}  "
    f"nulls={out['rev_per_ac'].isna().sum()}"
)

print("[stats] land_use distribution:")
lu_counts = out["land_use"].fillna("(unclassified)").value_counts()
total = len(out)
for label, n in lu_counts.items():
    rate = RATE.get(label, "—")
    print(f"  {label:<30} {n:>8}  ({100*n/total:5.1f}%)   rate=${rate}/1k")

if OUT_PATH.exists():
    OUT_PATH.unlink()
out.to_file(OUT_PATH, driver="GeoJSON")

import json
from datetime import datetime, timezone
manifest = {
    "output": OUT_PATH.name,
    "source_url": str(rpad_source),
    "row_count": len(out),
    "script": "fix_revenue.py",
    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "method": method,
    "class_field": "land_use",
    "value_field": "assessed_value",
    "raw_class_column": class_col,
    "rates_per_1k": RATE,
    "ra_tier2_threshold_usd": RA_TIER2_THRESHOLD,
    "ra_tier2_rate": RA_TIER2_RATE,
    "ovrclass_overrides": OVRCLASS_OVERRIDE,
    "land_use_distribution": {k: int(v) for k, v in lu_counts.items()},
}
OUT_PATH.with_suffix(OUT_PATH.suffix + ".manifest.json").write_text(
    json.dumps(manifest, indent=2) + "\n"
)
print(f"[done] {OUT_PATH.name} ({len(out)} rows)")
