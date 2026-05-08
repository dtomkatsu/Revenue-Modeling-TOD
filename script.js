// Skyline TOD revenue/cost map.
// Loads data/parcels_tod.geojson + data/stations.geojson, renders parcels colored
// by revenue_per_ac / cost_per_ac / net_per_ac, and supports per-station filtering
// + 3D extrusion.

const VIRIDIS = [
  '#440154', '#482878', '#3e4989', '#31688e',
  '#26828e', '#1f9e89', '#35b779', '#6ece58',
  '#b5de2b', '#fde725'
];

const DIVERGING_RWG = [
  '#a50026', '#d73027', '#f46d43', '#fdae61', '#fee08b',
  '#ffffff',
  '#d9ef8b', '#a6d96a', '#66bd63', '#1a9850', '#006837'
];

const METRIC_KEYS = {
  revenue: 'rev_per_ac',
  cost: 'cost_per_ac',
  net: 'net_per_ac',
};

const METRIC_LABELS = {
  revenue: 'Revenue / ac',
  cost: 'Cost / ac',
  net: 'Net / ac',
};

const STATE = {
  mode: 'revenue',
  extrude: true,
  stationId: '',
  parcels: null,        // raw FeatureCollection
  stations: null,
  railLine: null,       // buffered Skyline guideway ribbon polygon
  filtered: [],         // currently visible parcel features
  domain: [0, 1],       // [min, max] of color metric (98th-pct clipped)
  heightDomain: [0, 1], // [min, max] of rev_per_ac across visible parcels
};

// Height is ALWAYS revenue per acre (Urban3 convention: bar height = parcel
// productivity, color = whatever the user wants to see — net is the iconic view).
const HEIGHT_KEY = 'rev_per_ac';

const fmtUSD0 = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', maximumFractionDigits: 0,
});
const fmtUSDk = (n) => {
  if (!Number.isFinite(n)) return '—';
  if (Math.abs(n) >= 1e6) return `$${(n / 1e6).toFixed(2)}M`;
  if (Math.abs(n) >= 1e3) return `$${(n / 1e3).toFixed(1)}k`;
  return fmtUSD0.format(n);
};
const fmtInt = new Intl.NumberFormat('en-US');

// Register the pmtiles:// protocol so MapLibre can fetch tile ranges out of
// our self-hosted single-file Hawaii basemap (data/honolulu_basemap.pmtiles).
// Must run BEFORE `new maplibregl.Map()` constructs the source.
const _pmProtocol = new pmtiles.Protocol();
maplibregl.addProtocol('pmtiles', _pmProtocol.tile);

// Expose for debugging probes (window.map is shadowed by the <map> element).
const map = window._tod_map = new maplibregl.Map({
  container: 'map',
  // Self-hosted OpenMapTiles-schema basemap, forked from openfreemap positron.
  // Style file lives in data/, points its 'openmaptiles' source at our
  // local .pmtiles via the pmtiles:// protocol.
  style: 'data/basemap_style.json',
  customAttribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  center: [-157.95, 21.38],
  zoom: 11,
  pitch: 0,
  bearing: 0,
  // Cache enough tiles to hold all of Oahu z8–z11 (prewarmed) plus the
  // user's z14–z15 working views. Default (~64 tiles) evicts the prewarm.
  maxTileCacheSize: 1024,
  maxTileCacheZoomLevels: 8,
});

// Once the vector style finishes loading, recolor the ocean and hide POI/
// transit clutter so our parcel bars stay the focal point. Suburb/
// neighbourhood names get added later as floating HTML badges (see
// addAreaBadges) — symbol-layer text was too faint to read over parcels.
map.on('style.load', () => {
  if (map.getLayer('water')) {
    map.setPaintProperty('water', 'fill-color', '#a8d5e2');
  }
  const hideIfPresent = [
    'poi', 'poi-housenumber', 'poi-non-essential',
    'transit-station-label', 'transit_stop_label',
    'place_other', 'place_village',
  ];
  for (const id of hideIfPresent) {
    if (map.getLayer(id)) map.setLayoutProperty(id, 'visibility', 'none');
  }
});

map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
map.addControl(new maplibregl.ScaleControl({ unit: 'imperial' }), 'bottom-right');

map.on('load', async () => {
  try {
    const [parcels, stations, railLine] = await Promise.all([
      fetchJSON('data/parcels_tod.geojson'),
      fetchJSON('data/stations.geojson'),
      fetchJSON('data/rail_line.geojson'),
    ]);
    STATE.parcels = parcels;
    STATE.stations = stations;
    STATE.railLine = railLine;

    populateStationDropdown(stations);
    addLayers();
    wireUI();
    refresh();
    // Tessellate Oahu basemap tiles into the in-memory cache while the map
    // is still hidden (opacity 0 in CSS). Camera moves between zooms are
    // invisible to the user; only the cache fills.
    await prewarmOahuTiles();
    // querySourceFeatures only sees features in CURRENTLY-loaded tiles. We
    // just walked the camera over Oahu z8–z11 in prewarm, so the place
    // tiles for the whole island are loaded — perfect time to harvest
    // suburb/neighbourhood centroids for the area badges.
    addAreaBadges();
    // 3D is on by default — pose the final view and reveal the map.
    map.jumpTo({
      center: [-157.95, 21.38],
      zoom: 15,
      pitch: 60,
      bearing: 0,
    });
    document.getElementById('map').classList.add('ready');
  } catch (err) {
    console.error(err);
    document.getElementById('summary').innerHTML =
      `<p class="muted">Failed to load data: ${escapeHTML(err.message)}.<br>` +
      `Run <code>python pipeline_run.py</code> to generate <code>data/parcels_tod.geojson</code> and <code>data/stations.geojson</code>.</p>`;
  }
});

async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

// Walk the camera over Oahu at z8–z11 so MapLibre tessellates the lower-zoom
// tiles into its in-memory cache before the user can ask for them. Runs while
// the map element is hidden (opacity 0 via CSS), so the user never sees the
// camera moves — only the final pitched z15 view appears once we add .ready.
//
// jumpTo() loads only the *current viewport* of tiles, so at z10/z11 (where
// Oahu is wider than one viewport) we sweep multiple centers to cover it.
// Pull suburb + neighbourhood place features out of the loaded basemap
// tiles and render each as a floating HTML badge anchored at its centroid.
// Vector tiles repeat features across tile boundaries — dedupe by name so
// a place doesn't get N stacked markers. Suburbs are rendered prominently
// (uppercase, slate background), neighbourhoods more subdued via the
// .area-badge.neighbourhood modifier so the visual hierarchy reads.
function addAreaBadges() {
  const seen = new Set();
  let added = 0;
  for (const cls of ['suburb', 'neighbourhood']) {
    const features = map.querySourceFeatures('openmaptiles', {
      sourceLayer: 'place',
      filter: ['==', ['get', 'class'], cls],
    });
    for (const f of features) {
      const name = f.properties.name_en || f.properties.name;
      if (!name || seen.has(name)) continue;
      // Vector-tile point features have geometry.coordinates as [lng, lat].
      const coords = f.geometry?.type === 'Point' && f.geometry.coordinates;
      if (!coords) continue;
      seen.add(name);
      const el = document.createElement('div');
      el.className = cls === 'neighbourhood'
        ? 'area-badge neighbourhood'
        : 'area-badge';
      el.textContent = name;
      new maplibregl.Marker({ element: el, anchor: 'center' })
        .setLngLat(coords)
        .addTo(map);
      added += 1;
    }
  }
  console.log(`[areas] ${added} badges placed`);
}

async function prewarmOahuTiles() {
  const sweeps = [
    { zoom:  8, centers: [[-157.95, 21.45]] },
    { zoom:  9, centers: [[-157.95, 21.45]] },
    { zoom: 10, centers: [[-157.95, 21.65], [-157.95, 21.30]] },
    { zoom: 11, centers: [
      [-158.15, 21.65], [-157.95, 21.65], [-157.75, 21.65],
      [-158.15, 21.30], [-157.95, 21.30], [-157.75, 21.30],
    ] },
  ];
  for (const { zoom, centers } of sweeps) {
    for (const center of centers) {
      map.jumpTo({ center, zoom, pitch: 0, bearing: 0 });
      while (!map.areTilesLoaded()) {
        await new Promise((r) => setTimeout(r, 30));
      }
    }
  }
}


function getStationId(f) {
  return f.properties?.station_id ?? f.properties?.id ?? f.id;
}

function getStationName(f) {
  return f.properties?.name ?? f.properties?.station_name ?? `Station ${getStationId(f)}`;
}

function populateStationDropdown(stations) {
  const sel = document.getElementById('station-select');
  const features = [...(stations.features || [])];
  features.sort((a, b) => getStationName(a).localeCompare(getStationName(b)));
  for (const f of features) {
    const opt = document.createElement('option');
    opt.value = String(getStationId(f));
    opt.textContent = getStationName(f);
    sel.appendChild(opt);
  }
}

function addLayers() {
  map.addSource('parcels', { type: 'geojson', data: STATE.parcels, promoteId: 'tmk' });
  map.addSource('stations', { type: 'geojson', data: STATE.stations });

  // Parcel fill (2D) — hidden by default since 3D is on.
  map.addLayer({
    id: 'parcels-fill',
    type: 'fill',
    source: 'parcels',
    layout: { visibility: STATE.extrude ? 'none' : 'visible' },
    paint: {
      'fill-color': '#cccccc',
      'fill-opacity': [
        'case', ['boolean', ['feature-state', 'hover'], false], 1.0, 0.85,
      ],
    },
  });

  // Parcel outline (separate layer so we can set stroke width). zoom must
  // be at the top level of any zoom expression (per maplibre style spec) —
  // wrap interpolate around case, not the other way around.
  map.addLayer({
    id: 'parcels-outline',
    type: 'line',
    source: 'parcels',
    layout: { visibility: STATE.extrude ? 'none' : 'visible' },
    paint: {
      'line-color': [
        'case', ['boolean', ['feature-state', 'hover'], false],
        '#0ea5e9', 'rgba(20,20,20,0.7)',
      ],
      'line-width': [
        'interpolate', ['linear'], ['zoom'],
        12, ['case', ['boolean', ['feature-state', 'hover'], false], 3, 0.3],
        14, ['case', ['boolean', ['feature-state', 'hover'], false], 3, 0.7],
        16, ['case', ['boolean', ['feature-state', 'hover'], false], 3, 1.2],
        18, ['case', ['boolean', ['feature-state', 'hover'], false], 3, 1.8],
      ],
      'line-color-transition': { duration: 0 },
      'line-width-transition': { duration: 0 },
    },
  });

  // Parcel extrusion (3D) — visible by default. fill-extrusion-opacity does
  // not support feature-state, but fill-extrusion-color does — applyPaint()
  // wraps the color in a hover case so the hovered bar lights up cyan.
  map.addLayer({
    id: 'parcels-extrude',
    type: 'fill-extrusion',
    source: 'parcels',
    layout: { visibility: STATE.extrude ? 'visible' : 'none' },
    paint: {
      'fill-extrusion-color': '#cccccc',
      // Disable the default 300ms color/opacity transitions. With a 'case'
      // expression flipping between an interpolate color (non-hover) and a
      // constant cyan (hover), MapLibre's intermediate-frame eval of the
      // transition produces a visible flash on every cursor move. Issue
      // ref: mapbox/mapbox-gl-js#6617. Instant swap = no flicker.
      'fill-extrusion-color-transition':   { duration: 0 },
      'fill-extrusion-opacity-transition': { duration: 0 },
      'fill-extrusion-opacity': 0.85,
      'fill-extrusion-height': 0,
      'fill-extrusion-base': 0,
    },
  });

  // Skyline guideway as a translucent cyan ribbon. Real-world viaduct sits
  // ~30 ft (9 m) above ground, but our parcel extrusions are scaled to a
  // 500 m visualization peak (data bars, not building heights). At realistic
  // viaduct elevation the ribbon would be permanently buried under data bars,
  // so we lift it to ~120 m where it floats clearly above the cityscape and
  // reads as an "above-it-all" route. Translucent + no vertical gradient so
  // the band looks like a glowing track rather than a shaded box. Hidden in
  // 2D mode along with the parcel extrusion.
  // Skyline guideway as a 2D ghost-trace that bleeds through parcel
  // extrusions. Plain fill drawn after the extrusion in style order; while
  // it does depth-test against the framebuffer, the alpha-blend with the
  // translucent parcel surfaces lets the rail color come through. Hidden in
  // 2D mode along with the parcels.
  map.addSource('rail-line', { type: 'geojson', data: STATE.railLine });
  map.addLayer({
    id: 'rail-line-xray',
    type: 'fill',
    source: 'rail-line',
    layout: { visibility: STATE.extrude ? 'visible' : 'none' },
    paint: {
      'fill-color': '#06b6d4',
      'fill-opacity': 0.32,
      'fill-antialias': false,
    },
  });

  // Ground-level outline of the hovered parcel — visible in both 2D and 3D
  // (in 3D it shows up as a ring at the base of the lit-up bar). Same
  // expression-vs-constant issue (mapbox-gl-js#6617) as the extrude color:
  // disable transition or the fade-in from 0→1 produces a visible flash.
  map.addLayer({
    id: 'parcels-hover-outline',
    type: 'line',
    source: 'parcels',
    paint: {
      'line-color': '#0ea5e9',
      'line-width': 3,
      'line-opacity': [
        'case', ['boolean', ['feature-state', 'hover'], false], 1, 0,
      ],
      'line-opacity-transition': { duration: 0 },
    },
  });

  // Station points (cyan dot at ground level — the "pin" beneath the label).
  map.addLayer({
    id: 'stations-circle',
    type: 'circle',
    source: 'stations',
    paint: {
      'circle-radius': 6,
      'circle-color': '#0ea5e9',
      'circle-stroke-width': 2,
      'circle-stroke-color': '#fff',
    },
  });

  // Floating glass-panel station labels — HTML markers (not symbol layer)
  // because symbol layers have no Z-axis. The negative pixel offset lifts
  // the label above the cyan dot, giving a "floating" look in 3D. A small
  // train glyph distinguishes these from the area-name badges at a glance.
  // SVG icon is the Material "directions_subway" path; currentColor lets
  // it inherit from the badge text color.
  const TRAIN_SVG =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor" ' +
    'aria-hidden="true">' +
    '<path d="M12 2c-4 0-8 .5-8 4v9.5C4 17.43 5.57 19 7.5 19L6 20.5v.5h2.23l' +
    '2-2h3.54l2 2H18v-.5L16.5 19c1.93 0 3.5-1.57 3.5-3.5V6c0-3.5-3.58-4-8-4zM' +
    '7.5 17c-.83 0-1.5-.67-1.5-1.5S6.67 14 7.5 14s1.5.67 1.5 1.5S8.33 17 7.5 ' +
    '17zM11 11H6V6.5h5V11zm2 0V6.5h5V11h-5zm3.5 6c-.83 0-1.5-.67-1.5-1.5s.67-' +
    '1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5z"/></svg>';
  for (const f of STATE.stations.features) {
    const el = document.createElement('div');
    el.className = 'station-floater';
    const icon = document.createElement('span');
    icon.className = 'station-icon';
    icon.innerHTML = TRAIN_SVG;
    const name = document.createElement('span');
    name.className = 'station-name';
    name.textContent = getStationName(f);
    el.appendChild(icon);
    el.appendChild(name);
    new maplibregl.Marker({ element: el, offset: [0, -32], anchor: 'bottom' })
      .setLngLat(f.geometry.coordinates)
      .addTo(map);
  }

  bindHoverPopup();
}

function bindHoverPopup() {
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });
  let hoveredTmk = null;
  let lastPopupTmk = null;

  const setHover = (tmk) => {
    if (hoveredTmk === tmk) return;
    if (hoveredTmk !== null) {
      map.setFeatureState({ source: 'parcels', id: hoveredTmk }, { hover: false });
    }
    hoveredTmk = tmk;
    if (tmk !== null) {
      map.setFeatureState({ source: 'parcels', id: tmk }, { hover: true });
    }
  };

  // Cheap polygon centroid (mean of first ring) — close enough for anchoring.
  const featureCentroid = (geom) => {
    const ring = geom.type === 'Polygon'
      ? geom.coordinates[0]
      : geom.coordinates[0][0];
    let cx = 0, cy = 0;
    for (const [x, y] of ring) { cx += x; cy += y; }
    return [cx / ring.length, cy / ring.length];
  };

  const buildHTML = (p) => {
    const addr = p.address && p.address !== 'null' ? p.address : null;
    const primaryClass = (m) => `pp-row${STATE.mode === m ? ' primary' : ''}`;
    return `
      <div class="pp-title">${escapeHTML(addr ?? p.tmk ?? p.parcel_id ?? 'Parcel')}</div>
      ${addr ? `<div class="pp-sub">TMK ${escapeHTML(p.tmk ?? '')}</div>` : ''}
      <div class="${primaryClass('revenue')}"><span class="k">Revenue / ac</span><span class="v">${fmtUSDk(+p.rev_per_ac)}</span></div>
      <div class="${primaryClass('cost')}"><span class="k">Cost / ac</span><span class="v">${fmtUSDk(+p.cost_per_ac)}</span></div>
      <div class="${primaryClass('net')}"><span class="k">Net / ac</span><span class="v">${fmtUSDk(+p.net_per_ac)}</span></div>
      ${p.area_ac ? `<div class="pp-row"><span class="k">Acres</span><span class="v">${(+p.area_ac).toFixed(2)}</span></div>` : ''}
      ${p.land_use ? `<div class="pp-row"><span class="k">Class</span><span class="v">${escapeHTML(String(p.land_use))}</span></div>` : ''}
    `;
  };

  const clearHover = () => {
    setHover(null);
    lastPopupTmk = null;
    popup.remove();
    map.getCanvas().style.cursor = '';
  };

  // Single global mousemove (NOT layer-scoped). Layer-scoped mousemove
  // misfires for fill-extrusion: pixel-perfect cursor stays visually on a
  // parcel but maplibre returns empty queryRenderedFeatures between gap
  // pixels of adjacent extrusions of different heights. We compensate
  // with two mechanisms:
  //   1. Generous bbox query (HOVER_BBOX_PX). With pitched 3D the rendered
  //      top of a parcel can be a few px off from the cursor's exact pixel;
  //      a small bbox absorbs that.
  //   2. Sticky hover. If the previously-hovered TMK is still among the
  //      candidates, keep it (don't shuffle to whatever happens to be
  //      first in the result list — that flips frame-to-frame).
  //   3. NO clearHover when query returns empty. Pitched 3D extrusions
  //      project the ground centroid below the rendered top face, so the
  //      cursor often lands on basemap pixels even though it's visually
  //      on the parcel. Only clear when cursor crosses to a *different*
  //      parcel candidate (or leaves the canvas, handled below).
  const HOVER_BBOX_PX = 6;
  map.on('mousemove', (e) => {
    const features = map.queryRenderedFeatures(
      [[e.point.x - HOVER_BBOX_PX, e.point.y - HOVER_BBOX_PX],
       [e.point.x + HOVER_BBOX_PX, e.point.y + HOVER_BBOX_PX]],
      { layers: ['parcels-extrude', 'parcels-fill'] }
    );
    if (!features.length) return;  // do not clear — gap pixels are expected
    let f = features[0];
    if (hoveredTmk != null) {
      const stick = features.find((c) => c.properties.tmk === hoveredTmk);
      if (stick) f = stick;
    }
    const tmk = f.properties.tmk ?? null;
    if (tmk === hoveredTmk) return;  // same parcel — nothing to update
    setHover(tmk);
    map.getCanvas().style.cursor = 'pointer';
    if (tmk !== lastPopupTmk) {
      popup.setLngLat(featureCentroid(f.geometry));
      popup.setHTML(buildHTML(f.properties));
      lastPopupTmk = tmk;
    }
    if (!popup.isOpen()) popup.addTo(map);
  });

  // Cursor leaves the map canvas entirely (e.g., into the sidebar) — this is
  // the only signal we trust to clear the popup, since gap-pixel mouseleaves
  // can't be distinguished from real ones.
  map.getCanvas().addEventListener('mouseleave', clearHover);
}

function wireUI() {
  document.getElementById('station-select').addEventListener('change', (e) => {
    selectStation(e.target.value);
  });

  document.querySelectorAll('#mode-toggle .seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#mode-toggle .seg-btn').forEach((b) => {
        b.classList.toggle('active', b === btn);
        b.setAttribute('aria-checked', b === btn ? 'true' : 'false');
      });
      STATE.mode = btn.dataset.mode;
      refresh();
    });
  });

  document.getElementById('extrude-toggle').addEventListener('change', (e) => {
    STATE.extrude = e.target.checked;
    map.setLayoutProperty('parcels-fill',     'visibility', STATE.extrude ? 'none' : 'visible');
    map.setLayoutProperty('parcels-outline',  'visibility', STATE.extrude ? 'none' : 'visible');
    map.setLayoutProperty('parcels-extrude',  'visibility', STATE.extrude ? 'visible' : 'none');
    map.setLayoutProperty('rail-line-xray',   'visibility', STATE.extrude ? 'visible' : 'none');
    if (STATE.extrude) {
      // 3D needs both pitch and zoom to be visible — pitch up to near max,
      // zoom in enough that 30–1500m bars register as buildings.
      const targetPitch = Math.max(map.getPitch(), 60);
      const targetZoom  = Math.max(map.getZoom(), 15);
      map.easeTo({ pitch: targetPitch, zoom: targetZoom, duration: 800 });
    } else {
      map.easeTo({ pitch: 0, duration: 600 });
    }
    refresh();
  });
}

function selectStation(id) {
  STATE.stationId = id || '';
  const sel = document.getElementById('station-select');
  if (sel.value !== STATE.stationId) sel.value = STATE.stationId;

  // Station filter: parcels carry a station_ids array (one entry per
  // walkshed they fall in). 'in' tests membership of the selected station's
  // numeric id within that array.
  const filter = STATE.stationId
    ? ['in', ['to-number', STATE.stationId], ['get', 'station_ids']]
    : null;
  map.setFilter('parcels-fill', filter);
  map.setFilter('parcels-extrude', filter);

  fitToStation();
  refresh();
}

function fitToStation() {
  if (!STATE.stationId) {
    if (STATE.parcels?.features?.length) {
      const b = bboxOf(STATE.parcels.features);
      if (b) map.fitBounds(b, { padding: 40, duration: 600 });
    }
    return;
  }
  const sid = +STATE.stationId;
  const matching = (STATE.parcels?.features || []).filter(
    (f) => (f.properties?.station_ids || []).includes(sid)
  );
  const b = bboxOf(matching);
  if (b) map.fitBounds(b, { padding: 60, duration: 600, maxZoom: 16 });
}

function refresh() {
  const colorKey = METRIC_KEYS[STATE.mode];
  const filterSid = STATE.stationId ? +STATE.stationId : null;
  STATE.filtered = (STATE.parcels?.features || []).filter((f) => {
    if (filterSid !== null
        && !(f.properties?.station_ids || []).includes(filterSid)) return false;
    return Number.isFinite(+f.properties?.[colorKey]);
  });

  STATE.domain       = computeDomain(STATE.filtered, colorKey,    STATE.mode === 'net');
  STATE.heightDomain = computeDomain(STATE.filtered, HEIGHT_KEY,  false);

  applyPaint();
  renderLegend();
  renderSummary();
}

function computeDomain(features, key, symmetric) {
  if (!features.length) return symmetric ? [-1, 1] : [0, 1];
  // Percentile clipping so the ramp distributes across the bulk of the data
  // instead of being collapsed by long-tail outliers (Honolulu RPAD has a few
  // commercial parcels >100x the median).
  const vals = [];
  for (const f of features) {
    const v = +f.properties[key];
    if (Number.isFinite(v)) vals.push(v);
  }
  if (!vals.length) return symmetric ? [-1, 1] : [0, 1];
  vals.sort((a, b) => a - b);
  const q = (p) => {
    const i = Math.max(0, Math.min(vals.length - 1, Math.floor(p * (vals.length - 1))));
    return vals[i];
  };
  let lo = q(0.02), hi = q(0.98);
  if (symmetric) {
    const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
    return [-m, m];
  }
  if (lo === hi) hi = lo + 1;
  return [lo, hi];
}

function applyPaint() {
  const key = METRIC_KEYS[STATE.mode];
  const [lo, hi] = STATE.domain;
  const palette = STATE.mode === 'net' ? DIVERGING_RWG : VIRIDIS;
  const colorExpr = ['interpolate', ['linear'], ['to-number', ['get', key]],
    ...rampStops(lo, hi, palette)];

  if (STATE.extrude) {
    // fill-extrusion-color supports feature-state — swap to cyan on hover so
    // the lit-up bar reads even when looking down the corridor in 3D.
    map.setPaintProperty('parcels-extrude', 'fill-extrusion-color', [
      'case', ['boolean', ['feature-state', 'hover'], false], '#0ea5e9', colorExpr,
    ]);
    // Height is always revenue/ac (Urban3 convention: tall = productive).
    // Sqrt scaling against the 98th-percentile peak so low-revenue parcels
    // still have visible bars; 10m floor so non-zero values register at z14.
    const peak = Math.max(Math.abs(STATE.heightDomain[0]),
                          Math.abs(STATE.heightDomain[1])) || 1;
    const HEIGHT_PEAK_M = 500;
    const MIN_HEIGHT_M  = 10;
    const scale = HEIGHT_PEAK_M / Math.sqrt(peak);
    // Clamp input to peak before sqrt so outliers never exceed HEIGHT_PEAK_M.
    map.setPaintProperty('parcels-extrude', 'fill-extrusion-height', [
      'max',
      MIN_HEIGHT_M,
      ['*', scale, ['sqrt', ['min', peak, ['abs', ['to-number', ['get', HEIGHT_KEY]]]]]],
    ]);
  } else {
    map.setPaintProperty('parcels-fill', 'fill-color', colorExpr);
  }
}

function rampStops(lo, hi, palette) {
  const stops = [];
  const n = palette.length;
  for (let i = 0; i < n; i++) {
    const t = n === 1 ? 0 : i / (n - 1);
    stops.push(lo + (hi - lo) * t, palette[i]);
  }
  return stops;
}

function renderLegend() {
  const legend = document.getElementById('legend');
  const palette = STATE.mode === 'net' ? DIVERGING_RWG : VIRIDIS;
  const [lo, hi] = STATE.domain;
  const gradient = `linear-gradient(to right, ${palette.join(', ')})`;
  const heightLine = STATE.extrude
    ? `<div class="muted" style="font-size:10px;margin-top:6px;">Bar height: revenue / ac</div>`
    : '';
  legend.innerHTML = `
    <div class="legend-title muted" style="font-size:11px;text-transform:uppercase;letter-spacing:0.04em;">Color: ${METRIC_LABELS[STATE.mode]}</div>
    <div class="legend-bar" style="background:${gradient};"></div>
    <div class="legend-labels"><span>${fmtUSDk(lo)}</span>${STATE.mode === 'net' ? '<span>0</span>' : ''}<span>${fmtUSDk(hi)}</span></div>
    ${heightLine}
  `;
}

function renderSummary() {
  const el = document.getElementById('summary');
  const features = STATE.filtered;
  if (!features.length) {
    el.innerHTML = `<p class="muted">No parcels for current selection.</p>`;
    return;
  }

  let totalRev = 0, totalCost = 0, totalAcres = 0;
  let hasAcres = false;
  for (const f of features) {
    const p = f.properties;
    const acres = +p.area_ac;
    if (Number.isFinite(acres)) {
      hasAcres = true;
      totalAcres += acres;
      if (Number.isFinite(+p.rev_per_ac)) totalRev += +p.rev_per_ac * acres;
      if (Number.isFinite(+p.cost_per_ac)) totalCost += +p.cost_per_ac * acres;
    }
  }
  const totalNet = totalRev - totalCost;

  const top = [...features]
    .filter((f) => Number.isFinite(+f.properties?.net_per_ac))
    .sort((a, b) => +b.properties.net_per_ac - +a.properties.net_per_ac)
    .slice(0, 5);

  const totalsHTML = hasAcres ? `
    <div class="row"><span class="k">Acres</span><span class="v">${fmtInt.format(Math.round(totalAcres))}</span></div>
    <div class="row"><span class="k">Total revenue</span><span class="v">${fmtUSDk(totalRev)}</span></div>
    <div class="row"><span class="k">Total cost</span><span class="v">${fmtUSDk(totalCost)}</span></div>
    <div class="row"><span class="k">Net</span><span class="v ${totalNet >= 0 ? 'net-pos' : 'net-neg'}">${fmtUSDk(totalNet)}</span></div>
  ` : `<p class="muted" style="margin:0;font-size:11px;">Add an <code>acres</code> property to parcels for absolute totals.</p>`;

  el.innerHTML = `
    <div class="row"><span class="k">Parcels</span><span class="v">${fmtInt.format(features.length)}</span></div>
    ${totalsHTML}
    <div class="top">
      <h3>Top 5 by net / ac</h3>
      <ol>
        ${top.map((f) => {
          const p = f.properties;
          const id = p.tmk ?? p.parcel_id ?? '—';
          return `<li>${escapeHTML(String(id))}<span class="v">${fmtUSDk(+p.net_per_ac)}</span></li>`;
        }).join('')}
      </ol>
    </div>
  `;
}

function bboxOf(features) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  let any = false;
  const visit = (coords) => {
    if (typeof coords[0] === 'number') {
      const [x, y] = coords;
      if (x < minX) minX = x;
      if (y < minY) minY = y;
      if (x > maxX) maxX = x;
      if (y > maxY) maxY = y;
      any = true;
      return;
    }
    for (const c of coords) visit(c);
  };
  for (const f of features) {
    if (f?.geometry?.coordinates) visit(f.geometry.coordinates);
  }
  return any ? [[minX, minY], [maxX, maxY]] : null;
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

window.selectStation = selectStation;
