"""Step 11 — extract HART capital financial totals from downloaded PDFs.

Reads:
  data/raw/hart/recovery_plan.pdf   — 2022 Recovery Plan (primary cost source)
  data/raw/hart/five_year_plan.pdf  — 2024 Amended FFGA (FY26 capital + funding schedule)

Writes:
  data/processed/hart_totals.json

Extracted fields
----------------
total_program_cost_usd  : EAC for truncated FFGA scope from Recovery Plan Table 6-1.
                          ~$9.148B as of June 2022.
fy26_capital_usd        : Total FY2026 obligation (federal + local) from FFGA Amended
                          funding-schedule table.
annualized_capital_usd  : floor(total_program_cost_usd / 30)
                          30-year straight-line annualization (see HART-PLAN.md §4).
funding_mix             : best-effort funding breakdown from Recovery Plan Table 3-1.

Hard-fails (exit 1)
-------------------
* pdftotext not found
* Either PDF is missing or appears image-only (< 100 chars of text)
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
RECOVERY_PDF  = _ROOT / "data" / "raw" / "hart" / "recovery_plan.pdf"
FIVE_YEAR_PDF = _ROOT / "data" / "raw" / "hart" / "five_year_plan.pdf"
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


def _find_pdftotext() -> str:
    global _PDFTOTEXT
    if _PDFTOTEXT:
        return _PDFTOTEXT
    # shutil.which respects PATH; fall back to the known Homebrew location
    candidate = shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext"
    if not Path(candidate).is_file():
        sys.exit(
            "[error] pdftotext not found.\n"
            "  Install poppler:  brew install poppler\n"
            "  Then re-run this script."
        )
    _PDFTOTEXT = candidate
    return candidate


def _pdf_to_text(pdf_path: Path) -> str:
    """Run pdftotext -layout on pdf_path; return stdout as string."""
    exe = _find_pdftotext()
    result = subprocess.run(
        [exe, "-layout", str(pdf_path), "-"],
        capture_output=True, text=True, timeout=120,
    )
    text = result.stdout
    if len(text.strip()) < 100:
        sys.exit(
            f"[error] pdftotext produced < 100 chars for {pdf_path.name}.\n"
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
# Extraction: total_program_cost_usd (Recovery Plan, Table 6-1)
# ---------------------------------------------------------------------------

def _extract_total_program_cost(text: str) -> int:
    """Return total program capital cost in USD from Recovery Plan Table 6-1.

    Targets the "Project Costs Estimate at Completion (EAC)" line, which
    appears in a table headed "(dollars in millions)".
    """
    # Primary pattern: EAC line in Table 6-1
    patterns = [
        r"Project Costs Estimate at Completion\s*\(EAC\)\s*\$?\s*([\d,]+\.?\d*)",
        r"Estimate at Completion\s*\(EAC\)\s*\$?\s*([\d,]+\.?\d*)",
        # Fallback: any "total" row with a plausible dollar amount (8,000+ M)
        r"Total\s+(?:Project\s+)?Cost[s]?\s+Estimate[^\n]*\$?\s*([\d,]+\.?\d*)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            raw = _strip_dollars(m.group(1))
            scaled = _apply_scale(raw, text, m.start())
            if TOTAL_COST_LO <= scaled <= TOTAL_COST_HI:
                return scaled
            # If not in-range, it might need scaling even if keyword wasn't nearby
            for mult in (1_000_000, 1_000, 1):
                candidate = math.floor(raw * mult)
                if TOTAL_COST_LO <= candidate <= TOTAL_COST_HI:
                    return candidate

    sys.exit(
        "[error] Could not extract total program cost from recovery_plan.pdf.\n"
        "  Expected 'Estimate at Completion (EAC)' line in Table 6-1.\n"
        "  Check if the Recovery Plan layout has changed and update the regex."
    )


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

    for pdf in (RECOVERY_PDF, FIVE_YEAR_PDF):
        if not pdf.exists():
            sys.exit(
                f"[error] {pdf.relative_to(_ROOT)} not found.\n"
                "  Run step 10 first:  python etl/10_fetch_hart_docs.py"
            )

    _find_pdftotext()

    print(f"[parse] {RECOVERY_PDF.relative_to(_ROOT)}")
    recovery_text = _pdf_to_text(RECOVERY_PDF)

    print(f"[parse] {FIVE_YEAR_PDF.relative_to(_ROOT)}")
    ffga_text = _pdf_to_text(FIVE_YEAR_PDF)

    # --- Extract key figures ---
    total_program_cost_usd = _extract_total_program_cost(recovery_text)
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

    print(f"  total_program_cost_usd  : ${total_program_cost_usd:,.0f}")
    print(f"  fy26_capital_usd        : ${fy26_capital_usd:,.0f}")
    print(f"  annualized_capital_usd  : ${annualized_capital_usd:,.0f}")

    funding_mix = _extract_funding_mix(recovery_text)

    # --- Write output ---
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "total_program_cost_usd":  total_program_cost_usd,
        "fy26_capital_usd":        fy26_capital_usd,
        "annualized_capital_usd":  annualized_capital_usd,
        "annualization_years":     ANNUALIZATION_YEARS,
        "funding_mix":             funding_mix,
        "provenance": {
            "total_program_cost_source": "Recovery Plan 2022, Table 6-1, Project Costs EAC (truncated FFGA scope)",
            "fy26_capital_source":       "2024 Amended FFGA, Proposed Schedule of Federal Funds, FY 2026 Total",
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
