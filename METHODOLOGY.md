# Methodology

Comprehensive reference for every data source, transformation, approximation,
and assumption in the Revenue-Modeling-TOD pipeline. If you're auditing a
number on the map, this is the authoritative document.

---

## Table of contents

1. [Data sources](#1-data-sources)
2. [Pipeline overview](#2-pipeline-overview)
3. [Catchment definition (walkshed)](#3-catchment-definition-walkshed)
4. [Coordinate reference systems](#4-coordinate-reference-systems)
5. [Revenue computation](#5-revenue-computation)
6. [Cost computation (frontage proration)](#6-cost-computation-frontage-proration)
7. [Frontend visualization](#7-frontend-visualization)
8. [Known approximations and limitations](#8-known-approximations-and-limitations)
9. [Gaps vs. Urban3 reference](#9-gaps-vs-urban3-reference)
10. [Reconciliation checks](#10-reconciliation-checks)
11. [Basemap](#11-basemap)

---

## 1. Data sources

All sources are public, no auth required. Slugs and download URLs are stable
as of 2026-05-07.

### 1.1 Honolulu ArcGIS Open Data Hub

Hub root: <https://honolulu-cchnl.opendata.arcgis.com>
DCAT catalog: `/api/feed/dcat-us/1.1.json`

| Logical name | Hub slug | Used for | Rows | Notes |
|---|---|---|---|---|
| `parcels_tax` | `cchnl::parcels-tax` | Parcel polygons | 171,952 | **Geometry-only.** No tax/assessed values. Despite the name, "Tax" refers to Tax Map Key (TMK), not tax dollars. Backing service: `services.arcgis.com/tNJpAOha4mODLkXz/.../Cadastral_2020/FeatureServer/1`. |
| `rail_transit_station_points` | `cchnl::rail-transit-station-points` | Station centroids | 13 (operating) + planned | Has `ID` (1-N west→east) and `STATION` name fields. We filter to IDs 1–13 (Segments 1+2). |
| `rail_transit_station_footprint` | `cchnl::rail-transit-station-footprint` | Station polygon footprints | 13+ | Not used in v1 but cached for future use. |
| `rail_transit_guideway_alignment_line` | `cchnl::rail-transit-guideway-alignment-line` | Skyline guideway centerline | 12 line features | Used to filter the rail out of road centerlines (otherwise parcels along the guideway get inflated road frontage). Backing service: `services6.arcgis.com/2cZSk3EXXiOHcbOl/.../HART_Guideway_Alignment_Line_PUBLIC`. |
| `road_centerlines` | `cchnl::oahu-street-centerlines` | City road network | 27,289 | Has `owner` field; we filter rows whose value contains "state", "hdot", "freeway", "h-1/2/3", etc. |
| `tod_special_district` | `cchnl::tod-special-district` | **Adopted** TOD Special District boundaries | 7 | Available but **not used in v1** — v1 uses 1-mi walksheds. v2 should switch to these. |
| `zoning` | `cchnl::zoning-5` | DPP zoning | 1,950 | Cached; not used in v1. |
| `asmtgis-table` | `cchnl::asmtgis-table` | RPAD assessment values + exemptions | 325,524 | **Authoritative for revenue.** Joined to parcels on `tmk`. See §5. |
| `asmtpitt-table` | `cchnl::asmtpitt-table` | RPAD tax-class (PITT) lookup | 327,136 | Same schema as `asmtgis`. Cached but redundant in v1 since both `taxratecode` and `ovrclass` columns are empty in the published data. |

**Slugs that don't exist** (verified against the DCAT catalog as of 2026-05-07):
`cchnl::2017_lud_oahu`, `cchnl::centerlines_haw`, `cchnl::sewer-mains`,
`cchnl::water-mains`, `cchnl::bws-water-mains`, `cchnl::potable-water-mains`,
`cchnl::wastewater-mains`. The Hub publishes BWS facility points (hydrants,
pumps) but not network linework — see fallback in §6.4.

### 1.2 Honolulu FY26 Budget PDFs

Source: <https://www4.honolulu.gov/docushare/dsweb/View/Collection-15858>
Files cached to `data/raw/budget/`:

| File | Pages used | Extracts |
|---|---|---|
| `operating_fy26.pdf` | 175 (Road Maintenance), Wastewater O&M section | `road_om_total_usd` ($48,070,770), `sewer_om_total_usd` ($188,675,341) |
| `capital_fy26.pdf` | 181, 185, 215, 219 (road CIP); 252, 370 (sewer CIP) | `road_cip_total_usd` ($123,324,333/yr, 6yr-avg), `sewer_cip_total_usd` ($787,481,666/yr, 6yr-avg) |
| BWS budget book + Amendment #4 (manual override) | n/a | `water_om_total_usd` ($362,439,988), `water_cip_total_usd` ($190,364,333/yr, 6yr-avg), `water_cip_fy26_usd` ($283,327,500) |

Extraction is via pdfplumber's `extract_tables()` with hard-coded page hints
and label regexes (see `etl/03_extract_budget_totals.py` for O&M and
`etl/03b_extract_cip_totals.py` for CIP). Manual override files:
`data/budget_overrides.json` (O&M) and `data/cip_overrides.json` (CIP) —
any non-null value there wins over the PDF extraction.

**Water O&M and Water CIP** are loaded via the override files because the
Board of Water Supply is semi-autonomous and is not in the City budget
PDFs. Sources (retrieved 2026-05-08):

- Water O&M: BWS FY26 Combined Operating + CIP Budget, post-Amendment #4
  (2026-01-14). `water_om_total_usd = $362,439,988` is the FY26 Total
  Operating Expenditures line, correctly disjoint from the $79.1M
  operating-funded slice of CIP (which is captured in CIP).
- Water CIP: BWS Six-Year CIP FY21–26, total $1,142,186,000 ÷ 6 =
  $190,364,333/yr annualized (matches road/sewer CIP methodology). The
  current FY26 single-year all-funds CIP is $283,327,500 (Operating
  Fund $79.1M + SRF $67.9M + Special Expendable $10.9M + Improvement
  $43.4M + Extramural $19M + WIFIA $63M, post-Amendment #4) and is
  preserved in `cip_overrides.json` as `water_cip_fy26_usd` for
  provenance.

If the BWS budget is amended again, update the override files and re-run
the pipeline. A Tier-2 follow-up would automate `etl/02b` (download) +
`etl/03c` (parse) using `pdftotext -layout`; for now the manual override
is the source of truth.

**CIP annualization** — projects in `capital_fy26.pdf` are programmed
across FY26–FY31 with substantial year-to-year lumpiness (e.g. Sand Island
secondary treatment alone is $1.5B in FY28). To produce a representative
annual cost-to-serve, CIP totals are annualized as
`(Σ Total-6-Years) ÷ 6`. The FY26-only column is preserved in
`cip_totals.json.provenance` for reference.

**CIP categories** rolled up from `capital_fy26.pdf` Program Summary pages:

| Category | Programs included |
|---|---|
| Road CIP | Highways, Streets And Roadways · Bridges, Viaducts And Grade Separation · Storm Drainage · Street Lighting |
| Sewer CIP | Sewage Collection And Disposal · Improvement District-Sewers |
| Water CIP | (BWS not in this PDF; loaded via `cip_overrides.json` from BWS Six-Year CIP FY21–26) |

Mass Transit (Skyline/TheBus) and Bikeways are excluded — Transit capital
is a HART/DTS regional investment rather than parcel-level cost-to-serve;
bikeways have no O&M counterpart in v1's budget extraction.

### 1.3 Geographic-only sources we considered but didn't fetch

- **OSM walking network** — for proper walkshed routing. v2 (`osmnx`).
- **Honolulu RPT ordinance** — for class-specific millage rates. The RPAD
  data exposes `taxratecode` but it's empty across all 325k rows in the
  published table, so per-class rates can't be applied even if we had them.

---

## 2. Pipeline overview

Eight numbered ETL steps in `etl/`, orchestrated by `pipeline_run.py`. Each
step is idempotent (skip if cached unless `--force`) and writes a sibling
`<output>.manifest.json` recording source URL, timestamp, row count, and
script version.

```
01_fetch_arcgis        → data/raw/*.geojson           (7 layers + RPAD CSVs via fix_revenue.py)
02_fetch_budget_pdfs   → data/raw/budget/*.pdf
03_extract_budget_totals → data/processed/budget_totals.json    (O&M road/sewer/water)
03b_extract_cip_totals → data/processed/cip_totals.json         (CIP road/sewer/water, 6yr-avg)
04_build_tod_areas     → data/processed/tod_areas.geojson  (6 adopted TOD Special Districts)
05_join_parcels        → data/processed/parcels_in_walksheds.geojson  (envelope = TOD ∪ 1.6-mi station buffer)
05b_walking_distances  → in-place: adds walk_dist_ft + nearest_station_id via NetworkX on Oahu Street Centerlines
06_compute_revenue     → data/processed/parcels_revenue.geojson  (rev_per_ac per parcel)
   (in v1, supplanted by fix_revenue.py — see §5)
07_compute_frontage_costs → data/processed/parcels_costs.geojson  (cost_om_per_ac, cip_per_ac, cost_per_ac per parcel)
08_emit_frontend_data  → data/parcels_tod.geojson + data/stations.geojson  (committed)
```

`etl/04_build_walksheds.py` (legacy 1-mi straight-line walksheds) is preserved
for reference but no longer wired into the pipeline.

The frontend (`index.html` + `script.js` + `styles.css`) consumes only the
two committed `data/*.geojson` files.

---

## 3. Catchment definition (TOD scope)

The dataset envelope is the **union** of two source polygons:

1. **Honolulu's adopted TOD Special District boundaries** (from
   `cchnl::zoning-special-district`, filtered to the 6 records named
   `Transit-Oriented Development Special District`). These cover the central
   stretch of the line (stations 4–9: Hoʻaeʻae through Hālawa). Stations
   1–3 (East Kapolei area) and 10–13 (Pearl Harbor → Middle Street) are
   **not** inside any adopted TOD district as of FY26.

2. **A 1.6-mile straight-line buffer** around each of the 13 operating
   stations (computed in EPSG:32604, UTM Zone 4N, meters). 1.6 mi is
   chosen to safely contain every parcel that could fall within the
   frontend's 1.5-mi walking-distance slider — typical walk-route detour
   factors are 1.05-1.20×.

A parcel is included iff it intersects this union. Each parcel carries:

- `in_tod_area` (bool) — flagged in the popup with a "TOD-zoned" tag.
- `walk_dist_ft` — actual road-network shortest-path distance to nearest
  operating station, computed in `etl/05b_walking_distances.py` via
  NetworkX on Honolulu DPP's Oahu Street Centerlines layer
  (`cchnl::oahu-street-centerlines`, ~27k segments). Highway-class roads
  (street_class = 1, e.g. H-1) are excluded since pedestrians can't use
  them. Distances are computed in EPSG:2783 (HI State Plane Z3, US-survey-
  feet).
- `nearest_station_id` (int) — the station that minimises walk_dist_ft.

The **frontend "TOD scope" slider** (default: 1.5 mi) filters parcels by
`walk_dist_ft`. Parcels with `in_tod_area = true` always pass the filter
regardless of slider position — they're shown for analytical context even
when far from a station by walking. Parcels whose centroid couldn't snap
to the road network (~10 of 19,872, e.g. military complexes) are also
kept so the slider can't silently drop them.

**v2 upgrades** (in priority order):
1. ~~Switch to `cchnl::tod-special-district`~~ — done.
2. ~~Switch to road-network walking distance~~ — done.
2. Compute walking-network catchments via `osmnx` + OSM road network.
3. Account for the rail guideway, freeway, and gulch barriers explicitly.

---

## 4. Coordinate reference systems

| Operation | CRS | EPSG | Why |
|---|---|---|---|
| Buffering (walksheds) | UTM Zone 4N | 32604 | True meters; lat/lon would distort radius |
| Area (acres) | UTM Zone 4N | 32604 | Polygon area in m², ÷ 4046.86 → acres |
| Frontage length | HI State Plane Z3 | 2783 | US-survey-feet directly, no conversion |
| Storage / web rendering | WGS84 | 4326 | What MapLibre and ArcGIS Hub serve |

Mixing CRSs across length/area operations produces 5–10% errors. All
transforms are centralized: revenue uses UTM 4N (meters → acres); cost uses
HI State Plane Z3 (feet directly). Storage/web is always WGS84.

The Esri-computed `Shape__Area` field on the parcels layer is in **degrees²**
(because the layer is published in EPSG:4326). Do not use it; reproject and
recompute.

---

## 5. Revenue computation

### 5.1 The data shape

The Hub's `cchnl::parcels-tax` layer has only parcel **geometry** — no
assessed values, no exemptions, no tax amounts. The actual roll lives in
two RPAD CSV tables on the same Hub:

- `cchnl::asmtgis-table` (325,524 rows) — assessed values + exemptions
- `cchnl::asmtpitt-table` (327,136 rows) — same schema, possibly different snapshot

Schema (both tables, identical):

| Field | Type | Description |
|---|---|---|
| `tmk` | int (8 digits) | Join key. We zero-pad to 8 chars and match on the parcel layer's `tmk` string. |
| `taxyr` | int | Tax year. We filter to 2026. |
| `buildingvalue` | int | Assessed building value (USD). |
| `landvalue` | int | Assessed land value (USD). |
| `buildingexemption` | int | Building exemption amount. |
| `landexemption` | int | Land exemption amount. |
| `tnettaxval` | int | **Net taxable value** = land + building − exemptions. Authoritative for the tax base. |
| `taxratecode` | float | **Empty across all rows in the published table.** Should encode property class (residential A tier 1, commercial, hotel/resort, etc.). |
| `ovrclass` | float | **Empty across all rows.** Override class. |
| `pittsqft` / `pittacre` | int | Likely PITT-system area; equals zero on most rows. |

### 5.2 Computing annual tax

Because `taxratecode` and `ovrclass` are empty, we **cannot** apply per-class
millage rates from the FY26 RPT ordinance. v1 falls back to a flat blended
rate:

```
annual_tax = tnettaxval × FALLBACK_MILLAGE / 1000
```

where `FALLBACK_MILLAGE = 5.70 ($/$1k)` — chosen as a midpoint between
owner-occupied Residential A Tier 1 ($4.50) and the city-wide weighted
average. **This understates commercial parcels** (real rate ~$12.40/$1k)
**and overstates owner-occupied residential** (real rate ~$3.50). The
relative ranking of parcels is preserved, but absolute dollar amounts and
the tall-bar/short-bar contrast in the urban core are muted.

**To fix**: source the FY26 RPT class assignments from elsewhere — possibly
the City Council ordinance PDF, the RPAD online lookup tool, or a request
to RPAD for a class-keyed bulk export. Then update `fix_revenue.py` (or
restore `etl/06_compute_revenue.py` when it's reworked) to apply
class-specific rates.

### 5.3 Field-name auto-detection

The original `etl/06_compute_revenue.py` was written before the data shape
was confirmed and uses candidate-list heuristics for `TAX_FIELD`,
`VALUE_FIELD`, `CLASS_FIELD`, and `TMK_FIELD`. v1 supplants it with a
narrower script (`fix_revenue.py`) that knows the exact RPAD column names.
v2 should consolidate.

### 5.4 What's excluded from "revenue"

Revenue here is **annual real-property tax only**. Urban3's full method also
includes:
- General Excise Tax (GET) allocated by parcel
- Fees and assessments (sewer fee, refuse, etc.)
- Federal / state transfers received by the City
- TIF or special-district capture mechanisms

These are not modeled in v1.

---

## 6. Cost computation (frontage proration)

The "Urban3 classic" approach: every linear foot of city-maintained
infrastructure is allocated to the parcels adjacent to it, weighted by
frontage length. Tall, narrow lots in dense areas end up with low cost-per-
acre; sprawling parcels with long frontages end up with high cost-per-acre.

The cost model has **two components**: annual operating (O&M) and annualized
capital (CIP). They use the same frontage proration with separate per-foot
rates, then sum to the parcel's `cost_per_ac`.

### 6.1 City-wide per-foot rates

Computed once before the per-parcel loop:

**Operating (O&M):**
```
road_om_rate_$/ft  =  48,070,770 / 3,728,867 ft  ≈ $12.89/ft
sewer_om_rate_$/ft = 188,675,341 / 3,728,867 ft  ≈ $50.60/ft  (denom = roads, see §6.4)
water_om_rate_$/ft = 362,439,988 / 3,728,867 ft  ≈ $97.20/ft  (denom = roads, see §6.4)
```

**Capital (CIP, annualized 6yr-avg):**
```
road_cip_rate_$/ft  = 123,324,333 / 3,728,867 ft ≈ $33.07/ft
sewer_cip_rate_$/ft = 787,481,666 / 3,728,867 ft ≈ $211.19/ft  (denom = roads, see §6.4)
water_cip_rate_$/ft = 190,364,333 / 3,728,867 ft ≈ $51.05/ft   (denom = roads, see §6.4)
```

Numerators are FY26 O&M totals (from `operating_fy26.pdf`) and 6-year-average
CIP totals (from `capital_fy26.pdf`'s Program Summary pages, divided by 6).
Denominators are the sum of LineString lengths in the relevant ArcGIS
layer, after filters.

### 6.2 Per-parcel frontage

```python
ROAD_BUFFER_FT = 50.0   # see §6.3
for parcel in unique_parcels:
    p_buf = parcel.buffer(ROAD_BUFFER_FT)         # in EPSG:2783, US-survey-feet
    road_ft  = sum(seg.intersection(p_buf).length for seg in road_index.query(p_buf))
    sewer_ft = sum(...)  # actually equals road_ft because we use the same network
    water_ft = sum(...)
    om_total  = road_ft·road_om_rate  + sewer_ft·sewer_om_rate  + water_ft·water_om_rate
    cip_total = road_ft·road_cip_rate + sewer_ft·sewer_cip_rate + water_ft·water_cip_rate
    cost_total = om_total + cip_total
    cost_per_ac    = cost_total / area_ac    # ← what the frontend "Cost" mode shows
    cost_om_per_ac = om_total   / area_ac    # exposed for popup breakdown
    cip_per_ac     = cip_total  / area_ac    # exposed for popup breakdown
```

We use a `shapely.strtree.STRtree` for the spatial indexes.

### 6.3 Buffer width: 50 ft, not 5 ft

The original plan called for 5 ft as a "snap tolerance." That's wrong in
practice: street centerlines run down the **middle** of a road, so the parcel
edge is typically 15–25 ft from the centerline. A 5-ft buffer flagged 89% of
parcels as landlocked.

50 ft is half a typical right-of-way (35–80 ft for residential streets) plus
a small setback margin. With this buffer, 5.3% of parcels are landlocked
(1,046 of 19,872) — a much more plausible rate for an urban TOD area, mostly
representing flag lots, mid-block parcels behind other parcels, and
condominium common areas.

### 6.4 Sewer / water network: road-centerlines as proxy

The Hub does not publish sewer or water main linework — only facilities
(treatment plants, hydrants, pumps). We fall back to using the **filtered
road centerline network** as a proxy for both, on the assumption that
mains roughly follow streets.

This is a defensible approximation in dense urban areas (sewer and water
genuinely do follow streets) but gets less accurate at the urban fringe
where utility easements diverge from the road network. The approximation
**double-counts** road frontage for the cost computation: each parcel-foot
of frontage contributes to road, sewer, AND water costs.

To improve: request network linework from BWS (water) and ENV (wastewater).
Both are public records available via DPS request, but not pre-published
on the open data hub.

### 6.5 Filters applied to road centerlines

Before any rate or frontage computation, two filters reduce the 27,289 raw
centerlines:

1. **Skyline guideway exclusion (167 dropped)**: Load
   `rail_transit_guideway_alignment_line.geojson`, buffer by 10 ft,
   intersect with each road centerline. Drop the road if ≥50% of its length
   sits inside the guideway buffer (i.e., it is co-located with the rail).
   This avoids inflating road frontage for parcels along the rail line.

2. **State-owned roads (3,013 dropped)**: We auto-detect the owner field
   (`owner` in the published data) and drop rows whose value contains any of:
   `state`, `hdot`, `hi-dot`, `dot`, `freeway`, `interstate`, `fwy`, `h-1`,
   `h-2`, `h-3`. These roads are maintained by HI DOT, not the City.

After filters: **24,109 centerlines kept**, total length 3,728,867 ft.

### 6.6 Edge cases

- **Corner parcels** double-count along both adjacent streets. This is
  correct behavior — they're served by two roads.
- **Landlocked** parcels (`road_ft < 10`) get flagged `landlocked=true`.
  Their cost is zero from roads (and zero from sewer/water by proxy
  fallback).
- **Easements / shared driveways**: v1 ignores. v2 could split frontage
  proportionally among the parcels accessed.
- **Multi-parcel buildings (condos)**: each row in the parcels layer is a
  TMK; multi-unit buildings may be a single TMK or split. We treat each
  parcel-polygon independently.
- **Citywide rate vs. district rate**: rates are city-wide. A finer model
  could compute neighborhood-specific rates, but Honolulu's budget is
  organized by department, not geography, so this isn't possible without
  proxy allocation.

---

## 7. Frontend visualization

The single-page MapLibre app encodes two metrics simultaneously, following
Urban3 convention:

### 7.1 Bivariate encoding

- **Bar height** is **always** revenue per acre (rev_per_ac). Tall = the
  parcel is productive; short = it isn't.
- **Color** reflects the user-selected metric: revenue / cost / net per acre.
  - Revenue & cost: viridis (sequential, dark→bright).
  - Net: red-white-green diverging, centered on $0.

The "Net" view is the iconic Urban3 frame:
- Tall green = high-revenue, profitable (TOD success cases)
- Tall red = high-revenue, still losing money on infra
- Short red = low-revenue subsidized parcels (suburban drains)
- Short green = low-revenue but also low-cost (efficient frugality)

### 7.2 Percentile clipping

Both color and height domains use the **2nd–98th percentile** of currently
visible parcels, not absolute min/max. The Honolulu RPAD distribution is
heavily right-skewed (a few commercial parcels are 100x the median), so a
linear scale from absolute min to absolute max collapses 99% of parcels
into the bottom 5% of the ramp.

The legend displays the 2nd and 98th-percentile values, so absolute numbers
are still readable.

### 7.3 Height curve

```
peak_98 = 98th-percentile rev_per_ac across visible parcels
scale   = 500m / sqrt(peak_98)
height  = max(10m, scale × sqrt(rev_per_ac))
```

Sqrt scaling compresses the long tail (so even low-revenue parcels are
visible), 10m floor prevents zero-revenue parcels from disappearing,
500m peak is roughly 2x the tallest Honolulu high-rise (visible at z14 but
not so tall that it dominates the camera).

### 7.4 Per-station filtering

The "Station" dropdown sets a MapLibre filter on `station_id`. Selecting a
station fits the camera bounds to the matching parcels. Selecting "All
stations" fits to the union of all walksheds.

---

## 8. Known approximations and limitations

Listed in rough order of impact on the displayed numbers:

1. **Flat $5.70/$1k millage rate** (§5.2) — biggest single source of error.
   Understates commercial revenue, overstates Res-A. Rankings preserved,
   absolute amounts off by up to ~2x at the extremes.
2. **Walkshed = 1-mi straight buffer** (§3) — overstates catchments,
   especially across barriers like the H-1 or the gulches.
3. **Sewer/water frontage = road frontage** (§6.4) — may over- or
   underestimate depending on whether mains follow streets in that
   specific block.
4. ~~Water O&M and Water CIP = null~~ — **closed in v1.2.** Loaded
   manually from the BWS FY26 Combined Op+CIP Budget (Amendment #4) and
   the BWS Six-Year CIP FY21–26. `water_om = $362.4M`, `water_cip =
   $190.4M/yr` (6yr-avg). Source URLs in `data/{budget,cip}_overrides.json`
   `_notes`. Update on next BWS amendment or fiscal year rollover.
5. ~~Operating costs only, no capital replacement~~ — **closed in v1.1.**
   CIP is annualized as 6-year-average and added on top of O&M; see §6.1.
   Caveat: CIP is *attributed* via the same frontage proration as O&M,
   which underweights capital projects whose footprint isn't linear (e.g.
   treatment-plant upgrades). A more accurate model would split CIP
   into linear (mains, roadway rehab) and point/area (treatment, pump
   stations) and attribute each appropriately.
6. **State roads filtered, but state-funded improvements not credited** —
   freeway interchanges generate land value but we don't see the parcels
   credited for that.
7. **Parcels-tax field naming** — `tmk` is normalized to 8-digit
   zero-padded string. Mismatches with the RPAD `tmk` column (also
   8-digit) result in some rows dropping silently. Currently 34 of 25,834
   rows are unmatched (0.13%).
8. **Corner-parcel double-counting** (§6.6) — defensible but inflates
   cost-per-acre at intersections by up to 2x.

---

## 9. Gaps vs. Urban3 reference

[Urban3](https://www.urbanthree.com/services/revenue-modeling/) is the
canonical comparison. Major deltas:

| Aspect | Urban3 | This v1 |
|---|---|---|
| Revenue side | Property tax + GET + fees + transfers | Property tax only (flat-rate) |
| Cost side | Operating + capital amortization | Operating + capital (6yr-avg, frontage-prorated) |
| Geographic scope | Whole city/county for context | TOD walksheds only |
| Geographic baseline | Suburban parcels visible for contrast | Urban TOD only — no contrast |
| Bivariate encoding | Yes (height = $/ac, color = net) | Yes (after refactor) |
| Comparison views | a/b side-by-side (e.g., Walmart vs. Main St) | Single station filter |
| Methodology audit | Published reports, peer-reviewed | This file |
| Adopted boundaries | Project-specific scope | 1-mi walksheds (TOD Special District boundaries available but not yet used) |

To close the most rhetorically important gap (Honolulu's TOD parcels
compared against Honolulu's suburban parcels), v2 would fetch all 172k
parcels, compute revenue + cost for all, and style the TOD overlay
distinctly.

---

## 10. Reconciliation checks

These are listed in the original plan but **not yet implemented** as tests.
v1.1 should add `tests/test_reconciliation.py`:

1. **Sum-to-budget reconciliation** —
   `Σ cost_road across all citywide parcels` should equal
   `road_om_total_usd ± 2%`. Failures here mean the frontage denominator
   (citywide road centerline length) is wrong.
2. **Revenue sanity** —
   `Σ annual_tax across all parcels` should equal the FY26 RPT forecast
   in the budget exec summary `± 3%`. Failures point to either the flat
   millage rate being too far off or RPAD join-loss issues.
3. **Hand-spot a station** — pick 3 Hālawa-station parcels, manually
   measure frontage in QGIS, multiply by computed rates, confirm the
   pipeline output matches `± 5%`.
4. **Manifest freshness** — `pipeline_run.py --check` confirms every
   `data/processed/*.manifest.json` exists and is recent.

---

## 11. Basemap

### 11.1 Source

The vector basemap is built from the [Geofabrik](https://download.geofabrik.de/)
daily Hawaii OSM extract:

```
https://download.geofabrik.de/north-america/us/hawaii-latest.osm.pbf
```

OSM snapshot date (from PBF header): see `data/honolulu_basemap.pmtiles.manifest.json`.

### 11.2 Build tool

[Planetiler](https://github.com/onthegomap/planetiler) v0.10.2. Produces
a single `data/honolulu_basemap.pmtiles` file (~22 MB) using the
[OpenMapTiles schema](https://openmaptiles.org/schema/) at zoom levels 0–14.

Requires Java 21+. macOS: `brew install openjdk@21`. Script:

```bash
python etl/09_build_basemap_tiles.py
# optional: --force to rebuild, --bbox=MINLON,MINLAT,MAXLON,MAXLAT to clip
```

Build time: ~5–15 minutes on a Mac mini (M-series). The JAR (~93 MB) is cached
in `data/cache/` (gitignored). The PBF (~24 MB) is cached in `data/raw/osm/`
(gitignored). Step 09 is **not** included in the default `pipeline_run.py`
run because of the Java dependency and build time; rebuild manually (yearly or
on demand).

### 11.3 Serving

The pmtiles file is committed to git and served directly by GitHub Pages.
The frontend reads it via the
[`pmtiles` JS v3.0.7](https://github.com/protomaps/PMTiles) protocol shim
registered with MapLibre GL JS:

```js
const _pmProtocol = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', _pmProtocol.tile);
```

The style (`data/basemap_style.json`) points at
`pmtiles://./data/honolulu_basemap.pmtiles`. The browser caches tiles
natively via HTTP Range requests — no tile server, no prewarming needed.

### 11.4 Style

Forked from [OpenFreeMap](https://openfreemap.org/) Positron
(`tiles.openfreemap.org/styles/positron`, fetched 2026-05-07). Only change:
the `openmaptiles` source URL replaced with the local PMTiles path. Layer
names follow the OpenMapTiles schema so the `style.load` recolor handler
in `script.js` (water → soft blue, POI labels suppressed) continues to work.

Glyphs and sprites still point at `tiles.openfreemap.org` — these are the
remaining CDN dependency. Offline mode will break label rendering but not
polygon rendering.

### 11.5 Why self-host

- **No CDN lag** — with a CDN-hosted vector basemap, first pan/zoom triggers
  a network round-trip per tile. With PMTiles, the browser's HTTP cache warms
  after the initial full-file fetch; subsequent pans read from cache.
- **Future overlays** — future versions will repaint road colors, add
  isochrones, and visualize GTFS bus routes. All of these require owning
  the basemap layers (you can't restyle a raster or a CDN-opaque vector
  source).
- **OpenMapTiles schema** — Planetiler's output preserves layer names (`water`,
  `road`, `poi`, `building`, etc.) that the existing `script.js` style
  handler already targets.

---

## Provenance

- **Pipeline written by**: Claude Opus 4.7 (madison remote agent worklist),
  2026-05-07.
- **Last reviewed**: 2026-05-07.
- **Pinned data snapshots** (see `data/processed/*.manifest.json` for fetch
  timestamps): RPAD asmtgis 2026-05-07, ArcGIS layers 2026-05-07,
  FY26 budget PDFs 2026-05-07.
