# Methodology

Documentation of every approximation, assumption, and modeling choice in the
Revenue-Modeling-TOD pipeline. If you're auditing a number on the map, start
here.

## 1. Catchment definition (walkshed)

Each of the 13 operating Skyline stations gets a **0.5-mile straight-line
buffer** around its centroid, computed in EPSG:32604 (UTM Zone 4N, meters)
to avoid lat/lon distortion. Radius = 804.672 m.

This **overstates** the real walkable area — actual walking-network distance
follows streets, crosses obstacles, etc. v2 should swap in `osmnx` walking-
network routing (or fetch real DPP TOD Special District boundaries when DPP
exposes them; they're not currently in the open data catalog).

## 2. Coordinate reference systems

We use two CRSs depending on operation:

| Operation | CRS | EPSG | Why |
|---|---|---|---|
| Buffering (walksheds) | UTM Zone 4N | 32604 | True meters; avoids lat/lon distortion |
| Area (acres) | UTM Zone 4N | 32604 | Polygon area in m², ÷ 4046.86 → acres |
| Frontage length | HI State Plane Z3 | 2783 | True US-survey-feet, no unit conversion |

Mixing units across these will produce 5–10% errors. All transformations are
centralized in `common/`.

## 3. Revenue side

Revenue per parcel = annual property tax (from `cchnl::parcels-tax` field —
exact name confirmed at first fetch). Revenue-per-acre = tax / acres.

If the dataset only exposes assessed value (no tax field), we fall back to:

```
estimated_tax = assessed_value × statutory_millage_rate
```

with millage rates from the FY26 Real Property Tax ordinance (varies by
land-use class).

**Caveats**:
- Excludes non-property-tax revenue (GET, fees, federal transfers).
  Urban3's full method allocates these too; v1 keeps it property-tax-only.
- Exemptions (homeowner, charitable) reduce tax but not assessed value;
  we use the post-exemption tax field where available.

## 4. Cost side — frontage-based proration (Urban3 classic)

City-wide per-foot rates are derived once:

```
road_rate_$/ft  = road_om_total_$  / total_road_centerline_ft_citywide
sewer_rate_$/ft = sewer_om_total_$ / total_sewer_main_ft_citywide
water_rate_$/ft = water_om_total_$ / total_water_main_ft_citywide
```

Numerators come from FY26 Operating Budget tables (extracted with pdfplumber).
Denominators come from summing LineString lengths in the relevant ArcGIS layer.

Per parcel:

```python
p_buf = parcel.buffer(5_ft)              # snap tolerance for "adjacent"
road_ft  = sum(seg ∩ p_buf .length  for seg in roads)
sewer_ft = sum(seg ∩ p_buf .length  for seg in sewer_mains)
water_ft = sum(seg ∩ p_buf .length  for seg in water_mains)
cost_per_ac = (road_ft·road_rate + sewer_ft·sewer_rate + water_ft·water_rate) / acres
```

### Filters & edge cases

- **Skyline guideway** must be filtered out of `centerlines_haw` — otherwise
  parcels alongside the rail get inflated road frontage.
- **State-owned roads (freeways)** are filtered — they're maintained by HI DOT,
  not the City. Filter on the ownership/jurisdiction field.
- **Corner parcels** naturally double-count along both streets — this is correct
  (they're adjacent to two roads).
- **Landlocked parcels** with `road_ft < 10` get flagged `landlocked=true`
  for inspection.
- **Easements / shared frontage**: v1 ignores; v2 could split frontage among
  abutting parcels.

### Sewer / water fallback

If BWS / wastewater layers expose only **facilities** (treatment plants,
hydrants) and not network linework, v1 falls back to using **road centerlines
as a proxy** for sewer/water frontage on the assumption that mains roughly
follow streets. Documented as a known approximation; tracked for v2.

## 5. Citywide rate vs. district rate

Rates are computed **citywide** (Honolulu CCD denominator), not per-district.
This matches Urban3's standard method. A finer model could compute per-
neighborhood rates if budget data were available at that grain — Honolulu's
budget is by department, not geography, so this isn't viable in v1.

## 6. What's *not* modeled (v1)

- Capital infrastructure replacement (Urban3's $/ac prism height also reflects
  CIP liability). v1 is operating-cost only.
- Schools, libraries, parks O&M (counted as City spending but not allocated
  by frontage — they're allocated by population in Urban3's full method).
- Federal / state transfers received by the City.
- TIF / value-capture mechanisms.

## 7. Reconciliation checks (run on every pipeline build)

See [tests/test_reconciliation.py](tests/test_reconciliation.py):

1. `Σ cost_road across all citywide parcels` ≈ `road_om_total_usd` (±2%)
2. `Σ TaxAmount across all parcels` ≈ FY26 RPT forecast (±3%)
3. Manual hand-check of 3 Hālawa-station parcels — frontage measured in QGIS,
   compared to algorithm output (±5%)
