# CIP Funding Integration — Plan

Adds Capital Improvement Program (CIP) costs alongside the existing O&M
costs in the Honolulu Skyline TOD revenue/cost map. Without CIP, the
"true cost to serve" is understated and net-revenue numbers are too rosy.

This is a **plan only** — implementation lands in subsequent commits.

---

## 1. Context

Today's `cost_per_ac` on `data/parcels_tod.geojson` is O&M only:

```
cost_per_ac = (road_om + sewer_om + water_om) annual frontage-prorated / acres
```

with the three FY26 numerators extracted in `etl/03_extract_budget_totals.py`
from `data/raw/budget/operating_fy26.pdf`. The capital PDF
(`data/raw/budget/capital_fy26.pdf`, 528 pp) was downloaded by step 02 but
never parsed. CIP is the missing half: new construction, replacement,
expansion of the same systems.

Per Urban3, total cost-to-serve = annual O&M + annualized CIP. Closing
this gap is **methodology limitation #5** in `METHODOLOGY.md`.

---

## 2. Data extraction

### 2.1 Capital PDF structure

Each project in `capital_fy26.pdf` is one page with a phase × fund-source
table broken out across `Encumb / Appn 2024 / Appn 2025 / 2026 / 2027 /
2028 / 2029 / 2030 / 2031 / Total 6 Years / Future Years` columns. Numbers
are **in thousands** (footer: "dollars in thousands").

Projects roll up to **Program Summary** pages — these are the right
extraction targets because they aggregate every project under a function
into one totals table. Found by scanning `data/raw/budget/extracted_tables.json`:

| Program Summary | Page | Maps to | Notes |
|---|---|---|---|
| Highways, Streets And Roadways | 181 | road | Roadway rehab, intersections, ADA, etc. |
| Bridges, Viaducts And Grade Separation | 185 | road | Bridge replacement & repair |
| Storm Drainage | 215 | road | Drains physically follow roads in Honolulu |
| Street Lighting | 219 | road | Tied to road network |
| Sewage Collection And Disposal | 370 | sewer | Sand Island, mains, pump stations |
| Improvement District-Sewers | 252 | sewer | District-funded sewer extensions |
| (Mass Transit) | 522 | excluded | Skyline / TheBus capital is HART/DTS, not infra-to-serve a parcel |
| (Bikeways And Bikepaths) | 146 | excluded | Optional infrastructure, not in O&M denominator |
| (Waste Collection And Disposal) | 241 | excluded | Refuse — out of scope (no O&M counterpart in v1) |

**Water CIP is null.** The Board of Water Supply is semi-autonomous and
publishes a separate budget; it isn't in `capital_fy26.pdf`. Same gap as
water O&M (METHODOLOGY §1.2). Override pattern: drop a value into
`data/cip_overrides.json` to manually supply it.

### 2.2 Annualization decision

CIP is famously lumpy: Sand Island Treatment Plant alone shows $1.5B in
FY28 and ~$50M in other years. Single-year FY26 numbers would mis-cost
parcels in the year the project happens to fall.

**Recommendation: 6-year average** (`Total 6 Years ÷ 6`). Smooths the
lumpiness, gives a representative annual CIP burden, comparable scale to
annual O&M. The alternative (FY26 only) understates road CIP in years
where major projects haven't started.

### 2.3 Filter rule

Within each Program Summary, take the **`Total` row's `Total 6 Years`
column** as the authoritative numerator, then divide by 6.

```
road_cip_total_usd  = (Σ Total 6 Years across the 4 road programs) × 1,000 / 6
sewer_cip_total_usd = (Σ Total 6 Years across the 2 sewer programs) × 1,000 / 6
water_cip_total_usd = override or null
```

The `× 1,000` is because the PDF prints in thousands.

### 2.4 New ETL step: `etl/03b_extract_cip_totals.py`

Mirrors step 03's shape:
- Reads `capital_fy26.pdf` via pdfplumber.
- Locates each Program Summary page by label match (`Program Summary: Highways, Streets And Roadways`).
- Pulls the FY26 column and the Total-6-Years column from the table's `Total` row.
- Sums per category (road / sewer / water) and annualizes to USD/year.
- Writes `data/processed/cip_totals.json`:
  ```json
  {
    "road_cip_total_usd":   ...,
    "sewer_cip_total_usd":  ...,
    "water_cip_total_usd":  null,
    "road_cip_fy26_usd":    ...,
    "sewer_cip_fy26_usd":   ...,
    "annualization":        "6yr_avg",
    "source_pdfs":          ["data/raw/budget/capital_fy26.pdf"],
    "extracted_at":         "...",
    "provenance": { "<key>": { "page": ..., "program": "...", "total_6yr_thousands": ..., "fy26_thousands": ... } },
    "notes": "..."
  }
  ```
- Reuses the audit pattern (`extracted_tables.json` already includes capital).
- Manual override file `data/cip_overrides.json` — same precedence as O&M.

Decision: separate file `cip_totals.json` rather than co-mingling with
`budget_totals.json`. Cleaner manifest, separate provenance, and the
override files don't collide.

---

## 3. Per-parcel attribution

### 3.1 Method: frontage proration

**Recommendation: same frontage-proration method as O&M.** The CIP for
roads/sewers is dominated by linear-infrastructure work (rehab, mains,
extensions) that scales with linear feet of network. Frontage proration
is the standard Urban3 approach and gives a single coherent cost model.

Alternatives considered:
- **Area-prorated** — over-allocates to large vacant lots that don't actually drive infrastructure.
- **Service area** — would need explicit service-area maps we don't have.
- **Mixed** (frontage for linear, area for facility-based projects) — adds complexity without obvious gain at v1 fidelity.

Same denominators as O&M (filtered citywide road centerline ft, with
sewer/water using road as proxy per METHODOLOGY §6.4). This means the
proxy-fallback caveat applies to CIP too.

### 3.2 Implementation

Extend `etl/07_compute_frontage_costs.py` (don't fork — the frontage
geometry computation is identical; only the rates differ):

```python
# loaded alongside budget_totals.json
cip = json.loads(CIP_PATH.read_text())

cip_road_rate  = _rate(cip.get("road_cip_total_usd"),  total_road_ft)
cip_sewer_rate = _rate(cip.get("sewer_cip_total_usd"), total_sewer_ft)
cip_water_rate = _rate(cip.get("water_cip_total_usd"), total_water_ft)
```

Per parcel, multiply existing frontage values by the new rates:

```python
parcels_unique["cip_road_usd"]  = road_ft  * cip_road_rate
parcels_unique["cip_sewer_usd"] = sewer_ft * cip_sewer_rate
parcels_unique["cip_water_usd"] = water_ft * cip_water_rate

cip_total          = nansum(cip_road, cip_sewer, cip_water)
parcels["cip_per_ac"] = cip_total / area_ac
```

Output gains: `cip_road_usd`, `cip_sewer_usd`, `cip_water_usd`,
`cip_total_usd`, `cip_per_ac` columns alongside existing
`cost_*` columns.

---

## 4. Frontend changes

### 4.1 Cost-meaning decision: redefine `cost_per_ac` as O&M + CIP

Three options were on the table:
- **A. Extend `cost_per_ac` to mean total** (O&M + CIP). ← **chosen**
- B. Keep `cost_per_ac` = O&M only; add CIP as a 4th metric button.
- C. Keep `cost_per_ac` = O&M; show CIP only in the click popup.

Picked A. Rationale:
- The scientific story is "true cost to serve, vs revenue." Splitting O&M
  and CIP into separate metric buttons puts the user in charge of
  interpretation and makes "net" ambiguous (net of what?).
- Net per acre is the iconic Urban3 frame; it has to be `revenue − total cost`.
- Bar height stays revenue, so the canonical Urban3 visual is unchanged.
  Only the color (in cost/net mode) reflects the new total.
- Popup keeps the breakdown so users can see O&M-only and CIP-only
  amounts when curious.

### 4.2 Field changes on `data/parcels_tod.geojson`

| Field | v1 (now) | v1.1 (this change) |
|---|---|---|
| `cost_per_ac` | O&M frontage cost / ac | **(O&M + CIP) frontage cost / ac** |
| `cost_om_per_ac` | (does not exist) | **NEW** — O&M-only / ac |
| `cip_per_ac` | (does not exist) | **NEW** — CIP-only / ac |
| `net_per_ac` | rev − O&M | **rev − (O&M + CIP)** |

`cost_om_per_ac` is added so the popup can show the breakdown without
re-reading the cost frontage map.

### 4.3 Frontend code touchpoints

- `etl/08_emit_frontend_data.py` — carry `cost_om_per_ac`, `cip_per_ac`
  through. Update `cost_per_ac` to be `cost_om_per_ac + cip_per_ac`
  (NaN-safe). Recompute `net_per_ac`.
- `script.js`:
  - No `METRIC_KEYS` change — `cost_per_ac` still maps to the Cost button.
  - `buildPopupHTML` Assessment section: split "Infrastructure cost" into
    "Operating cost (O&M)" + "Capital cost (CIP)" + "Total cost" rows.
    Per-acre section gets the analogous breakdown.
  - `FISCAL_YEAR` constant already covers both — no change.
- `index.html`, `styles.css` — no structural change. Same 3-button
  segmented control, same diverging palette.

### 4.4 Backwards compatibility / migration

This redefines `cost_per_ac` and `net_per_ac`. Anyone with bookmarked
screenshots showing "cost = X" will see different numbers post-merge.
Acceptable risk — v1 is internal/exploratory, not yet a public artifact.

`METHODOLOGY.md` will be updated to flag the change explicitly. No data
migration script needed (the geojson is regenerated end-to-end from raw
inputs).

The old O&M-only number remains available as `cost_om_per_ac` for one
release cycle so anyone investigating historical outputs can still get
the v1 number directly.

---

## 5. Testing plan

Per the task spec, three layers of verification:

### 5.1 Sanity on extracted totals

- Print `cip_totals.json` after running 03b. Verify:
  - `road_cip_total_usd` in $20M–$200M annualized range (sanity: typical road CIP for a city of Honolulu's size).
  - `sewer_cip_total_usd` in $50M–$1B annualized range (Sand Island alone is multi-billion over 6 years).
  - `water_cip_total_usd` null with notes about override mechanism.
  - Provenance maps each number back to a specific page + program label.

### 5.2 Per-parcel sum-to-budget reconciliation

After step 07 runs:
```
Σ (cip_per_ac × area_ac) across all parcels in parcels_tod.geojson
  ≈ Σ extracted CIP totals × (TOD-acres / city-acres)
```

But because v1 attributes only to TOD-walkshed parcels (not the whole
city), we expect the sum to be ~5–15% of the citywide totals (TOD area
is a fraction of the island). Reconciliation: compare the per-parcel
frontage-weighted sum against the extracted totals × (TOD-frontage-ft
÷ citywide-frontage-ft). Target: within ±5%.

If delta > 5%, investigate before committing the implementation.

### 5.3 Distribution and spot checks

- Print `min/p10/p50/p90/p99/max` of `cip_per_ac` across all parcels.
  Verify no negatives, no NaN/Inf, p99 not 100× median.
- Spot-check 5 parcels:
  - Small residential lot
  - Large industrial parcel
  - Downtown high-rise
  - Kalihi parcel
  - Ahua-area parcel (per task)

  Print their `cip_per_ac`, `cost_om_per_ac`, `cost_per_ac`, frontage,
  area, and eyeball plausibility. CIP-per-ac should be on the order of
  the O&M-per-ac (perhaps 0.5×–3×).

### 5.4 Regression snapshot

Before any implementation work in phase 2, capture
`data/_pre_cip_snapshot.json` (gitignored) recording
`{tmk: {rev, cost_om, net_om}}` for ~50 sentinel parcels (the same 5
spot-checks plus 45 random). After phase 2, diff:
- `rev_per_ac` must be unchanged (within float epsilon).
- `cost_om_per_ac` must equal pre-CIP `cost_per_ac` (within ε).
- `cost_per_ac` should equal `cost_om_per_ac + cip_per_ac` (within ε).

### 5.5 Frontend smoke

`python3 -m http.server 8000` at repo root. Verify with `curl` and a
JSON parse that `data/parcels_tod.geojson` includes `cip_per_ac` and
`cost_om_per_ac` on every feature, and `cost_per_ac` value differs from
the v1 value. `node --check script.js` for syntax. Visual rendering
verification deferred to a human session — flagged in the final
implementation summary.

---

## 6. Open questions / risks

1. **Storm Drainage / Street Lighting inclusion in road CIP** — defensible
   (they physically follow roads in Honolulu and are city-maintained), but
   one could argue Street Lighting should be its own bucket. v1.1 lumps
   them in; can split later if a stakeholder objects.

2. **Bridge CIP per parcel** — Bridges aren't really frontage-prorated in
   reality; a single-bridge lump-sum gets distributed across all parcels
   citywide. This is consistent with how O&M handles paved-road maintenance
   (citywide rate × per-parcel frontage), so we accept the approximation.

3. **Water CIP gap** — same as water O&M. Underestimates total cost by
   roughly the size of BWS CIP (likely tens of millions/year).
   Documented in METHODOLOGY; can be filled with `cip_overrides.json`
   later from the BWS Six-Year CIP report.

4. **6-year-average vs FY26-only** — averaging hides project timing.
   For a stakeholder asking "what is FY26 CIP exactly?", we expose
   `road_cip_fy26_usd` etc. in `cip_totals.json` provenance even though
   the per-parcel rates use the smoothed value.

5. **Mass Transit Program Summary excluded** — the rationale is that
   Skyline/TheBus capital is funded by HART/DTS and reflects regional
   transit investment, not parcel-level cost-to-serve. If a reviewer
   disagrees, we can add it back as `transit_cip_total_usd` and surface
   it as a separate metric in the frontend.

6. **Annualization swap** — if we later decide to switch from 6yr-avg
   to FY26-only, only `etl/03b` changes; downstream code is rate-agnostic.

---

## 7. Implementation order (Phase 2)

1. Snapshot 50 sentinel parcels → `data/_pre_cip_snapshot.json`.
2. `etl/03b_extract_cip_totals.py` + `data/cip_overrides.json` template.
3. Extend `etl/07_compute_frontage_costs.py` — emit `cost_om_*` and `cip_*` columns; `cost_per_ac` becomes total.
4. Extend `etl/08_emit_frontend_data.py` — pass `cost_om_per_ac` + `cip_per_ac` through.
5. `script.js buildPopupHTML` — split breakdown rows.
6. `METHODOLOGY.md` + `README.md` updates.
7. Tests (5.1–5.5).
8. Commit in logical chunks; only push if all tests pass.

---

## 8. Provenance

- Plan written by: Claude Opus 4.7 (madison remote agent worklist), 2026-05-08.
- Inputs: `METHODOLOGY.md`, `CLAUDE.md`, `etl/03_extract_budget_totals.py`, `etl/07_compute_frontage_costs.py`, `etl/08_emit_frontend_data.py`, `data/raw/budget/extracted_tables.json` audit dump.
- Reviewed before phase 2 by: laptop user (Devin), via standalone CIP-PLAN.md commit on `madison-work`.
