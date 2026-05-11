"""Step 03c — extract FY26 BWS water O&M / CIP totals from the downloaded PDFs.

Reads PDFs fetched by ``etl/02b_fetch_bws_budget.py``:

* ``data/raw/budget/bws/combined.pdf``         — Operating + CIP book (FY26)
* ``data/raw/budget/bws/amendment_<N>.pdf``    — latest numbered amendment
* ``data/raw/budget/bws/six_year_cip.pdf``     — FY21-26 Six-Year CIP rollup

Writes ``data/processed/bws_totals.json`` + manifest sidecar with:

* ``water_om_total_usd``  : "Total Expenditures" FY26 column on the combined
                              book's expenditure summary pages (~p.19, ~p.21).
                              Amendments do NOT alter this — they only
                              reprogram CIP.
* ``water_cip_fy26_usd``  : "FY2026 CIP Budget (as Amended)" line on the
                              latest amendment PDF; falls back to the combined
                              book's "Capital Improvement" all-funds row sum
                              if no amendment exists.
* ``water_cip_total_usd`` : "TOTAL CAPITAL IMPROVEMENT" 6-yr column on the
                              Six-Year CIP rollup (in thousands); annualized
                              as floor(6yr_usd / 6) to match etl/03b.

Hard-fails (exit 1) if:

* a required PDF is missing or appears image-only (< 100 chars of text)
* an expected row is not matched (with a clear error pointing at the PDF)
* any sanity-assert bound is exceeded (see BWS-AUTO-PLAN.md §6)

Idempotent: skipped if output + manifest exist and ``--force`` is not passed.

Usage::

    python etl/03c_extract_bws_totals.py
    python etl/03c_extract_bws_totals.py --force
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import read_manifest, write_manifest  # noqa: E402


SCRIPT_NAME    = "etl/03c_extract_bws_totals.py"
BWS_DIR        = _ROOT / "data" / "raw" / "budget" / "bws"
COMBINED_PDF   = BWS_DIR / "combined.pdf"
SIX_YEAR_PDF   = BWS_DIR / "six_year_cip.pdf"
OUT_DIR        = _ROOT / "data" / "processed"
OUT_PATH       = OUT_DIR / "bws_totals.json"

ANNUALIZATION_YEARS = 6

# Sanity-assert bounds (BWS-AUTO-PLAN.md §6).
WATER_OM_LO       =   300_000_000
WATER_OM_HI       =   500_000_000
WATER_CIP_FY26_LO =   200_000_000
WATER_CIP_FY26_HI =   400_000_000
WATER_CIP_6YR_LO  =   150_000_000
WATER_CIP_6YR_HI  =   250_000_000


# ---------------------------------------------------------------------------
# PDF text extraction (pdftotext primary, pdfplumber fallback)
# ---------------------------------------------------------------------------

_PDFTOTEXT: str | None = None
_PARSER_BACKEND: str = ""  # "pdftotext" | "pdfplumber"


def _find_pdftotext() -> str | None:
    global _PDFTOTEXT
    if _PDFTOTEXT:
        return _PDFTOTEXT
    candidate = shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext"
    if not Path(candidate).is_file():
        return None
    _PDFTOTEXT = candidate
    return candidate


def _pdf_to_text(pdf_path: Path) -> str:
    """Return layout-preserving text extraction of pdf_path.

    Prefers ``pdftotext -layout`` (poppler) for consistency with the rest of
    the ETL pipeline; falls back to ``pdfplumber`` if poppler isn't
    installed. Hard-fails if the resulting text is < 100 chars (likely an
    image-only PDF).
    """
    global _PARSER_BACKEND
    exe = _find_pdftotext()
    if exe is not None:
        _PARSER_BACKEND = "pdftotext"
        result = subprocess.run(
            [exe, "-layout", str(pdf_path), "-"],
            capture_output=True, text=True, timeout=120,
        )
        text = result.stdout
    else:
        _PARSER_BACKEND = "pdfplumber"
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    if len(text.strip()) < 100:
        sys.exit(
            f"[error] PDF parser ({_PARSER_BACKEND}) produced < 100 chars "
            f"for {pdf_path.name}.\n"
            "  The PDF may be image-only (scanned). Inspect the file and\n"
            "  re-run etl/02b to refresh if it was truncated."
        )
    return text


def _strip_int(s: str) -> int:
    return int(s.replace(",", "").replace("$", "").replace(" ", "").strip())


# ---------------------------------------------------------------------------
# Active-amendment discovery
# ---------------------------------------------------------------------------

def _find_active_amendment() -> tuple[Path | None, int | None, str | None]:
    """Return (path, amendment_number, amendment_date) for the highest-N
    cached amendment, or (None, None, None) if none exists.

    Amendment date is parsed from the filename pattern
    ``<YYYY-MM-DD>-fy-...amendment-no-<N>...pdf`` (falls back to the
    ``anchor_text`` in the manifest, then to None).
    """
    candidates: list[tuple[int, Path, str]] = []
    for path in BWS_DIR.glob("amendment_*.pdf"):
        manifest = read_manifest(path)
        n = manifest.get("amendment_number") if manifest else None
        if not isinstance(n, int):
            continue
        anchor_text = (manifest.get("anchor_text") if manifest else "") or ""
        candidates.append((n, path, anchor_text))
    if not candidates:
        return None, None, None

    candidates.sort(reverse=True)
    n, path, anchor_text = candidates[0]

    # Date inference, in order of trust:
    #   1. source_url slug (BWS embeds the document date as YYYY-MM-DD prefix
    #      on the PDF URL — most reliable signal we have).
    #   2. Repository-side filename ``path.name`` (our slug is amendment_<N>.pdf,
    #      so this almost never matches but is kept for forward-compat).
    #   3. Anchor text (the visible link label rarely carries a date).
    # Cover-page text is intentionally NOT used: it surfaces board-meeting
    # dates rather than the document-effective date.
    date_str: str | None = None
    source_url = (manifest.get("source_url") if manifest else "") or ""
    for candidate in (source_url, path.name, anchor_text):
        m = re.search(r"(\d{4}-\d{2}-\d{2})", candidate)
        if m:
            date_str = m.group(1)
            break

    return path, n, date_str


# ---------------------------------------------------------------------------
# Extractions
# ---------------------------------------------------------------------------

def _extract_water_om(text: str) -> tuple[int, dict[str, object]]:
    """Total Operating Expenditures FY26 column from the combined book.

    Targets the "Total Expenditures   <fy24>   <fy25>   <fy26>" row that
    appears on multiple summary pages of the combined book. We take the
    third (FY26) column.

    pdfplumber sometimes splits adjacent table cells with a stray space on
    the first occurrence (e.g. ``2 50,787,923 3 41,079,998 3 62,439,988``),
    so we scan ALL matches and accept the first whose FY26 value falls
    within the sanity range — that filters out text-extraction artifacts.
    """
    pat = re.compile(
        r"Total\s+Expenditures\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        re.IGNORECASE,
    )
    candidates: list[tuple[int, re.Match[str]]] = []
    for m in pat.finditer(text):
        try:
            val = _strip_int(m.group(3))
        except ValueError:
            continue
        candidates.append((val, m))

    for val, m in candidates:
        if WATER_OM_LO <= val <= WATER_OM_HI:
            return val, {
                "row":     "Total Expenditures",
                "column":  "FY26 Budget (3rd of 3 cols)",
                "regex":   pat.pattern,
            }

    # Fallback: single-column "Total $ <N>" on the OPERATING BUDGET
    # EXPENDITURES (INCLUDING CIP) page.
    pat2 = re.compile(
        r"OPERATING\s+BUDGET\s+EXPENDITURES.*?Total\s*\$\s*([\d,]+)",
        re.IGNORECASE | re.DOTALL,
    )
    m = pat2.search(text)
    if m:
        val = _strip_int(m.group(1))
        if WATER_OM_LO <= val <= WATER_OM_HI:
            return val, {
                "row":   "Operating Budget Expenditures (including CIP) — Total",
                "regex": pat2.pattern,
            }

    sys.exit(
        "[error] Could not extract Total Operating Expenditures from "
        f"{COMBINED_PDF.name}.\n"
        f"  Found {len(candidates)} candidate match(es) but none fell within "
        f"the sanity range [${WATER_OM_LO:,}, ${WATER_OM_HI:,}].\n"
        "  BWS may have changed the FY26 book layout; verify the\n"
        "  'Total Expenditures' row and the sanity bounds in this script."
    )


def _extract_water_cip_fy26_from_amendment(text: str) -> tuple[int, dict[str, object]]:
    """Find "FY2026 CIP Budget (as Amended) $<N>" on the amendment PDF.

    Scans all matches and picks the first whose value falls within the
    sanity range — defensive against pdfplumber whitespace artifacts.
    """
    pat = re.compile(
        r"FY\s*2026\s+CIP\s+Budget\s*\(\s*as\s+Amended\s*\)\s*\$?\s*([\d,]+)",
        re.IGNORECASE,
    )
    for m in pat.finditer(text):
        try:
            val = _strip_int(m.group(1))
        except ValueError:
            continue
        if WATER_CIP_FY26_LO <= val <= WATER_CIP_FY26_HI:
            return val, {
                "row":   "FY2026 CIP Budget (as Amended)",
                "regex": pat.pattern,
            }

    sys.exit(
        "[error] Could not extract 'FY2026 CIP Budget (as Amended)' "
        "from the active amendment PDF.\n"
        "  No match fell within the sanity range "
        f"[${WATER_CIP_FY26_LO:,}, ${WATER_CIP_FY26_HI:,}].\n"
        "  The amendment layout may have changed; inspect the file or\n"
        "  adjust the regex / sanity bounds in this script."
    )


def _extract_water_cip_6yr(text: str) -> tuple[int, dict[str, object]]:
    """Six-year CIP grand total in thousands from the Six-Year CIP rollup.

    The "TOTAL CAPITAL IMPROVEMENT" row appears on the program summary page
    (p.23 of the FY21-26 book) with seven numeric columns: the 6-year total
    and six per-fiscal-year breakdowns. We take the leading column (the
    6-year total). Page footers indicate units are in thousands of dollars.

    Defensive: the value in thousands must annualize to within the
    6-yr sanity range after × 1000 / 6.
    """
    def _accept(total_k: int) -> bool:
        annualized = (total_k * 1_000) // ANNUALIZATION_YEARS
        return WATER_CIP_6YR_LO <= annualized <= WATER_CIP_6YR_HI

    # Primary: the program summary line.
    pat = re.compile(
        r"TOTAL\s+CAPITAL\s+IMPROVEMENT\s+([\d,]+)",
        re.IGNORECASE,
    )
    for m in pat.finditer(text):
        try:
            total_k = _strip_int(m.group(1))
        except ValueError:
            continue
        if _accept(total_k):
            return total_k, {
                "row":                 "TOTAL CAPITAL IMPROVEMENT",
                "total_6yr_thousands": total_k,
                "regex":               pat.pattern,
            }

    # Fallback: the "with Adjustments" row on the alternate summary page.
    pat2 = re.compile(
        r"FY\s*2021-2026\s+TOTALS?\s+\(\s*with\s+Adjustments?\s*\)\s+([\d,]+)",
        re.IGNORECASE,
    )
    for m in pat2.finditer(text):
        try:
            total_k = _strip_int(m.group(1))
        except ValueError:
            continue
        if _accept(total_k):
            return total_k, {
                "row":                 "FY 2021-2026 TOTALS (with Adjustments)",
                "total_6yr_thousands": total_k,
                "regex":               pat2.pattern,
            }

    sys.exit(
        "[error] Could not extract Six-Year CIP total from "
        f"{SIX_YEAR_PDF.name}.\n"
        "  No 'TOTAL CAPITAL IMPROVEMENT' match annualized to within the\n"
        f"  sanity range [${WATER_CIP_6YR_LO:,}, ${WATER_CIP_6YR_HI:,}].\n"
        "  BWS may have changed the rollup layout."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def extract_totals(*, force: bool) -> int:
    if not force and OUT_PATH.exists():
        print(f"[skip] {OUT_PATH.relative_to(_ROOT)} (cached; use --force to recompute)")
        return 0

    for pdf in (COMBINED_PDF, SIX_YEAR_PDF):
        if not pdf.exists():
            sys.exit(
                f"[error] {pdf.relative_to(_ROOT)} not found.\n"
                "  Run step 02b first:  python etl/02b_fetch_bws_budget.py"
            )

    amendment_path, amendment_n, amendment_date = _find_active_amendment()
    if amendment_path is None:
        sys.exit(
            "[error] No numbered amendment PDF found under "
            f"{BWS_DIR.relative_to(_ROOT)}/.\n"
            "  Tier 1 baseline was set post-Amendment #4; aborting to\n"
            "  avoid silently regressing to the pre-amendment combined-book\n"
            "  figure. Re-run etl/02b to refresh."
        )

    print(f"[parse] {COMBINED_PDF.relative_to(_ROOT)}")
    combined_text  = _pdf_to_text(COMBINED_PDF)
    print(f"[parse] {amendment_path.relative_to(_ROOT)} (active amendment #{amendment_n})")
    amendment_text = _pdf_to_text(amendment_path)
    print(f"[parse] {SIX_YEAR_PDF.relative_to(_ROOT)}")
    six_year_text  = _pdf_to_text(SIX_YEAR_PDF)

    # --- Extract ---
    water_om,  prov_om   = _extract_water_om(combined_text)
    water_cip_fy26, prov_cip = _extract_water_cip_fy26_from_amendment(amendment_text)
    total_6yr_k, prov_6yr   = _extract_water_cip_6yr(six_year_text)
    total_6yr_usd     = total_6yr_k * 1_000
    water_cip_total   = math.floor(total_6yr_usd / ANNUALIZATION_YEARS)
    prov_6yr["total_6yr_usd"]     = total_6yr_usd
    prov_6yr["annualization"]     = "6yr_avg"
    prov_6yr["annualized_usd"]    = water_cip_total

    # --- Sanity asserts (hard-fail before write) ---
    def _assert(cond: bool, msg: str) -> None:
        if not cond:
            sys.exit(f"[error] Sanity check failed: {msg}")

    _assert(
        WATER_OM_LO <= water_om <= WATER_OM_HI,
        f"water_om_total_usd={water_om:,} outside [{WATER_OM_LO:,}, {WATER_OM_HI:,}]",
    )
    _assert(
        WATER_CIP_FY26_LO <= water_cip_fy26 <= WATER_CIP_FY26_HI,
        f"water_cip_fy26_usd={water_cip_fy26:,} outside "
        f"[{WATER_CIP_FY26_LO:,}, {WATER_CIP_FY26_HI:,}]",
    )
    _assert(
        WATER_CIP_6YR_LO <= water_cip_total <= WATER_CIP_6YR_HI,
        f"water_cip_total_usd={water_cip_total:,} outside "
        f"[{WATER_CIP_6YR_LO:,}, {WATER_CIP_6YR_HI:,}]",
    )

    print(f"  water_om_total_usd     : ${water_om:,}")
    print(f"  water_cip_fy26_usd     : ${water_cip_fy26:,}  (Amendment #{amendment_n})")
    print(f"  water_cip_total_usd    : ${water_cip_total:,}  (= ${total_6yr_usd:,} / 6)")

    # --- Write output ---
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    source_pdfs = [
        str(COMBINED_PDF.relative_to(_ROOT)),
        str(amendment_path.relative_to(_ROOT)),
        str(SIX_YEAR_PDF.relative_to(_ROOT)),
    ]
    payload: dict[str, object] = {
        "water_om_total_usd":   water_om,
        "water_cip_total_usd":  water_cip_total,
        "water_cip_fy26_usd":   water_cip_fy26,
        "amendment_number":     amendment_n,
        "amendment_date":       amendment_date,
        "annualization":        "6yr_avg",
        "source_pdfs":          source_pdfs,
        "extracted_at":         datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": {
            "water_om":       {"source_pdf": COMBINED_PDF.name,   **prov_om},
            "water_cip_fy26": {"source_pdf": amendment_path.name, **prov_cip},
            "water_cip_6yr":  {"source_pdf": SIX_YEAR_PDF.name,   **prov_6yr},
        },
        "notes": (
            "6yr-avg annualization matches etl/03b/road+sewer convention. "
            "Amendments overlay only water_cip_fy26; water_om and the 6-yr "
            "rollup are not amendment-affected. Manual overrides in "
            "data/{budget,cip}_overrides.json take precedence over this file."
        ),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n")

    write_manifest(
        OUT_PATH,
        source_url=f"file://{COMBINED_PDF}",
        row_count=3,
        script=SCRIPT_NAME,
        extras={
            "source_pdfs":      source_pdfs,
            "amendment_number": amendment_n,
            "amendment_date":   amendment_date,
            "parser_backend":   _PARSER_BACKEND,
            "values": {
                "water_om_total_usd":  water_om,
                "water_cip_total_usd": water_cip_total,
                "water_cip_fy26_usd":  water_cip_fy26,
            },
        },
    )
    print(f"[done] {OUT_PATH.relative_to(_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--force", action="store_true",
                    help="Re-extract even if the output already exists.")
    args = ap.parse_args(argv)
    return extract_totals(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
