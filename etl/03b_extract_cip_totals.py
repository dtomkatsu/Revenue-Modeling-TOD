"""Step 03b — extract FY26 road/sewer/water CIP (capital) totals from the
six-year capital improvement program PDF.

Companion to step 03 (which extracts the operating O&M totals). The capital
PDF organizes projects by **Program Summary** function/sub-function pages.
Each summary page has a "Total" row that aggregates every project under
that function across the six fiscal years.

Per CIP-PLAN.md §2:

* Road CIP   = Σ Total-6-Years over (Highways/Streets/Roadways +
              Bridges/Viaducts/Grade Separation + Storm Drainage +
              Street Lighting)
* Sewer CIP  = Σ Total-6-Years over (Sewage Collection And Disposal +
              Improvement District-Sewers)
* Water CIP  = null (Board of Water Supply is semi-autonomous; not in
              this PDF). Override via ``data/cip_overrides.json``.

The numerator on the per-parcel rate is **annualized** by dividing the
six-year total by six. CIP is famously lumpy across years; smoothing it
gives a more representative annual cost-to-serve.

Output: ``data/processed/cip_totals.json`` with shape::

    {
      "road_cip_total_usd":   <annualized USD/year>,
      "sewer_cip_total_usd":  <annualized USD/year>,
      "water_cip_total_usd":  null,
      "road_cip_fy26_usd":    <FY26-only USD>,
      "sewer_cip_fy26_usd":   <FY26-only USD>,
      "water_cip_fy26_usd":   null,
      "annualization":        "6yr_avg",
      "source_pdfs":          ["data/raw/budget/capital_fy26.pdf"],
      "extracted_at":         "<ISO timestamp>",
      "provenance":           { "<key>": { "page": ..., "program": "...",
                                "total_6yr_thousands": ..., "fy26_thousands": ... } }
    }

Source precedence (highest wins):

  1. ``data/cip_overrides.json``                  — manual escape hatch
  2. ``data/processed/bws_totals.json``           — auto-extracted by
                                                    ``etl/03c`` (BWS keys only)
  3. PDF extraction from the City capital book  — this script

BWS is semi-autonomous and absent from the City capital PDF; for BWS keys
the practical precedence is (1) then (2). Manual override format
(``data/cip_overrides.json``)::

    {
      "water_cip_total_usd": 75000000,
      "_notes": "From the BWS Six-Year CIP, p. ..."
    }

Idempotent: skipped if the output and manifest exist. Pass ``--force``
to rebuild.

Usage::

    python etl/03b_extract_cip_totals.py
    python etl/03b_extract_cip_totals.py --force
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import write_manifest  # noqa: E402


SCRIPT_NAME = "etl/03b_extract_cip_totals.py"

CAPITAL_PDF    = _ROOT / "data" / "raw"       / "budget" / "capital_fy26.pdf"
OVERRIDES_PATH = _ROOT / "data" / "cip_overrides.json"
BWS_TOTALS     = _ROOT / "data" / "processed" / "bws_totals.json"
OUTPUT_PATH    = _ROOT / "data" / "processed" / "cip_totals.json"

# BWS keys whose default is sourced from bws_totals.json (Tier 2).
_BWS_KEYS = {"water_cip_total_usd", "water_cip_fy26_usd"}

# Program-Summary pages that aggregate to road / sewer CIP. Labels are
# matched case-insensitively against the "Program Summary: <label>" line
# on each page. Order doesn't matter; we sum across all matching pages.
ROAD_PROGRAMS  = (
    "Highways, Streets And Roadways",
    "Bridges, Viaducts And Grade Separation",
    "Storm Drainage",
    "Street Lighting",
)
SEWER_PROGRAMS = (
    "Sewage Collection And Disposal",
    "Improvement District-Sewers",
)

# Six-Year CIP Program Summary "Total" rows have 11 numeric columns:
#   Total Encumb | Appn 2024 | Appn 2025 | 2026 | 2027 | 2028 | 2029 | 2030 | 2031 | Total 6yrs | Future Years
# We need the FY26 column (index 3) and the Total-6-Years column (index 9).
# Numbers are printed in thousands per the page footer.
_NUMS = r"(?P<encumb>[\d,]+)\s+(?P<a24>[\d,]+)\s+(?P<a25>[\d,]+)\s+" \
        r"(?P<fy26>[\d,]+)\s+(?P<fy27>[\d,]+)\s+(?P<fy28>[\d,]+)\s+" \
        r"(?P<fy29>[\d,]+)\s+(?P<fy30>[\d,]+)\s+(?P<fy31>[\d,]+)\s+" \
        r"(?P<total6>[\d,]+)\s+(?P<future>[\d,]+)"
_TOTAL_RE = re.compile(rf"^\s*Total\s+{_NUMS}\s*$", re.MULTILINE)


def _to_int(s: str) -> int:
    return int(s.replace(",", "").strip())


def _find_program_total(
    pages_text: list[str], program: str
) -> tuple[int | None, int | None, int | None]:
    """Return (page_1based, fy26_thousands, total_6yr_thousands) or (None, None, None)
    if the page can't be located or its Total row can't be matched."""
    program_u = program.upper()
    for i, txt in enumerate(pages_text):
        if "PROGRAM SUMMARY" not in txt.upper():
            continue
        if program_u not in txt.upper():
            continue
        # Match the Phase Total row's Total line. The summary page has two
        # such rows (Fund Source Totals + Phase Total) — both equal, take first.
        m = _TOTAL_RE.search(txt)
        if m is None:
            return i + 1, None, None
        return i + 1, _to_int(m["fy26"]), _to_int(m["total6"])
    return None, None, None


def _load_overrides() -> dict[str, object]:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        return json.loads(OVERRIDES_PATH.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Cannot parse {OVERRIDES_PATH}: {e}") from e


def _load_bws_totals() -> dict[str, object]:
    """Read data/processed/bws_totals.json if present (Tier 2 auto-extract)."""
    if not BWS_TOTALS.exists():
        return {}
    try:
        return json.loads(BWS_TOTALS.read_text())
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Cannot parse {BWS_TOTALS}: {e}") from e


def extract(*, force: bool) -> int:
    if not CAPITAL_PDF.exists():
        raise FileNotFoundError(
            f"Missing {CAPITAL_PDF}. Run `python etl/02_fetch_budget_pdfs.py` first."
        )

    manifest_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".manifest.json")
    if not force and OUTPUT_PATH.exists() and manifest_path.exists():
        print(f"[skip] {OUTPUT_PATH.name} (cached)")
        return 0

    print(f"[open] {CAPITAL_PDF.relative_to(_ROOT)}")
    with pdfplumber.open(CAPITAL_PDF) as pdf:
        pages_text = [p.extract_text() or "" for p in pdf.pages]
        print(f"[scan] {len(pages_text)} pages")

    provenance: dict[str, dict[str, object]] = {}

    def _scan(category: str, programs: tuple[str, ...]) -> tuple[int, int, list[dict]]:
        fy26_sum_k = 0
        total6_sum_k = 0
        prov_list: list[dict] = []
        for prog in programs:
            page, fy26_k, total6_k = _find_program_total(pages_text, prog)
            if page is None:
                print(f"[warn] {category}: program {prog!r} not found in PDF")
                prov_list.append({"program": prog, "page": None, "found": False})
                continue
            if fy26_k is None or total6_k is None:
                print(f"[warn] {category}: page {page} for {prog!r} has no Total row match")
                prov_list.append({"program": prog, "page": page, "found": True,
                                  "total_row_matched": False})
                continue
            fy26_sum_k   += fy26_k
            total6_sum_k += total6_k
            prov_list.append({
                "program":             prog,
                "page":                page,
                "fy26_thousands":      fy26_k,
                "total_6yr_thousands": total6_k,
            })
            print(f"[extract] {category}: {prog!r} (p.{page}) "
                  f"FY26=${fy26_k:,}K  6yr=${total6_k:,}K")
        return fy26_sum_k, total6_sum_k, prov_list

    road_fy26_k,  road_total6_k,  road_prov  = _scan("road",  ROAD_PROGRAMS)
    sewer_fy26_k, sewer_total6_k, sewer_prov = _scan("sewer", SEWER_PROGRAMS)

    # PDF prints in thousands; convert to dollars. Annualized = 6yr ÷ 6.
    road_total  = road_total6_k  * 1_000 // 6 if road_total6_k  else None
    sewer_total = sewer_total6_k * 1_000 // 6 if sewer_total6_k else None
    road_fy26   = road_fy26_k    * 1_000     if road_fy26_k    else None
    sewer_fy26  = sewer_fy26_k   * 1_000     if sewer_fy26_k   else None

    values: dict[str, int | None] = {
        "road_cip_total_usd":   road_total,
        "sewer_cip_total_usd":  sewer_total,
        "water_cip_total_usd":  None,
        "road_cip_fy26_usd":    road_fy26,
        "sewer_cip_fy26_usd":   sewer_fy26,
        "water_cip_fy26_usd":   None,
    }

    provenance["road_cip"]  = {
        "programs":            road_prov,
        "fy26_total_usd":      road_fy26,
        "annualized_usd":      road_total,
        "annualization":       "6yr_avg",
    }
    provenance["sewer_cip"] = {
        "programs":            sewer_prov,
        "fy26_total_usd":      sewer_fy26,
        "annualized_usd":      sewer_total,
        "annualization":       "6yr_avg",
    }
    provenance["water_cip"] = {
        "note": "Board of Water Supply is semi-autonomous; not in capital_fy26.pdf. "
                "Supply via data/cip_overrides.json.",
    }

    # Tier 2 auto-extracted BWS defaults (bws_totals.json). City capital
    # PDF doesn't contain BWS data, so this is the only programmatic source
    # for water CIP. Manual overrides still win over this default.
    bws = _load_bws_totals()
    for key in ("water_cip_total_usd", "water_cip_fy26_usd"):
        if bws.get(key) is not None and values[key] is None:
            values[key] = int(bws[key])
            provenance["water_cip"] = {
                **provenance.get("water_cip", {}),
                "source":           "bws_totals.json (auto)",
                "file":             str(BWS_TOTALS.relative_to(_ROOT)),
                "amendment_number": bws.get("amendment_number"),
                "amendment_date":   bws.get("amendment_date"),
            }
            print(f"[bws-auto] {key} = ${values[key]:,}  "
                  f"(amendment #{bws.get('amendment_number')})")

    overrides = _load_overrides()
    for key in ("road_cip_total_usd", "sewer_cip_total_usd", "water_cip_total_usd",
                "road_cip_fy26_usd",  "sewer_cip_fy26_usd",  "water_cip_fy26_usd"):
        if key in overrides and overrides[key] is not None:
            values[key] = int(overrides[key])
            cat = key.split("_")[0] + "_cip"
            provenance.setdefault(cat, {})["override_applied"] = True
            print(f"[override] {key} = ${values[key]:,}")

    payload: dict[str, object] = {
        **values,
        "annualization":   "6yr_avg",
        "source_pdfs":     [str(CAPITAL_PDF.relative_to(_ROOT))],
        "extracted_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance":      provenance,
        "notes":           ("Annualized CIP = (Σ Total-6-Years across the listed "
                            "Program Summary pages, in thousands) × 1000 ÷ 6. "
                            "Water is null pending BWS override."),
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    write_manifest(
        OUTPUT_PATH,
        source_url=f"file://{CAPITAL_PDF}",
        row_count=len([k for k, v in values.items() if v is not None]),
        script=SCRIPT_NAME,
        extras={
            "source_pdfs":    [str(CAPITAL_PDF.relative_to(_ROOT))],
            "overrides_file": str(OVERRIDES_PATH.relative_to(_ROOT)),
            "values":         values,
            "annualization":  "6yr_avg",
        },
    )

    print(f"[done] {OUTPUT_PATH.relative_to(_ROOT)}")
    if values["water_cip_total_usd"] is None:
        print(f"[note] water_cip_total_usd is null. Add to "
              f"{OVERRIDES_PATH.relative_to(_ROOT)} and rerun with --force "
              f"once a BWS CIP figure is sourced.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the output exists.")
    args = ap.parse_args(argv)
    return extract(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
