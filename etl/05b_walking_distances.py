"""Step 05b — compute road-network walking distance per parcel to each station.

Builds an undirected graph from ``data/raw/road_centerlines.geojson`` (Oahu
Street Centerlines from Honolulu DPP, fetched by step 01) where each
LineString endpoint becomes a node, snapped to a 0.5-ft grid so adjacent
segments share endpoints. Edge weight = segment length in feet (computed in
EPSG:2783, HI State Plane Z3, US-survey-feet).

Highways/freeways (street_class 1, e.g. H-1) are excluded from the walk
graph since pedestrians can't use them. Skyline guideway (already excluded
by upstream filtering of OSM data) doesn't appear in centerlines.

For each of the 13 operating stations, runs a single-source Dijkstra from
the station's nearest graph node → distance to every reachable node.
For each parcel in ``data/processed/parcels_in_walksheds.geojson`` (output
of step 05), snaps its centroid to the nearest graph node and looks up the
station-distance. Records the **minimum** across all 13 stations as
``walk_dist_ft`` and the corresponding ``nearest_station_id``.

A parcel whose nearest graph node is more than ``MAX_SNAP_FT`` from the
parcel centroid (e.g. island parcels with no street access nearby) gets
``walk_dist_ft = None``. The frontend treats those as "always-visible" so
they aren't silently dropped by the slider filter.

Output: ``data/processed/parcels_in_walksheds.geojson`` is rewritten in
place with ``walk_dist_ft`` and ``nearest_station_id`` columns added.
``STATION_ID`` (set by step 05 from straight-line nearest) is overwritten
with the walking-network nearest, since they can differ for parcels near
station-area boundaries.

Idempotent: skipped if ``walk_dist_ft`` is already present in the input
file. Pass ``--force`` to recompute.

Usage::

    python etl/05b_walking_distances.py
    python etl/05b_walking_distances.py --force
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import geopandas as gpd
import networkx as nx
import numpy as np
import pandas as pd
from shapely.geometry import Point

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from common.manifest import write_manifest  # noqa: E402

SCRIPT_NAME = "etl/05b_walking_distances.py"

ROADS_PATH      = _ROOT / "data" / "raw"       / "road_centerlines.geojson"
STATIONS_PATH   = _ROOT / "data" / "raw"       / "rail_transit_station_points.geojson"
PARCELS_PATH    = _ROOT / "data" / "processed" / "parcels_in_walksheds.geojson"

WGS84      = 4326
HI_SP_Z3   = 2783  # US survey feet — already used elsewhere in the pipeline

# Snap endpoint coords to this grid (in feet) so two segments that share an
# endpoint at slightly different float coords still merge into one node.
SNAP_FT = 0.5

# Highway / freeway classes excluded from the walk graph. street_class:
# 1 = freeway/interstate, 2 = arterial, 3 = collector, 4 = local, 5 = etc.
# Pedestrians can't use 1; everything else is fair game (sidewalks assumed).
EXCLUDED_STREET_CLASSES: set[int] = {1}

# If the nearest graph node is further than this from a parcel/station, we
# assume routing failed (parcel is on a private road or an island).
MAX_SNAP_FT = 1000.0  # ~305 m; generous for Honolulu's network density

OPERATING_STATION_IDS: set[int] = set(range(1, 14))


def _snap(xy: tuple[float, float]) -> tuple[int, int]:
    return (round(xy[0] / SNAP_FT), round(xy[1] / SNAP_FT))


def build_walk_graph(roads_gdf: gpd.GeoDataFrame) -> tuple[nx.Graph, dict[tuple[int, int], tuple[float, float]]]:
    """Build an undirected NetworkX graph from a roads GeoDataFrame in HI SP Z3 ft.

    Returns (graph, node_xy_lookup) where node IDs are snapped (int, int) keys
    and node_xy_lookup maps each ID back to a representative (x, y) in feet.
    """
    G = nx.Graph()
    node_xy: dict[tuple[int, int], tuple[float, float]] = {}
    def add_line(coords: list[tuple[float, float]]) -> None:
        for a, b in zip(coords[:-1], coords[1:]):
            ka, kb = _snap(a), _snap(b)
            if ka == kb:
                continue
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = (dx * dx + dy * dy) ** 0.5
            # If two segments share endpoints, keep the shorter (rare).
            if G.has_edge(ka, kb):
                if length < G[ka][kb]["length"]:
                    G[ka][kb]["length"] = length
            else:
                G.add_edge(ka, kb, length=length)
            node_xy.setdefault(ka, a)
            node_xy.setdefault(kb, b)

    for geom in roads_gdf.geometry:
        if geom is None or geom.is_empty:
            continue
        gtype = geom.geom_type
        if gtype == "LineString":
            add_line(list(geom.coords))
        elif gtype == "MultiLineString":
            for part in geom.geoms:
                add_line(list(part.coords))
        # silently skip other geometry types
    return G, node_xy


def nearest_node(point: Point, node_xy: dict[tuple[int, int], tuple[float, float]],
                 node_array: np.ndarray, node_keys: list[tuple[int, int]]) -> tuple[tuple[int, int], float]:
    """Linear-scan nearest node to *point* (x, y in feet). For ~50k parcels
    on ~30k road segments, O(P*N) is fine (~10s); cKDTree would speed it
    further but adds a scipy dependency."""
    px, py = point.x, point.y
    dx = node_array[:, 0] - px
    dy = node_array[:, 1] - py
    d2 = dx * dx + dy * dy
    i = int(d2.argmin())
    return node_keys[i], float(d2[i] ** 0.5)


def compute_walking_distances(*, force: bool) -> int:
    if not ROADS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {ROADS_PATH}. Run "
            f"`python etl/01_fetch_arcgis.py road_centerlines` first."
        )
    if not STATIONS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {STATIONS_PATH}. Run "
            f"`python etl/01_fetch_arcgis.py rail_transit_station_points` first."
        )
    if not PARCELS_PATH.exists():
        raise FileNotFoundError(
            f"Missing {PARCELS_PATH}. Run `python etl/05_join_parcels.py` first."
        )

    parcels = gpd.read_file(PARCELS_PATH)
    if "walk_dist_ft" in parcels.columns and not force:
        print(f"[skip] walk_dist_ft already present in {PARCELS_PATH.name}")
        return 0

    print(f"[read] {ROADS_PATH.relative_to(_ROOT)}")
    roads = gpd.read_file(ROADS_PATH)
    if "street_class" in roads.columns:
        before = len(roads)
        roads = roads[~roads["street_class"].isin(EXCLUDED_STREET_CLASSES)].copy()
        print(f"[filter] dropped {before - len(roads)} freeway-class segments "
              f"(street_class in {sorted(EXCLUDED_STREET_CLASSES)}); {len(roads)}/{before} remain")

    if roads.crs is None:
        roads = roads.set_crs(WGS84)
    roads = roads.to_crs(HI_SP_Z3)

    print(f"[read] {STATIONS_PATH.relative_to(_ROOT)}")
    stations = gpd.read_file(STATIONS_PATH)
    if stations.crs is None:
        stations = stations.set_crs(WGS84)
    stations = stations.to_crs(HI_SP_Z3)
    operating = stations[stations["ID"].isin(OPERATING_STATION_IDS)].copy()
    operating["ID"] = operating["ID"].astype(int)

    print("[graph] building NetworkX walk graph (snap=%.2f ft)" % SNAP_FT)
    t0 = time.time()
    G, node_xy = build_walk_graph(roads)
    print(f"[graph] {G.number_of_nodes()} nodes, {G.number_of_edges()} edges "
          f"(took {time.time()-t0:.1f}s)")

    # Pull out the largest connected component — shortest paths are only
    # defined within a component. Most of Oahu's network is one big component;
    # private/military roads form smaller islands.
    components = list(nx.connected_components(G))
    components.sort(key=len, reverse=True)
    main_nodes = components[0]
    print(f"[graph] {len(components)} components; using main component "
          f"with {len(main_nodes)} nodes "
          f"({len(main_nodes)/G.number_of_nodes()*100:.1f}% of total)")

    # Restrict graph + node lookup to the main component for routing.
    G_main = G.subgraph(main_nodes).copy()
    node_keys = [k for k in main_nodes]
    node_array = np.array([node_xy[k] for k in node_keys], dtype=np.float64)

    # For each station, snap to nearest node in the main component, then
    # single-source Dijkstra → distance to every reachable node.
    print("[dijkstra] running single-source shortest paths from each station")
    station_dist_maps: dict[int, dict[tuple[int, int], float]] = {}
    station_node_keys: dict[int, tuple[int, int]] = {}
    for _, row in operating.iterrows():
        sid = int(row["ID"])
        snap_key, snap_d = nearest_node(row.geometry, node_xy, node_array, node_keys)
        station_node_keys[sid] = snap_key
        if snap_d > MAX_SNAP_FT:
            print(f"  [warn] station {sid} ({row['STATION']!r}) is {snap_d:.0f} ft "
                  f"from nearest road node — routing may be unreliable")
        t1 = time.time()
        dists = nx.single_source_dijkstra_path_length(G_main, snap_key, weight="length")
        station_dist_maps[sid] = dists
        print(f"  station {sid:2d}: {len(dists)} reachable nodes "
              f"(took {time.time()-t1:.1f}s, snap={snap_d:.0f} ft)")

    # For each parcel: snap centroid → look up min(station_distance + snap_distance).
    print("[parcels] computing walking distances per parcel")
    parcels_proj = parcels.to_crs(HI_SP_Z3)
    centroids = parcels_proj.geometry.centroid

    walk_dist_ft: list[float | None] = []
    nearest_sid: list[int | None] = []
    n_unreachable = 0
    n_far_snap = 0
    t2 = time.time()
    for cent in centroids:
        snap_key, snap_d = nearest_node(cent, node_xy, node_array, node_keys)
        if snap_d > MAX_SNAP_FT:
            walk_dist_ft.append(None)
            nearest_sid.append(None)
            n_far_snap += 1
            continue
        best_d = float("inf")
        best_sid: int | None = None
        for sid, dist_map in station_dist_maps.items():
            d = dist_map.get(snap_key)
            if d is None:
                continue
            total = d + snap_d  # add the parcel's own snap distance
            if total < best_d:
                best_d = total
                best_sid = sid
        if best_sid is None:
            walk_dist_ft.append(None)
            nearest_sid.append(None)
            n_unreachable += 1
        else:
            walk_dist_ft.append(round(best_d, 1))
            nearest_sid.append(best_sid)
    print(f"[parcels] {len(walk_dist_ft)} processed "
          f"(took {time.time()-t2:.1f}s, "
          f"{n_far_snap} far-snap, {n_unreachable} unreachable)")

    parcels["walk_dist_ft"]       = walk_dist_ft
    parcels["nearest_station_id"] = pd.array(nearest_sid, dtype="Int64")
    # Overwrite STATION_ID and STATION_NAME with walking-nearest values where
    # available; fall back to straight-line attribution if walk routing failed.
    sid_to_name = dict(zip(operating["ID"].astype(int), operating["STATION"].astype(str)))
    new_station_id   = []
    new_station_name = []
    for prelim_sid, walk_sid in zip(parcels.get("STATION_ID", [None] * len(parcels)),
                                     nearest_sid):
        sid = walk_sid if walk_sid is not None else (int(prelim_sid) if pd.notna(prelim_sid) else None)
        new_station_id.append(sid)
        new_station_name.append(sid_to_name.get(sid))
    parcels["STATION_ID"]   = pd.array(new_station_id, dtype="Int64")
    parcels["STATION_NAME"] = new_station_name

    # Stats
    walk_array = pd.Series([w for w in walk_dist_ft if w is not None])
    print(f"[stats] walk_dist_ft median {walk_array.median():.0f} ft, "
          f"p25 {walk_array.quantile(0.25):.0f}, p75 {walk_array.quantile(0.75):.0f}, "
          f"max {walk_array.max():.0f}")

    PARCELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    parcels.to_file(PARCELS_PATH, driver="GeoJSON")

    write_manifest(
        PARCELS_PATH,
        source_url=f"file://{ROADS_PATH}",
        row_count=len(parcels),
        script=SCRIPT_NAME,
        extras={
            "graph_nodes":           G.number_of_nodes(),
            "graph_edges":           G.number_of_edges(),
            "main_component_nodes":  len(main_nodes),
            "snap_grid_ft":          SNAP_FT,
            "max_snap_ft":           MAX_SNAP_FT,
            "excluded_street_classes": sorted(EXCLUDED_STREET_CLASSES),
            "parcels_with_walk_dist": int(len(walk_array)),
            "parcels_unreachable":    int(n_unreachable + n_far_snap),
            "walk_dist_ft_median":    float(walk_array.median()) if len(walk_array) else None,
            "walk_dist_ft_max":       float(walk_array.max())    if len(walk_array) else None,
            "crs_for_routing":        f"EPSG:{HI_SP_Z3}",
        },
    )

    print(f"[done] {PARCELS_PATH.relative_to(_ROOT)} updated with walk_dist_ft + nearest_station_id")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="Recompute even if walk_dist_ft is already present.")
    args = ap.parse_args(argv)
    return compute_walking_distances(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
