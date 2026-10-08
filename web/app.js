'use strict';

const CIREN_URL = 'https://esri.ciren.cl/server/rest/services/USOS_DE_LA_TIERRA__CONAF/MapServer';
const PREDIOS_URL = 'https://esri.ciren.cl/server/rest/services/IDEMINAGRI/PROPIEDADES_RURALES/MapServer';
const WAYBACK_CONFIG_URL = 'https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json';
const SIN_DATOS = 'Sin datos por el momento';
const ZOOM_MEJORAR = 14;
const POLL_MS = 5000;
const PAGINA = 50;
const VACIO = { type: 'FeatureCollection', features: [] };

const ESTADOS = {
  alerta: { etq: 'Alerta', color: '#c0392b' },
  en_regla: { etq: 'En regla', color: '#0b6b3a' },
  revisar: { etq: 'Revisar', color: '#b7791f' },
  sin_datos: { etq: 'Sin datos', color: '#b7791f' },
  sin_cambio: { etq: 'Sin cambio', color: '#5d6b62' },
  descartado_no_forestal: { etq: 'No forestal', color: '#5d6b62' },
};

const $ = (id) => document.getElementById(id);

const estado = {
  regiones: {},
  region: null,
  consulta: null,       // {region, t0, t1, cloud} de la última detección
  resultados: [],       // polígonos con cambio (orden del backend: delta más negativo primero)
  mostrados: 0,
  sel: null,
  deteccion: 0,         // id de la detección en curso (cancela el polling anterior)
  ndviModo: '',
  sentinel: {},         // clave region|t0|t1 -> respuesta del backend
  wayback: { config: null, capturas: [], a: 0, b: 1, viendo: 'a', comparar: false },
  fichaPedido: 0,
};

/* ---------- utilidades ---------- */

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fechaCL(v) {
  if (!v) return '—';
  const m = String(v).match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[3]}-${m[2]}-${m[1]}` : esc(v);
}

function num(v, dec = 2) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(String(v).replace(',', '.'));
  return Number.isFinite(n) ? n.toLocaleString('es-CL', { maximumFractionDigits: dec }) : esc(v);
}

function ha(v) {
  const t = num(v);
  return t === '—' ? t : `${t} ha`;
}

// 'PANGUIPULLI' -> 'Panguipulli'
function titulo(v) {
  return String(v ?? '').toLowerCase().replace(/(^|[\s-])(\p{L})/gu, (m, a, b) => a + b.toUpperCase());
}

function infoEstado(e) {
  return ESTADOS[e] || { etq: e || 'Sin datos', color: '#5d6b62' };
}

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

/* ---------- mapa ---------- */

// Bing no usa {z}/{x}/{y} sino quadkey: el protocolo bing://z/x/y lo convierte.
function quadkey(z, x, y) {
  let q = '';
  for (let i = z; i > 0; i -= 1) {
    const m = 1 << (i - 1);
    q += String((x & m ? 1 : 0) + (y & m ? 2 : 0));
  }
  return q;
}

maplibregl.addProtocol('bing', async (params, abort) => {
  const [z, x, y] = params.url.replace('bing://', '').split('/').map(Number);
  const resp = await fetch(`https://ecn.t${(x + y) % 4}.tiles.virtualearth.net/tiles/a${quadkey(z, x, y)}.jpeg?g=14000`, { signal: abort.signal });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return { data: await resp.arrayBuffer() };
});

const COLOR_ESTADO = ['match', ['get', 'estado'],
  'alerta', '#c0392b', 'en_regla', '#0b6b3a', 'revisar', '#d4a017', 'sin_datos', '#d4a017', '#5d6b62'];

// Vista compartible: #zoom/lat/lon (hash de MapLibre) y ?base=sat|bing|google|osm.
let vistaEnUrl = /^#\d/.test(window.location.hash);

const map = new maplibregl.Map({
  container: 'map',
  attributionControl: false,
  center: [-72.2, -39.64],
  zoom: 9,
  maxZoom: 20,
  hash: true,
  style: {
    version: 8,
    sources: {
      sat: {
        type: 'raster', tileSize: 256, maxzoom: 19,
        tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
        attribution: 'Imagen © Esri, Maxar, Earthstar Geographics',
      },
      bing: {
        type: 'raster', tileSize: 256, maxzoom: 20,
        tiles: ['bing://{z}/{x}/{y}'],
        attribution: 'Imagen © Microsoft (Bing Maps)',
      },
      google: {
        type: 'raster', tileSize: 256, maxzoom: 20,
        tiles: [0, 1, 2, 3].map((n) => `https://mt${n}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}`),
        attribution: 'Imágenes © Google',
      },
      osm: {
        type: 'raster', tileSize: 256, maxzoom: 19,
        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
        attribution: '© OpenStreetMap',
      },
      rotulos: {
        type: 'raster', tileSize: 256, maxzoom: 19,
        tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}'],
      },
      cambios: { type: 'geojson', data: VACIO },
      rodales: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
      predio: { type: 'geojson', data: VACIO },
      referencias: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
    },
    layers: [
      { id: 'base-sat', type: 'raster', source: 'sat', layout: { visibility: 'none' } },
      { id: 'base-bing', type: 'raster', source: 'bing', layout: { visibility: 'none' } },
      { id: 'base-google', type: 'raster', source: 'google' },
      { id: 'base-osm', type: 'raster', source: 'osm', layout: { visibility: 'none' } },
      // Wayback se inserta aquí (antes de 'tope-base'); NDVI y CIREN antes de 'tope-capas'.
      { id: 'tope-base', type: 'background', paint: { 'background-opacity': 0 } },
      { id: 'tope-capas', type: 'background', paint: { 'background-opacity': 0 } },
      { id: 'rotulos', type: 'raster', source: 'rotulos' },
      { id: 'cambios-relleno', type: 'fill', source: 'cambios', paint: { 'fill-color': COLOR_ESTADO, 'fill-opacity': 0.35 } },
      { id: 'cambios-borde', type: 'line', source: 'cambios', paint: { 'line-color': COLOR_ESTADO, 'line-width': ['interpolate', ['linear'], ['zoom'], 9, 1, 15, 2.5] } },
      {
        id: 'cambios-sel', type: 'line', source: 'cambios', filter: ['==', ['get', 'i'], -1],
        paint: { 'line-color': '#ffffff', 'line-width': 4, 'line-dasharray': [2, 1] },
      },
      { id: 'predio-halo', type: 'line', source: 'predio', paint: { 'line-color': '#000000', 'line-width': 5, 'line-opacity': 0.45 } },
      { id: 'predio-borde', type: 'line', source: 'predio', paint: { 'line-color': '#ffdd00', 'line-width': 2.5 } },
      { id: 'rodales-relleno', type: 'fill', source: 'rodales', paint: { 'fill-color': '#7fdc9a', 'fill-opacity': 0.15 } },
      { id: 'rodales-borde', type: 'line', source: 'rodales', paint: { 'line-color': '#7fdc9a', 'line-width': 2 } },
      {
        id: 'referencias', type: 'circle', source: 'referencias',
        paint: { 'circle-radius': 5, 'circle-color': '#ffffff', 'circle-stroke-color': '#0b6b3a', 'circle-stroke-width': 2 },
      },
    ],
  },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-right');
map.addControl(new maplibregl.AttributionControl({ compact: true }), 'bottom-right');

function compactarAtribucion() {
  document.querySelectorAll('.maplibregl-ctrl-attrib').forEach((el) => el.classList.remove('maplibregl-compact-show'));
}
map.on('load', compactarAtribucion);
map.on('styledata', compactarAtribucion);

const mapaListo = new Promise((ok) => map.on('load', ok));

/* ---------- imagen base ---------- */

document.querySelectorAll('[data-base]').forEach((btn) => btn.addEventListener('click', () => {
  const base = btn.dataset.base;
  document.querySelectorAll('[data-base]').forEach((b) => {
    b.classList.toggle('activo', b === btn);
    b.setAttribute('aria-pressed', String(b === btn));
  });
  ['sat', 'bing', 'google', 'osm'].forEach((b) => map.setLayoutProperty(`base-${b}`, 'visibility', base === b ? 'visible' : 'none'));
  map.setLayoutProperty('rotulos', 'visibility', base === 'osm' ? 'none' : 'visible');
}));

/* ---------- CIREN ---------- */

function cirenOpacidad() {
  return Number($('ciren-opacidad').value) / 100;
}

function ponerCapaCiren(layerId) {
  if (map.getLayer('ciren')) map.removeLayer('ciren');
  if (map.getSource('ciren')) map.removeSource('ciren');
  map.addSource('ciren', {
    type: 'raster', tileSize: 256,
    tiles: [`${CIREN_URL}/export?bbox={bbox-epsg-3857}&bboxSR=3857&imageSR=3857&size=256,256&format=png32&transparent=true&layers=show:${layerId}&f=image`],
    attribution: 'Uso de suelo © CIREN / CONAF',
  });
  map.addLayer({
    id: 'ciren', type: 'raster', source: 'ciren',
    layout: { visibility: $('ciren-on').checked ? 'visible' : 'none' },
    paint: { 'raster-opacity': cirenOpacidad() },
  }, map.getLayer('predios') ? 'predios' : 'tope-capas');
}

$('ciren-on').addEventListener('change', () => {
  if (map.getLayer('ciren')) map.setLayoutProperty('ciren', 'visibility', $('ciren-on').checked ? 'visible' : 'none');
});
$('ciren-opacidad').addEventListener('input', () => {
  $('ciren-opacidad-val').textContent = `${$('ciren-opacidad').value} %`;
  if (map.getLayer('ciren')) map.setPaintProperty('ciren', 'raster-opacity', cirenOpacidad());
});

/* ---------- predios rurales CIREN ---------- */

// La simbología por defecto (borde rojo fino) casi no se ve sobre el satélite:
// se redibuja la capa con contorno amarillo y sin relleno (dynamicLayers). Sin
// rótulos: el servidor los repetiría en cada tesela; el rol sale al hacer clic.
function dynamicLayersPredios(layerId) {
  return JSON.stringify([{
    id: layerId,
    source: { type: 'mapLayer', mapLayerId: layerId },
    drawingInfo: {
      showLabels: false,
      renderer: {
        type: 'simple',
        symbol: { type: 'esriSFS', style: 'esriSFSNull', outline: { type: 'esriSLS', style: 'esriSLSSolid', color: [255, 221, 0, 255], width: 1.5 } },
      },
    },
  }]);
}

function ponerCapaPredios(layerId) {
  if (map.getLayer('predios')) map.removeLayer('predios');
  if (map.getSource('predios')) map.removeSource('predios');
  map.addSource('predios', {
    type: 'raster', tileSize: 256,
    tiles: [`${PREDIOS_URL}/export?bbox={bbox-epsg-3857}&bboxSR=3857&imageSR=3857&size=256,256&format=png32&transparent=true&dynamicLayers=${encodeURIComponent(dynamicLayersPredios(layerId))}&f=image`],
    attribution: 'Predios rurales © CIREN (IDE Minagri)',
  });
  map.addLayer({
    id: 'predios', type: 'raster', source: 'predios',
    layout: { visibility: $('predios-on').checked ? 'visible' : 'none' },
    paint: { 'raster-opacity': Number($('predios-opacidad').value) / 100 },
  }, 'tope-capas');
}

$('predios-on').addEventListener('change', () => {
  if (map.getLayer('predios')) map.setLayoutProperty('predios', 'visibility', $('predios-on').checked ? 'visible' : 'none');
  if (!$('predios-on').checked && estado.sel === null) dibujarPredio(null);
});
$('predios-opacidad').addEventListener('input', () => {
  $('predios-opacidad-val').textContent = `${$('predios-opacidad').value} %`;
  if (map.getLayer('predios')) map.setPaintProperty('predios', 'raster-opacity', Number($('predios-opacidad').value) / 100);
});

function dibujarPredio(geometria) {
  const fc = geometria ? { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: geometria, properties: {} }] } : VACIO;
  if (map.getSource('predio')) map.getSource('predio').setData(fc);
}

function textoPredio(p, anio) {
  const fuente = `CIREN ${p.capa?.anio || anio || ''}`.trim();
  return `Predio rol ${p.rol || 'sin rol'} · ${titulo(p.comuna) || '—'} (${fuente})`;
}

async function htmlPredioPunto(lat, lng) {
  let d;
  try {
    d = await getJSON(`api/v1/predio/punto?region=${encodeURIComponent(estado.region)}&lat=${lat.toFixed(6)}&lon=${lng.toFixed(6)}`);
  } catch (err) {
    return `<span class="muted">Predios CIREN: ${SIN_DATOS}</span>`;
  }
  if (d.estado === 'sin_predio') { dibujarPredio(null); return '<span class="muted">Sin predio CIREN en este punto</span>'; }
  if (d.estado !== 'ok') return `<span class="muted">Predios CIREN: ${SIN_DATOS}</span>`;
  dibujarPredio(d.geometria);
  let planes;
  if (d.saff?.estado !== 'ok') planes = `Planes SAFF: ${SIN_DATOS}`;
  else if (!d.rol_norm) planes = 'Rol no numérico: no se cruza con SAFF';
  else if (!d.planes_saff) planes = 'Sin planes SAFF para este rol';
  else {
    const lista = d.planes.slice(0, 5).map((p) => `${esc(p.nro_solicitud || 's/n')}${p.aprobado ? ' (aprobado)' : ''}`).join(', ');
    planes = `<b>${d.planes_saff}</b> plan(es) SAFF para este rol: ${lista}${d.planes.length > 5 ? '…' : ''}`;
  }
  return `<b>Rol ${esc(d.rol || '—')}</b> · ${esc(titulo(d.comuna) || '—')}
    <br>${planes}
    <br><span class="muted chico">Predios rurales CIREN ${esc(d.capa?.anio || '')} · comuna SUBDERE ${esc(d.comudere || '—')}</span>`;
}

let leyendaCiren = null;
async function cargarLeyendaCiren(layerId) {
  const cont = $('ciren-leyenda-items');
  try {
    leyendaCiren = leyendaCiren || await getJSON(`${CIREN_URL}/legend?f=json`);
    const capa = (leyendaCiren.layers || []).find((l) => l.layerId === layerId);
    if (!capa || !capa.legend?.length) throw new Error('sin leyenda');
    cont.innerHTML = capa.legend.map((it) => `<div><img alt="" src="data:${esc(it.contentType)};base64,${esc(it.imageData)}">${esc(it.label)}</div>`).join('');
  } catch (err) {
    cont.innerHTML = `<span class="muted">${SIN_DATOS}</span>`;
  }
}

async function htmlCirenPunto(lat, lng) {
  try {
    const d = await getJSON(`api/v1/ciren/punto?region=${encodeURIComponent(estado.region)}&lat=${lat.toFixed(6)}&lon=${lng.toFixed(6)}`);
    if (d.estado === 'ok') {
      return `<b>${esc(d.uso)}</b><br>${esc(d.subuso || '')}${d.uso_tierra ? `<br><span class="muted">${esc(d.uso_tierra)}</span>` : ''}
        <br>Superficie polígono: ${ha(d.superf_ha)}<br>Comuna: ${esc(d.nom_com || '—')}
        <br><span class="muted chico">CIREN capa ${esc(d.capa?.id)}${d.capa?.anio ? ` · CONAF ${esc(d.capa.anio)}` : ''}</span>`;
    }
    if (d.estado === 'sin_poligono') return `<span class="muted">${esc(d.mensaje)}</span>`;
  } catch (err) { /* cae al mensaje sin datos */ }
  return `<span class="muted">CIREN: ${SIN_DATOS}</span>`;
}

let ignorarClickHasta = 0;
map.on('click', async (ev) => {
  const conCiren = $('ciren-on').checked;
  const conPredios = $('predios-on').checked;
  if (Date.now() < ignorarClickHasta || (!conCiren && !conPredios) || !estado.region) return;
  if (map.queryRenderedFeatures(ev.point, { layers: ['referencias', 'cambios-relleno'] }).length) return;
  const { lng, lat } = ev.lngLat;
  const popup = new maplibregl.Popup({ maxWidth: '300px' }).setLngLat(ev.lngLat).setHTML('<span class="muted">Consultando CIREN…</span>').addTo(map);
  const partes = await Promise.all([
    conPredios ? htmlPredioPunto(lat, lng) : '',
    conCiren ? htmlCirenPunto(lat, lng) : '',
  ]);
  popup.setHTML(partes.filter(Boolean).join('<hr class="sep">'));
});

/* ---------- NDVI Sentinel-2 ---------- */

function claveSentinel() {
  const c = consultaActual();
  return `${c.region}|${c.t0}|${c.t1}`;
}

function consultaActual() {
  return { region: $('region').value, t0: $('t0').value, t1: $('t1').value, cloud: $('cloud').value };
}

function quitarNdvi() {
  ['t0', 't1'].forEach((t) => {
    if (map.getLayer(`ndvi-${t}`)) map.removeLayer(`ndvi-${t}`);
    if (map.getSource(`ndvi-${t}`)) map.removeSource(`ndvi-${t}`);
  });
}

function marcarNdvi(modo) {
  document.querySelectorAll('[data-ndvi]').forEach((b) => {
    b.classList.toggle('activo', b.dataset.ndvi === modo);
    b.setAttribute('aria-pressed', String(b.dataset.ndvi === modo));
  });
}

async function aplicarNdvi(modo) {
  estado.ndviModo = modo;
  marcarNdvi(modo);
  quitarNdvi();
  $('ndvi-leyenda').hidden = !modo;
  if (!modo) { $('ndvi-estado').textContent = ''; return; }
  const c = consultaActual();
  if (!c.t0 || !c.t1 || c.t0 >= c.t1) {
    $('ndvi-estado').textContent = 'Fechas t0/t1 inválidas.';
    return;
  }
  const clave = claveSentinel();
  $('ndvi-estado').textContent = 'Cargando teselas NDVI…';
  let data = estado.sentinel[clave];
  if (!data) {
    try {
      data = await getJSON(`api/v1/sentinel/tiles?region=${encodeURIComponent(c.region)}&t0=${c.t0}&t1=${c.t1}`);
      estado.sentinel[clave] = data;
    } catch (err) {
      data = null;
    }
  }
  if (estado.ndviModo !== modo || claveSentinel() !== clave) return;
  const capa = data && data[modo];
  if (!capa || capa.estado !== 'ok') {
    $('ndvi-estado').textContent = (capa && capa.mensaje) || `Sentinel-2: ${SIN_DATOS}`;
    $('ndvi-leyenda').hidden = true;
    return;
  }
  map.addSource(`ndvi-${modo}`, { type: 'raster', tileSize: 256, tiles: [capa.tile_url], attribution: 'Copernicus Sentinel-2 vía Google Earth Engine' });
  map.addLayer({
    id: `ndvi-${modo}`, type: 'raster', source: `ndvi-${modo}`,
    paint: { 'raster-opacity': Number($('ndvi-opacidad').value) / 100 },
  }, map.getLayer('ciren') ? 'ciren' : 'tope-capas');
  const v = capa.ventana || {};
  $('ndvi-estado').textContent = `${modo}: ventana ${fechaCL(v.inicio)} a ${fechaCL(v.fin)} · ${capa.images} imagen(es)`;
}

document.querySelectorAll('[data-ndvi]').forEach((b) => b.addEventListener('click', () => aplicarNdvi(b.dataset.ndvi)));
$('ndvi-opacidad').addEventListener('input', () => {
  $('ndvi-opacidad-val').textContent = `${$('ndvi-opacidad').value} %`;
  ['t0', 't1'].forEach((t) => {
    if (map.getLayer(`ndvi-${t}`)) map.setPaintProperty(`ndvi-${t}`, 'raster-opacity', Number($('ndvi-opacidad').value) / 100);
  });
});

/* ---------- regiones ---------- */

async function cargarRegiones() {
  const sel = $('region');
  try {
    const data = await getJSON('api/v1/regiones');
    data.items.forEach((r) => { estado.regiones[r.id] = r; });
    sel.innerHTML = data.items.map((r) => `<option value="${esc(r.id)}">${esc(r.nombre)}</option>`).join('');
  } catch (err) {
    sel.innerHTML = `<option value="">${SIN_DATOS}</option>`;
    return;
  }
  await mapaListo;
  cambiarRegion();
}

function cambiarRegion() {
  const r = estado.regiones[$('region').value];
  if (!r) return;
  estado.region = r.id;
  $('ciren-anio').textContent = r.ciren_anio ? String(r.ciren_anio) : '';
  ponerCapaCiren(r.ciren_layer);
  cargarLeyendaCiren(r.ciren_layer);
  $('predios-anio').textContent = r.predios_anio ? `(CIREN ${r.predios_anio})` : '(CIREN)';
  ponerCapaPredios(r.predios_layer);
  const [w, s, e, n] = r.bbox;
  if (!vistaEnUrl) map.fitBounds([[w, s], [e, n]], { padding: 20, animate: false });
  vistaEnUrl = false;
  if (estado.ndviModo) aplicarNdvi(estado.ndviModo);
}

$('region').addEventListener('change', cambiarRegion);
['t0', 't1'].forEach((id) => $(id).addEventListener('change', () => { if (estado.ndviModo) aplicarNdvi(estado.ndviModo); }));

/* ---------- detección y lista de polígonos ---------- */

function estadoDe(r) {
  return r.estado || 'sin_datos';
}

function f3(v) {
  return v === null || v === undefined ? '—' : Number(v).toLocaleString('es-CL', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
}

function geojsonCambios() {
  return {
    type: 'FeatureCollection',
    features: estado.resultados.map((r, i) => ({ type: 'Feature', geometry: r.geometry, properties: { i, id: r.id, estado: estadoDe(r) } })),
  };
}

function pintarCambiosMapa() {
  if (map.getSource('cambios')) map.getSource('cambios').setData(geojsonCambios());
}

function rolTarjeta(r) {
  const p = r.predio || {};
  if (p.estado === 'ok') return `Rol ${esc(p.rol || 'sin rol')}`;
  if (p.estado === 'sin_predio') return 'Sin predio CIREN';
  return 'Rol: sin datos';
}

function tarjetaPoligono(r, i) {
  const e = estadoDe(r);
  const inf = infoEstado(e);
  const el = document.createElement('button');
  el.type = 'button';
  el.className = `punto${estado.sel === i ? ' sel' : ''}`;
  el.dataset.i = i;
  el.innerHTML = `
    <span class="n" style="background:${inf.color}">${i + 1}</span>
    <span class="fila"><span class="chip ${esc(e)}">${esc(inf.etq)}</span><b>${esc(r.subuso || r.uso || 'Bosque')}</b><span class="muted">${ha(r.superf_ha)}</span><span class="coords">${rolTarjeta(r)}</span><span class="coords">CIREN ${esc(r.id)}</span></span>
    <span class="fila ndvi">NDVI t0 <b>${f3(r.ndvi_t0)}</b> · t1 <b>${f3(r.ndvi_t1)}</b> · Δ <b style="color:#c0392b">${f3(r.delta)}</b></span>
    <span class="fila motivo">${esc(r.motivo || '')}</span>`;
  el.addEventListener('click', () => seleccionar(i));
  return el;
}

function pintarLista() {
  const lista = $('lista');
  lista.innerHTML = '';
  estado.resultados.slice(0, estado.mostrados).forEach((r, i) => lista.appendChild(tarjetaPoligono(r, i)));
  const resto = estado.resultados.length - estado.mostrados;
  if (resto > 0) {
    const mas = document.createElement('button');
    mas.type = 'button';
    mas.className = 'btn-secundario ver-mas';
    mas.textContent = `Ver más (${resto.toLocaleString('es-CL')} restantes)`;
    mas.addEventListener('click', () => { estado.mostrados += PAGINA; pintarLista(); });
    lista.appendChild(mas);
  }
}

function actualizarConteos() {
  const contar = (e) => estado.resultados.filter((r) => estadoDe(r) === e).length;
  $('num-cambio').textContent = estado.resultados.length.toLocaleString('es-CL');
  $('num-alertas').textContent = contar('alerta').toLocaleString('es-CL');
  $('num-revisar').textContent = contar('revisar').toLocaleString('es-CL');
  $('num-en-regla').textContent = contar('en_regla').toLocaleString('es-CL');
  $('num-sin-datos').textContent = contar('sin_datos').toLocaleString('es-CL');
}

function textoResumen(d) {
  const s = d.resumen;
  const partes = [`Se evaluaron ${s.poligonos_evaluados.toLocaleString('es-CL')} polígonos de bosque CIREN (${num(s.ha_evaluadas, 0)} ha) de ${num(d.min_ha)} ha o más.`];
  if (d.ventanas) {
    partes.push(`NDVI Sentinel-2: t0 ${fechaCL(d.ventanas.t0.inicio)} a ${fechaCL(d.ventanas.t0.fin)}, t1 ${fechaCL(d.ventanas.t1.inicio)} a ${fechaCL(d.ventanas.t1.fin)}.`);
  }
  if (s.con_cambio) partes.push(`${s.con_cambio.toLocaleString('es-CL')} con caída de NDVI ≥ 0,15 (${num(s.ha_con_cambio, 0)} ha).`);
  if (s.sin_ndvi) partes.push(`${s.sin_ndvi.toLocaleString('es-CL')} sin NDVI (nubes, sin imagen o sin respuesta de GEE).`);
  if (d.gee?.estado === 'parcial') partes.push(`per_api_gee no respondió en ${d.gee.lotes_fallidos} de ${d.gee.lotes} lotes.`);
  if (d.saff?.estado !== 'ok') partes.push(`SAFF: ${SIN_DATOS} (se está cargando); los cambios quedan «sin datos».`);
  if (d.predios?.estado !== 'ok') partes.push(`Predios CIREN: ${SIN_DATOS} (se están cargando); sin rol los cambios quedan «sin datos».`);
  return partes.join(' ');
}

const esperar = (ms) => new Promise((ok) => setTimeout(ok, ms));

function limpiarResultados() {
  estado.resultados = [];
  estado.mostrados = 0;
  pintarCambiosMapa();
}

async function detectar(ev) {
  ev.preventDefault();
  const c = consultaActual();
  const lista = $('lista');
  if (!c.t0 || !c.t1 || c.t0 >= c.t1) {
    lista.innerHTML = '<p class="msg">La fecha t0 debe ser anterior a t1.</p>';
    return;
  }
  const id = ++estado.deteccion;
  $('btn-detectar').disabled = true;
  $('resumen').hidden = true;
  lista.innerHTML = '<p class="msg">Calculando NDVI por polígono y cruzando fuentes…</p>';
  deseleccionar();
  limpiarResultados();
  const url = `api/v1/deteccion?region=${encodeURIComponent(c.region)}&t0=${c.t0}&t1=${c.t1}&cloud=${c.cloud}`;
  try {
    let data;
    for (;;) {
      const resp = await fetch(url);
      data = await resp.json().catch(() => ({}));
      if (id !== estado.deteccion) return;
      if (!resp.ok) {
        lista.innerHTML = `<p class="msg">No se pudo calcular: ${esc(data.detail || `HTTP ${resp.status}`)}</p>`;
        return;
      }
      if (data.estado !== 'calculando') break;
      lista.innerHTML = `<p class="msg">${esc(data.fase)}… <b>${esc(data.avance)}</b> · ${data.segundos ?? 0} s</p>`;
      await esperar(POLL_MS);
      if (id !== estado.deteccion) return;
    }
    estado.consulta = c;
    if (!data.resumen) {
      lista.innerHTML = `<p class="msg">${esc(data.mensaje || SIN_DATOS)}</p>`;
      return;
    }
    estado.resultados = data.poligonos || [];
    estado.mostrados = PAGINA;
    $('resumen-texto').textContent = textoResumen(data);
    actualizarConteos();
    $('resumen').hidden = false;
    pintarCambiosMapa();
    if (data.estado === 'sin_datos') {
      lista.innerHTML = `<p class="msg">${esc(data.mensaje || SIN_DATOS)}</p>`;
      return;
    }
    if (!estado.resultados.length) {
      lista.innerHTML = '<p class="msg">Ningún polígono de bosque con caída de NDVI en este rango.</p>';
      return;
    }
    pintarLista();
  } catch (err) {
    if (id === estado.deteccion) lista.innerHTML = `<p class="msg">${SIN_DATOS} (error de conexión).</p>`;
  } finally {
    if (id === estado.deteccion) $('btn-detectar').disabled = false;
  }
}

$('form-controles').addEventListener('submit', detectar);

map.on('click', 'cambios-relleno', (ev) => {
  ignorarClickHasta = Date.now() + 400;
  const i = ev.features[0].properties.i;
  if (Number.isInteger(i)) seleccionar(i);
});
map.on('mouseenter', 'cambios-relleno', () => { map.getCanvas().style.cursor = 'pointer'; });
map.on('mouseleave', 'cambios-relleno', () => { map.getCanvas().style.cursor = ''; });

/* ---------- selección y ficha ---------- */

function deseleccionar() {
  estado.sel = null;
  if (map.getLayer('cambios-sel')) map.setFilter('cambios-sel', ['==', ['get', 'i'], -1]);
  $('ficha').hidden = true;
  limpiarFichaMapa();
  cerrarWayback();
  actualizarBotonMejorar();
}

function cajaGeometria(g) {
  const polis = g.type === 'MultiPolygon' ? g.coordinates : [g.coordinates];
  const b = new maplibregl.LngLatBounds();
  polis.forEach((p) => p[0].forEach((pt) => b.extend(pt)));
  return b;
}

function seleccionar(i) {
  const r = estado.resultados[i];
  if (!r) return;
  if (i >= estado.mostrados) {
    estado.mostrados = Math.ceil((i + 1) / PAGINA) * PAGINA;
    pintarLista();
  }
  estado.sel = i;
  document.querySelectorAll('.punto').forEach((el) => el.classList.toggle('sel', Number(el.dataset.i) === i));
  const tarjeta = document.querySelector(`.punto[data-i="${i}"]`);
  if (tarjeta) tarjeta.scrollIntoView({ block: 'nearest' });
  map.setFilter('cambios-sel', ['==', ['get', 'i'], i]);
  cerrarWayback();
  map.fitBounds(cajaGeometria(r.geometry), { padding: 60, maxZoom: 17 });
  cargarFicha(i);
}

function actualizarEstado(i, est) {
  const r = estado.resultados[i];
  if (!r || !est?.estado) return;
  r.estado = est.estado;
  r.motivo = est.motivo;
  pintarCambiosMapa();
  actualizarConteos();
  const vieja = document.querySelector(`.punto[data-i="${i}"]`);
  if (vieja) vieja.replaceWith(tarjetaPoligono(r, i));
}

function limpiarFichaMapa() {
  const vacio = { type: 'FeatureCollection', features: [] };
  if (map.getSource('rodales')) map.getSource('rodales').setData(vacio);
  if (map.getSource('referencias')) map.getSource('referencias').setData(vacio);
  dibujarPredio(null);
}

function filaDato(dt, dd) {
  return `<dt>${dt}</dt><dd>${dd}</dd>`;
}

function tarjetaPlan(p) {
  const u = p.ubicacion || {};
  const sig = p.sigcra || {};
  let rodales = 'No consultado (solo los 5 primeros planes)';
  if (sig.estado === 'ok') rodales = `${sig.rodales} rodal(es)${p.punto_en_rodal ? ` · el punto está dentro del rodal ${esc(p.punto_en_rodal)}` : ''}`;
  else if (sig.estado === 'sin_datos') rodales = `SIGCRA: ${SIN_DATOS}`;
  const ubic = p.ubicacion
    ? `${num(p.dist_km, 2)} km${u.aproximada ? ' · ubicación aproximada (datum no WGS84/SIRGAS)' : ''}${u.huso_inferido ? ` · huso ${u.huso} inferido` : ''}`
    : 'sin coordenadas convertibles';
  const aprobadoT1 = p.aprobado_a_t1 ? ' <span class="chip">Aprobado a t1</span>' : '';
  return `<article class="plan${p.punto_en_rodal ? ' dentro' : ''}${p.mismo_rol ? ' mismo-rol' : ''}">
    <h3>Plan ${esc(p['Nro.Solicitud'] || '—')} <span class="chip${p.aprobado ? '' : ' sin_datos'}">${esc(p['Estdo Solicitud'] || 'Sin estado')}</span>${p.mismo_rol ? aprobadoT1 : ''}</h3>
    <dl>
      ${filaDato('Tipo', esc(p['Tipo Solicitud Compl'] || '—'))}
      ${filaDato('Ingreso', fechaCL(p['Fec.Ingreso']))}
      ${filaDato('Resolución', `${esc(p['Nro.Resolucion'] || '—')} · ${fechaCL(p['Fec.Resolucion'])}`)}
      ${filaDato('Sup. solicitada', ha(p['Super.Solicitada']))}
      ${filaDato('Sup. aprobada', ha(p['Super.Aprobada']))}
      ${filaDato('Predio', `${esc(p['Nombre Predio'] || '—')} (cód. ${esc(p['Cod. Predio'] ?? '—')})`)}
      ${filaDato('Rol avalúo', esc(p['Rol Avaluo'] || '—'))}
      ${filaDato('Sup. predial', ha(p['Sup.Predial']))}
      ${filaDato('Comuna / oficina', `${esc(p.Comuna_Predio || '—')} / ${esc(p.Oficina || '—')}`)}
      ${filaDato('Pto. referencia', `${esc(p['Pto.Referencia01'] || '—')} · ${ubic}`)}
      ${filaDato('Rodales SIGCRA', rodales)}
    </dl>
  </article>`;
}

function claseConclusion(f) {
  if (f.saff?.estado !== 'ok') return 'sin_datos';
  const e = f.estado_cambio?.estado;
  if (e) return e === 'en_regla' ? '' : (e === 'alerta' ? 'alerta' : 'sin_datos');
  if (!f.planes?.length && !f.planes_rol?.length) return 'alerta';
  return '';
}

function htmlPredioFicha(p) {
  const anio = estado.regiones[estado.region]?.predios_anio;
  if (p?.estado === 'ok') {
    const sinRol = p.rol_norm ? '' : ' <span class="muted chico">(rol no numérico: no se cruza con SAFF)</span>';
    return `<p class="predio-ficha">${esc(textoPredio(p, anio))}${sinRol}</p>`;
  }
  if (p?.estado === 'sin_predio') return '<p class="aviso">Sin predio CIREN en el centroide.</p>';
  return `<p class="aviso">Predios CIREN: ${SIN_DATOS}.</p>`;
}

async function cargarFicha(i) {
  const r = estado.resultados[i];
  const pedido = ++estado.fichaPedido;
  $('ficha').hidden = false;
  $('ficha-titulo').textContent = `Ficha del polígono ${i + 1} · CIREN ${r.id}`;
  const cuerpo = $('ficha-cuerpo');
  cuerpo.innerHTML = '<p class="msg">Buscando predio (CIREN), planes de manejo (SAFF) y rodales (SIGCRA) desde el centroide…</p>';
  limpiarFichaMapa();
  $('ficha').scrollIntoView({ block: 'nearest' });
  const c = estado.consulta || consultaActual();
  let f;
  try {
    f = await getJSON(`api/v1/punto/ficha?region=${encodeURIComponent(c.region)}&lat=${r.centroide.lat}&lon=${r.centroide.lon}&radio_km=3&t1=${c.t1}`);
  } catch (err) {
    if (pedido === estado.fichaPedido) cuerpo.innerHTML = `<p class="aviso">${SIN_DATOS}: no se pudo consultar la ficha.</p>`;
    return;
  }
  if (pedido !== estado.fichaPedido) return;
  actualizarEstado(i, f.estado_cambio);

  const avisos = [];
  if (f.sigcra?.estado === 'sin_datos') avisos.push(`SIGCRA (rodales): ${SIN_DATOS}.`);
  if (f.sigcra?.estado === 'parcial') avisos.push('SIGCRA respondió solo para algunos planes.');
  if (f.saff?.truncado) avisos.push('La carga SAFF llegó al máximo de páginas; podría faltar algún plan.');

  const planesRol = f.planes_rol || [];
  const rolNorm = f.predio?.rol_norm;
  cuerpo.innerHTML = `
    ${htmlPredioFicha(f.predio)}
    <div class="conclusion ${claseConclusion(f)}">${esc(f.conclusion)}</div>
    ${avisos.map((a) => `<p class="aviso">${esc(a)}</p>`).join('')}
    <p class="chico muted">Estado del cambio: <span class="chip ${esc(estadoDe(r))}">${esc(infoEstado(estadoDe(r)).etq)}</span>${r.motivo ? ` ${esc(r.motivo)}` : ''}</p>
    <p class="chico muted">${esc(r.subuso || r.uso || 'Bosque')} · ${ha(r.superf_ha)} · NDVI ${f3(r.ndvi_t0)} → ${f3(r.ndvi_t1)} (Δ ${f3(r.delta)}).
      Centroide ${r.centroide.lat.toFixed(5)}, ${r.centroide.lon.toFixed(5)}: el cruce con rodales se evalúa en ese punto.</p>
    ${rolNorm && f.saff?.estado === 'ok' ? `<h3 class="sub">Planes SAFF del rol ${esc(rolNorm)} (${planesRol.length})</h3>
      ${planesRol.length ? `<div class="planes">${planesRol.map(tarjetaPlan).join('')}</div>` : '<p class="chico muted">Ningún plan SAFF con este rol en los últimos 10 años.</p>'}` : ''}
    ${f.saff?.estado === 'ok' ? `<h3 class="sub">Planes cercanos a 3 km (${(f.planes || []).length})</h3>` : ''}
    <p class="chico muted">${esc(f.nota_cercania)}${f.saff?.periodo ? ` Planes con resolución entre ${fechaCL(f.saff.periodo.desde)} y ${fechaCL(f.saff.periodo.hasta)}.` : ''}</p>
    <div class="planes">${(f.planes || []).map(tarjetaPlan).join('')}</div>`;

  dibujarPredio(f.predio?.geometry || null);
  if (map.getSource('rodales')) map.getSource('rodales').setData(f.rodales || { type: 'FeatureCollection', features: [] });
  const refs = [...planesRol, ...(f.planes || [])].filter((p) => p.ubicacion).map((p) => ({
    type: 'Feature',
    geometry: { type: 'Point', coordinates: [p.ubicacion.lon, p.ubicacion.lat] },
    properties: { plan: p['Nro.Solicitud'] || '' },
  }));
  if (map.getSource('referencias')) map.getSource('referencias').setData({ type: 'FeatureCollection', features: refs });
}

map.on('click', 'referencias', (ev) => {
  ignorarClickHasta = Date.now() + 400;
  const p = ev.features[0].properties;
  new maplibregl.Popup().setLngLat(ev.lngLat).setHTML(`Punto de referencia del predio<br>Plan ${esc(p.plan)}`).addTo(map);
});

/* ---------- Mejorar calidad de imagen (Esri Wayback) ---------- */

function actualizarBotonMejorar() {
  $('btn-mejorar').hidden = !(estado.sel !== null && map.getZoom() >= ZOOM_MEJORAR && $('wayback').hidden);
}
map.on('zoomend', actualizarBotonMejorar);
map.on('moveend', actualizarBotonMejorar);

async function configWayback() {
  if (estado.wayback.config) return estado.wayback.config;
  const raw = await getJSON(WAYBACK_CONFIG_URL);
  const lista = Object.entries(raw).map(([release, v]) => {
    const m = String(v.itemTitle || '').match(/(\d{4}-\d{2}-\d{2})/);
    return m ? { release, publicada: m[1], itemURL: v.itemURL, metadataLayerUrl: v.metadataLayerUrl } : null;
  }).filter(Boolean).sort((a, b) => a.publicada.localeCompare(b.publicada));
  estado.wayback.config = lista;
  return lista;
}

function versionesRelevantes(lista, t0, t1) {
  const antesDe = (f) => [...lista].reverse().find((v) => v.publicada < f);
  const elegidas = [antesDe(t0), antesDe(t1), lista[lista.length - 1]].filter(Boolean);
  return [...new Map(elegidas.map((v) => [v.release, v])).values()];
}

function fechaCaptura(attrs) {
  const s = String(attrs.SRC_DATE ?? '');
  if (/^\d{8}$/.test(s)) return `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}`;
  const s2 = String(attrs.SRC_DATE2 ?? '');
  if (/^\d{8}$/.test(s2)) return `${s2.slice(0, 4)}-${s2.slice(4, 6)}-${s2.slice(6, 8)}`;
  const m = s2.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})/);
  if (m) return `${m[3]}-${m[1].padStart(2, '0')}-${m[2].padStart(2, '0')}`;
  if (/^\d{10,13}$/.test(s2)) return new Date(Number(s2)).toISOString().slice(0, 10);
  return null;
}

async function capturaEnPunto(version, lon, lat) {
  const ext = `${lon - 0.05},${lat - 0.05},${lon + 0.05},${lat + 0.05}`;
  const url = `${version.metadataLayerUrl}/identify?geometry=${lon},${lat}&geometryType=esriGeometryPoint&sr=4326&layers=all&tolerance=0&mapExtent=${ext}&imageDisplay=400,400,96&returnGeometry=false&f=json`;
  const d = await getJSON(url);
  const res = (d.results || []).filter((x) => x.attributes);
  if (!res.length) return null;
  const mejor = res.reduce((a, b) => (Number(b.attributes.SRC_RES) < Number(a.attributes.SRC_RES) ? b : a));
  const fecha = fechaCaptura(mejor.attributes);
  if (!fecha) return null;
  return {
    fecha,
    resolucion: Number(mejor.attributes.SRC_RES),
    proveedor: mejor.attributes.NICE_DESC || mejor.attributes.NICE_NAME || '',
    version,
  };
}

function textoCaptura(c) {
  const res = Number.isFinite(c.resolucion) ? ` · ${c.resolucion.toLocaleString('es-CL', { maximumFractionDigits: 2 })} m` : '';
  return `captura ${fechaCL(c.fecha)}${res}${c.proveedor ? ` · ${c.proveedor}` : ''}`;
}

function mostrarCapturaWayback() {
  const w = estado.wayback;
  const idx = w.comparar && w.viendo === 'b' ? w.b : w.a;
  const c = w.capturas[idx];
  if (!c) return;
  if (map.getLayer('wayback')) map.removeLayer('wayback');
  if (map.getSource('wayback')) map.removeSource('wayback');
  map.addSource('wayback', {
    type: 'raster', tileSize: 256, maxzoom: 19,
    tiles: [c.version.itemURL.replace('{level}', '{z}').replace('{row}', '{y}').replace('{col}', '{x}')],
    attribution: 'Esri World Imagery Wayback',
  });
  map.addLayer({ id: 'wayback', type: 'raster', source: 'wayback' }, 'tope-base');
  $('wayback-titulo').textContent = `Imagen de alta resolución · ${textoCaptura(c)}`;
}

function opcionesCapturas(sel) {
  return estado.wayback.capturas.map((c, i) => `<option value="${i}"${i === sel ? ' selected' : ''}>${esc(textoCaptura(c))}</option>`).join('');
}

function pintarPanelWayback() {
  const w = estado.wayback;
  const cuerpo = $('wayback-cuerpo');
  if (!w.capturas.length) {
    cuerpo.innerHTML = `<p class="aviso">${SIN_DATOS}: no se encontró fecha de captura Wayback en este punto.</p>`;
    $('wayback-titulo').textContent = 'Imagen de alta resolución';
    return;
  }
  cuerpo.innerHTML = `
    <label class="chico muted">Fecha de captura ${w.comparar ? 'A' : ''}<select id="wb-a">${opcionesCapturas(w.a)}</select></label>
    ${w.capturas.length > 1 ? `<label class="check chico"><input type="checkbox" id="wb-comparar"${w.comparar ? ' checked' : ''}> Comparar</label>` : ''}
    ${w.comparar ? `<label class="chico muted">Fecha de captura B<select id="wb-b">${opcionesCapturas(w.b)}</select></label>
      <div class="segmentado" role="radiogroup" aria-label="Alternar A/B">
        <button type="button" data-ab="a" class="${w.viendo === 'a' ? 'activo' : ''}" aria-pressed="${w.viendo === 'a'}">Ver A</button>
        <button type="button" data-ab="b" class="${w.viendo === 'b' ? 'activo' : ''}" aria-pressed="${w.viendo === 'b'}">Ver B</button>
      </div>` : ''}`;
  $('wb-a').addEventListener('change', (e) => { w.a = Number(e.target.value); mostrarCapturaWayback(); });
  const comp = $('wb-comparar');
  if (comp) comp.addEventListener('change', (e) => { w.comparar = e.target.checked; w.viendo = 'a'; pintarPanelWayback(); mostrarCapturaWayback(); });
  const b = $('wb-b');
  if (b) b.addEventListener('change', (e) => { w.b = Number(e.target.value); mostrarCapturaWayback(); });
  cuerpo.querySelectorAll('[data-ab]').forEach((btn) => btn.addEventListener('click', () => {
    w.viendo = btn.dataset.ab;
    pintarPanelWayback();
    mostrarCapturaWayback();
  }));
  mostrarCapturaWayback();
}

async function mejorarImagen() {
  const r = estado.resultados[estado.sel];
  if (!r) return;
  const c = estado.consulta || consultaActual();
  const w = estado.wayback;
  $('btn-mejorar').hidden = true;
  $('wayback').hidden = false;
  $('wayback-titulo').textContent = 'Imagen de alta resolución';
  $('wayback-cuerpo').innerHTML = '<p class="muted">Buscando fechas de captura…</p>';
  const sel = estado.sel;
  try {
    const lista = await configWayback();
    const versiones = versionesRelevantes(lista, c.t0, c.t1);
    const { lat, lon } = r.centroide;
    const capturas = (await Promise.all(versiones.map((v) => capturaEnPunto(v, lon, lat).catch(() => null)))).filter(Boolean);
    const unicas = [...new Map(capturas.map((x) => [x.fecha, x])).values()].sort((a, b) => a.fecha.localeCompare(b.fecha));
    if (estado.sel !== sel || $('wayback').hidden) return;
    w.capturas = unicas;
    w.a = Math.max(0, unicas.length - 1);
    w.b = 0;
    w.viendo = 'a';
    w.comparar = false;
  } catch (err) {
    if (estado.sel !== sel || $('wayback').hidden) return;
    w.capturas = [];
  }
  pintarPanelWayback();
}

function cerrarWayback() {
  if (map.getLayer('wayback')) map.removeLayer('wayback');
  if (map.getSource('wayback')) map.removeSource('wayback');
  $('wayback').hidden = true;
  estado.wayback.capturas = [];
  actualizarBotonMejorar();
}

$('btn-mejorar').addEventListener('click', mejorarImagen);
$('wayback-volver').addEventListener('click', cerrarWayback);

/* ---------- inicio ---------- */

if (window.matchMedia('(max-width: 900px)').matches) $('capas').open = false;
mapaListo.then(() => {
  const params = new URLSearchParams(window.location.search);
  const btn = params.get('base') && document.querySelector(`[data-base="${CSS.escape(params.get('base'))}"]`);
  if (btn) btn.click();
  if (params.get('ciren') === '0') {
    $('ciren-on').checked = false;
    $('ciren-on').dispatchEvent(new Event('change'));
  }
  if (params.get('predios') === '1') {
    $('predios-on').checked = true;
    $('predios-on').dispatchEvent(new Event('change'));
  }
});
cargarRegiones();
