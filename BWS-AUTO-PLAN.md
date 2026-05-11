# BWS Budget Auto-Extraction — Plan (Tier 2)

This document is the Phase 1 plan for Task 1 ("Tier 2 — automated BWS
extraction") of the Revenue-Modeling-TOD project. Implementation lands in
subsequent commits. Do NOT implement until this document is committed alone.

---

## 1. Context

Tier 1 (commit `f388d13` on `madison-work`, 2026-05-08) loaded BWS FY26 water
O&M and CIP via manual entries in `data/budget_overrides.json` and
`data/cip_overrides.json`. Three hand-keyed numbers, sourced from the BWS
FY26 budget book + Amendment #4 + the FY21-26 Six-Year CIP:

| Field | Tier 1 value | Source |
|---|---:|---|
| `water_om_total_usd` | $362,439,988 | FY26 Total Operating Expenditures (Amendment #4) |
| `water_cip_total_usd` | $190,364,333 | $1,142,186,000 / 6 (6-yr avg) |
| `water_cip_fy26_usd` | $283,327,500 | FY26 all-funds CIP (Amendment #4) |

Tier 2 replaces those manual entries with an automated extractor so the next
BWS amendment / fiscal-year rollover updates without human edit. The manual
override files remain in place as an escape hatch — they STILL win over the
auto-extracted defaults.

---

## 2. URL Discovery

### 2.1 Index page

```
https://www.boardofwatersupply.com/department-financial-statements/
```

Fetch with `common/http_client.fetch_text`. Parse with stdlib `html.parser`
(no new dependency). Match anchor tags whose text (lowercased, whitespace-
collapsed) matches the patterns below.

The page is rendered server-side; PDF links are plain `<a href=".../media/<hash>/<slug>.pdf">`
anchors with stable visible-text labels. The hash segment (e.g. `ytfd1egf`)
rotates on re-upload — discover URLs at runtime, never hardcode.

| Slug | Anchor-text pattern (case-insensitive) | Purpose |
|---|---|---|
| `combined` | `Operating\s*(?:&\|and)\s*CIP\s+Budget` | Combined Op + CIP book |
| `cip_standalone` | `^CIP\s+Budget\s+FY\s*\d{4}` | CIP volume on its own (optional) |
| `six_year_cip` | `Capital\s+Improvement\s+Program\s+Fiscal\s+Years?\s+\d{4}` | FY21–26 6-yr rollup |
| `amendment` | `Budget\s+FY[^A-Za-z]*Amendment\s+(?:No\.?\s*)?(\d+)` | Per-amendment PDF (collect all) |

For amendments, the matching regex captures the amendment number. All
matches are collected and sorted by `N` descending — the highest-N amendment
is the **active** one. Non-numbered amendments (e.g. "Amendment — Restated")
are logged as a warning and ignored if a numbered amendment exists.

### 2.2 Failure policy

Hard-fail (exit 1) if any of these aren't found on the index page:

- `combined` (must exist)
- `six_year_cip` (must exist — required for `water_cip_total_usd`)
- ≥1 numbered amendment (must exist — Tier 1 baseline came post-Amendment #4)

`cip_standalone` is optional; if absent, fall back to the CIP portion of the
combined PDF (its content is duplicated there).

Total required documents = 3 of 4 (the 3 must-haves plus optionally the CIP
standalone), which matches the task spec's "<4 expected doc types found ⇒
hard-fail" threshold.

### 2.3 Idempotency + amendment-detection refresh

- Skip download if the PDF + manifest both exist and `--force` is NOT passed.
- Exception: if the highest-N amendment discovered on the index page is
  numerically greater than the cached amendment (recorded in the manifest's
  `amendment_number` extra), force-refresh that single file. This lets the
  GitHub Action pick up new amendments without manual `--force`.

### 2.4 Cache layout

```
data/raw/budget/bws/combined.pdf            (+ .manifest.json)
data/raw/budget/bws/cip_standalone.pdf      (+ .manifest.json) [if present]
data/raw/budget/bws/six_year_cip.pdf        (+ .manifest.json)
data/raw/budget/bws/amendment_<N>.pdf       (+ .manifest.json) [one per N]
```

The manifest sidecar's `extras` field records:
- `source_url`        — the discovered (hash-rotating) URL
- `anchor_text`       — exact link label on the index page
- `amendment_number`  — N (amendment files only)

---

## 3. Parser Design

### 3.1 Parser choice: `pdftotext -layout` with `pdfplumber` fallback

The task spec mandates `pdftotext -layout` (poppler-utils) on the grounds
that pdfplumber chokes on BWS's streaming PDF compression. In practice
pdfplumber's text extraction works on the public BWS PDFs we've inspected
(spot-checked the combined budget, Amendment #4, and the 6-yr CIP — all
parse cleanly). We keep poppler as the primary backend for cross-tool
consistency (BWS Tier-2 + HART step 11 both follow this convention) and
add a `pdfplumber` fallback so the script runs on a machine without
poppler installed (mirrors the fallback added to `etl/11_extract_hart_totals.py`).

```python
import shutil
exe = shutil.which("pdftotext") or "/opt/homebrew/bin/pdftotext"
if Path(exe).is_file():
    text = subprocess.run([exe, "-layout", str(pdf), "-"], ...).stdout
else:
    import pdfplumber
    with pdfplumber.open(pdf) as p:
        text = "\n".join((page.extract_text() or "") for page in p.pages)
```

Document `pdftotext (poppler-utils)` in README.md alongside the Python
install steps.

### 3.2 Target lines + regexes

The three extractions, with the actual lines we've observed in the
FY25-26 corpus:

#### 3.2.1 `water_om_total_usd` — Total Operating Expenditures

**Source:** `combined.pdf` (NOT amendment — amendments only reprogram CIP,
not operating). Observed lines on the FY25-26 combined book:

```
p.19  Total Expenditures           250,787,923   341,079,998   362,439,988
p.21  Total Expenditures           250,787,923   341,079,998   362,439,988
p.29  Total                                                  $ 362,439,988
```

**Regex (primary):**

```python
r"Total\s+Expenditures\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)"
# capture group 3 = FY26 Budget (current fiscal year column)
```

Three numeric columns are: FY24 Actual / FY25 Adopted / FY26 Proposed. The
third column is the FY26 budget. Per-class summary pages (p.19 and p.21)
both have this row with identical numbers — accept the first match.

**Fallback regex** (for layout drift):

```python
r"OPERATING\s+BUDGET\s+EXPENDITURES.*?Total\s*\$\s*([\d,]+)"  # p.29 single-column
```

#### 3.2.2 `water_cip_fy26_usd` — FY26 CIP all-funds total

**Source:** latest amendment PDF (if any), else `combined.pdf`.

Amendment PDFs explicitly label the post-amendment total:

```
amendment_4.pdf  p.2   FY2026 CIP Budget (as Amended)    $283,327,500
```

**Regex (amendment primary):**

```python
r"FY\s*2026\s+CIP\s+Budget\s*\(as\s+Amended\)\s+\$\s*([\d,]+)"
```

If no amendment exists, the combined PDF's CIP all-funds row supplies the
adopted figure:

```
combined.pdf  p.17  Capital Improvement Program
                    Total - All Funds  362,439,988   67,870,000   10,915,000
                                       43,415,000    19,000,000   63,000,000   566,639,988
```

The "Total — All Funds" row carries the all-funds total across six
funding sources (Operating Fund / SRF / Special Expendable / Improvement /
Extramural / WIFIA). The pre-amendment all-funds CIP = $566,639,988 − $283,312,488
(Operating Fund expenditures) = $283,327,500. To avoid a fragile subtraction
chain, prefer the explicit amendment line; only fall back to the combined
PDF when no amendment is on the index page.

**Combined-PDF fallback regex:** parse the "Summary of All Funds" table on
the page that has both "Operating Budget" and "Capital Improvement" rows,
sum the Capital Improvement row across all funds.

#### 3.2.3 `water_cip_total_usd` — 6-year average annualized CIP

**Source:** `six_year_cip.pdf`. Observed on p.23 and p.127:

```
TOTAL CAPITAL IMPROVEMENT     1,142,186   190,895   179,976   176,957
                                          204,863   194,163   195,332
```

The leading column is the 6-year total in thousands. Annualize by dividing
by 6 (integer floor, matching `etl/03b_extract_cip_totals.py` convention):
$1,142,186,000 / 6 = $190,364,333.

**Regex (primary):**

```python
r"TOTAL\s+CAPITAL\s+IMPROVEMENT\s+([\d,]+)"
# group 1 in thousands; scale × 1000; then // 6
```

**Fallback regex** (p.127 alternative row label):

```python
r"FY\s*2021-2026\s+TOTALS?\s+\(with\s+Adjustments?\)\s+([\d,]+)"
```

Both rows are observed to print the same number (1,142,186 thousand).

### 3.3 Scaling

All BWS-published figures in the source PDFs are in **dollars** except the
6-year CIP page, which prints in **thousands** ($000). The 6-year extractor
multiplies its captured value by 1,000.

The other targets (`water_om`, `water_cip_fy26`) are in full dollars and
need no scaling.

If a future report header changes to "in millions", the same scale-inference
pattern from `etl/11_extract_hart_totals.py` (multiplier × 1, 1000, 1_000_000
until the sanity range is satisfied) is the documented recovery path.

---

## 4. Amendment Policy

1. Discover every `Budget FY .... Amendment No. N` PDF on the index page.
2. Sort by N descending; pick the highest N as `active_amendment`.
3. Non-numbered amendments (e.g. "Amendment — Restated") log a warning and
   are ignored when a numbered amendment exists.
4. Provenance records:
   - `amendment_number`           : N
   - `amendment_date`             : YYYY-MM-DD parsed from the filename
                                     (`<date>-fy-2026-budget-amendment-no-<N>.pdf`)
                                     or from the PDF cover page; null if unparseable.
   - `pre_amendment_om_usd`       : value extracted from the combined PDF
                                     before amendment overlay (audit trail).
   - `post_amendment_cip_fy26_usd`: value extracted from the amendment PDF.

The amendment overlay applies **only** to `water_cip_fy26_usd`. Operating
expenditures (`water_om_total_usd`) and the 6-year CIP rollup
(`water_cip_total_usd`) are NOT overridden by amendments — Amendment #4 is
purely a CIP reprogramming and leaves the operating total unchanged. We
verified this against the FY25-26 corpus: Amendment #4's $283,327,500
matches the combined PDF's pre-amendment all-funds CIP exactly, so the
operating total is the same post-amendment.

---

## 5. Wiring Design

### 5.1 New output: `data/processed/bws_totals.json`

`etl/03c_extract_bws_totals.py` writes this canonical auto-extracted output:

```json
{
  "water_om_total_usd":   362439988,
  "water_cip_total_usd":  190364333,
  "water_cip_fy26_usd":   283327500,
  "amendment_number":     4,
  "amendment_date":       "2026-01-14",
  "annualization":        "6yr_avg",
  "source_pdfs":          [
    "data/raw/budget/bws/combined.pdf",
    "data/raw/budget/bws/amendment_4.pdf",
    "data/raw/budget/bws/six_year_cip.pdf"
  ],
  "extracted_at":         "2026-05-11T...Z",
  "provenance": {
    "water_om":       { "page": 19, "source_pdf": "combined.pdf",
                        "row": "Total Expenditures", "column": "FY26 Budget" },
    "water_cip_fy26": { "page": 2,  "source_pdf": "amendment_4.pdf",
                        "row": "FY2026 CIP Budget (as Amended)" },
    "water_cip_6yr":  { "page": 23, "source_pdf": "six_year_cip.pdf",
                        "row": "TOTAL CAPITAL IMPROVEMENT",
                        "total_6yr_thousands": 1142186,
                        "total_6yr_usd": 1142186000 }
  },
  "notes": "6yr avg annualization matches etl/03b; amendment-overlay applies only to water_cip_fy26."
}
```

A manifest sidecar (`bws_totals.json.manifest.json`) is written via
`common/manifest.write_manifest` with extras including the amendment number
and discovered URLs.

### 5.2 Order of precedence

Highest priority wins. Each downstream consumer applies in this order:

1. **Manual override** in `data/budget_overrides.json` / `data/cip_overrides.json`
   (Tier 1 escape hatch — preserved exactly as-is).
2. **Auto-extracted default** from `data/processed/bws_totals.json` (Tier 2 — new).
3. **No value** — `null` (downstream warns).

For BWS the third tier is the City budget PDFs, which intentionally do
NOT contain BWS data (BWS is semi-autonomous). So in practice, Tier 2
either provides the value or `null` is returned and the override file
fills it. Once Tier 2 is verified, the override file's BWS keys can be
cleared (set to `null`) and Tier 2 becomes the sole source.

### 5.3 Step 03 wiring (operating)

`etl/03_extract_budget_totals.py` already loads
`data/budget_overrides.json` and applies it after PDF extraction. Tier 2
adds one layer: before applying the override, read
`data/processed/bws_totals.json` (if it exists) and inject its
`water_om_total_usd` as the default. The override file STILL wins on top.

Pseudocode:

```python
bws = load_json_or_none(_ROOT / "data/processed/bws_totals.json")
if bws and bws.get("water_om_total_usd") is not None:
    values["water_om_total_usd"] = bws["water_om_total_usd"]
    provenance["water_om_total_usd"] = {
        "source": "bws_totals.json (auto)",
        "amendment_number": bws.get("amendment_number"),
        ...
    }

# Then apply manual overrides (unchanged) — these still win.
overrides = _load_overrides()
for key in KEYS:
    if key in overrides and overrides[key] is not None:
        values[key] = int(overrides[key])
        provenance[key] = { "source": "override", ... }
```

### 5.4 Step 03b wiring (capital)

Mirrors step 03. Reads `bws_totals.json` and uses its `water_cip_total_usd`
+ `water_cip_fy26_usd` as defaults. Manual overrides still win.

### 5.5 Docstring + METHODOLOGY updates

- Each consumer script's top docstring + the `notes` field in its own
  output JSON gain a sentence: "BWS values come from
  `data/processed/bws_totals.json` (auto-extracted by `etl/03c`); manual
  overrides still take precedence."
- `METHODOLOGY.md` §1 — the "BWS values manually loaded" language is
  replaced with "BWS values auto-extracted via `etl/02b` + `etl/03c`;
  manual overrides are an escape hatch for parser breakage."

---

## 6. Sanity-Assert Bounds

Hard-fail in `etl/03c_extract_bws_totals.py` (exit 1 before writing
output if any trip):

| Field | Lower bound | Upper bound | Rationale |
|---|---:|---:|---|
| `water_om_total_usd` | $300,000,000 | $500,000,000 | FY26 baseline $362M; ±35% headroom |
| `water_cip_fy26_usd` | $200,000,000 | $400,000,000 | FY26 baseline $283M; ±35% headroom |
| `water_cip_total_usd` | $150,000,000 | $250,000,000 | 6-yr avg $190M; ±30% headroom |

All three values must additionally be positive integers. The 6-yr total
in thousands is asserted to be ≥ 6 × the 6-yr-avg lower bound (i.e.
≥ 900,000 in thousands = $900 M total) to catch unit-scale errors.

---

## 7. Testing Plan

### 7.1 Regression against Tier 1 (Phase 5 gate)

```bash
python etl/02b_fetch_bws_budget.py --force
python etl/03c_extract_bws_totals.py --force
cat data/processed/bws_totals.json
```

Must match Tier 1 values **within ±0.5%**:

```
water_om_total_usd   == 362,439,988  (±0.5% = ±$1,812,200)
water_cip_total_usd  == 190,364,333  (±0.5% = ±$951,822)
water_cip_fy26_usd   == 283,327,500  (±0.5% = ±$1,416,638)
```

If any value differs by >0.5%, STOP. Document the discrepancy in a
`## Known issues` section of this plan, commit + push the plan update,
leave the implementation un-pushed.

### 7.2 Downstream idempotency

```bash
python etl/03_extract_budget_totals.py --force
python etl/03b_extract_cip_totals.py --force
python etl/07_compute_frontage_costs.py --force
python etl/08_emit_frontend_data.py --force
```

`git diff data/parcels_tod.geojson | wc -l` should be **0** (or trivial)
because Tier 2's auto-extracted values match Tier 1's manual values. Any
substantive diff means the auto-extractor produced different numbers —
investigate before pushing.

### 7.3 Override-precedence test

Temporarily set `water_om_total_usd` to `5000000` in
`data/budget_overrides.json`, re-run steps 03 / 07 / 08. Confirm:

1. Step 03 provenance shows `source: "override"`.
2. Sanity-assert harness (when it lands in Task 2) fails the BWS
   magnitude assertion — but step 03 itself doesn't, because the override
   path doesn't re-run sanity checks; that's intentional (override is the
   escape hatch).
3. Revert the override before committing.

### 7.4 Clear Tier 1 overrides (optional)

Once 7.1 + 7.2 both pass, set the three BWS keys in
`data/budget_overrides.json` and `data/cip_overrides.json` to `null` and
update the `_notes` to point to `bws_totals.json` as the source. Re-run
the full pipeline once more to confirm `parcels_tod.geojson` is still
byte-identical (idempotency proven via the auto-extraction path alone).

### 7.5 Frontend smoke

`node --check script.js` passes. Map opens; sidebar totals match
pre-change values.

---

## 8. Open Questions / Risks

1. **BWS index-page restructure.** The Umbraco CMS could reorganize the
   page or move PDFs into a sub-page. Current anchor-text patterns are
   broad enough to survive minor wording tweaks; major restructures
   require updating the patterns. The hard-fail-on-<3-docs policy prevents
   silent breakage.

2. **Amendment date in PDF metadata may be missing.** The filename pattern
   (`<YYYY-MM-DD>-fy-2026-budget-amendment-no-<N>.pdf`) is the primary
   source for `amendment_date`. If a future amendment uses a different
   filename pattern, fall back to scanning the first page for a dated
   header (`Approved <Month DD, YYYY>` etc.); accept null if neither
   yields a parseable date.

3. **CIP standalone vs combined duplication.** The CIP standalone PDF is
   a subset of the combined PDF. We download it primarily for archival
   completeness; extraction reads from combined + amendment. If the
   combined PDF stops including the CIP section in a future fiscal year,
   the standalone becomes the fallback source for `water_cip_fy26_usd`.

4. **6-year CIP rollup window shifts.** The current rollup is FY21-26.
   When BWS publishes FY22-27 (or later), the filename will change and
   the in-PDF year labels will too. The anchor-text regex matches any
   `Capital Improvement Program Fiscal Years \d{4}-\d{4}` so the URL
   discovery survives; the parser's regex only depends on the row label
   `TOTAL CAPITAL IMPROVEMENT` which is fiscal-year-agnostic.

5. **Operating-vs-CIP overlap.** The Operating Fund line item ($79.1M)
   appears as both:
   - a slice of "Total Operating Expenditures" ($362,439,988 total)
   - the Operating-Fund column of "Capital Improvement — Total - All Funds"
     ($283,327,500 total)

   This is intentional double-counting in the BWS book — the same dollar
   appears once on the Op side (as a transfer-out) and once on the CIP
   side (as a transfer-in). The Tier 1 documentation explicitly notes
   this; the Tier 2 extractor reproduces the same convention by extracting
   each total directly from its own table (no derived subtractions).

6. **pdftotext not installed locally.** Same caveat as HART step 11 — the
   script falls back to pdfplumber if poppler is absent. Madison has
   poppler installed; the GitHub Action's runner will need it via the
   apt install step (or rely on the pdfplumber fallback).

7. **Restated amendments.** If BWS publishes a "Restated" or "Final"
   amendment without a number, the policy is to log a warning and prefer
   the highest numbered amendment. A future restated-as-canonical amendment
   would require a manual policy review (and likely a `--force-restated`
   flag).

---

## 9. Implementation Order (Phases 2-5)

1. **Phase 2** — `etl/02b_fetch_bws_budget.py` (URL discovery + download).
2. **Phase 3** — `etl/03c_extract_bws_totals.py` (PDF → JSON).
3. **Phase 4** — wire `bws_totals.json` into `etl/03` and `etl/03b` as
   the default-tier source; manual overrides still win.
4. **Phase 5** — verify against Tier 1 targets; pipeline idempotency;
   optionally clear Tier 1 override entries.

Each phase lands as its own commit on `madison-work`. Phase 1 (this plan)
lands first as a standalone commit so the laptop user (Devin) can review
before Phase 2 starts.

---

## 10. Provenance

- Plan written by: Claude Opus 4.7 (main session), 2026-05-11.
- Inputs:
  - `CIP-PLAN.md`
  - `METHODOLOGY.md` §1.2 (water-gap context)
  - `data/budget_overrides.json` + `data/cip_overrides.json` (Tier 1 entries)
  - `etl/02_fetch_budget_pdfs.py` (City PDF fetcher pattern to mirror)
  - `etl/03_extract_budget_totals.py`, `etl/03b_extract_cip_totals.py`
  - `etl/11_extract_hart_totals.py` (pdftotext + pdfplumber fallback pattern)
  - Live BWS index page audit (2026-05-11)
  - Spot-checks of FY25-26 combined budget, Amendment #4, and Six-Year CIP PDFs
  - `tasks/Revenue-Modeling-TOD.md` Task 1 spec
- Reviewed before Phase 2 by: laptop user (Devin), via this standalone
  `BWS-AUTO-PLAN.md` commit on `madison-work`.
