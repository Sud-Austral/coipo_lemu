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

// Fracción 0..1 -> '92 %'
function pctFraccion(v) {
  return v === null || v === undefined ? '—' : `${Math.round(Number(v) * 100)} %`;
}

// Porcentaje de nubes (0..100) -> '3 %' / '< 1 %'
function pctNubes(v) {
  if (v === null || v === undefined) return '— %';
  return v > 0 && v < 1 ? '< 1 %' : `${Math.round(v)} %`;
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

// Colores del mapa: más luminosos que los de la lista para leerse sobre bosque oscuro.
const COLOR_ESTADO = ['match', ['get', 'estado'],
  'alerta', '#e5484d', 'en_regla', '#30a46c', 'revisar', '#f5b400', 'sin_datos', '#b8c2bc', '#8b9a90'];
// Orden de dibujo por gravedad: en bordes compartidos gana la alerta.
const ORDEN_ESTADO = ['match', ['get', 'estado'], 'alerta', 3, 'revisar', 2, 'en_regla', 1, 0];
const HALO = 'rgba(0,0,0,0.55)';
const LINEA_REDONDA = { 'line-join': 'round', 'line-cap': 'round' };
const ANCHO_BORDE = ['interpolate', ['linear'], ['zoom'], 9, 1, 13, 2, 16, 3];
const ANCHO_HALO = ['interpolate', ['linear'], ['zoom'], 9, 3, 13, 4, 16, 5];
const RELLENO = ['case', ['boolean', ['feature-state', 'hover'], false], 0.35, 0.2];
const SIN_SEL = ['==', ['get', 'i'], -1];
// Punto en el centroide solo si el polígono mide < 3 px: `px11` es su tamaño a
// zoom 11 y cada nivel menos lo reduce a la mitad (capa visible hasta zoom 12).
const chicoA = (px) => ['case', ['<', ['get', 'px11'], px], 1, 0];
// `f` atenúa (polígono no seleccionado): zoom solo puede ir en el step de afuera.
const puntoVisible = (f = 1) => {
  const v = (px) => ['*', chicoA(px), f];
  return ['step', ['zoom'], v(48), 8, v(24), 9, v(12), 10, v(6), 11, v(3)];
};

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
      'cambios-centros': { type: 'geojson', data: VACIO },
      rodales: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
      predio: { type: 'geojson', data: VACIO },
      referencias: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
    },
    layers: [
      { id: 'base-sat', type: 'raster', source: 'sat', layout: { visibility: 'none' } },
      { id: 'base-bing', type: 'raster', source: 'bing', layout: { visibility: 'none' } },
      { id: 'base-google', type: 'raster', source: 'google' },
      { id: 'base-osm', type: 'raster', source: 'osm', layout: { visibility: 'none' } },
      // Wayback se inserta aquí (antes de 'tope-base'); NDVI, CIREN y predios antes de 'tope-capas'.
      // Encima: rodales, predio, cambios (relleno, halo, borde), selección, referencias y rótulos.
      { id: 'tope-base', type: 'background', paint: { 'background-opacity': 0 } },
      { id: 'tope-capas', type: 'background', paint: { 'background-opacity': 0 } },
      { id: 'rodales-borde', type: 'line', source: 'rodales', layout: LINEA_REDONDA, paint: { 'line-color': '#3dd6f5', 'line-width': 1.5 } },
      { id: 'predio-halo', type: 'line', source: 'predio', layout: LINEA_REDONDA, paint: { 'line-color': HALO, 'line-width': 4 } },
      { id: 'predio-borde', type: 'line', source: 'predio', paint: { 'line-color': '#ffffff', 'line-width': 2, 'line-dasharray': [3, 2] } },
      { id: 'cambios-relleno', type: 'fill', source: 'cambios', paint: { 'fill-color': COLOR_ESTADO, 'fill-opacity': RELLENO } },
      { id: 'cambios-halo', type: 'line', source: 'cambios', layout: { ...LINEA_REDONDA, 'line-sort-key': ORDEN_ESTADO }, paint: { 'line-color': HALO, 'line-width': ANCHO_HALO } },
      { id: 'cambios-borde', type: 'line', source: 'cambios', layout: { ...LINEA_REDONDA, 'line-sort-key': ORDEN_ESTADO }, paint: { 'line-color': COLOR_ESTADO, 'line-width': ANCHO_BORDE } },
      {
        id: 'cambios-puntos', type: 'circle', source: 'cambios-centros', maxzoom: 12,
        paint: {
          'circle-color': COLOR_ESTADO,
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 8, 3, 11, 5],
          'circle-stroke-color': HALO,
          'circle-stroke-width': 1,
          'circle-opacity': puntoVisible(),
          'circle-stroke-opacity': puntoVisible(),
        },
      },
      { id: 'cambios-sel-halo', type: 'line', source: 'cambios', filter: SIN_SEL, layout: LINEA_REDONDA, paint: { 'line-color': HALO, 'line-width': 5.5 } },
      { id: 'cambios-sel', type: 'line', source: 'cambios', filter: SIN_SEL, layout: LINEA_REDONDA, paint: { 'line-color': '#ffffff', 'line-width': 3.5 } },
      {
        id: 'referencias', type: 'circle', source: 'referencias',
        paint: { 'circle-radius': 5, 'circle-color': '#ffffff', 'circle-stroke-color': '#0b6b3a', 'circle-stroke-width': 2 },
      },
      { id: 'rotulos', type: 'raster', source: 'rotulos' },
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

// Al mostrar resultados el raster CIREN baja a 30 % para no competir con los
// polígonos, salvo que el usuario ya haya ajustado la capa.
let cirenTocado = false;

function atenuarCiren() {
  // Sus colores de uso (granate, oliva, verde) se confunden con los de estado:
  // se apaga al mostrar resultados, salvo que el usuario ya la haya ajustado.
  if (cirenTocado) return;
  $('ciren-on').checked = false;
  if (map.getLayer('ciren')) map.setLayoutProperty('ciren', 'visibility', 'none');
}

$('ciren-on').addEventListener('change', () => {
  cirenTocado = true;
  if (map.getLayer('ciren')) map.setLayoutProperty('ciren', 'visibility', $('ciren-on').checked ? 'visible' : 'none');
});
$('ciren-opacidad').addEventListener('input', () => {
  cirenTocado = true;
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
  if (map.queryRenderedFeatures(ev.point, { layers: ['referencias', 'cambios-relleno', 'cambios-puntos'] }).length) return;
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

// Teselas NDVI t0/t1 de la comuna (también las usa el comparador); null si falló.
async function datosNdvi(c) {
  const clave = `${c.region}|${c.t0}|${c.t1}`;
  if (!estado.sentinel[clave]) {
    try {
      estado.sentinel[clave] = await getJSON(`api/v1/sentinel/tiles?region=${encodeURIComponent(c.region)}&t0=${c.t0}&t1=${c.t1}`);
    } catch (err) {
      return null;
    }
  }
  return estado.sentinel[clave];
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
  const data = await datosNdvi(c);
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
    features: estado.resultados.map((r, i) => ({ type: 'Feature', id: i, geometry: r.geometry, properties: { i, id: r.id, estado: estadoDe(r) } })),
  };
}

// Tamaño en px a zoom 11 (lado mayor de la caja; el mundo mide 512 px a zoom 0).
function px11(r) {
  const b = cajaGeometria(r.geometry);
  const cos = Math.cos((r.centroide.lat * Math.PI) / 180);
  const m = Math.max((b.getEast() - b.getWest()) * 111320 * cos, (b.getNorth() - b.getSouth()) * 110540);
  return m / ((40075016 * cos) / 512 / 2048);
}

function geojsonCentros() {
  return {
    type: 'FeatureCollection',
    features: estado.resultados.map((r, i) => ({
      type: 'Feature',
      geometry: { type: 'Point', coordinates: [r.centroide.lon, r.centroide.lat] },
      properties: { i, estado: estadoDe(r), px11: px11(r) },
    })),
  };
}

let hoverCambio = null;

function pintarCambiosMapa() {
  if (!map.getSource('cambios')) return;
  hoverCambio = null;
  map.removeFeatureState({ source: 'cambios' });
  map.getSource('cambios').setData(geojsonCambios());
  map.getSource('cambios-centros').setData(geojsonCentros());
}

function mostrarCambiosMapa() {
  pintarCambiosMapa();
  if (estado.resultados.length) atenuarCiren();
}

// Con un polígono seleccionado: borde blanco en él y el resto al 60 %.
function marcarSeleccion() {
  const i = estado.sel ?? -1;
  const filtro = ['==', ['get', 'i'], i];
  map.setFilter('cambios-sel', filtro);
  map.setFilter('cambios-sel-halo', filtro);
  const atenuar = i < 0 ? 1 : ['case', ['==', ['get', 'i'], i], 1, 0.6];
  map.setPaintProperty('cambios-relleno', 'fill-opacity', i < 0 ? RELLENO : ['*', RELLENO, atenuar]);
  ['cambios-halo', 'cambios-borde'].forEach((l) => map.setPaintProperty(l, 'line-opacity', atenuar));
  ['circle-opacity', 'circle-stroke-opacity'].forEach((prop) => map.setPaintProperty('cambios-puntos', prop, puntoVisible(atenuar)));
}

function rolTarjeta(r) {
  const p = r.predio || {};
  if (p.estado === 'ok') return `Rol ${esc(p.rol || 'sin rol')}`;
  if (p.estado === 'sin_predio') return 'Sin predio CIREN';
  return 'Rol: sin datos';
}

// Fracción del polígono con píxeles sin nube (Cloud Score+) en cada periodo.
function textoCobertura(r) {
  if (r.cobertura_t0 == null && r.cobertura_t1 == null) return '';
  return `Cobertura limpia t0 ${pctFraccion(r.cobertura_t0)} · t1 ${pctFraccion(r.cobertura_t1)}`;
}

function tarjetaPoligono(r, i) {
  const e = estadoDe(r);
  const inf = infoEstado(e);
  const el = document.createElement('div');
  el.setAttribute('role', 'button');
  el.tabIndex = 0;
  el.className = `punto${estado.sel === i ? ' sel' : ''}`;
  el.dataset.i = i;
  el.innerHTML = `
    <span class="n" style="background:${inf.color}">${i + 1}</span>
    <span class="fila"><span class="chip ${esc(e)}">${esc(inf.etq)}</span><b>${esc(r.subuso || r.uso || 'Bosque')}</b><span class="muted">${ha(r.superf_ha)}</span><span class="coords">${rolTarjeta(r)}</span><span class="coords">CIREN ${esc(r.id)}</span></span>
    <span class="fila ndvi">NDVI t0 <b>${f3(r.ndvi_t0)}</b> · t1 <b>${f3(r.ndvi_t1)}</b> · Δ <b style="color:#c0392b">${f3(r.delta)}</b></span>
    ${textoCobertura(r) ? `<span class="fila coords">${esc(textoCobertura(r))}</span>` : ''}
    <span class="fila motivo">${esc(r.motivo || '')}</span>
    <span class="fila"><button type="button" class="${e === 'alerta' ? 'btn-primario' : 'btn-secundario'} btn-comparar">Comparar antes / después</button></span>`;
  el.addEventListener('click', () => seleccionar(i));
  el.addEventListener('keydown', (ev) => {
    if (ev.target === el && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); seleccionar(i); }
  });
  el.querySelector('.btn-comparar').addEventListener('click', (ev) => {
    ev.stopPropagation();
    seleccionar(i);
    abrirComparador(i);
  });
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
  if (s.sin_imagen_limpia) partes.push(`${s.sin_imagen_limpia.toLocaleString('es-CL')} polígonos sin imagen limpia (nubes sobre más de la mitad del polígono en t0 o t1): no se evalúan.`);
  if (s.sin_ndvi) partes.push(`${s.sin_ndvi.toLocaleString('es-CL')} sin NDVI (sin imagen o sin respuesta de GEE).`);
  if (d.mascara_nubes === 'cs_plus') partes.push('Nubes y sombras quitadas píxel a píxel (Cloud Score+).');
  if (d.gee?.estado === 'parcial') partes.push(`La API GEE no respondió en ${d.gee.lotes_fallidos} de ${d.gee.lotes} lotes.`);
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
    mostrarCambiosMapa();
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

['cambios-relleno', 'cambios-puntos'].forEach((capa) => {
  map.on('click', capa, (ev) => {
    if (Date.now() < ignorarClickHasta) return;  // punto y relleno en el mismo clic
    ignorarClickHasta = Date.now() + 400;
    const i = ev.features[0].properties.i;
    if (!Number.isInteger(i)) return;
    seleccionar(i);
    abrirComparador(i);
  });
  map.on('mouseenter', capa, () => { map.getCanvas().style.cursor = 'pointer'; });
  map.on('mouseleave', capa, () => { map.getCanvas().style.cursor = ''; });
});

function hoverEn(i) {
  if (hoverCambio === i) return;
  if (hoverCambio !== null) map.setFeatureState({ source: 'cambios', id: hoverCambio }, { hover: false });
  hoverCambio = i;
  if (i !== null) map.setFeatureState({ source: 'cambios', id: i }, { hover: true });
}
map.on('mousemove', 'cambios-relleno', (ev) => hoverEn(ev.features[0].id ?? null));
map.on('mouseleave', 'cambios-relleno', () => hoverEn(null));

/* ---------- selección y ficha ---------- */

function deseleccionar() {
  estado.sel = null;
  if (map.getLayer('cambios-sel')) marcarSeleccion();
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
  marcarSeleccion();
  cerrarWayback();
  map.fitBounds(cajaGeometria(r.geometry), { padding: 60, maxZoom: 17 });
  cargarFicha(i);
}

function actualizarEstado(i, est) {
  const r = estado.resultados[i];
  if (!r || !est?.estado) return;
  r.estado = est.estado;
  r.motivo = est.motivo;
  if (estado.sel === i) marcarBotonFicha(r);
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

function marcarBotonFicha(r) {
  $('ficha-comparar').className = `${estadoDe(r) === 'alerta' ? 'btn-primario' : 'btn-secundario'} btn-comparar`;
}

$('ficha-comparar').addEventListener('click', () => { if (estado.sel !== null) abrirComparador(estado.sel); });

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
  marcarBotonFicha(r);
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
    <p class="chico muted">${esc(r.subuso || r.uso || 'Bosque')} · ${ha(r.superf_ha)} · NDVI ${f3(r.ndvi_t0)} → ${f3(r.ndvi_t1)} (Δ ${f3(r.delta)}).${textoCobertura(r) ? ` ${esc(textoCobertura(r))} (fracción del polígono sin nubes).` : ''}
      Centroide ${r.centroide.lat.toFixed(5)}, ${r.centroide.lon.toFixed(5)}: el cruce con rodales se evalúa en ese punto.${r.geometria === 'simplificada' ? ' Contorno simplificado (CIREN no entregó el exacto).' : ''}</p>
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

// Fecha de captura de una versión en un punto; cacheada (la piden la ficha y el comparador).
const cacheCapturas = new Map();
function capturaEnPunto(version, lon, lat) {
  const clave = `${version.release}|${lon.toFixed(5)}|${lat.toFixed(5)}`;
  if (!cacheCapturas.has(clave)) {
    cacheCapturas.set(clave, consultarCaptura(version, lon, lat).catch((err) => { cacheCapturas.delete(clave); throw err; }));
  }
  return cacheCapturas.get(clave);
}

async function consultarCaptura(version, lon, lat) {
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

function urlWayback(version) {
  return version.itemURL.replace('{level}', '{z}').replace('{row}', '{y}').replace('{col}', '{x}');
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
    tiles: [urlWayback(c.version)],
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

/* ---------- comparador antes / después (cortina) ---------- */

// Dos mapas apilados y sincronizados: abajo ANTES (t0), arriba DESPUÉS (t1)
// recortado con clip-path desde la línea. Solo imágenes reales con su fecha.
const WAYBACK_ANIOS_ATRAS = 3;   // versiones Wayback publicadas desde t0 − 3 años
const WAYBACK_CONCURRENCIA = 6;
const LADOS = {
  a: { nombre: 'ANTES', ref: 't0' },
  b: { nombre: 'DESPUÉS', ref: 't1' },
};
const FUENTES_CMP = {
  wayback: 'Alta resolución (Wayback)',
  google: 'Google (solo foto actual)',
  sentinel: 'Sentinel-2 color real',
  infrarrojo: 'Sentinel-2 infrarrojo',
  ndvi: 'NDVI Sentinel-2',
};
// Fuentes Sentinel-2 de /api/v1/comparar/sentinel -> su `combinacion`.
const COMBINACION_CMP = { sentinel: 'color_real', infrarrojo: 'infrarrojo' };
const porCombinacion = (fn) => Object.fromEntries(Object.keys(COMBINACION_CMP).map((f) => [f, fn()]));

const cmp = {
  i: null,
  pedido: 0,
  mapas: null,
  listos: null,
  pct: 50,
  consulta: null,
  lados: { a: { fuente: 'wayback', captura: null }, b: { fuente: 'wayback', captura: null } },
  capturas: null,      // null = buscando; [] = ninguna
  avance: '',
  // Por fuente Sentinel (sentinel = color real, infrarrojo):
  sentinel: porCombinacion(() => null),              // respuesta de /api/v1/comparar/sentinel (null = sin pedir o cargando)
  sentinelCargando: porCombinacion(() => new Set()), // lados ('t0'/'t1') con una escena elegida a mano en camino
  sentinelSerie: porCombinacion(() => 0),
  escenas: { t0: null, t1: null },  // escena Sentinel-2 elegida a mano por lado (null = automática), común a ambas combinaciones
  ndvi: undefined,     // undefined = sin pedir o cargando; null = falló
  pidiendo: new Set(),
  volverFoco: null,
};

function dias(f, ref) {
  return Math.round((Date.parse(f) - Date.parse(ref)) / 86400000);
}

function difDias(f, ref, nombre) {
  const d = dias(f, ref);
  if (d === 0) return `mismo día que ${nombre}`;
  return `${d < 0 ? '−' : '+'}${Math.abs(d).toLocaleString('es-CL')} d de ${nombre}`;
}

async function mapaLimitado(items, n, fn, alAvanzar) {
  const salida = new Array(items.length);
  let siguiente = 0;
  let listos = 0;
  async function trabajador() {
    while (siguiente < items.length) {
      const j = siguiente++;
      salida[j] = await fn(items[j]).catch(() => null);
      listos += 1;
      if (alAvanzar) alAvanzar(listos, items.length);
    }
  }
  await Promise.all(Array.from({ length: Math.min(n, items.length) }, trabajador));
  return salida;
}

// Capturas Wayback distintas (por fecha real) en el punto, de la más antigua a la más nueva.
async function capturasWaybackPunto(lon, lat, desde, alAvanzar) {
  const lista = await configWayback();
  const versiones = lista.filter((v) => v.publicada >= desde);
  const res = (await mapaLimitado(versiones, WAYBACK_CONCURRENCIA, (v) => capturaEnPunto(v, lon, lat), alAvanzar)).filter(Boolean);
  const unicas = new Map();
  res.forEach((c) => { if (!unicas.has(c.fecha)) unicas.set(c.fecha, c); });  // gana la primera versión que la publicó
  return [...unicas.values()].sort((x, y) => x.fecha.localeCompare(y.fecha));
}

// ANTES: la captura ≤ t0 más cercana (si no hay, la más antigua). DESPUÉS: la
// más cercana a t1 entre las posteriores a ANTES (de preferencia ≥ t0).
function elegirCapturas(caps, t0, t1) {
  if (!caps.length) return { a: null, b: null };
  const previa = [...caps].reverse().find((c) => c.fecha <= t0);
  const a = previa ? caps.indexOf(previa) : 0;
  const despues = caps.filter((c) => c.fecha > caps[a].fecha);
  const pref = despues.filter((c) => c.fecha >= t0);
  const pool = pref.length ? pref : despues;
  if (!pool.length) return { a, b: a };
  const mejor = pool.reduce((m, c) => (Math.abs(dias(c.fecha, t1)) < Math.abs(dias(m.fecha, t1)) ? c : m));
  return { a, b: caps.indexOf(mejor) };
}

function crearMapaCmp(id, posAtribucion) {
  const m = new maplibregl.Map({
    container: id,
    attributionControl: false,
    maxZoom: 19,
    dragRotate: false,
    pitchWithRotate: false,
    style: {
      version: 8,
      sources: { poli: { type: 'geojson', data: VACIO } },
      layers: [
        { id: 'fondo', type: 'background', paint: { 'background-color': '#1b2420' } },
        { id: 'poli-halo', type: 'line', source: 'poli', layout: LINEA_REDONDA, paint: { 'line-color': HALO, 'line-width': 6 } },
        { id: 'poli-borde', type: 'line', source: 'poli', layout: LINEA_REDONDA, paint: { 'line-color': '#ffffff', 'line-width': 2.5 } },
      ],
    },
  });
  m.addControl(new maplibregl.AttributionControl({ compact: true }), posAtribucion);
  m.on('styledata', compactarAtribucion);
  return m;
}

// center/zoom/bearing/pitch de un mapa al otro, con guarda anti-bucle.
function sincronizarMapas(m1, m2) {
  let copiando = false;
  const copiar = (de, a) => () => {
    if (copiando) return;
    copiando = true;
    a.jumpTo({ center: de.getCenter(), zoom: de.getZoom(), bearing: de.getBearing(), pitch: de.getPitch() });
    copiando = false;
  };
  m1.on('move', copiar(m1, m2));
  m2.on('move', copiar(m2, m1));
}

function asegurarMapasCmp() {
  if (cmp.mapas) return cmp.listos;
  const a = crearMapaCmp('cmp-map-a', 'bottom-left');
  const b = crearMapaCmp('cmp-map-b', 'bottom-right');
  b.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');
  sincronizarMapas(a, b);
  cmp.mapas = { a, b };
  cmp.listos = Promise.all([a, b].map((m) => new Promise((ok) => (m.loaded() ? ok() : m.on('load', ok)))));
  new ResizeObserver(aplicarCortina).observe($('cmp-mapas'));
  return cmp.listos;
}

function aplicarCortina() {
  const ancho = $('cmp-mapas').clientWidth;
  const x = Math.round((ancho * cmp.pct) / 100);
  $('cmp-map-b').style.clipPath = `inset(0 0 0 ${x}px)`;
  $('cmp-linea').style.left = `${x}px`;
  // Cada etiqueta queda dentro de su lado: nunca tapa la linea ni la otra foto.
  ajustarEtiqueta($('cmp-etq-a'), x - 34);
  ajustarEtiqueta($('cmp-etq-b'), ancho - x - 34);
  const manija = $('cmp-manija');
  manija.setAttribute('aria-valuenow', String(Math.round(cmp.pct)));
  manija.setAttribute('aria-valuetext', `${Math.round(cmp.pct)} % ANTES`);
}

function ajustarEtiqueta(el, disponible) {
  if (!el) return;
  el.style.maxWidth = `${Math.max(0, disponible)}px`;
  el.classList.toggle('compacta', disponible < 190);
  el.classList.toggle('oculta', disponible < 80);
}

function moverCortina(pct) {
  cmp.pct = Math.min(100, Math.max(0, pct));
  aplicarCortina();
}

(() => {
  const linea = $('cmp-linea');
  let arrastrando = null;
  linea.addEventListener('pointerdown', (ev) => {
    arrastrando = ev.pointerId;
    linea.setPointerCapture(ev.pointerId);
    linea.classList.add('arrastrando');
    $('cmp-manija').focus();
    ev.preventDefault();
  });
  linea.addEventListener('pointermove', (ev) => {
    if (arrastrando !== ev.pointerId) return;
    const caja = $('cmp-mapas').getBoundingClientRect();
    moverCortina(((ev.clientX - caja.left) / caja.width) * 100);
  });
  const soltar = (ev) => {
    if (arrastrando !== ev.pointerId) return;
    arrastrando = null;
    linea.classList.remove('arrastrando');
  };
  linea.addEventListener('pointerup', soltar);
  linea.addEventListener('pointercancel', soltar);
  $('cmp-manija').addEventListener('keydown', (ev) => {
    const paso = { ArrowLeft: -5, ArrowRight: 5, Home: -100, End: 100 }[ev.key];
    if (paso === undefined) return;
    ev.preventDefault();
    moverCortina(cmp.pct + paso);
  });
})();

/* --- qué muestra cada lado --- */

function textoRes(m) {
  return Number.isFinite(m) ? `${m.toLocaleString('es-CL', { maximumFractionDigits: 2 })} m` : '';
}

// {estado: ok|cargando|sin, tiles, linea, detalle, ndvi}
function infoLado(l) {
  const c = cmp.consulta;
  const { nombre, ref } = LADOS[l];
  const fechaRef = c[ref];
  const lado = cmp.lados[l];
  if (lado.fuente === 'wayback') {
    const linea = `${nombre} · alta resolución`;
    if (cmp.capturas === null) return { estado: 'cargando', linea, detalle: `Buscando fechas de captura Wayback… ${cmp.avance}` };
    const cap = cmp.capturas[lado.captura];
    if (!cap) return { estado: 'sin', linea, detalle: 'Sin fecha de captura Wayback en este punto' };
    const notas = [difDias(cap.fecha, fechaRef, ref)];
    if (l === 'a' && cap.fecha > c.t0) notas.push('posterior a t0');
    if (l === 'b' && cap.fecha < c.t0) notas.push('anterior a t0');
    return {
      estado: 'ok', tiles: [urlWayback(cap.version)], maxzoom: 19, atribucion: 'Esri World Imagery Wayback',
      linea: `${nombre} · captura ${fechaCL(cap.fecha)} · Wayback ${textoRes(cap.resolucion)}`.trim(),
      detalle: `${notas.join(' · ')}${cap.proveedor ? ` · ${cap.proveedor}` : ''}`,
    };
  }
  if (lado.fuente === 'google') {
    return {
      estado: 'ok', tiles: [0, 1, 2, 3].map((n) => `https://mt${n}.google.com/vt/lyrs=s&x={x}&y={y}&z={z}`), maxzoom: 20, atribucion: 'Imágenes © Google',
      linea: `${nombre} · Google · fecha no informada`, detalle: 'Foto actual de Google; no se conoce su fecha de captura',
    };
  }
  if (COMBINACION_CMP[lado.fuente]) {
    const f = lado.fuente;
    const ir = f === 'infrarrojo';
    const nombreFuente = ir ? 'Sentinel-2 infrarrojo' : 'Sentinel-2';
    const linea = `${nombre} · ${FUENTES_CMP[f]}`;
    const datos = cmp.sentinel[f];
    if (datos === null) return { estado: 'cargando', linea, detalle: 'Buscando la escena con menos nubes sobre el polígono (±15, ±30 y ±60 días)…' };
    if (cmp.sentinelCargando[f].has(ref)) return { estado: 'cargando', linea, detalle: `Cargando la escena del ${fechaCL(cmp.escenas[ref])}…` };
    const s = datos[ref];
    if (!s || s.estado !== 'ok') return { estado: 'sin', linea, detalle: (s && s.mensaje) || datos.mensaje || SIN_DATOS };
    const fecha = s.fecha_escena || (s.fecha_min === s.fecha_max ? s.fecha_min : null);
    const partes = [`${nombre} · ${nombreFuente}`, fecha ? `escena ${fechaCL(fecha)}` : 'fecha de escena no informada'];
    if (s.nubes_pct !== null && s.nubes_pct !== undefined) partes.push(`${pctNubes(s.nubes_pct)} nubes sobre el polígono`);
    if (fecha) partes.push(difDias(fecha, fechaRef, ref));
    const detalle = [];
    if (s.eleccion === 'manual') detalle.push('escena elegida a mano');
    else if (s.limpia === false) detalle.push(`ninguna escena con ≤ ${datos.nubes_max_pct ?? 10} % de nubes a ±${s.ventana?.dias ?? '—'} d: se muestra la menos nublada`);
    else detalle.push(`la más limpia a ±${s.ventana?.dias ?? '—'} d`);
    // Contraste: igual en ambos lados solo si el backend pudo aplicar el rango común.
    if (s.estiramiento_comun) detalle.push('contraste ajustado a la zona, igual en ambos lados');
    else if (s.estiramiento?.modo === 'local') detalle.push('contraste ajustado a la zona solo en este lado');
    else if (s.estiramiento) detalle.push('contraste fijo');
    if (s.nubes_pct > 0) detalle.push('zonas sin dato por nubes (fondo oscuro)');
    if (s.incompleto) detalle.push('La API GEE no respondió al ampliar la ventana');
    return {
      estado: 'ok', tiles: [s.tile_url], maxzoom: 20, atribucion: 'Copernicus Sentinel-2 vía Google Earth Engine · Cloud Score+', ir,
      linea: partes.join(' · '), detalle: detalle.join(' · '),
    };
  }
  // NDVI t0/t1 (endpoint existente, ventana [fecha, fecha + 15 días])
  const linea = `${nombre} · NDVI Sentinel-2`;
  if (cmp.ndvi === undefined) return { estado: 'cargando', linea, detalle: 'Cargando teselas NDVI…' };
  const n = cmp.ndvi && cmp.ndvi[ref];
  if (!n || n.estado !== 'ok') return { estado: 'sin', linea, detalle: (n && n.mensaje) || `Sentinel-2: ${SIN_DATOS}` };
  const v = n.ventana || {};
  return {
    estado: 'ok', tiles: [n.tile_url], maxzoom: 20, atribucion: 'Copernicus Sentinel-2 vía Google Earth Engine', ndvi: true,
    linea: `${linea} · ventana ${fechaCL(v.inicio)} a ${fechaCL(v.fin)} · 10 m`,
    detalle: `${n.images} imagen(es), mediana · no es color verdadero`,
  };
}

function opcionesFuente(l) {
  return Object.entries(FUENTES_CMP)
    .filter(([k]) => l === 'b' || k !== 'google')
    .map(([k, etq]) => `<option value="${k}"${cmp.lados[l].fuente === k ? ' selected' : ''}>${esc(etq)}</option>`).join('');
}

function opcionesCapturaLado(l) {
  const { ref } = LADOS[l];
  return cmp.capturas.map((c, j) => `<option value="${j}"${j === cmp.lados[l].captura ? ' selected' : ''}>captura ${fechaCL(c.fecha)} (${esc(difDias(c.fecha, cmp.consulta[ref], ref))}) · ${esc(textoRes(c.resolucion))}</option>`).join('');
}

// Candidatas Sentinel-2 del lado (fecha · % nubes sobre el polígono), para elegir otra escena a mano.
function selectorEscenas(l) {
  const { ref, nombre } = LADOS[l];
  const f = cmp.lados[l].fuente;
  if (!COMBINACION_CMP[f] || cmp.sentinelCargando[f].has(ref)) return '';
  const s = cmp.sentinel[f] && cmp.sentinel[f][ref];
  if (!s) return '';
  const cands = s.candidatas || [];
  if (cands.length < 2) return '';
  const actual = s.fecha_escena || s.escena_pedida;  // si la escena elegida no cargó, sigue marcada para poder cambiarla
  const opciones = cands.map((c) => `<option value="${esc(c.fecha)}"${c.fecha === actual ? ' selected' : ''}>${fechaCL(c.fecha)} · ${esc(pctNubes(c.nubes_pct))} nubes</option>`).join('');
  return `<select class="cmp-sel" data-escena="${l}" aria-label="Escena Sentinel-2 ${nombre} (fecha y nubes sobre el polígono)">${opciones}</select>`;
}

function pintarEtiqueta(l, info) {
  const lado = cmp.lados[l];
  const capturas = lado.fuente === 'wayback' && cmp.capturas && cmp.capturas.length > 1
    ? `<select class="cmp-sel" data-captura="${l}" aria-label="Fecha de captura ${LADOS[l].nombre}">${opcionesCapturaLado(l)}</select>` : selectorEscenas(l);
  $(`cmp-etq-${l}`).innerHTML = `
    <div class="cmp-etq-linea">${esc(info.linea)}</div>
    <div class="cmp-etq-dif">${esc(info.detalle || '')}</div>
    <div class="cmp-etq-ctrl">
      <select class="cmp-sel" data-fuente="${l}" aria-label="Fuente de imagen ${LADOS[l].nombre}">${opcionesFuente(l)}</select>
      ${capturas}
    </div>`;
  const msg = $(`cmp-msg-${l}`);
  msg.hidden = info.estado === 'ok';
  msg.innerHTML = info.estado === 'cargando'
    ? `<span class="cmp-spin" aria-hidden="true"></span>${esc(info.detalle)}`
    : `<b>Sin imagen para esta fecha</b><br>${esc(info.detalle || '')}`;
}

function pintarLado(l) {
  const m = cmp.mapas[l];
  const info = infoLado(l);
  const clave = info.estado === 'ok' ? info.tiles[0] : '';
  if (m.imgClave !== clave) {
    if (m.getLayer('img')) m.removeLayer('img');
    if (m.getSource('img')) m.removeSource('img');
    if (clave) {
      m.addSource('img', { type: 'raster', tileSize: 256, maxzoom: info.maxzoom, tiles: info.tiles, attribution: info.atribucion });
      m.addLayer({ id: 'img', type: 'raster', source: 'img' }, 'poli-halo');
    }
    m.imgClave = clave;
  }
  pintarEtiqueta(l, info);
  return info;
}

function pintarComparador() {
  if (!cmp.mapas || cmp.i === null) return;
  const infos = { a: pintarLado('a'), b: pintarLado('b') };
  const { a, b } = cmp.lados;
  document.querySelectorAll('[data-cmp-fuente]').forEach((btn) => {
    const activo = a.fuente === btn.dataset.cmpFuente && b.fuente === btn.dataset.cmpFuente;
    btn.classList.toggle('activo', activo);
    btn.setAttribute('aria-pressed', String(activo));
  });
  $('cmp-leyenda-ndvi').hidden = !(infos.a.ndvi || infos.b.ndvi);
  $('cmp-leyenda-ir').hidden = !(infos.a.ir || infos.b.ir);
  const usaWayback = a.fuente === 'wayback' || b.fuente === 'wayback';
  const misma = a.fuente === 'wayback' && b.fuente === 'wayback' && cmp.capturas?.length && a.captura === b.captura;
  const ninguna = usaWayback && cmp.capturas && !cmp.capturas.length;
  const aviso = $('cmp-aviso');
  const auto = cmp.autoSentinel && (COMBINACION_CMP[a.fuente] || COMBINACION_CMP[b.fuente]);
  if (auto) {
    aviso.hidden = false;
    aviso.innerHTML = `<span>${esc(cmp.autoSentinel)}</span>
    <button type="button" class="btn-primario" id="cmp-volver-wayback">Ver foto de archivo igual</button>`;
    $('cmp-volver-wayback').addEventListener('click', () => { cmp.autoSentinel = null; usarFuente('wayback'); });
    return;
  }
  aviso.hidden = !(misma || ninguna);
  if (aviso.hidden) return;
  aviso.innerHTML = `<span>${misma ? 'No hay foto de alta resolución distinta entre t0 y t1; usa Sentinel-2.' : 'No hay foto de alta resolución con fecha en este punto; usa Sentinel-2.'}</span>
    <button type="button" class="btn-primario" id="cmp-usar-sentinel">Usar Sentinel-2</button>`;
  $('cmp-usar-sentinel').addEventListener('click', () => usarFuente('sentinel'));
}

// f: 'sentinel' (color real) o 'infrarrojo'. El backend iguala el contraste de t0 y t1.
async function cargarSentinelCmp(pedido, f) {
  const r = estado.resultados[cmp.i];
  const c = cmp.consulta;
  const serie = ++cmp.sentinelSerie[f];
  const elegidas = ['t0', 't1'].filter((ref) => cmp.escenas[ref]).map((ref) => `&escena_${ref}=${cmp.escenas[ref]}`).join('');
  let d;
  try {
    d = await getJSON(`api/v1/comparar/sentinel?region=${encodeURIComponent(c.region)}&id=${encodeURIComponent(r.id)}&t0=${c.t0}&t1=${c.t1}&cloud=${encodeURIComponent(c.cloud || 80)}&combinacion=${COMBINACION_CMP[f]}${elegidas}`);
  } catch (err) {
    d = { estado: 'sin_datos', mensaje: `Sentinel-2: ${SIN_DATOS}` };
  }
  if (pedido !== cmp.pedido || serie !== cmp.sentinelSerie[f]) return;
  cmp.sentinel[f] = d;
  cmp.sentinelCargando[f].clear();
  pintarComparador();
}

// Escena elegida a mano en un lado (vale para color real e infrarrojo): se
// vuelven a pedir las combinaciones en uso (el otro lado sale de la cache del
// backend); las que no están en uso se olvidan y se piden al usarlas.
function elegirEscena(l, fecha) {
  const { ref } = LADOS[l];
  cmp.escenas[ref] = fecha;
  const usadas = new Set([cmp.lados.a.fuente, cmp.lados.b.fuente]);
  Object.keys(COMBINACION_CMP).forEach((f) => {
    if (usadas.has(f)) {
      cmp.sentinelCargando[f].add(ref);
      cargarSentinelCmp(cmp.pedido, f);
    } else {
      cmp.sentinelSerie[f] += 1;  // descarta una respuesta en camino con la escena anterior
      cmp.sentinel[f] = null;
      cmp.pidiendo.delete(f);
    }
  });
}

async function cargarNdviCmp(pedido) {
  const d = await datosNdvi(cmp.consulta);
  if (pedido !== cmp.pedido) return;
  cmp.ndvi = d;
  pintarComparador();
}

// Pide solo los datos de las fuentes elegidas (Sentinel y NDVI se piden al usarlas).
function cargarFuentesCmp() {
  const usadas = new Set([cmp.lados.a.fuente, cmp.lados.b.fuente]);
  Object.keys(COMBINACION_CMP).forEach((f) => {
    if (usadas.has(f) && !cmp.pidiendo.has(f)) {
      cmp.pidiendo.add(f);
      cargarSentinelCmp(cmp.pedido, f);
    }
  });
  if (usadas.has('ndvi') && !cmp.pidiendo.has('ndvi')) {
    cmp.pidiendo.add('ndvi');
    cargarNdviCmp(cmp.pedido);
  }
}

function usarFuente(fuente) {
  ['a', 'b'].forEach((l) => { cmp.lados[l].fuente = fuente; });
  cargarFuentesCmp();
  pintarComparador();
}

async function cargarWaybackCmp(pedido) {
  const r = estado.resultados[cmp.i];
  const c = cmp.consulta;
  const desde = `${Number(c.t0.slice(0, 4)) - WAYBACK_ANIOS_ATRAS}${c.t0.slice(4)}`;
  let caps;
  try {
    caps = await capturasWaybackPunto(r.centroide.lon, r.centroide.lat, desde, (n, total) => {
      if (pedido !== cmp.pedido) return;
      cmp.avance = `${n}/${total} versiones`;
      ['a', 'b'].forEach((l) => { if (cmp.lados[l].fuente === 'wayback') pintarEtiqueta(l, infoLado(l)); });
    });
  } catch (err) {
    caps = [];
  }
  if (pedido !== cmp.pedido) return;
  cmp.capturas = caps;
  const elegidas = elegirCapturas(caps, c.t0, c.t1);
  cmp.lados.a.captura = elegidas.a;
  cmp.lados.b.captura = elegidas.b;
  // Sin foto de alta resolución distinta entre t0 y t1: pasa solo a Sentinel-2
  // (solo si el usuario no cambió la fuente mientras se buscaba).
  const sinFotos = !caps.length;
  const misma = caps.length && elegidas.a === elegidas.b;
  const intactos = cmp.lados.a.fuente === 'wayback' && cmp.lados.b.fuente === 'wayback';
  if ((sinFotos || misma) && intactos) {
    cmp.autoSentinel = sinFotos
      ? 'No hay foto de alta resolución en este punto: se muestra Sentinel-2.'
      : 'No hay foto de alta resolución distinta entre t0 y t1: se muestra Sentinel-2.';
    usarFuente('sentinel');
    return;
  }
  pintarComparador();
}

async function abrirComparador(i) {
  const r = estado.resultados[i];
  if (!r) return;
  const pedido = ++cmp.pedido;
  Object.assign(cmp, {
    i,
    consulta: estado.consulta || consultaActual(),
    lados: { a: { fuente: 'wayback', captura: null }, b: { fuente: 'wayback', captura: null } },
    capturas: null,
    avance: '',
    sentinel: porCombinacion(() => null),
    escenas: { t0: null, t1: null },
    sentinelCargando: porCombinacion(() => new Set()),
    ndvi: undefined,
    pidiendo: new Set(),
    autoSentinel: null,
    pct: 50,
  });
  if ($('comparador').hidden) cmp.volverFoco = document.activeElement;
  $('comparador').hidden = false;
  $('cmp-titulo').textContent = `Antes / después · polígono ${i + 1} · CIREN ${r.id}`;
  $('cmp-fechas').textContent = `t0 ${fechaCL(cmp.consulta.t0)} · t1 ${fechaCL(cmp.consulta.t1)} · ${infoEstado(estadoDe(r)).etq}`;
  $('cmp-cerrar').focus();
  await asegurarMapasCmp();
  if (pedido !== cmp.pedido) return;
  Object.values(cmp.mapas).forEach((m) => {
    m.resize();
    m.getSource('poli').setData({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: r.geometry, properties: {} }] });
  });
  cmp.mapas.a.fitBounds(cajaGeometria(r.geometry), { padding: 60, maxZoom: 17, animate: false });
  aplicarCortina();
  pintarComparador();
  cargarWaybackCmp(pedido);
}

function cerrarComparador() {
  if ($('comparador').hidden) return;
  cmp.pedido += 1;
  cmp.i = null;
  $('comparador').hidden = true;
  if (cmp.volverFoco && document.contains(cmp.volverFoco)) cmp.volverFoco.focus();
}

$('cmp-cerrar').addEventListener('click', cerrarComparador);
$('comparador').addEventListener('click', (ev) => { if (ev.target === $('comparador')) cerrarComparador(); });
document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && !$('comparador').hidden) cerrarComparador(); });
document.querySelectorAll('[data-cmp-fuente]').forEach((btn) => btn.addEventListener('click', () => usarFuente(btn.dataset.cmpFuente)));
$('cmp-mapas').addEventListener('change', (ev) => {
  const t = ev.target;
  if (t.dataset.fuente) {
    cmp.lados[t.dataset.fuente].fuente = t.value;
    cargarFuentesCmp();
  } else if (t.dataset.captura) {
    cmp.lados[t.dataset.captura].captura = Number(t.value);
  } else if (t.dataset.escena) {
    elegirEscena(t.dataset.escena, t.value);
  } else {
    return;
  }
  pintarComparador();
});

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
