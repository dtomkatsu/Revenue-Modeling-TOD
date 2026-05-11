"""Step 12 — compute corridor-uniform rail CIP per acre.

Reads:
  data/processed/hart_totals.json   (from step 11)
  data/parcels_tod.geojson          (from step 08)

Writes:
  data/processed/rail_cip.json

The per-parcel rate is:

    rail_cip_per_ac = annualized_capital_usd / total_unique_corridor_acreage

where total_unique_corridor_acreage = sum(area_ac for unique TMKs in parcels_tod.geojson).

Every parcel in the TOD corridor gets the same constant $/ac value. The rate
is applied by the frontend toggle (default OFF); it is NOT added to cost_per_ac
in the GeoJSON.

Usage::

    python etl/12_compute_rail_cip.py
    python etl/12_compute_rail_cip.py --force
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

SCRIPT_NAME    = "etl/12_compute_rail_cip.py"
HART_TOTALS    = _ROOT / "data" / "processed" / "hart_totals.json"
PARCELS_GEOJSON = _ROOT / "data" / "parcels_tod.geojson"
OUT_PATH       = _ROOT / "data" / "processed" / "rail_cip.json"


def compute_rail_cip(*, force: bool) -> int:
    if not force and OUT_PATH.exists():
        print(f"[skip] {OUT_PATH.relative_to(_ROOT)} (cached; use --force to recompute)")
        return 0

    for p in (HART_TOTALS, PARCELS_GEOJSON):
        if not p.exists():
            src = "step 11" if p == HART_TOTALS else "step 08"
            sys.exit(
                f"[error] {p.relative_to(_ROOT)} not found.\n"
                f"  Run {src} first."
            )

    print(f"[read]  {HART_TOTALS.relative_to(_ROOT)}")
    totals = json.loads(HART_TOTALS.read_text())
    annualized = totals["annualized_capital_usd"]

    print(f"[read]  {PARCELS_GEOJSON.relative_to(_ROOT)}")
    gj = json.loads(PARCELS_GEOJSON.read_text())
    features = gj["features"]

    # Sum area_ac per unique TMK (deduplicate in case future pipeline creates dups)
    seen_tmks: set[str] = set()
    total_acreage = 0.0
    parcel_count = 0
    for feat in features:
        props = feat["properties"]
        tmk = str(props.get("tmk", ""))
        area_ac = float(props.get("area_ac") or 0)
        if tmk and tmk not in seen_tmks:
            seen_tmks.add(tmk)
            total_acreage += area_ac
            parcel_count += 1

    if total_acreage <= 0:
        sys.exit("[error] total_unique_corridor_acreage is 0 — check parcels_tod.geojson")

    rail_cip_per_ac = annualized / total_acreage

    # Audit: rail_cip_per_ac × total_acreage should ≈ annualized (within $1)
    recomputed = rail_cip_per_ac * total_acreage
    assert abs(recomputed - annualized) < 1.0, (
        f"Audit failed: {rail_cip_per_ac} × {total_acreage} = {recomputed} "
        f"≠ {annualized}"
    )

    print(f"  unique TMKs             : {parcel_count:,}")
    print(f"  total_corridor_acreage  : {total_acreage:,.2f} ac")
    print(f"  annualized_capital_usd  : ${annualized:,.0f}")
    print(f"  rail_cip_per_ac         : ${rail_cip_per_ac:,.2f}/ac/yr")

    payload = {
        "rail_cip_per_ac":                rail_cip_per_ac,
        "annualized_capital_usd":         annualized,
        "total_unique_corridor_acreage":  total_acreage,
        "unique_parcel_count":            parcel_count,
        "annualization_years":            totals["annualization_years"],
        "total_program_cost_usd":         totals["total_program_cost_usd"],
        "provenance": {
            "method":  "corridor-uniform: annualized_capital_usd / total_unique_corridor_acreage",
            "sources": [str(HART_TOTALS.relative_to(_ROOT)), str(PARCELS_GEOJSON.relative_to(_ROOT))],
            "script":  SCRIPT_NAME,
        },
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"[done]  {OUT_PATH.relative_to(_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--force", action="store_true",
                    help="Recompute even if output already exists.")
    args = ap.parse_args(argv)
    return compute_rail_cip(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
