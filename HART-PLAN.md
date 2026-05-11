# HART Rail Capital — Data Capture + Dashboard Toggle Plan

This document is the Phase 1 plan for Task 3 of the Revenue-Modeling-TOD
project. Implementation lands in subsequent commits. Do NOT implement until
this document is committed alone.

---

## 1. Context

The cost model captures county roads/sewer/water (O&M + CIP, frontage-
prorated). Rail (HART) is the largest single piece of corridor infrastructure
and is absent: HART capital (~$10–12B total program) is funded by C&C GET
surcharge + FTA FFGA + property-tax surcharge — it is NOT in the state budget
(verified against HB 1800 SD1 in BudgetPrimerFinal — zero HART line items)
and is NOT in the Honolulu City budget PDFs used by `etl/02_fetch_budget_pdfs.py`.

Without rail CIP, the `cost_per_ac` understates the true infrastructure burden
on TOD parcels — the corridors whose walkability scores are the entire premise
of the analysis are only viable because of a ~$10B+ rail investment.

This task:
1. Fetches HART's published financial documents (`etl/10_fetch_hart_docs.py`).
2. Extracts total program cost + FY26 capital + funding mix (`etl/11_extract_hart_totals.py`).
3. Computes a per-parcel `rail_cip_per_ac` using corridor-uniform attribution (`etl/12_compute_rail_cip.py`).
4. Wires `rail_cip_per_ac` into each parcel's GeoJSON properties (step 08 extension).
5. Adds a sidebar toggle in `script.js` (default OFF) that adds `rail_cip_per_ac`
   to the active mode's cost before color-ramp evaluation.

**Default: toggle OFF** — map behavior on first load is byte-identical to pre-change.

**Independent of tasks 1+2** (BWS Tier-2 and sanity-assert harness). No shared
files; can be worked in parallel.

---

## 2. URL Discovery

### 2.1 Index page

```
https://www.honolulutransit.org/about/financial-information/
```

Fetch with `common/http_client.fetch_text`. Parse with stdlib `html.parser`
(no new dependency). Match anchor tags whose text (lowercased, whitespace-
collapsed) matches:

| Pattern | Purpose | Download to |
|---|---|---|
| `five.?year financial plan` or `5.?year financial plan` | Latest 5-Year plan | `data/raw/hart/five_year_plan.pdf` |
| `recovery plan` | Sept 2022 baseline program estimate | `data/raw/hart/recovery_plan.pdf` |
| `annual report.*fy\s*\d+` | Latest HART annual report | `data/raw/hart/annual_report.pdf` |

If the index page redirects, follow with `urllib.request` automatically (urllib
follows HTTP 301/302 by default).

### 2.2 Failure policy

Hard-fail (exit 1) if fewer than 2 of the 3 expected document types are found.
Print which types are missing. Do not proceed with a partial cache to avoid
silent stale-data issues.

### 2.3 Idempotency + forced refresh

Skip download if the target PDF and its manifest sidecar both exist and
`--force` is NOT passed. No version-keyed refresh (unlike the BWS amendment
policy) because HART document URLs appear to be consistent slugs on
honolulutransit.org rather than rotating CMS hashes. If a new annual
report replaces a prior one at the same URL, `--force` is the manual refresh
trigger.

---

## 3. Parser Design

### 3.1 Parser choice: pdftotext -layout

HART PDFs use complex multi-column table layouts with merged cells and landscape
orientation. `pdfplumber.extract_tables()` (used for City budget PDFs in steps
03/03b) produces unreliable cell boundaries on such documents. BWS Tier-2
adopted `pdftotext -layout` (poppler-utils) as the authoritative parser — we
mirror that choice here.

```bash
pdftotext -layout <pdf_path> -    # stdout → captured in Python subprocess
```

Validate `pdftotext` exists at script start:
```python
import shutil
if not shutil.which("pdftotext"):
    sys.exit("pdftotext not found. Install: brew install poppler")
```

**README.md dependency note**: add `pdftotext (poppler-utils)` to the
install section alongside Python + pip deps.

### 3.2 Target lines + regexes

**3.2.1 From the Recovery Plan (or 5-Year plan headline)**:

Target: total program capital cost.

```
regex: r"(?i)(total\s+program\s+cost|revised\s+project\s+budget)\s*\$?([\d,]+)"
```

Expected: $9–12B range. Capture the dollar amount, strip commas, multiply
by 1,000 if the table heading says "in thousands" (check first 3 pages for
"in thousands" / "thousands of dollars").

Fallback: scan for the largest dollar figure on the "Program Budget Summary"
or "Project Budget" page. If multiple candidates, prefer the one on a row
whose label contains "total" or "revised".

**3.2.2 From the 5-Year Financial Plan (FY26 column)**:

Target: FY26 capital expenditure (capital outlay / capital program column).

```
regex: r"(?i)fy\s*2026.*?\$([\d,]+)"   # or scan column by header position
```

The 5-year plan typically has a column header row like:
`| FY 2025 | FY 2026 | FY 2027 | FY 2028 | FY 2029 |`

Locate the FY 2026 column index, then scan rows whose label matches
`capital` / `expenditure` / `capital program` / `total capital`.

**3.2.3 Funding mix (best-effort)**:

Target: breakdown by source (FTA FFGA, GET surcharge, property-tax surcharge,
bonds). Parse the funding-source table in the Recovery Plan or 5-Year plan.
Set null for components that are not separately disclosed. Store as
`funding_mix` dict in `hart_totals.json`. This is informational only — it
does NOT affect the per-parcel rate calculation.

### 3.3 Scaling / unit normalization

After extraction:
1. Strip `$`, `,`, whitespace.
2. If the surrounding text contains "in millions" → multiply by 1,000,000.
3. If "in thousands" or "in $000s" → multiply by 1,000.
4. Else assume the number is already in USD.
5. Floor to integer.

---

## 4. Annualization Decision (30-year horizon)

**Choice: 30-year straight-line annualization.**

```
annualized_capital_usd = floor(total_program_cost_usd / 30)
```

Rationale:
- Road/sewer CIP uses the 6-year programmatic period available in the City
  budget book. HART doesn't have a comparable 6-year table — the natural
  unit is the full program lifecycle.
- 30 years matches the bond debt-service period typical for major transit
  capital and infrastructure bonds in Hawaii. It is also the commonly cited
  depreciation horizon for transit vehicles and most fixed guideway
  components.
- Using FY26 capital expenditure alone ($~0.5–1B in active construction
  year) would over-represent the cost attributable to that single year.
  Using total program / 30 produces a stable long-run annual burden.

The FY26 single-year capital figure (`fy26_capital_usd`) is preserved in
`hart_totals.json` for reference / audit.

---

## 5. Attribution Method (Corridor-Uniform $/acre)

**Choice: corridor-uniform `rail_cip_per_ac`.**

```
total_unique_corridor_acreage = sum(area_ac for unique TMKs in parcels_tod.geojson)
rail_cip_per_ac = annualized_capital_usd / total_unique_corridor_acreage
```

Every parcel in any walkshed gets the identical `rail_cip_per_ac` value.

Rationale:
- Matches the existing per-acre framing (all other costs are also $/ac).
- Does not require verified distance-decay parameters or per-station
  ridership/capacity weights that we don't have.
- Defensible: the rail line serves the corridor as a whole; the TOD Special
  District rationale is that proximity to any station justifies intensification.
- Simple to audit: `rail_cip_per_ac × total_unique_corridor_acreage ≈ annualized_capital_usd` (within $1 of floating-point floor).

**Deferred refinements** (not in this task):
- Distance-decay from nearest station (weight parcels closer to station more).
- Per-station capacity weighting (stations with higher projected ridership
  bear more of the capital burden).
- Segment-level breakdown (West Oahu/Farrington vs Kamehameha vs Airport
  sections have different per-mile costs).

**DO NOT** add `rail_cip_per_ac` to `cost_per_ac` or `net_per_ac` in the
GeoJSON. The toggle is frontend-only. The server-side parcel cost model
remains unchanged — only the new `rail_cip_per_ac` property is added.

---

## 6. Sanity-Assert Bounds

These are hard-fail assertions in `etl/11_extract_hart_totals.py` (exit 1
before writing output if any trip):

| Field | Lower bound | Upper bound | Rationale |
|---|---|---|---|
| `total_program_cost_usd` | $8,000,000,000 | $15,000,000,000 | Published range $9–12B; $8B low buffer for revision; $15B hard ceiling |
| `fy26_capital_usd` | $100,000,000 | $1,500,000,000 | Active construction phase; $1.5B ceiling handles a large single-year tranche |
| `annualized_capital_usd` | $1 | n/a | Any positive value (floor of total/30); guard against zero/negative |

---

## 7. Dashboard-Toggle UX

### 7.1 UI element

Add a checkbox in the sidebar below the 3D toggle:
```html
<label class="toggle-rail">
  <input type="checkbox" id="chk-rail-cip">
  Include rail CIP in cost
  <span class="methodology-note">(methodology pending review)</span>
</label>
```

Default: unchecked (`chk-rail-cip.checked === false`).

### 7.2 Behavior when checked

The toggle modifies the JS color-ramp computation for the active mode
(`revenue` / `cost` / `net`) before `getFillColor` is called:

```js
const railAdj = chkRailCip.checked
  ? (feature.properties.rail_cip_per_ac || 0)
  : 0;

// in cost mode:
effectiveCost = cost_per_ac + railAdj;

// in net mode:
effectiveNet  = rev_per_ac - effectiveCost;
```

Recompute `p2` / `p98` domain endpoints from the adjusted values for the
currently visible parcels. Update `updateTriggers` on the deck.gl
`GeoJsonLayer` so deck.gl knows to re-evaluate `getFillColor`/`getLineColor`.
Recompute the summary-panel totals.

Legend min/max rescales to the adjusted domain.

### 7.3 Behavior when unchecked

Pre-change behavior is byte-identical: `railAdj = 0`, no effect on existing
color or cost values.

### 7.4 Accessibility + discovery

Add a small `ⓘ` tooltip on hover explaining:
> "HART rail capital (~$9–12B total program) annualized over 30 years and
> distributed uniformly across all corridor parcels by acreage. Methodology
> subject to review — default OFF."

---

## 8. Wiring into Step 08

Modify `etl/08_emit_frontend_data.py` to:
1. Read `data/processed/rail_cip.json` if it exists.
2. Extract the constant `rail_cip_per_ac` value.
3. Add it as a property on every parcel feature in the output GeoJSON.
4. If `rail_cip.json` doesn't exist, omit the property silently (step 08
   must remain runnable without step 12 having been run).

The property is added alongside existing `cost_per_ac`, `cip_per_ac`, etc.
It does NOT alter `cost_per_ac`, `net_per_ac`, or any existing field.

---

## 9. Testing Plan

### 9.1 ETL verification (Phase 6)

```bash
python etl/10_fetch_hart_docs.py --force
python etl/11_extract_hart_totals.py --force
python etl/12_compute_rail_cip.py --force
python etl/08_emit_frontend_data.py --force
```

1. Print `data/processed/hart_totals.json` — verify sanity-assert bounds pass.
2. Print `data/processed/rail_cip.json` — verify:
   - `rail_cip_per_ac × total_unique_corridor_acreage ≈ annualized_capital_usd` (within $1).
   - `total_unique_corridor_acreage` matches parcel count (19,872 parcels, but unique
     acreage is the sum of `area_ac` per unique TMK).
3. Confirm `data/parcels_tod.geojson` features now have `rail_cip_per_ac` property.
4. Confirm `data/parcels_tod.geojson` `cost_per_ac` and `net_per_ac` are unchanged
   from pre-run values (toggle is frontend-only).

### 9.2 Frontend smoke

- `node --check script.js` passes.
- Open the map. Default state (toggle unchecked): map is byte-identical to
  pre-change behavior.
- Check the toggle: every parcel's displayed cost increases by exactly
  `rail_cip_per_ac` (constant across all parcels). Legend rescales.
- Uncheck: reverts to original values.

### 9.3 Regression

`git diff data/parcels_tod.geojson` should show only the new
`rail_cip_per_ac` property added to each feature — no changes to
`cost_per_ac`, `cip_per_ac`, `net_per_ac`, `rev_per_ac`, or geometry.

---

## 10. Open Questions / Risks

1. **HART page structure may change.** The index page at
   `https://www.honolulutransit.org/about/financial-information/` could be
   restructured or the relevant links moved to sub-pages. If `<2 of 3`
   docs are found, the script hard-fails — investigate the page HTML and
   update the anchor-text regexes.

2. **Program cost figure ambiguity.** HART has published multiple program
   budget estimates over time (original $5.1B baseline, ~$9.2B Recovery Plan
   baseline, current revised estimate ~$10–12B). If multiple "total program
   cost" figures appear, prefer the most recent (highest amendment number or
   most recent date in the document header). Document the chosen figure and
   its source page in provenance.

3. **PDF OCR quality.** HART PDFs are sometimes scanned / image-based in
   older annual reports. `pdftotext -layout` produces empty output for
   image-only pages. Detect empty output (< 100 chars after stripping
   whitespace) and hard-fail with a clear message asking the user to check
   if the PDF is image-based.

4. **Corridor-uniform rate is methodology-defensible but may be contentious.**
   Distributing $10B over ~3,700 unique corridor acres (rough estimate) yields
   ~$2.7M/ac/30yr = ~$90k/ac/yr. This is a substantial number relative to
   current `cost_per_ac`. The toggle-off default is intentional — this number
   warrants stakeholder review before being presented as a default cost.

5. **Annual report may have no parseable capital figures.** The annual report
   is fetched for completeness (historical actuals) but the primary extraction
   targets are the 5-Year plan and Recovery Plan. If the annual report has no
   relevant table, skip extraction from it and mark `fy26_capital_usd` source
   as "5-year plan" in provenance.

6. **pdftotext not installed.** The `brew install poppler` dependency must be
   documented in README.md before this task lands. Hard-fail with install
   instructions if missing.

---

## 11. Provenance

- Plan written by: Claude Sonnet 4.6 (main session), 2026-05-10.
- Inputs: `CIP-PLAN.md`, `METHODOLOGY.md` §1, `etl/08_emit_frontend_data.py`,
  `common/http_client.py`, `common/manifest.py`,
  `tasks/Revenue-Modeling-TOD.md` Task 3 spec.
- Reviewed before Phase 2 by: laptop user (Devin), via standalone `HART-PLAN.md`
  commit on `madison-work`.
