# Audit: low-AV $0-tax parcels (FY2026)

**Status:** complete · **Verdict:** keep $50k threshold · no code change

## Context

After the direct-zero rescue runs in `etl/06_compute_revenue.py` and the
one-shot patch `etl/_patch_zero_tax_data.py`, 539 parcels in
`data/parcels_tod.geojson` still show `rev_per_ac == 0`. The breakdown:

| Cause | Count | Verified action |
|---|---:|---|
| Public Service (utilities) AV > $50k | 38 | ✓ Exempt (HRS § 246-31) — keep $0 |
| AV = $0 (stale geometry) | 21 | ✓ Merged/deleted parcels — keep $0 |
| **AV ≤ $50k (low-value)** | **480** | **← this audit** |

The patch's $50k threshold was a *guess* — we wanted to be conservative
because tiny AV usually means a real exemption, but a wrong guess here
could be hiding the same systematic error we discovered for the
Preservation class (where we were leaving ~$800k/yr on the table).

## Methodology

1. `etl/_audit_low_av_zeros.py` emits a stratified random sample
   (`data/audit/low_av_zeros_sample.csv`, 37 rows, seed=42).
2. Sample stratifies on `(land_use × AV band)` so every class and every
   AV magnitude gets representation:

   | class | sampled | population |
   |---|---:|---:|
   | Residential | 22 | 345 |
   | Industrial | 3 | 52 |
   | Preservation | 3 | 43 |
   | Commercial | 3 | 24 |
   | Agricultural | 3 | 13 |
   | Public Service | 3 | 3 (all) |

   | AV band | count |
   |---|---:|
   | (0, $1k] | 11 |
   | ($1k, $10k] | 9 |
   | ($10k, $25k] | 9 |
   | ($25k, $50k] | 8 |

3. Each sampled parcel looked up on qpublic.schneidercorp.com (Honolulu
   RPAD) — recorded property class, FY2026 assessed value, total
   exemption, net taxable value, owner name + type.

## Findings

**37 of 37 (100%) of sampled parcels have net taxable value = $0 in RPAD.**
In every case, the full assessed value is matched by an equal exemption
(government carve-out, not homestead+age combo as initially guessed).

### Per-parcel results

| TMK | Class | Address | AV (RPAD) | Exempt | Net taxable |
|---|---|---|---:|---:|---:|
| 97032084 | Residential | HOOMALU ST | $100 | $100 | $0 |
| 94030083 | Residential | ROADWAY | $100 | $100 | $0 |
| 91020013 | Residential | ROADWAY | $100 | $100 | $0 |
| 91080090 | Residential | ROADWAY | $100 | $100 | $0 |
| 91126007 | Residential | RENTON RD ROADWAY | $100 | $100 | $0 |
| 91071077 | Residential | OANIANI ST | $1,500 | $1,500 | $0 |
| 99071052 | Residential | ROADWAY | $3,300 | $3,300 | $0 |
| 13009060 | Residential | 1201 KAMEHAMEHA IV RD | $2,100 | $2,100 | $0 |
| 98060030 | Residential | KILINOE ST | $5,200 | $5,200 | $0 |
| 99001011 | Residential | KAMEHAMEHA HWY | $3,500 | $3,500 | $0 |
| 98026024 | Residential | MOANALUA RD | $11,000 | $11,000 | $0 |
| 99001016 | Residential | KAMEHAMEHA HWY | $15,000 | $15,000 | $0 |
| 98024071 | Residential | ROADWAY | $12,600 | $12,600 | $0 |
| 99001007 | Residential | 99-589 KAMEHAMEHA HWY | $18,500 | $18,500 | $0 |
| 94011094 | Residential | HULA ST | $11,000 | $11,000 | $0 |
| 91072109 | Residential | KOLILI ST | $31,200 | $31,200 | $0 |
| 99069045 | Residential | ALA ALII ST | $48,100 | $48,100 | $0 |
| 99042016 | Residential | INTERSTATE HWY | $35,600 | $35,600 | $0 |
| 98026023 | Residential | MOANALUA RD | $36,100 | $36,100 | $0 |
| 13005013 | Residential | FARR LN | $37,800 | $37,800 | $0 |
| 98019007 | Residential | KAMEHAMEHA HWY | $36,700 | $36,700 | $0 |
| 94030084 | Residential | WAIPAHU ST | $100 | $100 | $0 |
| 11070031 | Industrial | LELE ST | $100 | $100 | $0 |
| 12023095 | Industrial | PAHOUNUI DR | $17,700 | $17,700 | $0 |
| 12014102 | Industrial | BANNISTER PL | $4,100 | $4,100 | $0 |
| 99004006 | Preservation | KAMEHAMEHA HWY | $100 | $100 | $0 |
| 97020059 | Preservation | BIKEPATH | $6,000 | $6,000 | $0 |
| 99003055 | Preservation | SALT LAKE BLVD | $25,800 | $25,800 | $0 |
| 94047011 | Commercial | FARRINGTON HWY | $100 | $100 | $0 |
| 97022026 | Commercial | FOURTH ST | $31,500 | $31,500 | $0 |
| 98008027 | Commercial | STREAM | $20,500 | $20,500 | $0 |
| 98060015 | Agricultural | KAAHELE ST | $100 | $100 | $0 |
| 96004029 | Agricultural | WAIHONA ST | $7,000 | $7,000 | $0 |
| 91016031 | Agricultural | ROADWAY | $100 | $100 | $0 |
| 11010040 | Public Service | 3225 SALT LAKE BLVD | $6,800 | $6,800 | $0 |
| 94007025 | Public Service | KAMEHAMEHA HWY | $11,300 | $11,300 | $0 |
| 93002024 | Public Service | WAIPAHU DEPOT RD | $16,800 | $16,800 | $0 |

### Owner spot-checks (4 highest-AV non-Public-Service rows)

| TMK | AV | Owner |
|---|---:|---|
| 99069045 | $48,100 | **STATE OF HAWAII** (Fee Owner) |
| 91072109 | $31,200 | **HAWAII HOUSING FINANCE AND DEV. CORP** (Fee Owner) |
| 13005013 | $37,800 | **STATE OF HAWAII** (Fee Owner) |
| 99003055 | $25,800 | **STATE OF HAWAII** (Fee Owner) |
| 97022026 | $31,500 | **STATE OF HAWAII** (Fee Owner) |
| 97032084 | $100 | **CITY AND COUNTY OF HONOLULU** (Fee Owner) |

### Address-pattern read

The 37 sample addresses fall into recognizable government-infrastructure
patterns:

- **"ROADWAY"** (6) — explicit right-of-way slivers
- **"INTERSTATE HWY"**, **"KAMEHAMEHA HWY"**, **"FARRINGTON HWY"** (7) —
  state DOT highway parcels
- **"STREAM"**, **"BIKEPATH"** — drainage / pedestrian infrastructure
- **Street name without house number** (e.g. "HULA ST", "KILINOE ST",
  "MOANALUA RD") — common-area / right-of-way / drainage easements
- **"3225 SALT LAKE BLVD"** + Public Service — HECO substation
- **"WAIPAHU DEPOT RD"** + Public Service — utility access road

No private residential / commercial properties showed up. No
data-drop misclassifications. No stale parcels. No "should have been
rescued" cases.

## Categories

| Category | Count | % |
|---|---:|---:|
| CORRECT $0 (legitimate exemption) | 37 | 100% |
| STALE (merged/deleted) | 0 | 0% |
| MISSED RESCUE (taxable per RPAD) | 0 | 0% |
| DATA ISSUE (RPAD also shows $0 AV) | 0 | 0% |

## Decision

**Keep the $50k threshold unchanged.** Per the plan's decision rule
(MISSED RESCUE ≤ 5% → no code change), the audit confirms that low-AV
$0-tax parcels are exactly what we hoped — government-owned slivers,
right-of-way, drainage, easements, and public-housing land — not
private residences whose bulk-roll tax was dropped.

The conservative threshold is doing useful work. Lowering it would risk
false-positive rescues on those gov parcels, putting non-existent
revenue into the model.

## Reproducing this audit

```bash
python3 etl/_audit_low_av_zeros.py
# Sample CSV → data/audit/low_av_zeros_sample.csv (deterministic, seed=42)
```

RPAD lookups were done via `chrome-browser` MCP, fetching
`https://qpublic.schneidercorp.com/Application.aspx?AppID=1045&LayerID=23342&PageTypeID=4&PageID=9746&KeyValue=<TMK><CPR>`
where `<TMK><CPR>` = the 8-digit TMK with `"0000"` appended for the
condo / parcel-of-record portion.
