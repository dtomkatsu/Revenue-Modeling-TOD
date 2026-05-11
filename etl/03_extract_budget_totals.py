"""Step 03 — extract FY26 road/sewer/water O&M totals from the budget PDFs.

Reads ``data/raw/budget/operating_fy26.pdf`` (and optionally ``capital_fy26.pdf``)
fetched by step 02, walks every page, and pulls three numbers used by the
frontage-rate model in METHODOLOGY.md §4:

* ``road_om_total_usd``  — DFM Division of Road Maintenance, FY26 Total Budget
* ``sewer_om_total_usd`` — ENV Sewer Fund total (covers wastewater O&M
                            inclusive of admin allocation)
* ``water_om_total_usd`` — BWS water-system O&M. The Board of Water Supply
                            is semi-autonomous and publishes its own budget,
                            so this PDF generally won't contain it. Supply via
                            ``data/budget_overrides.json`` (see below).

Output: ``data/processed/budget_totals.json`` (+ manifest sidecar) with
``provenance`` recording which source each number came from.

Source precedence (highest wins):

  1. ``data/budget_overrides.json``                 — manual escape hatch
  2. ``data/processed/bws_totals.json``             — auto-extracted by
                                                      ``etl/03c`` (Tier 2,
                                                      BWS keys only)
  3. PDF extraction from the City budget book      — this script

BWS values are not present in the City budget PDFs (BWS is semi-autonomous),
so for ``water_om_total_usd`` the practical precedence is (1) then (2). The
manual override file remains in place as an escape hatch for parser
breakage.

Manual override format — drop a ``data/budget_overrides.json`` with any
of the three keys::

    {
      "water_om_total_usd": 195000000,
      "_notes": "From the BWS FY26 Budget Book, p. 12, Operating Total"
    }

Every table on every page is also dumped to ``data/raw/budget/extracted_tables.json``
as an audit log so you can verify extracted numbers by page/row.

Idempotent: skipped if the output and manifest exist. Pass ``--force`` to rebuild.

Usage::

    python etl/03_extract_budget_totals.py
    python etl/03_extract_budget_totals.py --force
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


SCRIPT_NAME = "etl/03_extract_budget_totals.py"

BUDGET_DIR     = _ROOT / "data" / "raw"       / "budget"
OPERATING_PDF  = BUDGET_DIR / "operating_fy26.pdf"
CAPITAL_PDF    = BUDGET_DIR / "capital_fy26.pdf"
TABLES_AUDIT   = BUDGET_DIR / "extracted_tables.json"
OVERRIDES_PATH = _ROOT / "data" / "budget_overrides.json"
BWS_TOTALS     = _ROOT / "data" / "processed" / "bws_totals.json"
OUTPUT_PATH    = _ROOT / "data" / "processed" / "budget_totals.json"

# BWS keys whose default is sourced from bws_totals.json (Tier 2). Other keys
# in this script come from the City budget PDF and are unaffected.
_BWS_KEYS = {"water_om_total_usd"}

# A row in a Honolulu departmental budget table is a label followed by 5
# numeric columns: FY24 Actual / FY25 Appropriated / FY26 Current Svcs /
# FY26 Issues / FY26 Total Budget. pdfplumber's text extraction collapses
# whitespace; we only need the trailing 5 numbers, with optional $ / commas.
_NUM      = r"\$?\s*([\d,]+)"
_ROW_TAIL = rf"{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}"

# Targets we want to extract. Each is (logical key, dept-summary-page predicate,
# row-label regex, human description for provenance).
TARGETS: list[tuple[str, str, str, str]] = [
    (
        "road_om_total_usd",
        "Department of Facility Maintenance",
        r"Road Maintenance",
        "DFM > Road Maintenance program, FY26 Total Budget",
    ),
    (
        "sewer_om_total_usd",
        "Department of Environmental Services",
        r"Sewer Fund",
        "ENV > Sewer Fund (Source of Funds), FY26 Total Budget",
    ),
    (
        "water_om_total_usd",
        "Board of Water Supply",
        r"Water Operating",  # speculative; BWS is normally not in this PDF
        "BWS Water Operating (semi-autonomous; usually requires override)",
    ),
]

KEYS = tuple(t[0] for t in TARGETS)


def _to_int(s: str) -> int:
    return int(s.replace(",", "").replace("$", "").strip())


def _find_dept_summary_page(pages_text: list[str], dept: str) -> int | None:
    """Return the 0-based index of the dept's summary page (the one that has
    'EXPENDITURES BY PROGRAM' AND 'SOURCE OF FUNDS' on the same page), or None.
    """
    dept_u = dept.upper()
    for i, txt in enumerate(pages_text):
        u = txt.upper()
        if dept_u in u and "EXPENDITURES BY PROGRAM" in u and "SOURCE OF FUNDS" in u:
            return i
    return None


def _find_row_fy26_total(text: str, row_label_re: str) -> int | None:
    """Search for a row whose label matches ``row_label_re`` followed by 5
    numeric columns; return the FY26 Total Budget (the 5th number) or None.
    """
    pat = re.compile(rf"{row_label_re}\s+{_ROW_TAIL}", re.IGNORECASE)
    m = pat.search(text)
    return _to_int(m.group(5)) if m else None


def _extract_target(
    pages_text: list[str], dept: str, row_re: str
) -> tuple[int | None, dict[str, object]]:
    idx = _find_dept_summary_page(pages_text, dept)
    if idx is None:
        return None, {"reason": f"{dept!r} summary page not found in PDF"}
    val = _find_row_fy26_total(pages_text[idx], row_re)
    if val is None:
        return None, {"page": idx + 1, "reason": f"row {row_re!r} not matched on dept summary page"}
    return val, {"page": idx + 1, "row_label_re": row_re}


def _audit_tables(pdf_path: Path, pdf: pdfplumber.PDF) -> list[dict]:
    """Extract every table on every page and return audit records."""
    out: list[dict] = []
    for i, page in enumerate(pdf.pages):
        try:
            tables = page.extract_tables() or []
        except Exception as e:
            out.append({"pdf": pdf_path.name, "page": i + 1, "error": repr(e)})
            continue
        for ti, table in enumerate(tables):
            if not table:
                continue
            out.append({
                "pdf":    pdf_path.name,
                "page":   i + 1,
                "table":  ti,
                "n_rows": len(table),
                "n_cols": max((len(r) for r in table), default=0),
                "rows":   [[(c if c is not None else "") for c in row] for row in table],
            })
    return out


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


def extract(*, force: bool, audit: bool = True) -> int:
    if not OPERATING_PDF.exists():
        raise FileNotFoundError(
            f"Missing {OPERATING_PDF}. Run `python etl/02_fetch_budget_pdfs.py` first."
        )

    manifest_path = OUTPUT_PATH.with_suffix(OUTPUT_PATH.suffix + ".manifest.json")
    if not force and OUTPUT_PATH.exists() and manifest_path.exists():
        print(f"[skip] {OUTPUT_PATH.name} (cached)")
        return 0

    values:     dict[str, int | None] = {}
    provenance: dict[str, dict[str, object]] = {}
    audit_records: list[dict] = []

    print(f"[open] {OPERATING_PDF.relative_to(_ROOT)}")
    with pdfplumber.open(OPERATING_PDF) as pdf:
        pages_text = [p.extract_text() or "" for p in pdf.pages]
        print(f"[scan] {len(pages_text)} pages")

        for key, dept, row_re, desc in TARGETS:
            val, prov = _extract_target(pages_text, dept, row_re)
            values[key] = val
            provenance[key] = {
                "source":      "extracted" if val is not None else "missing",
                "pdf":         OPERATING_PDF.name,
                "description": desc,
                **prov,
            }
            shown = ("$" + format(val, ",")) if val is not None else "MISSING"
            note  = prov.get("reason") or f"p.{prov.get('page')}"
            print(f"[extract] {key} = {shown}  ({note})")

        if audit:
            print(f"[audit] dumping all tables in {OPERATING_PDF.name}")
            audit_records.extend(_audit_tables(OPERATING_PDF, pdf))

    if audit and CAPITAL_PDF.exists():
        with pdfplumber.open(CAPITAL_PDF) as pdf:
            print(f"[audit] dumping all tables in {CAPITAL_PDF.name}")
            audit_records.extend(_audit_tables(CAPITAL_PDF, pdf))

    # Apply Tier 2 auto-extracted BWS defaults (bws_totals.json). City PDF
    # extraction can't capture BWS data since BWS is semi-autonomous, so this
    # is the only programmatic source for water_om. Manual overrides still
    # win over this default — see precedence list in the module docstring.
    bws = _load_bws_totals()
    for key in KEYS:
        if key not in _BWS_KEYS:
            continue
        if bws.get(key) is not None and values[key] is None:
            values[key] = int(bws[key])
            provenance[key] = {
                "source":           "bws_totals.json (auto)",
                "file":             str(BWS_TOTALS.relative_to(_ROOT)),
                "amendment_number": bws.get("amendment_number"),
                "amendment_date":   bws.get("amendment_date"),
                "description":      provenance[key].get("description", ""),
            }
            print(f"[bws-auto] {key} = ${values[key]:,}  "
                  f"(amendment #{bws.get('amendment_number')})")

    # Apply manual overrides — these always win.
    overrides = _load_overrides()
    for key in KEYS:
        if key in overrides and overrides[key] is not None:
            values[key]      = int(overrides[key])
            provenance[key]  = {
                "source":      "override",
                "file":        str(OVERRIDES_PATH.relative_to(_ROOT)),
                "description": provenance[key].get("description", ""),
            }
            print(f"[override] {key} = ${values[key]:,}")

    if audit:
        TABLES_AUDIT.parent.mkdir(parents=True, exist_ok=True)
        TABLES_AUDIT.write_text(json.dumps(audit_records, indent=2))
        print(f"[audit] wrote {len(audit_records)} table records to "
              f"{TABLES_AUDIT.relative_to(_ROOT)}")

    source_pdfs = [str(OPERATING_PDF.relative_to(_ROOT))]
    if CAPITAL_PDF.exists():
        source_pdfs.append(str(CAPITAL_PDF.relative_to(_ROOT)))

    payload: dict[str, object] = {
        **{k: values[k] for k in KEYS},
        "source_pdfs":  source_pdfs,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance":   provenance,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    write_manifest(
        OUTPUT_PATH,
        source_url=f"file://{OPERATING_PDF}",
        row_count=len(KEYS),
        script=SCRIPT_NAME,
        extras={
            "source_pdfs":     source_pdfs,
            "overrides_file":  str(OVERRIDES_PATH.relative_to(_ROOT)),
            "audit_log":       str(TABLES_AUDIT.relative_to(_ROOT)) if audit else None,
            "values":          {k: values[k] for k in KEYS},
        },
    )

    missing = [k for k in KEYS if values[k] is None]
    if missing:
        print(f"[warn] missing value(s): {missing}. Add to "
              f"{OVERRIDES_PATH.relative_to(_ROOT)} and rerun with --force.")

    print(f"[done] {OUTPUT_PATH.relative_to(_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Rebuild even if the output exists.")
    ap.add_argument("--no-audit", action="store_true",
                    help="Skip the table-audit dump (faster; useful for retries).")
    args = ap.parse_args(argv)
    return extract(force=args.force, audit=not args.no_audit)


if __name__ == "__main__":
    sys.exit(main())
