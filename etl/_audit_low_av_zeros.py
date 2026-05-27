"""One-shot: stratified random sample of the low-AV parcels that still have
``rev_per_ac == 0`` after ``_patch_zero_tax_data.py``, for manual RPAD
audit.

Background
----------
After the direct-zero rescue runs, ~480 parcels remain at $0 because their
assessed value is ≤ $50k. The patch skips them on the assumption that tiny
AV usually means a real exemption (homestead + age combo, charity, govt
sliver). This script samples the 480 to validate that assumption — we look
each one up on qpublic.schneidercorp.com and check whether RPAD agrees the
parcel is genuinely $0-taxable.

If >5% of the sample turns out to be misclassified (taxable per RPAD), we
lower the $50k threshold in the patch and re-run.

Usage::

    python etl/_audit_low_av_zeros.py

Outputs::

    data/audit/low_av_zeros_sample.csv  ← 30 rows, deterministic (seed=42)

CSV columns: tmk_raw, tmk_dashed, land_use, assessed_value, area_ac,
address, av_band.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = _ROOT / "data" / "parcels_tod.geojson"
OUT_DIR = _ROOT / "data" / "audit"
OUT_FILE = OUT_DIR / "low_av_zeros_sample.csv"

AV_THRESHOLD = 50_000   # match _patch_zero_tax_data.py's FALLBACK_AV_THRESHOLD
TARGET_SAMPLE = 30
SEED = 42

# Honolulu's qpublic parcel-search form wants the dashed TMK form
# (e.g. "9-8-003-010"). Our data stores it as the 8-digit zero-padded form
# (e.g. "98003010"). Split: 1 + 1 + 3 + 3.
def _to_dashed(t: str) -> str:
    t = str(t).strip()
    if len(t) != 8 or not t.isdigit():
        return t  # bail; downstream lookup will fail loudly
    return f"{t[0]}-{t[1]}-{t[2:5]}-{t[5:8]}"


def _av_band(av: float) -> str:
    if av <= 1_000:    return "(0, $1k]"
    if av <= 10_000:   return "($1k, $10k]"
    if av <= 25_000:   return "($10k, $25k]"
    return "($25k, $50k]"


def main() -> int:
    if not DATA_FILE.exists():
        print(f"[err] missing {DATA_FILE}", file=sys.stderr)
        return 1

    try:
        import pandas as pd
    except ImportError:
        print("[err] pandas is required; pip install pandas", file=sys.stderr)
        return 1

    print(f"[read] {DATA_FILE.relative_to(_ROOT)}")
    with open(DATA_FILE) as f:
        data = json.load(f)

    rows = []
    for ft in data.get("features", []):
        p = ft.get("properties") or {}
        rev = p.get("rev_per_ac")
        av  = p.get("assessed_value") or 0
        # Population: zero-rev parcels with 0 < AV ≤ $50k. AV == 0 is the
        # "stale geometry" bucket, audited separately.
        if (rev or 0) != 0:
            continue
        if not (0 < av <= AV_THRESHOLD):
            continue
        rows.append({
            "tmk_raw":   str(p.get("tmk") or ""),
            "land_use":  p.get("land_use") or "(unknown)",
            "assessed_value": float(av),
            "area_ac":   float(p.get("area_ac") or 0),
            "address":   p.get("address") or "",
        })

    df = pd.DataFrame(rows)
    df["av_band"] = df["assessed_value"].apply(_av_band)
    n_pop = len(df)
    print(f"[stats] eligible population (rev=0, 0 < AV ≤ ${AV_THRESHOLD:,}): {n_pop}")

    # Per-class sample size: cap at population, else proportional to
    # population share, floor at 3 to keep small classes represented.
    classes = df["land_use"].value_counts()
    print(f"[stats] population by class:")
    for cls, ct in classes.items():
        print(f"          {cls!s:20s} {ct:>4}")

    sample_parts = []
    rng_state = SEED
    for cls, pop_ct in classes.items():
        n = min(pop_ct, max(3, round(pop_ct * TARGET_SAMPLE / n_pop)))
        sub = df[df["land_use"] == cls]
        # Within class: stratify across AV bands so small + large within
        # the band each get a chance. groupby(av_band).sample(min(...))
        # naturally handles uneven band populations.
        band_groups = sub.groupby("av_band", sort=False)
        n_bands = band_groups.ngroups
        per_band = max(1, n // n_bands)
        picks = []
        for band, gdf in band_groups:
            take = min(len(gdf), per_band)
            picks.append(gdf.sample(n=take, random_state=rng_state))
            rng_state += 1
        cls_sample = pd.concat(picks) if picks else sub.head(0)
        # If we under-shot the per-class target (rounding), top up from the
        # remainder of the class so each class hits its quota.
        if len(cls_sample) < n:
            remaining = sub.drop(cls_sample.index)
            if len(remaining):
                extra = remaining.sample(
                    n=min(len(remaining), n - len(cls_sample)),
                    random_state=rng_state,
                )
                rng_state += 1
                cls_sample = pd.concat([cls_sample, extra])
        # Conversely, if rounding pushed us over, trim deterministically.
        cls_sample = cls_sample.head(n)
        sample_parts.append(cls_sample)

    sample = pd.concat(sample_parts).reset_index(drop=True)
    sample["tmk_dashed"] = sample["tmk_raw"].apply(_to_dashed)
    # Reorder columns for the CSV.
    sample = sample[[
        "tmk_raw", "tmk_dashed", "land_use",
        "assessed_value", "area_ac", "address", "av_band",
    ]]

    print(f"[stats] sample size: {len(sample)}")
    print(f"[stats] sample by class:")
    for cls, ct in sample["land_use"].value_counts().items():
        print(f"          {cls!s:20s} {ct:>4}")
    print(f"[stats] sample by AV band:")
    for band, ct in sample["av_band"].value_counts().items():
        print(f"          {band!s:18s} {ct:>4}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[write] {OUT_FILE.relative_to(_ROOT)}")
    sample.to_csv(OUT_FILE, index=False, quoting=csv.QUOTE_MINIMAL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
