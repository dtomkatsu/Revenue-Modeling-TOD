# Revenue Modeling TOD

Interactive map of Honolulu's Skyline rail corridor showing per-parcel
**revenue per acre** (property tax yield) vs. **infrastructure cost per acre**
(frontage-prorated road / sewer / water O&M), inspired by
[Urban3's revenue-modeling methodology](https://www.urbanthree.com/services/revenue-modeling/).

**v1 scope**: all 13 currently operating Skyline stations (Segments 1+2, west to east —
Kualakaʻi through Kahauiki/Middle Street). Each station's catchment is a 0.5-mile
straight-line walkshed buffer around the station point.

The output is a static MapLibre GL JS site backed by reproducible Python ETL,
hostable on GitHub Pages.

## Quick start

```bash
# create venv + install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# run the full pipeline (idempotent — skips cached steps)
python pipeline_run.py

# serve the map locally
python -m http.server 8000
# → open http://localhost:8000
```

`python pipeline_run.py --force` re-runs every step.
`python pipeline_run.py --check` audits manifest freshness.

## Data sources

All public, no auth required:

- **ArcGIS Hub** (`honolulu-cchnl.opendata.arcgis.com`): parcels + tax, rail station
  points / footprints / guideway alignment, road centerlines, land use districts,
  Board of Water Supply layers
- **Honolulu FY26 Budget PDFs**
  ([Collection-15858](https://www4.honolulu.gov/docushare/dsweb/View/Collection-15858)) —
  road / sewer / water Operating & Maintenance totals, extracted via pdfplumber

See [METHODOLOGY.md](METHODOLOGY.md) for cost-allocation math, CRS choices, and
caveats.

## Repository layout

```
common/         # http client, ArcGIS REST client, manifest helpers
etl/            # 01–09 numbered pipeline steps
data/           # raw/cache/processed are gitignored; final GeoJSONs committed
index.html      # single-page MapLibre app
script.js
styles.css
pipeline_run.py # orchestrator (runs steps 01–08; step 09 is manual)
```

### ETL steps

| Step | Script | Description | Auto-run? |
|---|---|---|---|
| 01 | `etl/01_fetch_arcgis.py` | Download ArcGIS Hub layers | Yes |
| 02 | `etl/02_fetch_budget_pdfs.py` | Download FY26 budget PDFs | Yes |
| 03 | `etl/03_extract_budget_totals.py` | Extract O&M totals from PDFs | Yes |
| 04 | `etl/04_build_walksheds.py` | 0.5-mile station walksheds | Yes |
| 05 | `etl/05_join_parcels.py` | Spatial join parcels → walksheds | Yes |
| 06 | `etl/06_compute_revenue.py` | Property tax per acre | Yes |
| 07 | `etl/07_compute_frontage_costs.py` | Infrastructure cost per acre | Yes |
| 08 | `etl/08_emit_frontend_data.py` | Emit `data/parcels_tod.geojson` + `stations.geojson` | Yes |
| 09 | `etl/09_build_basemap_tiles.py` | Build self-hosted Hawaii PMTiles basemap | **Manual / yearly** |

Step 09 requires Java 21+ and takes 5–15 minutes. Run it manually:

```bash
python etl/09_build_basemap_tiles.py
```

The output (`data/honolulu_basemap.pmtiles`, ~22 MB) is committed to git.
See [METHODOLOGY.md §11](METHODOLOGY.md#11-basemap) for details.

## Development

Tasks for this project live in
`~/.openclaw/workspace/tasks/Revenue-Modeling-TOD.md` and are auto-worked
by the madison remote agent. Queue new work with:

```bash
~/scripts/queue-task.sh Revenue-Modeling-TOD "<what to do>"
```
