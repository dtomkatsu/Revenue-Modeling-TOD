# Revenue-Modeling-TOD — Claude rules

## Hard rules

- **EPSG:2783** (HI State Plane Z3, US-survey-feet) for ALL length / frontage operations. Never compute frontage in lat/lon degrees.
- **Skyline guideway must be filtered out** of road centerlines: load `data/raw/rail_transit_guideway_alignment_line.geojson`, exclude any centerline within 10 ft.
- **State DOT freeways must be filtered out** by ownership/jurisdiction field. Counties don't pay for state-owned roads.
- **Per-class millage rates** (FY2026 by `taxratecode` / `ovrclass`), never the flat $5.70/agricultural rate.
- **Landlocked detection**: parcels with `road_ft < 10` get `landlocked=true`.
- **Repo-relative paths only** in commits/prompts. Madison's workdir is `~/repos/Revenue-Modeling-TOD/`.
- **Branch convention**: madison commits land on `madison-work`; merge to `main` after review.

## Frontend rules

- Single MapLibre + deck.gl canvas via `MapboxOverlay({interleaved: true})`.
- One `GeoJsonLayer` for parcels — NOT four separate layers (parcels-fill, -outline, -extrude, -hover-outline are deprecated patterns).
- Hover state via JS `hoveredTmk` variable consumed by `getFillColor`/`getLineColor` with `updateTriggers`. NOT via feature-state.
- Self-hosted basemap via Planetiler v0.10.2 from Geofabrik OSM. Target ≤60 MB.

## Stack

- ETL: Python (pandas, geopandas, shapely STRtree).
- Data: RPAD `asmtgis.csv` + `asmtpitt.csv`, ArcGIS layers (parcels, roads, sewer, water, rail, addresses).
- Frontend: MapLibre GL JS + deck.gl, static site, GitHub Pages.

## Source of truth

- `METHODOLOGY.md` — full pipeline, frontage proration, cost allocation.
- Urban3 reference: https://www.urbanthree.com/services/revenue-modeling/

## Companion docs (in vault)

- `~/.openclaw/workspace/projects/Revenue-Modeling-TOD.md`
- `~/.openclaw/workspace/tasks/Revenue-Modeling-TOD.md`
