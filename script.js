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
  extrude: false,
  stationId: '',
  parcels: null,        // raw FeatureCollection
  stations: null,
  filtered: [],         // currently visible parcel features
  domain: [0, 1],       // [min, max] of current metric across visible parcels
};

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

const map = new maplibregl.Map({
  container: 'map',
  style: {
    version: 8,
    sources: {
      'carto-positron': {
        type: 'raster',
        tiles: [
          'https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
          'https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
          'https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}@2x.png',
        ],
        tileSize: 256,
        attribution: '© OpenStreetMap contributors © CARTO',
      },
    },
    layers: [{ id: 'carto-positron', type: 'raster', source: 'carto-positron' }],
  },
  center: [-157.95, 21.38],
  zoom: 11,
  pitch: 0,
  bearing: 0,
});

map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), 'top-right');
map.addControl(new maplibregl.ScaleControl({ unit: 'imperial' }), 'bottom-right');

map.on('load', async () => {
  try {
    const [parcels, stations] = await Promise.all([
      fetchJSON('data/parcels_tod.geojson'),
      fetchJSON('data/stations.geojson'),
    ]);
    STATE.parcels = parcels;
    STATE.stations = stations;

    populateStationDropdown(stations);
    addLayers();
    wireUI();
    refresh();
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

  // Parcel fill (2D).
  map.addLayer({
    id: 'parcels-fill',
    type: 'fill',
    source: 'parcels',
    paint: {
      'fill-color': '#cccccc',
      'fill-opacity': 0.75,
      'fill-outline-color': 'rgba(0,0,0,0.25)',
    },
  });

  // Parcel extrusion (3D) — hidden by default.
  map.addLayer({
    id: 'parcels-extrude',
    type: 'fill-extrusion',
    source: 'parcels',
    layout: { visibility: 'none' },
    paint: {
      'fill-extrusion-color': '#cccccc',
      'fill-extrusion-opacity': 0.85,
      'fill-extrusion-height': 0,
      'fill-extrusion-base': 0,
    },
  });

  // Station points.
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

  map.addLayer({
    id: 'stations-label',
    type: 'symbol',
    source: 'stations',
    layout: {
      'text-field': ['coalesce', ['get', 'name'], ['get', 'station_name'], ''],
      'text-size': 11,
      'text-offset': [0, 1.1],
      'text-anchor': 'top',
      'text-allow-overlap': false,
    },
    paint: {
      'text-color': '#0c4a6e',
      'text-halo-color': '#fff',
      'text-halo-width': 1.5,
    },
  });

  bindHoverPopup();
}

function bindHoverPopup() {
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false });

  const show = (e) => {
    if (!e.features?.length) return;
    map.getCanvas().style.cursor = 'pointer';
    const p = e.features[0].properties;
    const html = `
      <div class="pp-title">${escapeHTML(p.tmk ?? p.parcel_id ?? 'Parcel')}</div>
      <div class="pp-row"><span class="k">Revenue / ac</span><span>${fmtUSDk(+p.rev_per_ac)}</span></div>
      <div class="pp-row"><span class="k">Cost / ac</span><span>${fmtUSDk(+p.cost_per_ac)}</span></div>
      <div class="pp-row"><span class="k">Net / ac</span><span>${fmtUSDk(+p.net_per_ac)}</span></div>
      ${p.area_ac ? `<div class="pp-row"><span class="k">Acres</span><span>${(+p.area_ac).toFixed(2)}</span></div>` : ''}
    `;
    popup.setLngLat(e.lngLat).setHTML(html).addTo(map);
  };
  const hide = () => {
    map.getCanvas().style.cursor = '';
    popup.remove();
  };

  for (const id of ['parcels-fill', 'parcels-extrude']) {
    map.on('mousemove', id, show);
    map.on('mouseleave', id, hide);
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
    map.setLayoutProperty('parcels-fill', 'visibility', STATE.extrude ? 'none' : 'visible');
    map.setLayoutProperty('parcels-extrude', 'visibility', STATE.extrude ? 'visible' : 'none');
    if (STATE.extrude && map.getPitch() < 30) {
      map.easeTo({ pitch: 50, duration: 600 });
    }
    refresh();
  });
}

function selectStation(id) {
  STATE.stationId = id || '';
  const sel = document.getElementById('station-select');
  if (sel.value !== STATE.stationId) sel.value = STATE.stationId;

  const filter = STATE.stationId
    ? ['==', ['to-string', ['get', 'station_id']], STATE.stationId]
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
  const matching = (STATE.parcels?.features || []).filter(
    (f) => String(f.properties?.station_id) === STATE.stationId
  );
  const b = bboxOf(matching);
  if (b) map.fitBounds(b, { padding: 60, duration: 600, maxZoom: 16 });
}

function refresh() {
  const key = METRIC_KEYS[STATE.mode];
  STATE.filtered = (STATE.parcels?.features || []).filter((f) => {
    if (STATE.stationId && String(f.properties?.station_id) !== STATE.stationId) return false;
    return Number.isFinite(+f.properties?.[key]);
  });

  STATE.domain = computeDomain(STATE.filtered, key, STATE.mode === 'net');

  applyPaint();
  renderLegend();
  renderSummary();
}

function computeDomain(features, key, symmetric) {
  if (!features.length) return symmetric ? [-1, 1] : [0, 1];
  let min = Infinity, max = -Infinity;
  for (const f of features) {
    const v = +f.properties[key];
    if (v < min) min = v;
    if (v > max) max = v;
  }
  if (symmetric) {
    const m = Math.max(Math.abs(min), Math.abs(max)) || 1;
    return [-m, m];
  }
  if (min === max) max = min + 1;
  return [min, max];
}

function applyPaint() {
  const key = METRIC_KEYS[STATE.mode];
  const [lo, hi] = STATE.domain;
  const palette = STATE.mode === 'net' ? DIVERGING_RWG : VIRIDIS;
  const colorExpr = ['interpolate', ['linear'], ['to-number', ['get', key]],
    ...rampStops(lo, hi, palette)];

  if (STATE.extrude) {
    map.setPaintProperty('parcels-extrude', 'fill-extrusion-color', colorExpr);
    map.setPaintProperty('parcels-extrude', 'fill-extrusion-height', [
      '/', ['abs', ['to-number', ['get', key]]], 10,
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
  legend.innerHTML = `
    <div class="legend-title muted" style="font-size:11px;text-transform:uppercase;letter-spacing:0.04em;">${METRIC_LABELS[STATE.mode]}</div>
    <div class="legend-bar" style="background:${gradient};"></div>
    <div class="legend-labels"><span>${fmtUSDk(lo)}</span>${STATE.mode === 'net' ? '<span>0</span>' : ''}<span>${fmtUSDk(hi)}</span></div>
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
    <div class="row"><span class="k">Net</span><span class="v" style="color:${totalNet >= 0 ? '#15803d' : '#b91c1c'}">${fmtUSDk(totalNet)}</span></div>
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
