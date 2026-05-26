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

// Property-type buckets. Apartment is split out as Multi-family because its
// economic profile (revenue-dense per acre, comparable to Hotel/Resort under
// Honolulu RPAD) differs sharply from single-family Residential. Public
// Service is split from Other so the filter can default-off Public Service
// (those parcels pay $0 property tax and dominate red-net rendering).
const LAND_USE_BUCKETS = {
  Residential:    ['Residential', 'Residential A'],
  'Multi-family': ['Apartment'],
  Commercial:     ['Commercial', 'Hotel and Resort'],
  Industrial:     ['Industrial'],
  Other:          ['Agricultural', 'Preservation'],
  PublicService:  ['Public Service'],
};
const LAND_USE_TO_BUCKET = (() => {
  const m = {};
  for (const [bucket, classes] of Object.entries(LAND_USE_BUCKETS)) {
    for (const c of classes) m[c] = bucket;
  }
  return m;
})();
const DEFAULT_TYPE_SELECTIONS = ['Residential','Multi-family','Commercial','Industrial','Other'];

const STATE = {
  mode: 'net',
  extrude: true,
  stationId: '',
  showAllParcels: false,
  // Intro card: shown on every page load until the user clicks "Start the
  // journey", clicks anywhere on the map, or picks a station. NOT persisted
  // across reloads — the intro explains the project and we want it visible
  // to every visitor (including returning users on hard refresh).
  introMode: true,
  parcels: null,        // raw FeatureCollection
  stations: null,
  stationsByWest: [],   // stations sorted longitude-ASC for the guided tour
  narratives: {},       // station-name → { framing, theme }
  railLine: null,       // buffered Skyline guideway ribbon polygon
  filtered: [],         // currently visible parcel features
  domain: [0, 1],       // [min, max] of color metric (98th-pct clipped)
  heightDomain: [0, 1], // [min, max] of rev_per_ac across visible parcels
  // Range-filter positions 0–100. [0,100] means no filter applied; position
  // 100 on the upper thumb means "no max" (Infinity) so users don't lop off
  // the top 1% (assessedMax/taxMax are 99th-pct, not absolute max).
  assessedRange: [0, 100],
  taxRange:      [0, 100],
  assessedMax: 0,       // 99th-pct of assessed_value across all parcels
  taxMax: 0,            // 99th-pct of (rev_per_ac × area_ac)
  // TOD scope: max walking distance (in MILES) from a station for a parcel
  // to be visible. Parcels inside an adopted TOD Special District remain
  // visible regardless. Parcels with null walk_dist_ft (unreachable on the
  // street network) are kept too — slider is "max", not "exclude unknown".
  walkDistMaxMi: 1.5,
  typeSelections: new Set(DEFAULT_TYPE_SELECTIONS),
  addressIndex: null,   // Map<normalizedAddress, Feature[]>
  sidebarCollapsed: false,
  railCipOn: false,   // sidebar toggle — adds rail_cip_per_ac to cost/net
};

// Height = net per acre (magnitude); color = selected metric.
// Using absolute value so negative-net parcels still extrude; symmetric domain.
const HEIGHT_KEY = 'net_per_ac';

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

// Non-interleaved overlay for the rail guideway line — renders on a separate
// transparent canvas composited above MapLibre's framebuffer, so it's never
// depth-tested against the 3D parcel bars and always draws on top.
const railOverlay = new deck.MapboxOverlay({ interleaved: false, layers: [] });
map.addControl(railOverlay);

// Map of (numeric) station id → station chip DOM element. Populated during
// addLayers(); read by renderNarrative() to toggle the .active class on
// whichever chip the guided tour is currently focused on.
const chipRefs = new Map();

// Static rail guideway path coordinates — flatMap-handled so MultiLineString
// segments are valid PathLayer inputs. Set once in style.load, reused by
// rebuildRailLayers() on every active-dot pulse frame.
let railPaths = null;

// Active-dot pulse animation state. pulsePhase advances each rAF frame and
// is consumed via Math.sin to oscillate the active station dot's radius.
let pulsePhase = 0;
let pulseRaf = null;

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

// Escape closes the click popup.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && selectedTmk !== null) closePopup();
});

// Click anywhere outside the popup closes it — sidebar, controls, search,
// AND the map (ocean, blank space, even another parcel). When the click lands
// on a parcel, deck.gl's onClick fires after our mousedown and reopens with
// the new parcel; net effect is "switch parcel" with no visible flicker.
document.addEventListener('mousedown', (e) => {
  if (popupEl.hidden) return;
  if (popupEl.contains(e.target)) return;
  closePopup();
});

// deck.gl MapboxOverlay (interleaved:true) calls triggerRepaint() every frame,
// keeping _styleDirty=true permanently so map.loaded() never returns true and
// the 'load' event never fires. Use 'style.load' instead — fires once when the
// style JSON/sprites finish, before deck.gl's render loop starts.
map.once('style.load', async () => {
  try {
    const [parcels, stations, railLine, railGuideway, narratives] = await Promise.all([
      fetchJSON('data/parcels_tod.geojson'),
      fetchJSON('data/stations.geojson'),
      fetchJSON('data/rail_line.geojson'),
      fetchJSON('data/raw/rail_transit_guideway_alignment_line.geojson'),
      fetchJSON('data/station_narratives.json'),
    ]);
    STATE.parcels = parcels;
    STATE.stations = stations;
    STATE.railLine = railLine;
    STATE.railGuideway = railGuideway;
    STATE.narratives = narratives;
    // Tour order for Prev/Next navigation. Uses the official HART station
    // numbering (id 1 = Kualakaʻi, id 13 = Kahauiki) which IS the line's
    // west-to-east sequence — and stays stable even when adjacent stations'
    // longitudes flip by hundredths of a degree.
    STATE.stationsByWest = [...stations.features].sort(
      (a, b) => +getStationId(a) - +getStationId(b)
    );

    populateStationDropdown(stations);
    // Default to the westernmost station so the page opens at the start of
    // the guided tour (Kualakaʻi) instead of "All stations". Setting
    // STATE.stationId here — before refresh() runs — means the first
    // filter pass scopes to this station, matching the narrative panel.
    if (STATE.stationsByWest.length) {
      STATE.stationId = String(getStationId(STATE.stationsByWest[0]));
      const sel = document.getElementById('station-select');
      if (sel) sel.value = STATE.stationId;
    }
    computeFilterMaxes(parcels.features);
    buildAddressIndex();
    addLayers();
    wireUI();
    refresh();

    // Rail guideway overlay — independent of basemap tile state, render now.
    // MultiLineString features (segment #10) are spread into separate paths
    // so getPath receives a flat [[lon,lat],...] array rather than nested
    // arrays.
    railPaths = STATE.railGuideway.features.flatMap(f =>
      f.geometry.type === 'MultiLineString'
        ? f.geometry.coordinates
        : [f.geometry.coordinates]
    );
    rebuildRailLayers();
    startPulseLoop();
    renderNarrative();

    // Jump the camera to the westernmost station BEFORE revealing — first
    // paint should show the final viewport, not a corridor-wide fitBounds.
    const firstStation = STATE.stationsByWest[0];
    const firstCoords = firstStation ? firstStation.geometry.coordinates : [-157.95, 21.38];
    map.jumpTo({
      center: firstCoords,
      zoom: 14.5,
      pitch: 55,
      bearing: 0,
    });

    // Reveal the map as soon as the CURRENT viewport's tiles are loaded.
    // Previously we awaited prewarmOahuTiles() (9 camera sweeps at z8–z11)
    // before adding .ready — that blocked the user behind 4–8 s of black
    // screen on a cold PMTiles cache. Cap the wait at 1.2 s so a slow
    // network can't strand the user looking at a black map forever.
    const viewportReady = (async () => {
      while (!map.areTilesLoaded()) {
        await new Promise(r => setTimeout(r, 30));
      }
    })();
    await Promise.race([
      viewportReady,
      new Promise(r => setTimeout(r, 1200)),
    ]);
    refreshLayer();
    document.getElementById('map').classList.add('ready');

    // Populate area badges from currently-loaded basemap tiles (corridor
    // viewport). As the user pans/zooms, MapLibre loads new place tiles and
    // we re-run addAreaBadges to pick them up. The function dedupes by
    // place name across calls, so repeat fires are cheap and idempotent.
    //
    // Why no prewarm? The old prewarmOahuTiles() used map.jumpTo() to walk
    // the camera across z8–z11, which only works when the map is hidden
    // (opacity 0). Awaiting it before reveal cost 4–8 seconds of black
    // screen on a cold PMTiles cache — by far the biggest perceived load
    // delay. Tiles now load on-demand instead.
    addAreaBadges();
    map.on('sourcedata', (e) => {
      if (e.sourceId === 'openmaptiles' && e.isSourceLoaded) {
        addAreaBadges();
      }
    });
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
// Module-scope dedup set so repeat calls (now fired on tile load) only add
// each place once instead of stacking duplicate markers.
const _seenPlaces = new Set();
function addAreaBadges() {
  let added = 0;
  for (const cls of ['suburb', 'neighbourhood']) {
    const features = map.querySourceFeatures('openmaptiles', {
      sourceLayer: 'place',
      filter: ['==', ['get', 'class'], cls],
    });
    for (const f of features) {
      const name = f.properties.name_en || f.properties.name;
      if (!name || _seenPlaces.has(name)) continue;
      // Vector-tile point features have geometry.coordinates as [lng, lat].
      const coords = f.geometry?.type === 'Point' && f.geometry.coordinates;
      if (!coords) continue;
      _seenPlaces.add(name);
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
  if (added > 0) console.log(`[areas] +${added} badges (total ${_seenPlaces.size})`);
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

// Range-slider [lo, hi] (0–100 each) → [loDollars, hiDollarsOrInfinity].
// Position 100 on the upper thumb maps to Infinity so users don't lose the
// long tail (the visual max anchors at the 99th percentile, not the absolute
// max). Lower 0 maps to 0; lower 100 would map to max but is normally
// constrained by the upper thumb so it doesn't hit Infinity.
function thresholdRange(range, max) {
  const [lo, hi] = range;
  const loDollars = lo === 0 ? 0 : (lo / 100) * max;
  const hiDollars = hi >= 100 ? Infinity : (hi / 100) * max;
  return [loDollars, hiDollars];
}

function populateStationDropdown(stations) {
  const sel = document.getElementById('station-select');
  const features = [...(stations.features || [])];
  features.sort((a, b) => +getStationId(a) - +getStationId(b));
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
      'fill-color': '#00c8e8',
      'fill-opacity': 0.62,
      'fill-antialias': false,
    },
  });

  // Station "pin" dots are rendered as a deck.gl ScatterplotLayer on the
  // non-interleaved railOverlay (alongside the rail-glow/rail-stroke paths),
  // so they composite above the 3D parcel bars instead of being buried at
  // ground level. See the railOverlay.setProps call in map.once('style.load').

  // Floating glass-panel station labels — HTML markers (not symbol layer)
  // because symbol layers have no Z-axis. The negative pixel offset lifts
  // the label above the cyan dot, giving a "floating" look in 3D. A small
  // train glyph distinguishes these from the area-name badges at a glance.
  // SVG icon is the Material "directions_subway" path; currentColor lets
  // it inherit from the badge text color.
  const TRAIN_SVG =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor" ' +
    'aria-hidden="true">' +
    '<path d="M12 2c-4 0-8 .5-8 4v9.5C4 17.43 5.57 19 7.5 19L6 20.5v.5h2.23l' +
    '2-2h3.54l2 2H18v-.5L16.5 19c1.93 0 3.5-1.57 3.5-3.5V6c0-3.5-3.58-4-8-4zM' +
    '7.5 17c-.83 0-1.5-.67-1.5-1.5S6.67 14 7.5 14s1.5.67 1.5 1.5S8.33 17 7.5 ' +
    '17zM11 11H6V6.5h5V11zm2 0V6.5h5V11h-5zm3.5 6c-.83 0-1.5-.67-1.5-1.5s.67-' +
    '1.5 1.5-1.5 1.5.67 1.5 1.5-.67 1.5-1.5 1.5z"/></svg>';
  for (const f of STATE.stations.features) {
    const el = document.createElement('div');
    el.className = 'station-floater';

    // Tour Prev button — hidden by default via CSS; visible only when the
    // chip carries .active. stopPropagation prevents the click bubbling to
    // the deck.gl canvas (which would otherwise miss-fire a parcel click).
    const prevBtn = document.createElement('button');
    prevBtn.className = 'chip-tour-btn chip-tour-prev';
    prevBtn.textContent = '‹';
    prevBtn.setAttribute('aria-label', 'Previous station');
    prevBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      stepTour(-1);
    });

    // Chip body — the icon + name. Wrapped so the buttons can flex around it.
    // Clicking the body jumps the tour to this station (any chip works as a
    // shortcut, not just Prev/Next on the active chip). stopPropagation
    // prevents the click bubbling through to deck.gl's parcel hit-test.
    const body = document.createElement('span');
    body.className = 'station-floater-body';
    body.addEventListener('click', (e) => {
      e.stopPropagation();
      goToStation(f);
    });
    const icon = document.createElement('span');
    icon.className = 'station-icon';
    icon.innerHTML = TRAIN_SVG;
    const name = document.createElement('span');
    name.className = 'station-name';
    name.textContent = getStationName(f);
    body.appendChild(icon);
    body.appendChild(name);

    const nextBtn = document.createElement('button');
    nextBtn.className = 'chip-tour-btn chip-tour-next';
    nextBtn.textContent = '›';
    nextBtn.setAttribute('aria-label', 'Next station');
    nextBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      stepTour(+1);
    });

    el.append(prevBtn, body, nextBtn);
    new maplibregl.Marker({ element: el, offset: [0, -32], anchor: 'bottom' })
      .setLngLat(f.geometry.coordinates)
      .addTo(map);
    // Stash a reference keyed by numeric station id so renderNarrative()
    // can toggle .active on the chip of whichever station is being narrated.
    chipRefs.set(+getStationId(f), el);
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
    <div class="pp-title">${escapeHTML(addr ?? p.tmk ?? p.parcel_id ?? 'Parcel')}</div>
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
  const annualRev    = hasAcres && Number.isFinite(+p.rev_per_ac)     ? +p.rev_per_ac     * acres : NaN;
  const annualCostOM = hasAcres && Number.isFinite(+p.cost_om_per_ac) ? +p.cost_om_per_ac * acres : NaN;
  const annualCIP    = hasAcres && Number.isFinite(+p.cip_per_ac)     ? +p.cip_per_ac     * acres : NaN;
  const annualCost   = hasAcres && Number.isFinite(+p.cost_per_ac)    ? +p.cost_per_ac    * acres : NaN;
  const annualNet    = hasAcres && Number.isFinite(+p.net_per_ac)     ? +p.net_per_ac     * acres : NaN;
  const netPosClass = (n) => Number.isFinite(n) ? (n >= 0 ? ' net-pos' : ' net-neg') : '';
  const subParts = [];
  if (p.tmk)      subParts.push(`TMK ${escapeHTML(String(p.tmk))}`);
  if (p.land_use) subParts.push(escapeHTML(String(p.land_use)));
  const stationNames = resolveStationNames(p.station_ids);

  const todTag = p.in_tod_area
    ? `<span class="parcel-popup__tod-tag" title="This parcel sits inside an adopted TOD Special District (Honolulu DPP).">TOD-zoned</span>`
    : '';
  return `
    <div class="parcel-popup__head">
      <button class="parcel-popup__close" aria-label="Close">×</button>
      <div class="parcel-popup__title">${escapeHTML(addr ?? p.tmk ?? 'Parcel')}</div>
      ${subParts.length ? `<div class="parcel-popup__sub">${subParts.join(' · ')} ${todTag}</div>`
                        : (todTag ? `<div class="parcel-popup__sub">${todTag}</div>` : '')}
    </div>

    <div class="parcel-popup__section">
      <h3>Assessment</h3>
      <div class="parcel-popup__row"><span class="k">Assessed value</span><span class="v">${fmtUSDk(+p.assessed_value)}</span></div>
      <div class="parcel-popup__row"><span class="k">Property tax</span><span class="v">${fmtUSDk(annualRev)}</span></div>
      <div class="parcel-popup__row"><span class="k">Operating cost (O&amp;M)</span><span class="v">${fmtUSDk(annualCostOM)}</span></div>
      <div class="parcel-popup__row"><span class="k">Capital cost (CIP)</span><span class="v">${fmtUSDk(annualCIP)}</span></div>
      <div class="parcel-popup__row"><span class="k">Total infra cost</span><span class="v">${fmtUSDk(annualCost)}</span></div>
      <div class="parcel-popup__row"><span class="k">Net</span><span class="v${netPosClass(annualNet)}">${fmtUSDk(annualNet)}</span></div>
    </div>

    <button type="button" class="parcel-popup__more-toggle" aria-expanded="false">More ▾</button>

    <div class="parcel-popup__more" hidden>
    <div class="parcel-popup__section">
      <h3>Per acre</h3>
      <div class="parcel-popup__row"><span class="k">Revenue / ac</span><span class="v">${fmtUSDk(+p.rev_per_ac)}</span></div>
      <div class="parcel-popup__row"><span class="k">O&amp;M / ac</span><span class="v">${fmtUSDk(+p.cost_om_per_ac)}</span></div>
      <div class="parcel-popup__row"><span class="k">CIP / ac</span><span class="v">${fmtUSDk(+p.cip_per_ac)}</span></div>
      <div class="parcel-popup__row"><span class="k">Cost / ac</span><span class="v">${fmtUSDk(+p.cost_per_ac)}</span></div>
      <div class="parcel-popup__row"><span class="k">Net / ac</span><span class="v${netPosClass(+p.net_per_ac)}">${fmtUSDk(+p.net_per_ac)}</span></div>
    </div>

    <div class="parcel-popup__section">
      <h3>Physical</h3>
      ${hasAcres ? `<div class="parcel-popup__row"><span class="k">Area</span><span class="v">${acres.toFixed(2)} ac</span></div>` : ''}
      ${Number.isFinite(+p.walk_dist_ft) ? `<div class="parcel-popup__row"><span class="k">Walk to nearest station</span><span class="v">${(+p.walk_dist_ft / 5280).toFixed(2)} mi</span></div>` : ''}
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
    </div>
  `;
}

// Place the popup near (x, y) — preferring right-of-click — and flip sides
// or clamp vertically if it would overflow the viewport.
//
// Avoidance: if the proposed rect would overlap the floating narrative card
// (the guided-tour card centered on the map), shift the popup above the
// card, then below, then beside, in that order. The narrative card is the
// only on-map UI big enough to compete with the popup; ignore it when
// collapsed (~50px tall, barely in the way).
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

  // Narrative-card avoidance. The popup rect proposed above might overlap
  // the narrative card — if so, try to nudge it out of the way without
  // losing the click-anchored feel.
  const card = document.getElementById('map-narrative-card');
  if (card && !card.hidden && !card.classList.contains('collapsed')) {
    const cardRect = card.getBoundingClientRect();
    const gap = 12;
    const overlapsX = px < cardRect.right + gap && px + w > cardRect.left - gap;
    const overlapsY = py < cardRect.bottom + gap && py + h > cardRect.top - gap;
    if (overlapsX && overlapsY) {
      // 1. Try above the card.
      const above = cardRect.top - gap - h;
      if (above >= rect.top + margin) {
        py = above;
      } else {
        // 2. Try below the card.
        const below = cardRect.bottom + gap;
        if (below + h <= rect.bottom - margin && below + h <= window.innerHeight - margin) {
          py = below;
        } else {
          // 3. Try beside the card — prefer the side the click came from
          // so the popup feels click-anchored even when nudged.
          const clickAbsX = rect.left + x;
          const cardCenterX = cardRect.left + cardRect.width / 2;
          const leftSlot = cardRect.left - gap - w;
          const rightSlot = cardRect.right + gap;
          const leftFits = leftSlot >= rect.left + margin;
          const rightFits = rightSlot + w <= rect.right - margin
                         && rightSlot + w <= window.innerWidth - margin;
          const clickIsRight = clickAbsX > cardCenterX;
          if (clickIsRight && rightFits)      px = rightSlot;
          else if (!clickIsRight && leftFits) px = leftSlot;
          else if (leftFits)                  px = leftSlot;
          else if (rightFits)                 px = rightSlot;
          // 4. If nothing fits, leave at the original spot — viewport too
          // narrow to avoid; the z-index stack lets the popup win.
        }
      }
    }
  }

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
  const moreBtn = popupEl.querySelector('.parcel-popup__more-toggle');
  const moreEl  = popupEl.querySelector('.parcel-popup__more');
  if (moreBtn && moreEl) {
    moreBtn.addEventListener('click', () => {
      const expanded = !moreEl.hidden;
      moreEl.hidden = expanded;
      moreBtn.setAttribute('aria-expanded', String(!expanded));
      moreBtn.textContent = expanded ? 'More ▾' : 'Less ▴';
      positionPopup(x, y);
    });
  }
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
      let v = +f.properties[colorKey];
      if (STATE.railCipOn) {
        const railRate = +f.properties.rail_cip_per_ac || 0;
        if (STATE.mode === 'cost') v = v + railRate;
        else if (STATE.mode === 'net') v = v - railRate;
      }
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
      getFillColor: [STATE.mode, lo, hi, hoveredTmk, STATE.railCipOn],
      getElevation: [STATE.heightDomain[0], STATE.heightDomain[1], STATE.extrude],
      getLineColor: [hoveredTmk, selectedTmk],
      getLineWidth: [hoveredTmk, selectedTmk],
    },
  });
}

function getLayers() {
  return [buildParcelLayer()];
}

function refreshLayer() {
  overlay.setProps({ layers: getLayers() });
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
    // Selecting any station dismisses the intro overlay.
    exitIntroMode();
    selectStation(e.target.value);
  });

  // Floating tour card — dismiss/reopen persistence. The × button fully
  // hides the card; the user reopens it by clicking any station chip
  // (handled in goToStation). State persists across reloads via localStorage.
  const card = document.getElementById('map-narrative-card');
  const closeBtn = document.getElementById('map-narrative-close');
  if (card) {
    // Apply / clear the intro-mode class based on STATE.introMode. HTML
    // defaults to .intro-mode on; if intro is off (e.g. future state
    // change), strip the class so the station body renders.
    if (!STATE.introMode) card.classList.remove('intro-mode');
    // Honor a previous dismiss only when we're past the intro — the intro
    // is project framing that every visitor should see, so on reload we
    // always show it regardless of an old dismiss flag.
    if (!STATE.introMode &&
        localStorage.getItem('tod-narrative-dismissed') === '1') {
      card.hidden = true;
    }
  }
  if (closeBtn) {
    closeBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      // Closing the intro just dismisses for this session — the intro
      // shows again on next page load so every visitor sees the framing.
      if (STATE.introMode) {
        STATE.introMode = false;
        card.classList.remove('intro-mode');
      }
      card.hidden = true;
      localStorage.setItem('tod-narrative-dismissed', '1');
    });
  }

  // "Start the journey" button on the intro card.
  const startBtn = document.getElementById('map-narrative-start');
  if (startBtn) {
    startBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      exitIntroMode();
    });
  }

  // Clicking anywhere on the map (parcel, basemap, blank area) also
  // dismisses the intro — the user has clearly engaged. Registered after
  // map exists so it doesn't no-op. Listener inside the card itself stops
  // propagation so clicks inside the card don't accidentally dismiss
  // (e.g. clicking the close button while still in intro mode).
  if (typeof map !== 'undefined' && map && map.on) {
    map.on('click', () => exitIntroMode());
  }
  if (card) {
    card.addEventListener('click', (e) => {
      // Let the start button / close button handle their own clicks; this
      // just prevents bubbling to the map click above.
      e.stopPropagation();
    });
  }

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

  // 3D extrusion is permanently on (toggle removed from sidebar); STATE.extrude
  // is initialized true and the rail-line xray layer is always visible.
  const extrudeToggle = document.getElementById('extrude-toggle');
  if (extrudeToggle) {
    extrudeToggle.addEventListener('change', (e) => {
      STATE.extrude = e.target.checked;
      map.setLayoutProperty('rail-line-xray', 'visibility', STATE.extrude ? 'visible' : 'none');
      if (STATE.extrude) {
        const targetPitch = Math.max(map.getPitch(), 60);
        const targetZoom  = Math.max(map.getZoom(), 15);
        map.easeTo({ pitch: targetPitch, zoom: targetZoom, duration: 800 });
      } else {
        map.easeTo({ pitch: 0, duration: 600 });
      }
      refresh();
    });
  }

  const chkRailCip = document.getElementById('chk-rail-cip');
  if (chkRailCip) {
    chkRailCip.addEventListener('change', () => {
      STATE.railCipOn = chkRailCip.checked;
      refresh();
    });
  }

  const chkAllParcels = document.getElementById('chk-all-parcels');
  if (chkAllParcels) {
    chkAllParcels.addEventListener('change', () => {
      STATE.showAllParcels = chkAllParcels.checked;
      refresh();
    });
  }

  wireRangeFilter('assessed', 'assessedRange');
  wireRangeFilter('tax',      'taxRange');

  // TOD-scope walking-distance slider — single-thumb, value in miles.
  const walkSlider = document.getElementById('walk-dist-slider');
  const walkValEl  = document.getElementById('walk-dist-val');
  if (walkSlider && walkValEl) {
    const updateWalkVal = () => {
      const mi = +walkSlider.value;
      STATE.walkDistMaxMi = mi;
      walkValEl.textContent = mi.toFixed(2).replace(/0$/, '') + ' mi';
    };
    walkSlider.addEventListener('input', () => {
      updateWalkVal();
      refresh();
    });
    updateWalkVal();
  }

  // Property-type checkboxes — each maps to a bucket key in
  // STATE.typeSelections (a Set). Public Service starts unchecked per the
  // asymmetric default; reset restores DEFAULT_TYPE_SELECTIONS.
  document.querySelectorAll('.type-check input[type=checkbox]').forEach((cb) => {
    const bucket = cb.dataset.bucket;
    cb.addEventListener('change', () => {
      if (cb.checked) STATE.typeSelections.add(bucket);
      else            STATE.typeSelections.delete(bucket);
      refresh();
    });
  });

  document.getElementById('filter-reset').addEventListener('click', () => {
    STATE.assessedRange = [0, 100];
    STATE.taxRange      = [0, 100];
    document.getElementById('assessed-min').value       = 0;
    document.getElementById('assessed-max-input').value = 100;
    document.getElementById('tax-min').value            = 0;
    document.getElementById('tax-max-input').value      = 100;
    STATE.walkDistMaxMi = 1.5;
    const walkSliderEl = document.getElementById('walk-dist-slider');
    const walkValElReset = document.getElementById('walk-dist-val');
    if (walkSliderEl) walkSliderEl.value = 1.5;
    if (walkValElReset) walkValElReset.textContent = '1.5 mi';
    STATE.typeSelections = new Set(DEFAULT_TYPE_SELECTIONS);
    document.querySelectorAll('.type-check input[type=checkbox]').forEach((cb) => {
      cb.checked = STATE.typeSelections.has(cb.dataset.bucket);
    });
    refresh();
  });

  const typeToggleBtn = document.querySelector('.type-filter-toggle');
  const typeMore = document.getElementById('type-filter-more');
  if (typeToggleBtn && typeMore) {
    typeToggleBtn.addEventListener('click', () => {
      const expanded = typeToggleBtn.getAttribute('aria-expanded') === 'true';
      typeToggleBtn.setAttribute('aria-expanded', String(!expanded));
      typeMore.hidden = expanded;
    });
  }

  wireSidebarToggle();
  wireSidebarResize();
  wireAddressSearch();
  wireSummaryTooltips();
  wireSegTooltip();
}

// Dual-thumb range input wiring. The two inputs occupy the same screen space;
// JS enforces lo ≤ hi by clamping the just-touched thumb against its
// counterpart, and bumps the active thumb's z-index so it stays draggable
// when both thumbs collide at 0/0 or 100/100.
function wireRangeFilter(prefix, stateKey) {
  const minEl = document.getElementById(`${prefix}-min`);
  const maxEl = document.getElementById(`${prefix}-max-input`);

  function bumpZ(active) {
    minEl.style.zIndex = active === minEl ? 3 : 2;
    maxEl.style.zIndex = active === maxEl ? 3 : 2;
  }

  minEl.addEventListener('input', () => {
    let lo = +minEl.value;
    const hi = +maxEl.value;
    if (lo > hi) { lo = hi; minEl.value = lo; }
    STATE[stateKey] = [lo, hi];
    bumpZ(minEl);
    refresh();
  });
  maxEl.addEventListener('input', () => {
    const lo = +minEl.value;
    let hi = +maxEl.value;
    if (hi < lo) { hi = lo; maxEl.value = hi; }
    STATE[stateKey] = [lo, hi];
    bumpZ(maxEl);
    refresh();
  });
}

// localStorage helpers — Safari denies on file:// origins. Wrap every
// access; degrade silently to defaults so the page keeps working.
function loadPref(key, fallback) {
  try {
    const v = window.localStorage.getItem(`tod.${key}`);
    if (v === null) return fallback;
    return JSON.parse(v);
  } catch (_) {
    return fallback;
  }
}
function savePref(key, val) {
  try { window.localStorage.setItem(`tod.${key}`, JSON.stringify(val)); }
  catch (_) { /* swallow */ }
}

function wireSidebarToggle() {
  const btn = document.getElementById('sidebar-toggle');
  const setCollapsed = (val) => {
    STATE.sidebarCollapsed = !!val;
    document.body.classList.toggle('sidebar-collapsed', !!val);
    btn.setAttribute('aria-label', val ? 'Expand sidebar' : 'Collapse sidebar');
    btn.title = val ? 'Expand sidebar' : 'Collapse sidebar';
    requestAnimationFrame(() => { try { map.resize(); } catch (_) {} });
    savePref('sidebarCollapsed', !!val);
  };
  btn.addEventListener('click', () => setCollapsed(!STATE.sidebarCollapsed));
  // Restore collapsed state from a previous session.
  if (loadPref('sidebarCollapsed', false)) setCollapsed(true);
}

function wireSidebarResize() {
  const handle = document.getElementById('sidebar-resize');
  if (!handle) return;
  // Restore previous width before first paint to avoid layout flash.
  const stored = loadPref('sidebarWidth', null);
  if (typeof stored === 'number' &&
      stored >= 280 && stored <= 600) {
    document.documentElement.style.setProperty('--sidebar-w', stored + 'px');
  }

  let dragging = false;
  let pendingWidth = null;
  let rafQueued = false;

  function applyPending() {
    rafQueued = false;
    if (pendingWidth !== null) {
      document.documentElement.style.setProperty('--sidebar-w', pendingWidth + 'px');
      try { map.resize(); } catch (_) {}
    }
  }

  handle.addEventListener('pointerdown', (e) => {
    if (STATE.sidebarCollapsed) return;
    dragging = true;
    handle.setPointerCapture(e.pointerId);
    document.body.classList.add('sidebar-resizing');
    e.preventDefault();
  });
  handle.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    // Sidebar lives flush against the left edge, so its width is just the
    // pointer's x-coord clamped to [min, max].
    const w = Math.max(280, Math.min(600, Math.round(e.clientX)));
    pendingWidth = w;
    if (!rafQueued) {
      rafQueued = true;
      requestAnimationFrame(applyPending);
    }
    e.preventDefault();
  });
  function endDrag(e) {
    if (!dragging) return;
    dragging = false;
    try { handle.releasePointerCapture(e.pointerId); } catch (_) {}
    document.body.classList.remove('sidebar-resizing');
    if (pendingWidth !== null) savePref('sidebarWidth', pendingWidth);
    pendingWidth = null;
  }
  handle.addEventListener('pointerup',     endDrag);
  handle.addEventListener('pointercancel', endDrag);
}

// --- Address search ---------------------------------------------------------

// Normalize an address for substring matching. Lowercase, strip punctuation
// other than spaces and digits, collapse whitespace.
function normalizeAddress(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/[^\w\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function buildAddressIndex() {
  const index = new Map();  // normalizedAddress -> Feature[]
  for (const f of STATE.parcels?.features || []) {
    const addr = f.properties?.address;
    if (!addr || addr === 'null') continue;
    const key = normalizeAddress(addr);
    if (!key) continue;
    const list = index.get(key);
    if (list) list.push(f);
    else index.set(key, [f]);
  }
  STATE.addressIndex = index;
}

// Bounding box of a GeoJSON Polygon or MultiPolygon — [w, s, e, n].
function bboxOfFeature(feature) {
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  const visit = (coords) => {
    if (typeof coords[0] === 'number') {
      if (coords[0] < w) w = coords[0];
      if (coords[0] > e) e = coords[0];
      if (coords[1] < s) s = coords[1];
      if (coords[1] > n) n = coords[1];
      return;
    }
    for (const c of coords) visit(c);
  };
  if (feature?.geometry?.coordinates) visit(feature.geometry.coordinates);
  if (!isFinite(w)) return null;
  return [w, s, e, n];
}

// Bbox center — guaranteed within the parcel's footprint area, unlike a
// vertex-average centroid which can fall outside MultiPolygons.
function centroidOfFeature(feature) {
  const b = bboxOfFeature(feature);
  if (!b) return null;
  return [(b[0] + b[2]) / 2, (b[1] + b[3]) / 2];
}

let searchActiveIndex = -1;
let searchRows = [];  // [{ feature?, features?, kind: 'addr'|'sub'|'tmk' }]

function isTmkLike(q) {
  return /^\d{6,9}$/.test(q.replace(/\s+/g, ''));
}

function runSearch(rawQuery) {
  const q = (rawQuery || '').trim();
  const out = document.getElementById('search-results');
  if (q.length < 2) {
    out.hidden = true;
    out.innerHTML = '';
    searchRows = [];
    searchActiveIndex = -1;
    return;
  }
  const rows = [];

  // TMK exact-prefix match. Feature TMKs are 8-digit strings.
  if (isTmkLike(q)) {
    const target = q.replace(/\s+/g, '');
    for (const f of STATE.parcels?.features || []) {
      const tmk = String(f.properties?.tmk || '');
      if (tmk.startsWith(target)) {
        rows.push({ kind: 'tmk', feature: f });
        if (rows.length >= 8) break;
      }
    }
  }

  // Address substring match.
  const norm = normalizeAddress(q);
  if (norm.length >= 3 && STATE.addressIndex) {
    const seen = new Set();
    for (const [key, features] of STATE.addressIndex) {
      if (!key.includes(norm)) continue;
      if (seen.has(key)) continue;
      seen.add(key);
      rows.push({ kind: 'addr', features });
      if (rows.length >= 8) break;
    }
  }

  searchRows = rows;
  searchActiveIndex = rows.length ? 0 : -1;
  renderSearchResults();
  out.hidden = false;
}

// When a multi-feature address row is expanded inline, this holds the
// feature list keyed by sub-row data-i.
let searchSubFeatures = null;

function renderSearchResults() {
  const out = document.getElementById('search-results');
  searchSubFeatures = null;
  if (!searchRows.length) {
    out.innerHTML = '<div class="search-empty">No matches</div>';
    return;
  }
  out.innerHTML = searchRows.map((row, i) => {
    const active = i === searchActiveIndex ? ' is-active' : '';
    if (row.kind === 'tmk') {
      const p = row.feature.properties;
      const addr = p.address && p.address !== 'null' ? p.address : '(no address)';
      return `<div class="search-result${active}" data-index="${i}">
        <div class="search-result__main">
          <div class="search-result__addr">TMK ${escapeHTML(String(p.tmk))}</div>
          <div class="search-result__meta">${escapeHTML(addr)}${p.land_use ? ' · ' + escapeHTML(String(p.land_use)) : ''}</div>
        </div>
      </div>`;
    }
    // Address — possibly multiple parcels (condos sharing a TMK or street).
    const features = row.features;
    const first = features[0].properties;
    const stationNames = resolveStationNames(first.station_ids).join(', ');
    const addr = first.address && first.address !== 'null' ? first.address : '(no address)';
    const meta = [
      stationNames ? stationNames : null,
      `${features.length} parcel${features.length > 1 ? 's' : ''}`,
    ].filter(Boolean).join(' · ');
    const badge = features.length > 1
      ? `<span class="search-result__count">× ${features.length}</span>`
      : '';
    return `<div class="search-result${active}" data-index="${i}">
      <div class="search-result__main">
        <div class="search-result__addr">${escapeHTML(addr)}</div>
        <div class="search-result__meta">${escapeHTML(meta)}</div>
      </div>
      ${badge}
    </div>`;
  }).join('');
}

function setSearchActive(idx) {
  if (idx === searchActiveIndex) return;
  searchActiveIndex = idx;
  const out = document.getElementById('search-results');
  out.querySelectorAll('.search-result').forEach((node) => {
    const i = node.dataset.index !== undefined ? +node.dataset.index : -1;
    node.classList.toggle('is-active', i === idx);
  });
}

function selectSearchRow(row) {
  if (row.kind === 'tmk') {
    flyToFeature(row.feature);
    closeSearchDropdown(true);
    return;
  }
  const features = row.features;
  if (features.length === 1) {
    flyToFeature(features[0]);
    closeSearchDropdown(true);
    return;
  }
  // Expand inline: replace the dropdown content with one row per matching
  // parcel so the user can disambiguate condos by TMK.
  const out = document.getElementById('search-results');
  searchSubFeatures = features;
  out.innerHTML = features.map((f, i) => {
    const p = f.properties;
    const meta = [`TMK ${p.tmk}`, p.land_use, p.area_ac ? `${(+p.area_ac).toFixed(2)} ac` : null]
      .filter(Boolean).join(' · ');
    return `<div class="search-result is-sub" data-i="${i}">
      <div class="search-result__main">
        <div class="search-result__addr">${escapeHTML(String(p.address || ''))}</div>
        <div class="search-result__meta">${escapeHTML(meta)}</div>
      </div>
    </div>`;
  }).join('');
}

function flyToFeature(feature) {
  const bbox = bboxOfFeature(feature);
  const center = bbox ? [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2] : centroidOfFeature(feature);
  if (!center) return;
  // Highlight the target parcel so the user sees what they selected even
  // before the camera lands.
  selectedTmk = feature.properties?.tmk ?? null;
  refreshLayer();

  const targetPitch = STATE.extrude ? 60 : 0;
  // fitBounds with padding ensures the parcel is on-screen at an appropriate
  // zoom regardless of size. Cap maxZoom so tiny parcels don't go to z22.
  if (bbox && (bbox[2] - bbox[0]) > 1e-6 && (bbox[3] - bbox[1]) > 1e-6) {
    map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], {
      padding: 120,
      maxZoom: 18,
      pitch: targetPitch,
      duration: 700,
      essential: true,
    });
  } else {
    map.flyTo({
      center,
      zoom: 18,
      pitch: targetPitch,
      duration: 700,
      essential: true,
    });
  }
  map.once('moveend', () => {
    try {
      const px = map.project(center);
      openPopup(feature.properties, px.x, px.y);
    } catch (_) { /* map may have unloaded */ }
  });
}

function closeSearchDropdown(clearInput) {
  const out = document.getElementById('search-results');
  out.hidden = true;
  out.innerHTML = '';
  searchRows = [];
  searchActiveIndex = -1;
  searchSubFeatures = null;
  if (clearInput) {
    const inp = document.getElementById('search-input');
    inp.value = '';
    inp.blur();
  }
}

function wireAddressSearch() {
  const input = document.getElementById('search-input');
  const out   = document.getElementById('search-results');
  let debounceTimer = null;

  input.addEventListener('input', () => {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => runSearch(input.value), 80);
  });

  input.addEventListener('keydown', (e) => {
    // Stop arrow keys / Enter / Escape from reaching the map's keyboard
    // handler so they navigate the dropdown instead of panning the map.
    if (['ArrowDown','ArrowUp','Enter','Escape'].includes(e.key)) {
      e.stopPropagation();
    }
    if (e.key === 'ArrowDown' && searchRows.length && !searchSubFeatures) {
      e.preventDefault();
      setSearchActive((searchActiveIndex + 1) % searchRows.length);
    } else if (e.key === 'ArrowUp' && searchRows.length && !searchSubFeatures) {
      e.preventDefault();
      setSearchActive((searchActiveIndex - 1 + searchRows.length) % searchRows.length);
    } else if (e.key === 'Enter') {
      const row = searchRows[searchActiveIndex];
      if (row) {
        e.preventDefault();
        selectSearchRow(row);
      }
    } else if (e.key === 'Escape') {
      closeSearchDropdown(true);
    }
  });

  input.addEventListener('focus', () => {
    if (input.value.trim().length >= 2 && searchRows.length) {
      out.hidden = false;
    }
  });

  // Delegated handlers on the dropdown container — listeners survive every
  // re-render, so clicks always fire even when innerHTML was just replaced.
  // Using mousedown (not click) because click fires after blur, by which
  // point the dropdown may already be hidden.
  out.addEventListener('mousedown', (e) => {
    const el = e.target.closest('.search-result');
    if (!el || !out.contains(el)) return;
    e.preventDefault();
    if (searchSubFeatures && el.dataset.i !== undefined) {
      const f = searchSubFeatures[+el.dataset.i];
      if (f) {
        flyToFeature(f);
        closeSearchDropdown(true);
      }
      return;
    }
    if (el.dataset.index !== undefined) {
      const row = searchRows[+el.dataset.index];
      if (row) selectSearchRow(row);
    }
  });

  out.addEventListener('mousemove', (e) => {
    if (searchSubFeatures) return;
    const el = e.target.closest('.search-result');
    if (!el || el.dataset.index === undefined) return;
    setSearchActive(+el.dataset.index);
  });

  // Click outside closes the dropdown. Use mousedown so the dropdown's own
  // mousedown handlers run first (they preventDefault and call select).
  document.addEventListener('mousedown', (e) => {
    if (!e.target.closest('#search-box')) closeSearchDropdown(false);
  });
}


function rangeLabel(range, max) {
  const [lo, hi] = range;
  if (lo === 0 && hi >= 100) return 'All';
  const [loD, hiD] = thresholdRange(range, max);
  if (lo === 0)        return '≤ ' + fmtUSDk(hiD);
  if (hi >= 100)       return '≥ ' + fmtUSDk(loD);
  return fmtUSDk(loD) + '–' + fmtUSDk(hiD);
}

function updateRangeFill(prefix, range) {
  const fill = document.getElementById(`${prefix}-fill`);
  if (!fill) return;
  const [lo, hi] = range;
  fill.style.left  = `${lo}%`;
  fill.style.width = `${Math.max(0, hi - lo)}%`;
}

function updateFilterUI() {
  const avEl = document.getElementById('assessed-val');
  const txEl = document.getElementById('tax-val');
  const avLabel = rangeLabel(STATE.assessedRange, STATE.assessedMax);
  const txLabel = rangeLabel(STATE.taxRange,      STATE.taxMax);
  avEl.textContent = avLabel;
  avEl.classList.toggle('off', avLabel === 'All');
  txEl.textContent = txLabel;
  txEl.classList.toggle('off', txLabel === 'All');
  updateRangeFill('assessed', STATE.assessedRange);
  updateRangeFill('tax',      STATE.taxRange);

  const allBuckets = Object.keys(LAND_USE_BUCKETS).length;
  const defaultTypes = DEFAULT_TYPE_SELECTIONS.length;
  const typesAreDefault =
    STATE.typeSelections.size === defaultTypes &&
    DEFAULT_TYPE_SELECTIONS.every((b) => STATE.typeSelections.has(b));
  const rangesActive =
    STATE.assessedRange[0] !== 0 || STATE.assessedRange[1] !== 100 ||
    STATE.taxRange[0]      !== 0 || STATE.taxRange[1]      !== 100;
  const walkActive = STATE.walkDistMaxMi < 1.5;
  const active = rangesActive || !typesAreDefault || walkActive;

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
  const [avLo, avHi] = thresholdRange(STATE.assessedRange, STATE.assessedMax);
  const [txLo, txHi] = thresholdRange(STATE.taxRange,      STATE.taxMax);
  const types = STATE.typeSelections;
  const allTypesSelected = types.size === Object.keys(LAND_USE_BUCKETS).length;
  // walk_dist_ft is in feet; slider is in miles.
  const walkMaxFt = STATE.walkDistMaxMi * 5280;

  STATE.filtered = (STATE.parcels?.features || []).filter((f) => {
    const p = f.properties;
    if (!STATE.showAllParcels) {
      if (filterSid !== null && !(p?.station_ids || []).includes(filterSid)) return false;
      // TOD scope: parcels in adopted TOD areas always pass; otherwise check
      // walking distance. Parcels with null walk_dist_ft (unreachable, ~10
      // out of 19,872) are kept so the slider can't silently drop them.
      if (!p?.in_tod_area) {
        const wd = +p?.walk_dist_ft;
        if (Number.isFinite(wd) && wd > walkMaxFt) return false;
      }
    }
    if (!Number.isFinite(+p?.[colorKey])) return false;
    const av = +p?.assessed_value;
    if (avLo > 0 && !(av >= avLo)) return false;
    if (avHi !== Infinity && !(av <= avHi)) return false;
    if (txLo > 0 || txHi !== Infinity) {
      const tax = (+p?.rev_per_ac) * (+p?.area_ac);
      if (txLo > 0 && !(tax >= txLo)) return false;
      if (txHi !== Infinity && !(tax <= txHi)) return false;
    }
    if (!allTypesSelected) {
      const bucket = LAND_USE_TO_BUCKET[p?.land_use];
      // Parcels with unknown / empty land_use are kept unless ALL buckets are
      // unchecked — otherwise unrecognised classes would silently disappear
      // from the filter UX.
      if (bucket && !types.has(bucket)) return false;
    }
    return true;
  });

  STATE.domain       = computeDomain(STATE.filtered, colorKey, STATE.mode === 'net');
  STATE.heightDomain = computeDomain(STATE.filtered, HEIGHT_KEY, true);

  // When rail toggle is on, shift the domain by rail_cip_per_ac so the
  // legend and color ramp reflect adjusted cost/net values.
  if (STATE.railCipOn && STATE.filtered.length) {
    const railRate = +STATE.filtered[0].properties.rail_cip_per_ac || 0;
    if (railRate > 0) {
      const [dLo, dHi] = STATE.domain;
      if (STATE.mode === 'cost') {
        // effectiveCost = cost_per_ac + railRate → domain shifts uniformly
        STATE.domain = [dLo + railRate, dHi + railRate];
      } else if (STATE.mode === 'net') {
        // effectiveNet = net_per_ac - railRate → recompute symmetric domain
        // from the shifted 2nd/98th percentile of the actual shifted values
        const shiftedVals = [];
        for (const f of STATE.filtered) {
          const v = +f.properties[colorKey] - railRate;
          if (Number.isFinite(v)) shiftedVals.push(v);
        }
        shiftedVals.sort((a, b) => a - b);
        if (shiftedVals.length) {
          const qi = (p) => shiftedVals[Math.max(0, Math.min(shiftedVals.length - 1, Math.floor(p * (shiftedVals.length - 1))))];
          const m = Math.max(Math.abs(qi(0.02)), Math.abs(qi(0.98))) || 1;
          STATE.domain = [-m, m];
        }
      }
      // revenue mode: unaffected by rail CIP
    }
  }

  refreshLayer();
  renderLegend();
  renderSummary();
  renderNarrative();
  updateFilterUI();
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
  const railRate = STATE.filtered.length ? (+STATE.filtered[0].properties.rail_cip_per_ac || 0) : 0;
  const railOnAndRelevant = STATE.railCipOn && railRate > 0 && STATE.mode !== 'revenue';
  const railLine = railOnAndRelevant
    ? `<div class="muted" style="font-size:10px;margin-top:4px;">+ rail CIP ${fmtUSDk(railRate)}/ac uniform across corridor</div>`
    : '';
  const loColor  = palette[0];
  const hiColor  = palette[palette.length - 1];
  // Mix a palette endpoint with white (t=0.6) so dark colors become legible
  // on the dark sidebar. Keeps the hue recognizable while boosting luminance.
  const lighten = (hex, t = 0.6) => {
    const [r, g, b] = hexToRgb(hex);
    const lr = Math.round(r + (255 - r) * t);
    const lg = Math.round(g + (255 - g) * t);
    const lb = Math.round(b + (255 - b) * t);
    return `rgb(${lr},${lg},${lb})`;
  };
  const loText  = lighten(loColor);
  const hiText  = lighten(hiColor);
  const midBadge = STATE.mode === 'net'
    ? `<span class="legend-badge" style="background:rgba(255,255,255,0.10);border-color:rgba(255,255,255,0.28);color:rgba(255,255,255,0.75);">0</span>`
    : '';
  legend.innerHTML = `
    <div class="legend-title">Color: ${METRIC_LABELS[STATE.mode]}</div>
    <div class="legend-bar" style="background:${gradient};"></div>
    <div class="legend-labels">
      <span class="legend-badge" style="background:${loColor}33;border-color:${loColor}99;color:${loText};">${fmtUSDk(lo)}</span>
      ${midBadge}
      <span class="legend-badge" style="background:${hiColor}33;border-color:${hiColor}99;color:${hiText};">${fmtUSDk(hi)}</span>
    </div>
    ${railLine}
  `;
}

// Returns the slice of STATE.filtered that the Summary panel and narrative
// card should aggregate over. Normally that's just STATE.filtered, but when
// "Show all parcels" is on we still want the station-anchored stats to
// reflect only the selected station's TOD — the toggle changes what's
// VISIBLE on the map, not what the chosen station's totals mean.
function summaryScopedFeatures() {
  if (!STATE.showAllParcels) return STATE.filtered;
  const sid = STATE.stationId ? +STATE.stationId : null;
  if (sid === null) return STATE.filtered;
  return STATE.filtered.filter(
    (f) => (f.properties?.station_ids || []).includes(sid)
  );
}

function renderSummary() {
  const el = document.getElementById('summary');
  const features = summaryScopedFeatures();
  if (!features.length) {
    el.innerHTML = `<p class="muted">No parcels for current selection.</p>`;
    return;
  }

  let totalRev = 0, totalCost = 0, totalCostOM = 0, totalCIP = 0, totalAcres = 0;
  let hasAcres = false;
  // Revenue breakdown by property-type bucket (matches the new filter UX
  // so users see the same vocabulary in both places).
  const revByBucket = {};
  for (const b of Object.keys(LAND_USE_BUCKETS)) revByBucket[b] = 0;
  let revOther = 0;
  for (const f of features) {
    const p = f.properties;
    const acres = +p.area_ac;
    if (!Number.isFinite(acres)) continue;
    hasAcres = true;
    totalAcres += acres;
    const rev    = Number.isFinite(+p.rev_per_ac)     ? +p.rev_per_ac     * acres : 0;
    const costOM = Number.isFinite(+p.cost_om_per_ac) ? +p.cost_om_per_ac * acres : 0;
    const cip    = Number.isFinite(+p.cip_per_ac)     ? +p.cip_per_ac     * acres : 0;
    const railAdj = STATE.railCipOn ? (+p.rail_cip_per_ac || 0) : 0;
    const cost   = Number.isFinite(+p.cost_per_ac)    ? (+p.cost_per_ac + railAdj) * acres : 0;
    totalRev    += rev;
    totalCostOM += costOM;
    totalCIP    += cip;
    totalCost   += cost;
    const bucket = LAND_USE_TO_BUCKET[p.land_use];
    if (bucket) revByBucket[bucket] += rev;
    else        revOther += rev;
  }
  const totalNet = totalRev - totalCost;

  // Build the multi-line tooltip strings. Newlines render via CSS
  // white-space: pre-line. Buckets with $0 are omitted to keep the
  // tooltip short when filters narrow the visible set.
  const revTooltipLines = [];
  for (const [bucket, classes] of Object.entries(LAND_USE_BUCKETS)) {
    const v = revByBucket[bucket] || 0;
    if (v <= 0) continue;
    const pct = totalRev > 0 ? Math.round((v / totalRev) * 100) : 0;
    const label = bucket === 'PublicService' ? 'Public Service' : bucket;
    revTooltipLines.push(`${label}: ${fmtUSDk(v)} (${pct}%)`);
  }
  if (revOther > 0) {
    const pct = totalRev > 0 ? Math.round((revOther / totalRev) * 100) : 0;
    revTooltipLines.push(`Unclassified: ${fmtUSDk(revOther)} (${pct}%)`);
  }
  const revTooltip = revTooltipLines.length
    ? 'Annual property tax by type (visible parcels)\n' + revTooltipLines.join('\n')
    : 'Annual property tax across visible parcels';

  const costTooltipLines = [];
  if (totalCostOM > 0) {
    const pct = totalCost > 0 ? Math.round((totalCostOM / totalCost) * 100) : 0;
    costTooltipLines.push(`Operating (O&M): ${fmtUSDk(totalCostOM)} (${pct}%)`);
  }
  if (totalCIP > 0) {
    const pct = totalCost > 0 ? Math.round((totalCIP / totalCost) * 100) : 0;
    costTooltipLines.push(`Capital (CIP, 6yr-avg): ${fmtUSDk(totalCIP)} (${pct}%)`);
  }
  const costTooltip = costTooltipLines.length
    ? 'Annual infrastructure cost (visible parcels)\n' + costTooltipLines.join('\n')
    : 'Annual cost across visible parcels';

  const top = [...features]
    .filter((f) => Number.isFinite(+f.properties?.net_per_ac))
    .sort((a, b) => +b.properties.net_per_ac - +a.properties.net_per_ac)
    .slice(0, 5);

  const totalsHTML = hasAcres ? `
    <div class="row"><span class="k">Acres</span><span class="v">${fmtInt.format(Math.round(totalAcres))}</span></div>
    <div class="row" data-tooltip="${escapeAttr(revTooltip)}"><span class="k">Total revenue</span><span class="v">${fmtUSDk(totalRev)}</span></div>
    <div class="row" data-tooltip="${escapeAttr(costTooltip)}"><span class="k">Total cost</span><span class="v">${fmtUSDk(totalCost)}</span></div>
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

// ============================================================
// Per-station narrative tour
// ============================================================

// Rebuilds the railOverlay's layer set: rail glow + stroke (static), the
// base station-dots layer (all 13, static styling), and an optional
// pulsing "active dot" layer for whichever station the tour is on. Called
// once on initial load and again every animation frame for the pulse —
// deck.gl diffs cheaply when only one ScatterplotLayer's props change.
function rebuildRailLayers() {
  if (!railPaths || !STATE.stations) return;

  const activeId = STATE.stationId ? +STATE.stationId : null;
  const activeStation = activeId !== null
    ? STATE.stations.features.find((f) => +getStationId(f) === activeId)
    : null;

  // Sine wave: radius oscillates 8 → 14px over ~1.5 sec (pulsePhase += 0.07 per frame).
  const activeRadius = 11 + 3 * Math.sin(pulsePhase);

  const layers = [
    new deck.PathLayer({
      id: 'rail-glow',
      data: railPaths,
      getPath: (d) => d,
      getColor: [255, 255, 255, 100],
      getWidth: 26,
      widthUnits: 'pixels',
      widthMinPixels: 14,
      capRounded: true,
      jointRounded: true,
      pickable: false,
    }),
    new deck.PathLayer({
      id: 'rail-stroke',
      data: railPaths,
      getPath: (d) => d,
      getColor: [0, 210, 240, 255],
      getWidth: 8,
      widthUnits: 'pixels',
      widthMinPixels: 4,
      capRounded: true,
      jointRounded: true,
      pickable: false,
    }),
    new deck.ScatterplotLayer({
      id: 'station-dots-base',
      data: STATE.stations.features,
      getPosition: (f) => f.geometry.coordinates,
      getRadius: 7,
      radiusUnits: 'pixels',
      getFillColor: [255, 255, 255, 255],
      getLineColor: [0, 210, 240, 255],
      lineWidthUnits: 'pixels',
      getLineWidth: 2,
      stroked: true,
      filled: true,
      pickable: false,
    }),
  ];

  if (activeStation) {
    layers.push(new deck.ScatterplotLayer({
      id: 'station-dots-active',
      data: [activeStation],
      getPosition: (f) => f.geometry.coordinates,
      getRadius: activeRadius,
      radiusUnits: 'pixels',
      getFillColor: [0, 210, 240, 180],     // cyan fill — pops above the white base dot
      getLineColor: [255, 255, 255, 255],
      lineWidthUnits: 'pixels',
      getLineWidth: 2.5,
      stroked: true,
      filled: true,
      pickable: false,
    }));
  }

  railOverlay.setProps({ layers });
}

// Drives the active station's pulse via requestAnimationFrame. deck.gl is
// already on its own render loop, so we're not creating an idle-CPU loop —
// just adjusting one layer's props each frame.
function startPulseLoop() {
  if (pulseRaf) return;
  function frame() {
    pulsePhase += 0.07;
    if (pulsePhase > Math.PI * 2) pulsePhase -= Math.PI * 2;
    rebuildRailLayers();
    pulseRaf = requestAnimationFrame(frame);
  }
  pulseRaf = requestAnimationFrame(frame);
}

// Strip the trailing " Station" suffix from the GeoJSON name so it matches
// the bare keys ("Kualakaʻi", "Hālawa", etc.) used in station_narratives.json.
function narrativeKeyFor(featureName) {
  return featureName.replace(/\s+Station$/i, '');
}

// Compute the same revenue/cost aggregates renderSummary uses, but returned
// as an object so the narrative panel can read the totals without parsing
// HTML. Single pass over STATE.filtered. Respects the Rail CIP toggle.
function computeFilteredTotals() {
  let totalRev = 0, totalCost = 0;
  const revByBucket = {};
  for (const b of Object.keys(LAND_USE_BUCKETS)) revByBucket[b] = 0;
  let revOther = 0;

  // Station-scope when "Show all parcels" is on so the narrative card's
  // stats stay tied to the selected station (see summaryScopedFeatures).
  const features = summaryScopedFeatures();
  for (const f of features) {
    const p = f.properties;
    const acres = +p.area_ac;
    if (!Number.isFinite(acres)) continue;
    const rev = Number.isFinite(+p.rev_per_ac) ? +p.rev_per_ac * acres : 0;
    const railAdj = STATE.railCipOn ? (+p.rail_cip_per_ac || 0) : 0;
    const cost = Number.isFinite(+p.cost_per_ac)
      ? (+p.cost_per_ac + railAdj) * acres
      : 0;
    totalRev += rev;
    totalCost += cost;
    const bucket = LAND_USE_TO_BUCKET[p.land_use];
    if (bucket) revByBucket[bucket] += rev;
    else revOther += rev;
  }

  return {
    parcels: features.length,
    totalRev,
    totalCost,
    totalNet: totalRev - totalCost,
    revByBucket,
    revOther,
  };
}

// Format the 4-stat mini-grid in the narrative panel. Dominant use is the
// land-use bucket contributing the largest share of revenue.
function buildStatsHTML(totals) {
  const buckets = Object.entries(totals.revByBucket)
    .filter(([, v]) => v > 0)
    .sort(([, a], [, b]) => b - a);
  let dominant = '—';
  if (buckets.length && totals.totalRev > 0) {
    const [bucket, value] = buckets[0];
    const pct = Math.round((value / totals.totalRev) * 100);
    const label = bucket === 'PublicService' ? 'Public Service' : bucket;
    dominant = `${label} · ${pct}%`;
  }
  return `
    <div class="narrative-stat"><span class="k">Parcels</span><span class="v">${fmtInt.format(totals.parcels)}</span></div>
    <div class="narrative-stat"><span class="k">Dominant use</span><span class="v">${escapeHTML(dominant)}</span></div>
    <div class="narrative-stat"><span class="k">Revenue / yr</span><span class="v">${fmtUSDk(totals.totalRev)}</span></div>
    <div class="narrative-stat"><span class="k">Cost / yr</span><span class="v">${fmtUSDk(totals.totalCost)}</span></div>
  `;
}

// Leave the first-load intro card and reveal the station narrative body.
// Called by: "Start the journey" button, any map click, the station-select
// change handler, and goToStation (station chip click). Idempotent — safe
// to call when already out of intro mode.
function exitIntroMode() {
  if (!STATE.introMode) return;
  STATE.introMode = false;
  const card = document.getElementById('map-narrative-card');
  if (card) {
    card.classList.remove('intro-mode');
    // If the user previously dismissed (X) and we just exited via a chip
    // click, ensure the card is visible too.
    card.hidden = false;
    localStorage.setItem('tod-narrative-dismissed', '0');
  }
  renderNarrative();
}

// Populate the floating map narrative card with the active station's
// authored framing and live computed totals. Also toggles .active on the
// map chip (which reveals the chip-attached Prev / Next buttons) and sets
// the chip buttons' disabled state for the tour endpoints.
function renderNarrative() {
  const card = document.getElementById('map-narrative-card');
  if (!card) return;
  // While the intro card is up we leave its DOM alone — the station
  // body is hidden anyway via the .intro-mode class.
  if (STATE.introMode) return;
  const sortedList = STATE.stationsByWest || [];
  if (!sortedList.length) return;

  const nameEl    = card.querySelector('.map-narrative-name');
  const counterEl = card.querySelector('.map-narrative-counter');
  const proseEl   = card.querySelector('.map-narrative-prose');
  const themeEl   = card.querySelector('.map-narrative-theme');
  const statsEl   = card.querySelector('.map-narrative-stats');
  const verdictEl = card.querySelector('.map-narrative-verdict');

  const activeId = STATE.stationId ? +STATE.stationId : null;
  const idx = activeId !== null
    ? sortedList.findIndex((f) => +getStationId(f) === activeId)
    : -1;

  // Helper: update the chip-attached Prev/Next buttons' disabled state. The
  // buttons live on the chip itself (one pair per station), but only the
  // active station's chip shows them via CSS — so we only need to update
  // the active chip's pair. All other chips' buttons stay enabled in the
  // DOM but are hidden.
  const updateChipButtons = () => {
    chipRefs.forEach((el, sid) => {
      const isActive = sid === activeId;
      el.classList.toggle('active', isActive);
      if (isActive) {
        const prev = el.querySelector('.chip-tour-prev');
        const next = el.querySelector('.chip-tour-next');
        if (prev) prev.disabled = idx <= 0;
        if (next) next.disabled = idx >= sortedList.length - 1;
      }
    });
  };

  // "All stations" mode — corridor-wide framing, no live numbers.
  if (idx === -1) {
    nameEl.textContent = 'All stations';
    nameEl.classList.add('muted');
    counterEl.textContent = `${sortedList.length} stations · full corridor`;
    proseEl.textContent = 'Showing the entire Skyline corridor. Pick a station from the dropdown to start the guided west-to-east tour.';
    themeEl.textContent = '';
    statsEl.innerHTML = '';
    verdictEl.textContent = '';
    verdictEl.className = 'map-narrative-verdict';
    chipRefs.forEach((el) => el.classList.remove('active'));
    return;
  }

  const station = sortedList[idx];
  const fullName = getStationName(station);
  const key = narrativeKeyFor(fullName);
  const narr = (STATE.narratives && STATE.narratives[key]) || {};

  nameEl.textContent = fullName;
  nameEl.classList.remove('muted');
  counterEl.textContent = `Station ${idx + 1} of ${sortedList.length}`;
  proseEl.textContent = narr.framing || '(No narrative written for this station yet — add one to data/station_narratives.json.)';
  themeEl.textContent = narr.theme || '';

  const totals = computeFilteredTotals();
  statsEl.innerHTML = buildStatsHTML(totals);

  if (totals.parcels === 0) {
    verdictEl.textContent = 'No parcels in current filter';
    verdictEl.className = 'map-narrative-verdict empty';
  } else if (totals.totalNet >= 0) {
    verdictEl.textContent = `Breaks even · +${fmtUSDk(totals.totalNet)} / yr`;
    verdictEl.className = 'map-narrative-verdict breaks-even';
  } else {
    verdictEl.textContent = `Net loss · ${fmtUSDk(totals.totalNet)} / yr`;
    verdictEl.className = 'map-narrative-verdict net-loss';
  }

  updateChipButtons();
}

// Jump the guided tour to a specific station feature. Updates the dropdown,
// filters, and summary via selectStation, then flies the camera at the
// tour's preferred angle. Used by both stepTour (Prev/Next) and the chip
// click handlers (jump to any station by clicking its label).
function goToStation(stationFeature) {
  if (!stationFeature) return;
  // Picking any station also implicitly dismisses the intro.
  exitIntroMode();
  const id = String(getStationId(stationFeature));
  // Clicking a chip always reopens the narrative card if the user had
  // previously dismissed it — the chip *is* the reopen affordance.
  const card = document.getElementById('map-narrative-card');
  if (card && card.hidden) {
    card.hidden = false;
    localStorage.setItem('tod-narrative-dismissed', '0');
  }
  // No-op fast-path: clicking the already-active chip just re-centers the
  // camera without redoing the filter pass (which is expensive on 19k parcels).
  if (id !== STATE.stationId) selectStation(id);
  map.flyTo({
    center: stationFeature.geometry.coordinates,
    zoom: 14.5,
    pitch: 55,
    bearing: 0,
    duration: 1400,
  });
}

// Step the guided tour by ±1 station along the west-east line order.
function stepTour(delta) {
  const sortedList = STATE.stationsByWest || [];
  if (!sortedList.length) return;
  const activeId = STATE.stationId ? +STATE.stationId : null;
  const currentIdx = activeId !== null
    ? sortedList.findIndex((f) => +getStationId(f) === activeId)
    : -1;
  // From "All stations" mode, Next opens at station 1; Prev does nothing.
  const nextIdx = currentIdx === -1
    ? (delta > 0 ? 0 : -1)
    : currentIdx + delta;
  if (nextIdx < 0 || nextIdx >= sortedList.length) return;
  goToStation(sortedList[nextIdx]);
}

// HTML attribute encoder — keeps newlines (\n) intact since the tooltip
// renders with CSS white-space: pre-line.
function escapeAttr(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

// Body-level tooltip for metric (Revenue / Cost / Net) buttons in the
// sidepanel. Pure-CSS ::after was clipped by #sidebar's overflow-y:auto.
const segTooltipEl = document.getElementById('seg-tooltip');
function wireSegTooltip() {
  const toggle = document.getElementById('mode-toggle');
  if (!toggle || !segTooltipEl) return;
  toggle.addEventListener('mouseover', (e) => {
    const btn = e.target.closest('.seg-btn[data-tooltip]');
    if (!btn) return;
    segTooltipEl.textContent = btn.dataset.tooltip;
    segTooltipEl.classList.add('is-visible');
    const r = btn.getBoundingClientRect();
    const tipW = segTooltipEl.offsetWidth  || 220;
    const tipH = segTooltipEl.offsetHeight || 60;
    // Prefer centered above the button; clamp to viewport edges.
    let x = r.left + r.width / 2 - tipW / 2;
    x = Math.max(8, Math.min(x, window.innerWidth - tipW - 8));
    let y = r.top - tipH - 10;
    if (y < 8) y = r.bottom + 10;
    segTooltipEl.style.left = x + 'px';
    segTooltipEl.style.top  = y + 'px';
  });
  toggle.addEventListener('mouseout', (e) => {
    const btn = e.target.closest('.seg-btn[data-tooltip]');
    if (!btn) return;
    if (e.relatedTarget && btn.contains(e.relatedTarget)) return;
    segTooltipEl.classList.remove('is-visible');
  });
}

// Body-level tooltip for the summary's "Total revenue" / "Total cost" rows.
// Pure-CSS pseudo-element approach was clipped by #sidebar's auto overflow
// (overflow-y:auto coerces overflow-x to auto per spec). The single fixed
// element lives outside the sidebar's stacking context so it escapes the
// clip. wireSummaryTooltips delegates one mouseover/mouseout pair to the
// summary container and reads data-tooltip on hover.
const summaryTooltipEl = document.getElementById('summary-tooltip');
let summaryTooltipsWired = false;
function wireSummaryTooltips() {
  if (summaryTooltipsWired) return;
  summaryTooltipsWired = true;
  const summary = document.getElementById('summary');
  summary.addEventListener('mouseover', (e) => {
    const row = e.target.closest('.row[data-tooltip]');
    if (!row || !summaryTooltipEl) return;
    summaryTooltipEl.textContent = row.dataset.tooltip || '';
    // Position to the right of the row, vertically centered, clamped to
    // viewport so the tooltip never falls off-screen.
    const r = row.getBoundingClientRect();
    summaryTooltipEl.classList.add('is-visible');
    // First make visible so we can measure; then position.
    const tipH = summaryTooltipEl.offsetHeight || 80;
    const tipW = summaryTooltipEl.offsetWidth  || 240;
    let x = r.right + 14;
    if (x + tipW > window.innerWidth - 8) x = Math.max(8, r.left - tipW - 14);
    let y = r.top + r.height / 2 - tipH / 2;
    y = Math.max(8, Math.min(y, window.innerHeight - tipH - 8));
    summaryTooltipEl.style.left = x + 'px';
    summaryTooltipEl.style.top  = y + 'px';
  });
  summary.addEventListener('mouseout', (e) => {
    const row = e.target.closest('.row[data-tooltip]');
    if (!row) return;
    // mouseout fires when entering child elements too; only hide when the
    // pointer truly left the row.
    if (e.relatedTarget && row.contains(e.relatedTarget)) return;
    if (summaryTooltipEl) summaryTooltipEl.classList.remove('is-visible');
  });
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
