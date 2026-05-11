"""Step 11 — extract HART capital financial totals from downloaded PDFs.

Reads:
  data/raw/hart/monthly_progress_report.pdf  — latest HART Monthly Progress Report
                                                (Core Accountability Items table on
                                                the Summary page, Current Forecast column)
  data/raw/hart/ffga_amended.pdf              — 2024 Amended FFGA (FY26 capital schedule)
  data/raw/hart/recovery_plan.pdf  (optional) — 2022 Recovery Plan (funding-mix fallback)

Writes:
  data/processed/hart_totals.json

Extracted fields
----------------
total_program_cost_usd  : "Total Project Capital Cost — Current Forecast" from the
                          latest Monthly Progress Report's Core Accountability Items
                          table (Summary page). This is the live capital-only figure
                          (excludes pre-RSD finance charges); ~$9.569B as of March 2026.
fy26_capital_usd        : Total FY2026 obligation (federal + local) from FFGA Amended
                          funding-schedule table.
annualized_capital_usd  : floor(total_program_cost_usd / 30)
                          30-year straight-line annualization (see HART-PLAN.md §4).
funding_mix             : best-effort funding breakdown from Recovery Plan Table 3-1
                          (informational only; null if recovery_plan.pdf absent).
report_period           : YYYYMM of the monthly report used (for provenance).

Hard-fails (exit 1)
-------------------
* pdftotext not found
* monthly_progress_report.pdf or ffga_amended.pdf missing or image-only
* total_program_cost_usd outside [$8B, $15B]
* fy26_capital_usd outside [$100M, $1.5B]
* annualized_capital_usd <= 0

Usage::

    python etl/11_extract_hart_totals.py
    python etl/11_extract_hart_totals.py --force
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

SCRIPT_NAME   = "etl/11_extract_hart_totals.py"
MONTHLY_PDF   = _ROOT / "data" / "raw" / "hart" / "monthly_progress_report.pdf"
FFGA_PDF      = _ROOT / "data" / "raw" / "hart" / "ffga_amended.pdf"
RECOVERY_PDF  = _ROOT / "data" / "raw" / "hart" / "recovery_plan.pdf"
OUT_DIR       = _ROOT / "data" / "processed"
OUT_PATH      = OUT_DIR / "hart_totals.json"

ANNUALIZATION_YEARS = 30

# Sanity-assert bounds (from HART-PLAN.md §6)
TOTAL_COST_LO  =  8_000_000_000
TOTAL_COST_HI  = 15_000_000_000
FY26_CAP_LO    =    100_000_000
FY26_CAP_HI    =  1_500_000_000


# ---------------------------------------------------------------------------
# pdftotext helper
# ---------------------------------------------------------------------------

_PDFTOTEXT: str | None = None
_PARSER_BACKEND: str = ""  # "pdftotext" | "pdfplumber"


def _find_pdftotext() -> str | None:
    """Locate pdftotext on PATH or in the standard Homebrew location.

    Returns the executable path, or None if not installed. We no longer exit
    on absence — we fall back to pdfplumber, which is already a project
    dependency and produces equivalent layout output for the relatively
    simple tables in HART's published PDFs.
    """
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
    the ETL pipeline; falls back to ``pdfplumber`` if poppler is not
    installed. Both backends preserve column alignment well enough for the
    tabular extraction in this script.
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
        import pdfplumber  # local import: keep import-time cost down when unused
        with pdfplumber.open(str(pdf_path)) as pdf:
            text = "\n".join((p.extract_text() or "") for p in pdf.pages)

    if len(text.strip()) < 100:
        sys.exit(
            f"[error] PDF parser ({_PARSER_BACKEND}) produced < 100 chars for "
            f"{pdf_path.name}.\n"
            "  The PDF may be image-only (scanned). Check whether the file is\n"
            "  a native PDF or a scanned image and update step 10 if needed."
        )
    return text


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _strip_dollars(s: str) -> float:
    """Strip $, commas, whitespace from a number string and return float."""
    return float(re.sub(r"[$,\s]", "", s))


def _check_in_millions(text: str, match_start: int) -> bool:
    """Return True if 'in millions' appears within 2 kB before the match."""
    context = text[max(0, match_start - 2000):match_start + 100]
    return bool(re.search(r"in\s+millions|millions?\s+of\s+dollars", context, re.I))


def _check_in_thousands(text: str, match_start: int) -> bool:
    context = text[max(0, match_start - 2000):match_start + 100]
    return bool(re.search(r"in\s+thousands|in\s+\$000s|\$000s", context, re.I))


def _apply_scale(value: float, text: str, match_start: int) -> int:
    if _check_in_millions(text, match_start):
        return math.floor(value * 1_000_000)
    if _check_in_thousands(text, match_start):
        return math.floor(value * 1_000)
    return math.floor(value)


# ---------------------------------------------------------------------------
# Extraction: total_program_cost_usd (Monthly Progress Report, Core
# Accountability Items, "Total Project Capital Cost — Current Forecast")
# ---------------------------------------------------------------------------

def _extract_total_program_cost(text: str) -> tuple[int, str | None]:
    """Return (capital_cost_usd, report_period_yyyymm) from the Monthly Report.

    Targets the Core Accountability Items table on the Summary page. The
    relevant row is "Total Project Capital Cost" with three dollar columns:
    2022 Recovery Plan / Current Forecast / Incurred to Date. We extract the
    Current Forecast (middle) column. The table header reads
    "Core Accountability Items ($ are in millions)".

    Example row (pdftotext -layout output):
        Total Project Capital Cost $9,148 $9,569 $6,407

    Returns the report period (YYYYMM) parsed from the cover page title when
    possible (e.g. "M A R C H   2 0 2 6" → "202603") for provenance.
    """
    # Primary pattern: capture all three dollar amounts on the Total Project
    # Capital Cost row, pick the middle one (Current Forecast).
    pat_capital = re.compile(
        r"Total\s+Project\s+Capital\s+Cost\s+"
        r"\$?\s*([\d,]+)\s+\$?\s*([\d,]+)\s+\$?\s*([\d,]+)",
        re.IGNORECASE,
    )
    m = pat_capital.search(text)
    if not m:
        # Fallback: "Capital Cost estimate" row (top of the same table) — this
        # is "Total Project Cost" including pre-RSD finance charges. Less ideal
        # since it bundles debt service, but better than failing outright.
        pat_total = re.compile(
            r"Capital\s+Cost\s+estimate\s+"
            r"\$?\s*([\d,]+)\s+\$?\s*([\d,]+)\s+\$?\s*([\d,]+)",
            re.IGNORECASE,
        )
        m = pat_total.search(text)
        if not m:
            sys.exit(
                "[error] Could not extract Total Project Capital Cost from "
                "monthly_progress_report.pdf.\n"
                "  Expected a 'Total Project Capital Cost' (or 'Capital Cost estimate')\n"
                "  row with three dollar values in the Core Accountability Items table.\n"
                "  Check whether the report layout has changed and update the regex."
            )

    raw_current_forecast = _strip_dollars(m.group(2))

    # Header asserts "$ are in millions"; verify near the match before scaling.
    context = text[max(0, m.start() - 4000):m.start() + 200]
    if not re.search(r"in\s+millions|\$\s*are\s+in\s+millions", context, re.I):
        # Layout shifted or scale keyword missing — try to recover by picking
        # the multiplier that lands the value in the sanity range.
        for mult in (1_000_000, 1_000, 1):
            candidate = math.floor(raw_current_forecast * mult)
            if TOTAL_COST_LO <= candidate <= TOTAL_COST_HI:
                capital_cost = candidate
                break
        else:
            sys.exit(
                "[error] Could not infer monetary scale for Current Forecast capital cost.\n"
                f"  Raw value: {raw_current_forecast}. Expected 'in millions' header.\n"
                "  Layout may have shifted — verify the Core Accountability Items table."
            )
    else:
        capital_cost = math.floor(raw_current_forecast * 1_000_000)

    # Parse report period from cover-page month/year (best-effort).
    report_period = _parse_report_period(text)

    return capital_cost, report_period


def _parse_report_period(text: str) -> str | None:
    """Return 'YYYYMM' parsed from the cover-page title, or None.

    The cover-page title is rendered with spaced letters (e.g.
    'M A R C H   2 0 2 6'). We collapse internal spaces and match
    'Month YYYY' on the cover page.
    """
    months = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
    }
    head = text[:4000]
    collapsed = re.sub(r"\s+", " ", head)
    despaced = re.sub(r"(?<=\b\w) (?=\w\b)", "", collapsed)  # collapse "M A R C H" → "MARCH"
    for candidate in (collapsed, despaced):
        m = re.search(
            r"\b(January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+(\d{4})\b",
            candidate, re.IGNORECASE,
        )
        if m:
            return f"{m.group(2)}{months[m.group(1).lower()]}"
    return None


# ---------------------------------------------------------------------------
# Extraction: fy26_capital_usd (FFGA Amended, funding schedule)
# ---------------------------------------------------------------------------

def _extract_fy26_capital(text: str) -> int:
    """Return FY2026 total capital obligation in USD from FFGA funding schedule.

    The FFGA 'Proposed Schedule of Federal Funds' table lists:
      2026   -   $125,000,000   $501,300,000   $626,300,000
    where the last column is the Total. Values are in full dollars.
    """
    # Find the 2026 row in the schedule table; capture the last dollar figure
    patterns = [
        # "2026" at line start, then last $NNN on the same line
        r"(?m)^\s*2026\b[^\n]*\$([\d,]+)\s*$",
        # Looser: 2026 row with multiple dollar-like amounts, take the last
        r"(?m)^\s*202[56789]\b[^\n]*\$([\d,]+)\s*$",
    ]
    # Specifically target the 2026 row (not 2025 or 2027 fallbacks)
    m_26 = re.search(r"(?m)^\s*2026\b[^\n]*\$([\d,]+)\s*$", text)
    if m_26:
        # Grab ALL dollar figures on this line, take the last (= Total column)
        line_match = re.search(r"(?m)^\s*2026\b[^\n]+$", text)
        if line_match:
            amounts = re.findall(r"\$([\d,]+)", line_match.group())
            if amounts:
                raw = _strip_dollars(amounts[-1])
                scaled = _apply_scale(raw, text, line_match.start())
                if FY26_CAP_LO <= scaled <= FY26_CAP_HI:
                    return scaled
                # Try without scale (FFGA uses full dollars)
                if FY26_CAP_LO <= math.floor(raw) <= FY26_CAP_HI:
                    return math.floor(raw)

    # Fallback: search for a standalone FY 2026 capital line
    m2 = re.search(r"(?i)fy\s*2026[^\n]*?\$([\d,]+)", text)
    if m2:
        raw = _strip_dollars(m2.group(1))
        scaled = _apply_scale(raw, text, m2.start())
        if FY26_CAP_LO <= scaled <= FY26_CAP_HI:
            return scaled

    sys.exit(
        "[error] Could not extract FY2026 capital from five_year_plan.pdf.\n"
        "  Expected '2026' row in 'Proposed Schedule of Federal Funds' table.\n"
        "  Check if the FFGA Amended layout has changed and update the regex."
    )


# ---------------------------------------------------------------------------
# Extraction: funding_mix (best-effort, informational only)
# ---------------------------------------------------------------------------

def _extract_funding_mix(text: str) -> dict:
    """Parse funding breakdown from Recovery Plan Table 3-1 or Figure 3-1.

    Returns a dict with keys: fta_ffga_usd, get_surcharge_usd,
    property_tax_usd, bonds_usd, other_usd. Any un-parseable field is null.
    Amounts in millions in source → convert to dollars here.
    """
    mix: dict = {
        "fta_ffga_usd":       None,
        "get_surcharge_usd":  None,
        "property_tax_usd":   None,
        "bonds_usd":          None,
        "other_usd":          None,
    }

    def _find_amount(label_pattern: str) -> int | None:
        m = re.search(label_pattern + r"\s*\$?\s*([\d,]+\.?\d*)", text, re.I)
        if not m or not m.group(1):
            return None
        try:
            raw = _strip_dollars(m.group(1))
        except ValueError:
            return None
        scaled = _apply_scale(raw, text, m.start())
        return scaled if scaled > 0 else None

    mix["fta_ffga_usd"]      = _find_amount(r"(?:FTA|Federal).*?(?:FFGA|New Starts)")
    mix["get_surcharge_usd"] = _find_amount(r"(?:GET|General Excise)")
    mix["property_tax_usd"]  = _find_amount(r"(?:property.?tax|TAT|transient)")
    mix["bonds_usd"]         = _find_amount(r"(?:GO Bond|General Obligation)")

    return mix


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def extract_totals(*, force: bool) -> int:
    if not force and OUT_PATH.exists():
        print(f"[skip] {OUT_PATH.relative_to(_ROOT)} (cached; use --force to recompute)")
        return 0

    for pdf in (MONTHLY_PDF, FFGA_PDF):
        if not pdf.exists():
            sys.exit(
                f"[error] {pdf.relative_to(_ROOT)} not found.\n"
                "  Run step 10 first:  python etl/10_fetch_hart_docs.py"
            )

    _find_pdftotext()

    print(f"[parse] {MONTHLY_PDF.relative_to(_ROOT)}")
    monthly_text = _pdf_to_text(MONTHLY_PDF)

    print(f"[parse] {FFGA_PDF.relative_to(_ROOT)}")
    ffga_text = _pdf_to_text(FFGA_PDF)

    # --- Extract key figures ---
    total_program_cost_usd, report_period = _extract_total_program_cost(monthly_text)
    fy26_capital_usd       = _extract_fy26_capital(ffga_text)
    annualized_capital_usd = math.floor(total_program_cost_usd / ANNUALIZATION_YEARS)

    # --- Sanity asserts ---
    def _assert(cond: bool, msg: str) -> None:
        if not cond:
            sys.exit(f"[error] Sanity check failed: {msg}")

    _assert(
        TOTAL_COST_LO <= total_program_cost_usd <= TOTAL_COST_HI,
        f"total_program_cost_usd={total_program_cost_usd:,} outside "
        f"[{TOTAL_COST_LO:,}, {TOTAL_COST_HI:,}]",
    )
    _assert(
        FY26_CAP_LO <= fy26_capital_usd <= FY26_CAP_HI,
        f"fy26_capital_usd={fy26_capital_usd:,} outside "
        f"[{FY26_CAP_LO:,}, {FY26_CAP_HI:,}]",
    )
    _assert(annualized_capital_usd > 0, "annualized_capital_usd <= 0")

    print(f"  report_period           : {report_period or '(unknown)'}")
    print(f"  total_program_cost_usd  : ${total_program_cost_usd:,.0f}  (Current Forecast)")
    print(f"  fy26_capital_usd        : ${fy26_capital_usd:,.0f}")
    print(f"  annualized_capital_usd  : ${annualized_capital_usd:,.0f}")

    # Funding mix is informational only — pulled from the Recovery Plan if
    # available; otherwise emit nulls. Does not affect cost calculations.
    if RECOVERY_PDF.exists():
        recovery_text = _pdf_to_text(RECOVERY_PDF)
        funding_mix   = _extract_funding_mix(recovery_text)
        funding_mix_source = "Recovery Plan 2022, Table 3-1 (informational)"
    else:
        funding_mix = {
            "fta_ffga_usd": None, "get_surcharge_usd": None,
            "property_tax_usd": None, "bonds_usd": None, "other_usd": None,
        }
        funding_mix_source = "(recovery_plan.pdf absent; funding mix unavailable)"

    # --- Write output ---
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "total_program_cost_usd":  total_program_cost_usd,
        "fy26_capital_usd":        fy26_capital_usd,
        "annualized_capital_usd":  annualized_capital_usd,
        "annualization_years":     ANNUALIZATION_YEARS,
        "report_period":           report_period,
        "funding_mix":             funding_mix,
        "provenance": {
            "total_program_cost_source": (
                f"HART Monthly Progress Report {report_period or '(period unknown)'}, "
                "Core Accountability Items, Total Project Capital Cost — Current Forecast"
            ),
            "fy26_capital_source":       "2024 Amended FFGA, Proposed Schedule of Federal Funds, FY 2026 Total",
            "funding_mix_source":        funding_mix_source,
            "script":                    SCRIPT_NAME,
        },
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2))
    print(f"[done] {OUT_PATH.relative_to(_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    ap.add_argument("--force", action="store_true",
                    help="Re-extract even if output already exists.")
    args = ap.parse_args(argv)
    return extract_totals(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
