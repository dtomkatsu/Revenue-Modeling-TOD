"""One-shot patch: retroactively recompute property tax for parcels in
``data/parcels_tod.geojson`` whose ``rev_per_ac`` is $0 despite a
meaningful assessed value.

Background
----------
The RPAD bulk roll's "Total Net Tax" column is occasionally $0 for
newly-built homes in the Hoʻopili / Mehana / Kapolei / Waipahu master-
planned communities — the source data hasn't picked up the new build's
tax cycle yet. step 06 used to trust that $0 verbatim, so ~10% of
parcels (mostly $1M+ single-family residences on tiny lots in TMK
prefixes 911 / 910 / 94x) showed $0 tax on the live site.

step 06 has been updated with a "direct-zero rescue" that falls back
to ``value × class_millage / 1000`` when direct tax = $0 AND AV > $50k
AND the class's FY26 millage > 0. Next full pipeline run will produce
corrected data automatically.

This script applies the SAME rescue to the already-committed
``data/parcels_tod.geojson`` so the live site reflects the corrected
numbers immediately, without requiring a re-download of the RPAD
bulk roll. Run once, then delete or keep as documentation.

Usage::

    python etl/_patch_zero_tax_data.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = _ROOT / "data" / "parcels_tod.geojson"

# Land-use bucket → FY26 millage (per $1,000 of net taxable value). Subset of
# etl/06_compute_revenue.py's ``FY26_MILLAGE_RATES_PER_1000``, mapped to the
# coarser bucket names that ``land_use`` uses in ``parcels_tod.geojson``.
#
# Public Service is deliberately omitted — utilities (HECO et al.) hold full
# exemptions and pay a state gross-receipts tax in lieu of county property
# tax. RPAD correctly reports $0 net taxable for every Public Service parcel
# (verified: TMK 98-003-010 HECO $40.7M AV → $40.7M exempt → $0 taxable; TMK
# 99-071-001 HECO $9.9M AV → $0 taxable). A rescue here would be wrong.
#
# Preservation IS included. Spot-checks against RPAD show high-AV
# Preservation parcels in the TOD corridor are typically privately held
# with $0 exemption and the full AV taxable at $5.70/$1000:
#   TMK 91-016-227 DAITO US INC      $20.66M taxable → ~$117,770/yr
#   TMK 11-063-017 JJKOO HAWAII INC  $18.23M taxable → ~$103,924/yr
#   TMK 98-011-034 Bishop Estate     $16.84M taxable → ~$96,011/yr
# Excluding Preservation was leaving ~$800k/yr of modeled revenue on the
# table across ~68 parcels.
BUCKET_MILLAGE: dict[str, float] = {
    "Residential":    3.50,
    "Residential A":  4.50,  # fallback when tier unknown
    "Commercial":    12.40,
    "Industrial":    12.40,
    "Hotel/Resort":  13.90,
    "Agricultural":   5.70,
    "Preservation":   5.70,
}
FALLBACK_AV_THRESHOLD = 50_000


def main() -> int:
    if not DATA_FILE.exists():
        print(f"[err] missing {DATA_FILE}", file=sys.stderr)
        return 1

    print(f"[read] {DATA_FILE.relative_to(_ROOT)}")
    with open(DATA_FILE) as f:
        data = json.load(f)

    feats = data.get("features", [])
    n_total = len(feats)
    n_already_taxed = 0
    n_patched = 0
    n_skipped_low_av = 0
    n_skipped_class = 0
    n_skipped_no_area = 0

    for ft in feats:
        p = ft.get("properties", {})
        rev = p.get("rev_per_ac")
        if rev is None or rev > 0:
            n_already_taxed += 1
            continue

        bucket  = p.get("land_use")
        av      = p.get("assessed_value") or 0
        area_ac = p.get("area_ac") or 0

        millage = BUCKET_MILLAGE.get(bucket)
        if millage is None:
            # Public Service, Preservation, or unknown — leave as $0.
            n_skipped_class += 1
            continue
        if av <= FALLBACK_AV_THRESHOLD:
            # Tiny AV — more likely a real exemption (homestead + age
            # combo, etc.) than a data drop.
            n_skipped_low_av += 1
            continue
        if area_ac <= 0:
            n_skipped_no_area += 1
            continue

        annual_tax     = av * millage / 1000.0
        new_rev_per_ac = annual_tax / area_ac
        cost_per_ac    = p.get("cost_per_ac") or 0
        new_net_per_ac = new_rev_per_ac - cost_per_ac

        p["rev_per_ac"] = new_rev_per_ac
        p["net_per_ac"] = new_net_per_ac
        n_patched += 1

    print(f"[stats] total parcels:                  {n_total:>6,}")
    print(f"[stats] already had rev_per_ac > 0:     {n_already_taxed:>6,}")
    print(f"[stats] patched (direct-zero rescue):   {n_patched:>6,}")
    print(f"[stats] skipped (AV ≤ ${FALLBACK_AV_THRESHOLD:,}):     {n_skipped_low_av:>6,}")
    print(f"[stats] skipped (exempt class):         {n_skipped_class:>6,}")
    print(f"[stats] skipped (area_ac = 0):          {n_skipped_no_area:>6,}")

    if n_patched == 0:
        print("[skip] no changes to write")
        return 0

    # Match the file's existing single-line, space-after-separator style
    # so the git diff stays minimal (only field values change).
    print(f"[write] {DATA_FILE.relative_to(_ROOT)}")
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, separators=(", ", ": "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
