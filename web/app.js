'use strict';

const CIREN_URL = 'https://esri.ciren.cl/server/rest/services/USOS_DE_LA_TIERRA__CONAF/MapServer';
const WAYBACK_CONFIG_URL = 'https://s3-us-west-2.amazonaws.com/config.maptiles.arcgis.com/waybackconfig.json';
const SIN_DATOS = 'Sin datos por el momento';
const ZOOM_MEJORAR = 14;

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
  resultados: [],
  sel: null,
  marcadores: [],
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

function infoEstado(e) {
  return ESTADOS[e] || { etq: e || 'Sin datos', color: '#5d6b62' };
}

async function getJSON(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

/* ---------- mapa ---------- */

const map = new maplibregl.Map({
  container: 'map',
  attributionControl: false,
  center: [-72.2, -39.64],
  zoom: 9,
  maxZoom: 19,
  style: {
    version: 8,
    sources: {
      sat: {
        type: 'raster', tileSize: 256, maxzoom: 19,
        tiles: ['https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}'],
        attribution: 'Imagen © Esri, Maxar, Earthstar Geographics',
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
      rodales: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
      referencias: { type: 'geojson', data: { type: 'FeatureCollection', features: [] } },
    },
    layers: [
      { id: 'base-sat', type: 'raster', source: 'sat' },
      { id: 'base-osm', type: 'raster', source: 'osm', layout: { visibility: 'none' } },
      // Wayback se inserta aquí (antes de 'tope-base'); NDVI y CIREN antes de 'tope-capas'.
      { id: 'tope-base', type: 'background', paint: { 'background-opacity': 0 } },
      { id: 'tope-capas', type: 'background', paint: { 'background-opacity': 0 } },
      { id: 'rotulos', type: 'raster', source: 'rotulos' },
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
  map.setLayoutProperty('base-sat', 'visibility', base === 'sat' ? 'visible' : 'none');
  map.setLayoutProperty('base-osm', 'visibility', base === 'osm' ? 'visible' : 'none');
  map.setLayoutProperty('rotulos', 'visibility', base === 'sat' ? 'visible' : 'none');
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
  }, 'tope-capas');
}

$('ciren-on').addEventListener('change', () => {
  if (map.getLayer('ciren')) map.setLayoutProperty('ciren', 'visibility', $('ciren-on').checked ? 'visible' : 'none');
});
$('ciren-opacidad').addEventListener('input', () => {
  $('ciren-opacidad-val').textContent = `${$('ciren-opacidad').value} %`;
  if (map.getLayer('ciren')) map.setPaintProperty('ciren', 'raster-opacity', cirenOpacidad());
});

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

let ignorarClickHasta = 0;
map.on('click', async (ev) => {
  if (Date.now() < ignorarClickHasta || !$('ciren-on').checked || !estado.region) return;
  if (map.getLayer('referencias') && map.queryRenderedFeatures(ev.point, { layers: ['referencias'] }).length) return;
  const { lng, lat } = ev.lngLat;
  const popup = new maplibregl.Popup({ maxWidth: '280px' }).setLngLat(ev.lngLat).setHTML('<span class="muted">Consultando CIREN…</span>').addTo(map);
  try {
    const d = await getJSON(`api/v1/ciren/punto?region=${encodeURIComponent(estado.region)}&lat=${lat.toFixed(6)}&lon=${lng.toFixed(6)}`);
    if (d.estado === 'ok') {
      popup.setHTML(`<b>${esc(d.uso)}</b><br>${esc(d.subuso || '')}${d.uso_tierra ? `<br><span class="muted">${esc(d.uso_tierra)}</span>` : ''}
        <br>Superficie polígono: ${ha(d.superf_ha)}<br>Comuna: ${esc(d.nom_com || '—')}
        <br><span class="muted chico">CIREN capa ${esc(d.capa?.id)}${d.capa?.anio ? ` · CONAF ${esc(d.capa.anio)}` : ''}</span>`);
    } else if (d.estado === 'sin_poligono') {
      popup.setHTML(`<span class="muted">${esc(d.mensaje)}</span>`);
    } else {
      popup.setHTML(`<span class="muted">CIREN: ${SIN_DATOS}</span>`);
    }
  } catch (err) {
    popup.setHTML(`<span class="muted">CIREN: ${SIN_DATOS}</span>`);
  }
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
    sel.innerHTML = data.items.map((r) => `<option value="${esc(r.id)}">${esc(r.nombre)} (${r.puntos} puntos piloto)</option>`).join('');
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
  const [w, s, e, n] = r.bbox;
  map.fitBounds([[w, s], [e, n]], { padding: 20, animate: false });
  if (estado.ndviModo) aplicarNdvi(estado.ndviModo);
}

$('region').addEventListener('change', cambiarRegion);
['t0', 't1'].forEach((id) => $(id).addEventListener('change', () => { if (estado.ndviModo) aplicarNdvi(estado.ndviModo); }));

/* ---------- detección y lista de puntos ---------- */

function limpiarMarcadores() {
  estado.marcadores.forEach((m) => m.remove());
  estado.marcadores = [];
}

function estadoDe(r) {
  return (r.lista_negra && r.lista_negra.estado) || 'sin_datos';
}

function f3(v) {
  return v === null || v === undefined ? '—' : Number(v).toLocaleString('es-CL', { minimumFractionDigits: 3, maximumFractionDigits: 3 });
}

function tarjetaPunto(r, i) {
  const e = estadoDe(r);
  const inf = infoEstado(e);
  const uso = r.uso_suelo && (r.uso_suelo.subuso || r.uso_suelo.uso);
  const motivo = r.lista_negra && r.lista_negra.motivo;
  const el = document.createElement('button');
  el.type = 'button';
  el.className = 'punto';
  el.dataset.i = i;
  el.innerHTML = `
    <span class="n" style="background:${inf.color}">${i + 1}</span>
    <span class="fila"><span class="chip ${esc(e)}">${esc(inf.etq)}</span><span class="coords">${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}</span></span>
    <span class="fila ndvi">NDVI t0 <b>${f3(r.ndvi_t0)}</b> · t1 <b>${f3(r.ndvi_t1)}</b> · Δ <b style="color:${r.cambio_detectado ? '#c0392b' : 'inherit'}">${f3(r.delta_ndvi)}</b></span>
    <span class="fila motivo">${uso ? `CIREN: ${esc(uso)}` : ''}${uso && motivo ? ' · ' : ''}${motivo ? esc(motivo) : ''}</span>`;
  el.addEventListener('click', () => seleccionar(i));
  return el;
}

function marcadorPunto(r, i) {
  const inf = infoEstado(estadoDe(r));
  const el = document.createElement('div');
  el.className = 'marcador';
  el.style.background = inf.color;
  el.textContent = String(i + 1);
  el.title = `Punto ${i + 1}: ${inf.etq}`;
  el.addEventListener('click', (ev) => {
    ev.stopPropagation();
    ignorarClickHasta = Date.now() + 400;
    seleccionar(i);
  });
  return new maplibregl.Marker({ element: el }).setLngLat([r.lon, r.lat]).addTo(map);
}

async function detectar(ev) {
  ev.preventDefault();
  const c = consultaActual();
  const lista = $('lista');
  if (!c.t0 || !c.t1 || c.t0 >= c.t1) {
    lista.innerHTML = '<p class="msg">La fecha t0 debe ser anterior a t1.</p>';
    return;
  }
  $('btn-detectar').disabled = true;
  $('resumen').hidden = true;
  lista.innerHTML = '<p class="msg">Calculando NDVI y cruzando fuentes…</p>';
  limpiarMarcadores();
  deseleccionar();
  try {
    const resp = await fetch(`api/v1/deteccion?region=${encodeURIComponent(c.region)}&t0=${c.t0}&t1=${c.t1}&cloud=${c.cloud}`);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.error) {
      lista.innerHTML = `<p class="msg">No se pudo calcular: ${esc(data.error || data.detail || `HTTP ${resp.status}`)}</p>`;
      return;
    }
    estado.consulta = c;
    estado.resultados = data.resultados || [];
    $('num-evaluados').textContent = data.puntos_evaluados ?? 0;
    $('num-alertas').textContent = data.alertas ?? 0;
    $('num-revisar').textContent = data.revisar ?? 0;
    $('resumen').hidden = false;
    if (!estado.resultados.length) {
      lista.innerHTML = '<p class="msg">Sin resultados para este rango.</p>';
      return;
    }
    lista.innerHTML = '';
    estado.resultados.forEach((r, i) => {
      lista.appendChild(tarjetaPunto(r, i));
      estado.marcadores.push(marcadorPunto(r, i));
    });
    const b = new maplibregl.LngLatBounds();
    estado.resultados.forEach((r) => b.extend([r.lon, r.lat]));
    map.fitBounds(b, { padding: 80, maxZoom: 13 });
  } catch (err) {
    lista.innerHTML = `<p class="msg">${SIN_DATOS} (error de conexión).</p>`;
  } finally {
    $('btn-detectar').disabled = false;
  }
}

$('form-controles').addEventListener('submit', detectar);

/* ---------- selección y ficha ---------- */

function deseleccionar() {
  estado.sel = null;
  $('ficha').hidden = true;
  limpiarFichaMapa();
  cerrarWayback();
  actualizarBotonMejorar();
}

function seleccionar(i) {
  const r = estado.resultados[i];
  if (!r) return;
  estado.sel = i;
  document.querySelectorAll('.punto').forEach((el) => el.classList.toggle('sel', Number(el.dataset.i) === i));
  estado.marcadores.forEach((m, j) => m.getElement().classList.toggle('sel', j === i));
  cerrarWayback();
  map.flyTo({ center: [r.lon, r.lat], zoom: 16 });
  cargarFicha(i);
}

function limpiarFichaMapa() {
  const vacio = { type: 'FeatureCollection', features: [] };
  if (map.getSource('rodales')) map.getSource('rodales').setData(vacio);
  if (map.getSource('referencias')) map.getSource('referencias').setData(vacio);
}

function filaDato(dt, dd) {
  return `<dt>${dt}</dt><dd>${dd}</dd>`;
}

function tarjetaPlan(p) {
  const u = p.ubicacion || {};
  const sig = p.sigcra || {};
  let rodales = 'No consultado (solo los 5 planes más cercanos)';
  if (sig.estado === 'ok') rodales = `${sig.rodales} rodal(es)${p.punto_en_rodal ? ` · el punto está dentro del rodal ${esc(p.punto_en_rodal)}` : ''}`;
  else if (sig.estado === 'sin_datos') rodales = `SIGCRA: ${SIN_DATOS}`;
  const ubic = `${num(p.dist_km, 2)} km${u.aproximada ? ' · ubicación aproximada (datum no WGS84/SIRGAS)' : ''}${u.huso_inferido ? ` · huso ${u.huso} inferido` : ''}`;
  return `<article class="plan${p.punto_en_rodal ? ' dentro' : ''}">
    <h3>Plan ${esc(p['Nro.Solicitud'] || '—')} <span class="chip${p.aprobado ? '' : ' sin_datos'}">${esc(p['Estdo Solicitud'] || 'Sin estado')}</span></h3>
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
  if (!f.planes?.length) return 'alerta';
  return '';
}

async function cargarFicha(i) {
  const r = estado.resultados[i];
  const pedido = ++estado.fichaPedido;
  $('ficha').hidden = false;
  $('ficha-titulo').textContent = `Ficha del punto ${i + 1}`;
  const cuerpo = $('ficha-cuerpo');
  cuerpo.innerHTML = '<p class="msg">Buscando planes de manejo (SAFF) y rodales (SIGCRA)…</p>';
  limpiarFichaMapa();
  $('ficha').scrollIntoView({ block: 'nearest' });
  const c = estado.consulta || consultaActual();
  let f;
  try {
    f = await getJSON(`api/v1/punto/ficha?region=${encodeURIComponent(c.region)}&lat=${r.lat}&lon=${r.lon}&radio_km=3&t1=${c.t1}`);
  } catch (err) {
    if (pedido === estado.fichaPedido) cuerpo.innerHTML = `<p class="aviso">${SIN_DATOS}: no se pudo consultar la ficha.</p>`;
    return;
  }
  if (pedido !== estado.fichaPedido) return;

  const avisos = [];
  if (f.sigcra?.estado === 'sin_datos') avisos.push(`SIGCRA (rodales): ${SIN_DATOS}.`);
  if (f.sigcra?.estado === 'parcial') avisos.push('SIGCRA respondió solo para algunos planes.');
  if (f.saff?.truncado) avisos.push('La carga SAFF llegó al máximo de páginas; podría faltar algún plan.');
  const ln = r.lista_negra || {};

  cuerpo.innerHTML = `
    <div class="conclusion ${claseConclusion(f)}">${esc(f.conclusion)}</div>
    ${avisos.map((a) => `<p class="aviso">${esc(a)}</p>`).join('')}
    <p class="chico muted">Estado de la detección: <span class="chip ${esc(estadoDe(r))}">${esc(infoEstado(estadoDe(r)).etq)}</span>${ln.motivo ? ` ${esc(ln.motivo)}` : ''}</p>
    <p class="chico muted">${esc(f.nota_cercania)}${f.saff?.periodo ? ` Planes con resolución entre ${fechaCL(f.saff.periodo.desde)} y ${fechaCL(f.saff.periodo.hasta)}.` : ''}</p>
    <div class="planes">${(f.planes || []).map(tarjetaPlan).join('')}</div>`;

  if (map.getSource('rodales')) map.getSource('rodales').setData(f.rodales || { type: 'FeatureCollection', features: [] });
  const refs = (f.planes || []).filter((p) => p.ubicacion).map((p) => ({
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
    const capturas = (await Promise.all(versiones.map((v) => capturaEnPunto(v, r.lon, r.lat).catch(() => null)))).filter(Boolean);
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
cargarRegiones();
