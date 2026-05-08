// Skyline TOD revenue/cost map.
// Loads data/parcels_tod.geojson + data/stations.geojson, renders parcels colored
// by revenue_per_ac / cost_per_ac / net_per_ac, and supports per-station filtering
// + 3D extrusion. Parcels are rendered via deck.gl GeoJsonLayer (interleaved
// with the MapLibre canvas) so the hover tooltip can be a DOM element placed
// at cursor pixels — avoiding the occlusion you'd get with a centroid-anchored
// MapLibre Popup behind a tall extruded bar.

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
  // Slider positions 0–100. 0 means filter is off; values map to a dollar
  // threshold via STATE.{assessedMax,taxMax} (99th-pct of the data so the
  // sliders aren't dominated by outliers).
  minAssessed: 0,
  minTax: 0,
  assessedMax: 0,       // 99th-pct of assessed_value across all parcels
  taxMax: 0,            // 99th-pct of (rev_per_ac × area_ac)
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

let hoveredTmk = null;
let selectedTmk = null;
const tooltipEl = document.getElementById('parcel-tooltip');
const popupEl = document.getElementById('parcel-popup');

// Format imperial-feet length values for the popup ("143 ft").
const fmtFt = (n) => Number.isFinite(+n) ? `${Math.round(+n).toLocaleString('en-US')} ft` : '—';

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

// interleaved:true lets deck.gl share the MapLibre canvas, so we get pickable
// 3D parcels alongside the raster basemap with one composited render.
const overlay = new deck.MapboxOverlay({ interleaved: true, layers: [] });
map.addControl(overlay);

// onHover fires with object:null on most off-parcel moves, but a fast mouse
// leave that skips empty map (e.g. straight onto the sidebar) can leave the
// tooltip stuck — bind mouseleave on the container to clear it. Bound to the
// outer container, NOT the canvas: the canvas is a sibling of overlay layers,
// so cursor-onto-overlay would fire canvas.mouseleave and create a flicker
// loop. The container wraps everything, so mouseleave fires only on a
// genuine map exit.
map.getContainer().addEventListener('mouseleave', () => {
  if (hoveredTmk !== null) {
    hoveredTmk = null;
    refreshLayer();
  }
  tooltipEl.hidden = true;
});

// Escape closes the click popup. Click-outside (i.e. clicking empty map)
// is already handled via deck.gl's onClick passing object:null.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && selectedTmk !== null) closePopup();
});

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
    computeFilterMaxes(parcels.features);
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

// Compute 99th-percentile maxes for the slider scaling. Outlier-resistant
// so the slider isn't dominated by the few mega-parcels in the dataset.
function computeFilterMaxes(features) {
  const assessed = [];
  const taxes = [];
  for (const f of features || []) {
    const av = +f.properties?.assessed_value;
    if (Number.isFinite(av) && av > 0) assessed.push(av);
    const rpa = +f.properties?.rev_per_ac;
    const ac  = +f.properties?.area_ac;
    if (Number.isFinite(rpa) && Number.isFinite(ac) && ac > 0) {
      taxes.push(rpa * ac);
    }
  }
  const pct99 = (arr) => {
    if (!arr.length) return 0;
    arr.sort((a, b) => a - b);
    return arr[Math.min(arr.length - 1, Math.floor(arr.length * 0.99))];
  };
  STATE.assessedMax = pct99(assessed);
  STATE.taxMax      = pct99(taxes);
}

// Slider position (0–100) → dollar threshold. 0 means the filter is off.
function thresholdFor(pct, max) {
  return pct === 0 ? 0 : (pct / 100) * max;
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
  // Source kept around even though parcels render via deck.gl — station
  // markers and the rail ribbon still consume the MapLibre source pipeline,
  // and a same-named source/layer pair is convenient for future MapLibre
  // overlays (e.g. parcel labels at high zoom).
  map.addSource('parcels', { type: 'geojson', data: STATE.parcels, promoteId: 'tmk' });
  map.addSource('stations', { type: 'geojson', data: STATE.stations });

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
}

// Reporting-year label used everywhere we surface dollar figures — keep
// in one place so we can bump it to FY2027 in a single edit when the
// budget + millage data refreshes.
const FISCAL_YEAR = 'FY2026';
const yearBadgeHTML = `<span class="year-badge">${FISCAL_YEAR}</span>`;

function buildHTML(p) {
  const addr = p.address && p.address !== 'null' ? p.address : null;
  const primaryClass = (m) => `pp-row${STATE.mode === m ? ' primary' : ''}`;
  const subBits = [];
  if (p.tmk) subBits.push(`TMK ${escapeHTML(String(p.tmk))}`);
  return `
    <div class="pp-title">${escapeHTML(addr ?? p.tmk ?? p.parcel_id ?? 'Parcel')} ${yearBadgeHTML}</div>
    ${subBits.length ? `<div class="pp-sub">${subBits.join(' · ')}</div>` : ''}
    <div class="${primaryClass('revenue')}"><span class="k">Revenue / ac</span><span class="v">${fmtUSDk(+p.rev_per_ac)}</span></div>
    <div class="${primaryClass('cost')}"><span class="k">Cost / ac</span><span class="v">${fmtUSDk(+p.cost_per_ac)}</span></div>
    <div class="${primaryClass('net')}"><span class="k">Net / ac</span><span class="v">${fmtUSDk(+p.net_per_ac)}</span></div>
    ${p.area_ac ? `<div class="pp-row"><span class="k">Acres</span><span class="v">${(+p.area_ac).toFixed(2)}</span></div>` : ''}
    ${p.land_use ? `<div class="pp-row"><span class="k">Class</span><span class="v">${escapeHTML(String(p.land_use))}</span></div>` : ''}
  `;
}

// Resolve a list of station_ids on a parcel to readable station names by
// looking up each id in STATE.stations.features. Falls back to "Station N"
// via getStationName() if the dropdown name isn't populated.
function resolveStationNames(stationIds) {
  if (!Array.isArray(stationIds) || !stationIds.length) return [];
  const features = STATE.stations?.features || [];
  return stationIds
    .map((id) => {
      const f = features.find((g) => +getStationId(g) === +id);
      return f ? getStationName(f) : `Station ${id}`;
    })
    .filter(Boolean);
}

function buildPopupHTML(p) {
  const addr = p.address && p.address !== 'null' ? p.address : null;
  const acres = +p.area_ac;
  const hasAcres = Number.isFinite(acres);
  // Annual dollar amounts derived from the per-acre rates × parcel area.
  const annualRev  = hasAcres && Number.isFinite(+p.rev_per_ac)  ? +p.rev_per_ac  * acres : NaN;
  const annualCost = hasAcres && Number.isFinite(+p.cost_per_ac) ? +p.cost_per_ac * acres : NaN;
  const annualNet  = hasAcres && Number.isFinite(+p.net_per_ac)  ? +p.net_per_ac  * acres : NaN;
  const netPosClass = (n) => Number.isFinite(n) ? (n >= 0 ? ' net-pos' : ' net-neg') : '';
  const subParts = [];
  if (p.tmk)      subParts.push(`TMK ${escapeHTML(String(p.tmk))}`);
  if (p.land_use) subParts.push(escapeHTML(String(p.land_use)));
  const stationNames = resolveStationNames(p.station_ids);

  return `
    <div class="parcel-popup__head">
      <button class="parcel-popup__close" aria-label="Close">×</button>
      <div class="parcel-popup__title">${escapeHTML(addr ?? p.tmk ?? 'Parcel')}</div>
      ${subParts.length ? `<div class="parcel-popup__sub">${subParts.join(' · ')}</div>` : ''}
    </div>

    <div class="parcel-popup__section">
      <h3>Assessment ${yearBadgeHTML}</h3>
      <div class="parcel-popup__row"><span class="k">Assessed value</span><span class="v">${fmtUSDk(+p.assessed_value)}</span></div>
      <div class="parcel-popup__row"><span class="k">Property tax</span><span class="v">${fmtUSDk(annualRev)}</span></div>
      <div class="parcel-popup__row"><span class="k">Infrastructure cost</span><span class="v">${fmtUSDk(annualCost)}</span></div>
      <div class="parcel-popup__row"><span class="k">Net</span><span class="v${netPosClass(annualNet)}">${fmtUSDk(annualNet)}</span></div>
    </div>

    <div class="parcel-popup__section">
      <h3>Per acre ${yearBadgeHTML}</h3>
      <div class="parcel-popup__row"><span class="k">Revenue / ac</span><span class="v">${fmtUSDk(+p.rev_per_ac)}</span></div>
      <div class="parcel-popup__row"><span class="k">Cost / ac</span><span class="v">${fmtUSDk(+p.cost_per_ac)}</span></div>
      <div class="parcel-popup__row"><span class="k">Net / ac</span><span class="v${netPosClass(+p.net_per_ac)}">${fmtUSDk(+p.net_per_ac)}</span></div>
    </div>

    <div class="parcel-popup__section">
      <h3>Physical</h3>
      ${hasAcres ? `<div class="parcel-popup__row"><span class="k">Area</span><span class="v">${acres.toFixed(2)} ac</span></div>` : ''}
      <div class="parcel-popup__row"><span class="k">Road frontage</span><span class="v">${fmtFt(p.frontage_road_ft)}</span></div>
      <div class="parcel-popup__row"><span class="k">Sewer frontage</span><span class="v">${fmtFt(p.frontage_sewer_ft)}</span></div>
      <div class="parcel-popup__row"><span class="k">Water frontage</span><span class="v">${fmtFt(p.frontage_water_ft)}</span></div>
      ${p.landlocked === true ? `<span class="parcel-popup__chip">Landlocked</span>` : ''}
    </div>

    ${stationNames.length ? `
    <div class="parcel-popup__section">
      <h3>Walking distance to</h3>
      <div style="font-size:12px;color:var(--text-soft);line-height:1.5;">
        ${stationNames.map(escapeHTML).join(', ')}
      </div>
    </div>` : ''}
  `;
}

// Place the popup near (x, y) — preferring right-of-click — and flip sides
// or clamp vertically if it would overflow the viewport.
function positionPopup(x, y) {
  const rect = map.getContainer().getBoundingClientRect();
  const w = popupEl.offsetWidth || 290;
  const h = popupEl.offsetHeight || 400;
  const margin = 8;
  let px = rect.left + x + 18;
  if (px + w > window.innerWidth - margin) {
    px = rect.left + x - w - 18;
  }
  px = Math.max(margin, px);
  let py = rect.top + y - h / 2;
  py = Math.max(rect.top + margin, Math.min(py, rect.bottom - h - margin, window.innerHeight - h - margin));
  popupEl.style.left = px + 'px';
  popupEl.style.top  = py + 'px';
}

function openPopup(props, x, y) {
  selectedTmk = props.tmk ?? null;
  popupEl.innerHTML = buildPopupHTML(props);
  popupEl.hidden = false;
  // Close button has to be wired after innerHTML is set.
  const closeBtn = popupEl.querySelector('.parcel-popup__close');
  if (closeBtn) closeBtn.addEventListener('click', closePopup);
  positionPopup(x, y);
  refreshLayer();
}

function closePopup() {
  if (selectedTmk === null && popupEl.hidden) return;
  selectedTmk = null;
  popupEl.hidden = true;
  refreshLayer();
}

function handleClick({ object, x, y }) {
  if (object) {
    openPopup(object.properties, x, y);
  } else {
    closePopup();
  }
}

function hexToRgb(hex) {
  const h = hex.replace('#', '');
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}

// Linear-interpolate a value's color from an evenly-spaced palette in [lo, hi].
// JS port of the maplibre `['interpolate', ['linear'], ...rampStops]` we used
// to drive parcels-extrude / parcels-fill paint.
function interpolateColor(value, lo, hi, palette) {
  if (!Number.isFinite(value)) return [200, 200, 200];
  const n = palette.length;
  const span = hi - lo || 1;
  const t = Math.max(0, Math.min(1, (value - lo) / span));
  const idx = t * (n - 1);
  const i0 = Math.floor(idx);
  const i1 = Math.min(n - 1, i0 + 1);
  const frac = idx - i0;
  const c0 = hexToRgb(palette[i0]);
  const c1 = hexToRgb(palette[i1]);
  return [
    Math.round(c0[0] + (c1[0] - c0[0]) * frac),
    Math.round(c0[1] + (c1[1] - c0[1]) * frac),
    Math.round(c0[2] + (c1[2] - c0[2]) * frac),
  ];
}

function buildParcelLayer() {
  const colorKey = METRIC_KEYS[STATE.mode];
  const [lo, hi] = STATE.domain;
  const palette = STATE.mode === 'net' ? DIVERGING_RWG : VIRIDIS;
  // Sqrt-scale heights against 98th-pct peak so low-revenue bars still
  // register and outliers don't blow past HEIGHT_PEAK_M.
  const peak = Math.max(Math.abs(STATE.heightDomain[0]),
                        Math.abs(STATE.heightDomain[1])) || 1;
  const HEIGHT_PEAK_M = 500;
  const MIN_HEIGHT_M = 10;
  const scale = HEIGHT_PEAK_M / Math.sqrt(peak);

  return new deck.GeoJsonLayer({
    id: 'parcels',
    data: STATE.filtered,
    pickable: true,
    stroked: true,
    filled: true,
    extruded: STATE.extrude,
    lineWidthUnits: 'pixels',
    getFillColor: (f) => {
      if (f.properties.tmk === hoveredTmk) return [14, 165, 233, 230];
      const v = +f.properties[colorKey];
      const [r, g, b] = interpolateColor(v, lo, hi, palette);
      return [r, g, b, 217];
    },
    getElevation: (f) => {
      const v = Math.abs(+f.properties[HEIGHT_KEY]);
      if (!Number.isFinite(v)) return MIN_HEIGHT_M;
      return Math.max(MIN_HEIGHT_M, scale * Math.sqrt(Math.min(peak, v)));
    },
    getLineColor: (f) => {
      if (f.properties.tmk === hoveredTmk)  return [14, 165, 233, 255];   // cyan hover
      if (f.properties.tmk === selectedTmk) return [107, 158, 120, 255];  // teal selected
      return [40, 50, 55, 200];
    },
    getLineWidth: (f) => {
      if (f.properties.tmk === selectedTmk) return 4;
      if (f.properties.tmk === hoveredTmk)  return 3;
      return 1;
    },
    onHover: handleHover,
    onClick: handleClick,
    updateTriggers: {
      getFillColor: [STATE.mode, lo, hi, hoveredTmk],
      getElevation: [STATE.heightDomain[0], STATE.heightDomain[1], STATE.extrude],
      getLineColor: [hoveredTmk, selectedTmk],
      getLineWidth: [hoveredTmk, selectedTmk],
    },
  });
}

function refreshLayer() {
  overlay.setProps({ layers: [buildParcelLayer()] });
}

function handleHover({ object, x, y }) {
  const newTmk = object?.properties?.tmk ?? null;
  if (newTmk !== hoveredTmk) {
    hoveredTmk = newTmk;
    refreshLayer();
  }
  if (object) {
    tooltipEl.innerHTML = buildHTML(object.properties);
    // x,y are CSS pixels relative to the deck container (map element); map
    // bounding rect converts to viewport coords for the fixed tooltip.
    const rect = map.getContainer().getBoundingClientRect();
    tooltipEl.style.left = (rect.left + x + 14) + 'px';
    tooltipEl.style.top = (rect.top + y + 12) + 'px';
    tooltipEl.hidden = false;
  } else {
    tooltipEl.hidden = true;
  }
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
    map.setLayoutProperty('rail-line-xray', 'visibility', STATE.extrude ? 'visible' : 'none');
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

  document.getElementById('min-assessed').addEventListener('input', (e) => {
    STATE.minAssessed = +e.target.value;
    refresh();
  });
  document.getElementById('min-tax').addEventListener('input', (e) => {
    STATE.minTax = +e.target.value;
    refresh();
  });
  document.getElementById('filter-reset').addEventListener('click', () => {
    STATE.minAssessed = 0;
    STATE.minTax = 0;
    document.getElementById('min-assessed').value = 0;
    document.getElementById('min-tax').value = 0;
    refresh();
  });
}

// Replace the static "all values per acre" with a metric-specific
// explanation so the panel itself tells the user what the active mode
// actually shows. Keeps technical jargon out — plain English.
const SEG_CAPTIONS = {
  revenue: 'Annual <strong>property tax paid</strong> by the parcel, per acre.',
  cost:    'Annual <strong>cost to the city</strong> for road, water, and sewer service, per acre.',
  net:     '<strong>Revenue minus cost</strong>, per acre. Green pays for itself; red is a net loss.',
};

function updateSegCaption() {
  const el = document.getElementById('seg-caption');
  if (el) el.innerHTML = SEG_CAPTIONS[STATE.mode] || 'all values per acre';
}

function updateFilterUI() {
  const minAv = thresholdFor(STATE.minAssessed, STATE.assessedMax);
  const minTx = thresholdFor(STATE.minTax,      STATE.taxMax);
  const avEl = document.getElementById('min-assessed-val');
  const txEl = document.getElementById('min-tax-val');
  if (STATE.minAssessed === 0) {
    avEl.textContent = 'Off';
    avEl.classList.add('off');
  } else {
    avEl.textContent = '≥ ' + fmtUSDk(minAv);
    avEl.classList.remove('off');
  }
  if (STATE.minTax === 0) {
    txEl.textContent = 'Off';
    txEl.classList.add('off');
  } else {
    txEl.textContent = '≥ ' + fmtUSDk(minTx);
    txEl.classList.remove('off');
  }
  const active = STATE.minAssessed > 0 || STATE.minTax > 0;
  document.getElementById('filter-reset').hidden = !active;
  document.getElementById('filter-count').textContent = active
    ? `${fmtInt.format(STATE.filtered.length)} parcels`
    : '';
}

function selectStation(id) {
  STATE.stationId = id || '';
  const sel = document.getElementById('station-select');
  if (sel.value !== STATE.stationId) sel.value = STATE.stationId;
  // Station filter is applied JS-side in refresh() against STATE.filtered —
  // the deck.gl layer just renders whatever's in that array.
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
  const minAv = thresholdFor(STATE.minAssessed, STATE.assessedMax);
  const minTx = thresholdFor(STATE.minTax,      STATE.taxMax);

  STATE.filtered = (STATE.parcels?.features || []).filter((f) => {
    const p = f.properties;
    if (filterSid !== null && !(p?.station_ids || []).includes(filterSid)) return false;
    if (!Number.isFinite(+p?.[colorKey])) return false;
    if (minAv > 0 && !(+p?.assessed_value >= minAv)) return false;
    if (minTx > 0) {
      const tax = (+p?.rev_per_ac) * (+p?.area_ac);
      if (!(tax >= minTx)) return false;
    }
    return true;
  });

  STATE.domain       = computeDomain(STATE.filtered, colorKey,    STATE.mode === 'net');
  STATE.heightDomain = computeDomain(STATE.filtered, HEIGHT_KEY,  false);

  refreshLayer();
  renderLegend();
  renderSummary();
  updateFilterUI();
  updateSegCaption();
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
