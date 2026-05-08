"""Step 06 — compute parcel revenue (annual property tax) and revenue-per-acre.

Reads ``data/processed/parcels_in_walksheds.geojson`` (output of step 05),
computes parcel area in acres, attaches an annual property-tax estimate, and
writes ``data/processed/parcels_revenue.geojson`` with a ``rev_per_ac`` field.

Per ``data/raw/parcels_tax_schema.md``, the cadastre layer ``parcels_tax``
exposes **no** tax or assessed-value fields. The script therefore looks for a
separate RPAD-roll CSV cached under ``data/raw/rpad/`` (any ``*.csv``) and
joins on TMK. The expected workflow:

1. Download the FY26 RPAD bulk roll from realpropertyhonolulu.com
   (Downloads → "Roll Data") and drop the CSV into ``data/raw/rpad/``.
2. Run this script.

Tax sourcing (in priority order):

1. **Direct tax field on the parcel rows** (e.g. an ArcGIS layer that exposes
   ``annual_property_tax`` / ``total_tax_net`` directly). First match wins.
2. **RPAD ``Total Net Tax``-style column** joined on TMK.
3. **Fallback: estimated tax = assessed value × FY26 millage rate** (per
   land-use class), per METHODOLOGY.md §3.

Area is always computed from the polygon geometry — reproject to EPSG:32604
(UTM 4N) for true m², then divide by 4046.86 to get acres. Per the schema
doc, ``Shape__Area``/``rec_area_*`` are unreliable.

Output: ``data/processed/parcels_revenue.geojson`` (+ manifest sidecar).

Idempotent: skipped if the output and its manifest already exist. Pass
``--force`` to rebuild.

Usage::

    python etl/06_compute_revenue.py
    python etl/06_compute_revenue.py --force
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import geopandas as gpd
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import write_manifest  # noqa: E402


SCRIPT_NAME = "etl/06_compute_revenue.py"

INPUT_PATH  = _ROOT / "data" / "processed" / "parcels_in_walksheds.geojson"
RPAD_DIR    = _ROOT / "data" / "raw"       / "rpad"
OUTPUT_PATH = _ROOT / "data" / "processed" / "parcels_revenue.geojson"

UTM_4N      = 32604
WGS84       = 4326
SQ_M_PER_AC = 4046.8564224  # exact; matches METHODOLOGY.md §2

# Candidate field names (lowercased) for direct tax / assessed value / class.
# The first match in the parcel attributes wins.
TAX_FIELD_CANDIDATES = (
    "annual_property_tax", "annual_tax", "total_net_tax", "totalnettax",
    "net_taxes", "net_tax", "tax_amt", "tax_amount", "total_tax",
)
VALUE_FIELD_CANDIDATES = (
    "net_taxable_value", "nettaxablevalue", "taxable_value",
    "total_assessed_value", "totalassessed", "assessed_value", "totalvalue",
)
CLASS_FIELD_CANDIDATES = (
    "tax_class", "property_class", "class_code", "land_use_class",
    "rpa_class", "puc", "class",
)
TMK_FIELD_CANDIDATES = ("tmk", "tmk9", "tmk9num", "tmk8num", "parcel_id")

# FY26 Honolulu Real Property Tax rates, $ per $1,000 of net taxable value.
# Source: City & County of Honolulu RPT ordinance for FY 2025–2026.
# **Verify** against the published ordinance before publishing numbers — the
# millage table is reset annually by the Council and may have shifted.
# Keys are lowercased class names; aliases get folded via _normalize_class().
FY26_MILLAGE_RATES_PER_1000: dict[str, float] = {
    "residential":                  3.50,
    "residential a tier 1":         4.50,
    "residential a tier 2":        11.40,
    "residential a":                4.50,   # fallback when tier unknown
    "hotel and resort":            13.90,
    "hotel/resort":                13.90,
    "commercial":                  12.40,
    "industrial":                  12.40,
    "agricultural":                 5.70,
    "preservation":                 5.70,
    "public service":               0.00,
    "vacant agricultural":          8.50,
    "bed and breakfast home":       6.50,
    "transient accommodations rental": 9.00,
    "tar":                          9.00,
}


def _normalize_class(s) -> str | None:
    """Lowercase, strip punctuation/extra whitespace; ``None`` for null/empty."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    s = str(s).strip().lower()
    if not s:
        return None
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _first_present(columns_lower: dict[str, str], candidates: tuple[str, ...]) -> str | None:
    """Return the actual (case-preserved) column name for the first candidate
    that's present, else None. ``columns_lower`` maps lowercased -> original."""
    for cand in candidates:
        if cand in columns_lower:
            return columns_lower[cand]
    return None


def _load_rpad_roll() -> pd.DataFrame | None:
    """Concat any CSVs under ``data/raw/rpad/``. Returns ``None`` if none exist."""
    if not RPAD_DIR.exists():
        return None
    csvs = sorted(RPAD_DIR.glob("*.csv"))
    if not csvs:
        return None
    print(f"[rpad] loading {len(csvs)} CSV(s) from {RPAD_DIR.relative_to(_ROOT)}")
    frames = [pd.read_csv(p, dtype=str, low_memory=False) for p in csvs]
    df = pd.concat(frames, ignore_index=True)
    df.columns = [c.strip() for c in df.columns]
    return df


def _normalize_tmk(s) -> str | None:
    """8-digit zero-padded TMK string. Drops county-prefix (Hon = 1) if 9-digit."""
    if s is None or (isinstance(s, float) and pd.isna(s)):
        return None
    digits = re.sub(r"\D", "", str(s))
    if not digits:
        return None
    if len(digits) == 9 and digits.startswith("1"):
        digits = digits[1:]
    return digits.zfill(8)


def _attach_rpad(parcels: gpd.GeoDataFrame, rpad: pd.DataFrame) -> gpd.GeoDataFrame:
    """Left-join RPAD onto parcels on the normalized 8-digit TMK."""
    rpad = rpad.copy()
    rpad_cols_lower = {c.lower().replace(" ", "_"): c for c in rpad.columns}
    rpad_tmk_col = _first_present(rpad_cols_lower, TMK_FIELD_CANDIDATES)
    if rpad_tmk_col is None:
        raise ValueError(
            f"RPAD roll has no TMK-like column. Columns: {list(rpad.columns)}"
        )
    rpad["_tmk8"] = rpad[rpad_tmk_col].map(_normalize_tmk)
    rpad = rpad.dropna(subset=["_tmk8"]).drop_duplicates(subset=["_tmk8"], keep="last")

    parcels = parcels.copy()
    if "tmk" not in parcels.columns:
        raise KeyError("parcels missing 'tmk' field — cannot join RPAD")
    parcels["_tmk8"] = parcels["tmk"].map(_normalize_tmk)
    merged = parcels.merge(rpad, on="_tmk8", how="left", suffixes=("", "_rpad"))
    merged = merged.drop(columns=["_tmk8"])
    probe_col = next((c for c in rpad.columns if c not in ("_tmk8",)), None)
    if probe_col is not None:
        n_matched = merged[probe_col].notna().sum()
        print(f"[rpad] joined {n_matched}/{len(parcels)} parcels matched RPAD rows")
    return merged


def _coerce_number(s: pd.Series) -> pd.Series:
    """Strip $/commas/whitespace, coerce to float, NaN on parse failure."""
    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    cleaned = s.astype(str).str.replace(r"[\$,\s]", "", regex=True)
    return pd.to_numeric(cleaned, errors="coerce")


def compute_revenue(*, force: bool) -> int:
    if not INPUT_PATH.exists():
        raise FileNotFoundError(
            f"Missing {INPUT_PATH}. Run `python etl/05_join_parcels.py` first."
        )

    manifest_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".manifest.json")
    if not force and OUTPUT_PATH.exists() and manifest_path.exists():
        print(f"[skip] {OUTPUT_PATH.name} (cached)")
        return 0

    print(f"[read] {INPUT_PATH.relative_to(_ROOT)}")
    parcels = gpd.read_file(INPUT_PATH)
    if parcels.crs is None:
        parcels = parcels.set_crs(WGS84)

    # --- 1. Compute area_ac via UTM 4N (m²/4046.86) ---------------------------
    print(f"[area] reprojecting to EPSG:{UTM_4N} for true area")
    geom_m2 = parcels.to_crs(UTM_4N).geometry.area
    parcels["area_m2"] = geom_m2
    parcels["area_ac"] = geom_m2 / SQ_M_PER_AC

    # --- 2. Try to attach RPAD if no direct tax field is present --------------
    cols_lower = {c.lower(): c for c in parcels.columns}
    tax_col   = _first_present(cols_lower, TAX_FIELD_CANDIDATES)
    value_col = _first_present(cols_lower, VALUE_FIELD_CANDIDATES)
    class_col = _first_present(cols_lower, CLASS_FIELD_CANDIDATES)

    rpad_used = False
    if tax_col is None and value_col is None:
        rpad = _load_rpad_roll()
        if rpad is None:
            raise FileNotFoundError(
                "No tax or assessed-value fields on the parcel rows, and no "
                f"RPAD CSV found under {RPAD_DIR.relative_to(_ROOT)}/.\n"
                "Download the FY26 bulk roll from realpropertyhonolulu.com "
                "(Downloads → 'Roll Data') and drop the CSV(s) there, then "
                "rerun. See data/raw/parcels_tax_schema.md."
            )
        parcels = _attach_rpad(parcels, rpad)
        rpad_used = True
        cols_lower = {c.lower(): c for c in parcels.columns}
        tax_col   = _first_present(cols_lower, TAX_FIELD_CANDIDATES)
        value_col = _first_present(cols_lower, VALUE_FIELD_CANDIDATES)
        class_col = _first_present(cols_lower, CLASS_FIELD_CANDIDATES)

    # --- 3. Compute annual_property_tax --------------------------------------
    method: str
    fallback_unmatched_classes: list[str] = []

    if tax_col is not None:
        print(f"[tax]  using direct tax field: {tax_col!r}")
        parcels["annual_property_tax"] = _coerce_number(parcels[tax_col])
        method = f"direct:{tax_col}"
    elif value_col is not None:
        if class_col is None:
            raise ValueError(
                "Found assessed value field "
                f"{value_col!r} but no land-use class field "
                f"(searched {CLASS_FIELD_CANDIDATES}). Cannot apply millage "
                "rates without class. Update CLASS_FIELD_CANDIDATES or "
                "supply a class column in the RPAD CSV."
            )
        print(f"[tax]  fallback: estimated_tax = {value_col!r} × FY26 millage[{class_col!r}]")
        values = _coerce_number(parcels[value_col])
        classes_norm = parcels[class_col].map(_normalize_class)
        rates = classes_norm.map(FY26_MILLAGE_RATES_PER_1000)

        unmatched = classes_norm[rates.isna() & classes_norm.notna()].unique().tolist()
        if unmatched:
            fallback_unmatched_classes = sorted(unmatched)
            print(f"[warn] {len(unmatched)} class label(s) not in FY26 millage table; "
                  f"those parcels get NaN tax: {unmatched[:10]}"
                  + ("..." if len(unmatched) > 10 else ""))

        parcels["annual_property_tax"] = values * rates / 1000.0
        method = f"estimated:{value_col}*millage[{class_col}]"
    else:
        raise ValueError(
            "Could not locate a tax or assessed-value column on parcels even "
            "after RPAD join. Update TAX_FIELD_CANDIDATES / VALUE_FIELD_CANDIDATES "
            f"to match your RPAD CSV columns. Available columns: {list(parcels.columns)}"
        )

    # --- 4. rev_per_ac --------------------------------------------------------
    parcels["rev_per_ac"] = parcels["annual_property_tax"] / parcels["area_ac"]

    n_total       = len(parcels)
    n_with_tax    = int(parcels["annual_property_tax"].notna().sum())
    n_with_revpa  = int(parcels["rev_per_ac"].notna().sum())
    median_revpa  = float(parcels["rev_per_ac"].median()) if n_with_revpa else None

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    parcels.to_file(OUTPUT_PATH, driver="GeoJSON")

    write_manifest(
        OUTPUT_PATH,
        source_url=f"file://{INPUT_PATH}",
        row_count=n_total,
        script=SCRIPT_NAME,
        extras={
            "method":                 method,
            "rpad_join_used":         rpad_used,
            "area_crs":               f"EPSG:{UTM_4N}",
            "sq_m_per_acre":          SQ_M_PER_AC,
            "tax_field":              tax_col,
            "value_field":            value_col,
            "class_field":            class_col,
            "rows_total":             n_total,
            "rows_with_tax":          n_with_tax,
            "rows_with_rev_per_ac":   n_with_revpa,
            "median_rev_per_ac":      median_revpa,
            "fallback_unmatched_classes": fallback_unmatched_classes,
        },
    )

    summary = f"[done] {OUTPUT_PATH.relative_to(_ROOT)} ({n_total} rows"
    if median_revpa is not None:
        summary += f", {n_with_revpa} with rev_per_ac, median={median_revpa:,.2f} $/ac"
    summary += ")"
    print(summary)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the cache exists.")
    args = ap.parse_args(argv)
    return compute_revenue(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
