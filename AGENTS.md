# per_lemu2.0

Deteccion de cambios de cobertura forestal (corta no autorizada) cruzando
NDVI Sentinel-2 (per_api_gee), uso de suelo CIREN, predios rurales CIREN
(rol SII), planes de manejo SAFF (per_api_saff) y rodales SIGCRA
(per_api_sigcra).

- `app/`: backend FastAPI. `clientes/` = un cliente HTTP por fuente externa;
  `logica/` = orquestacion y reglas de cruce (sin tocar directamente las APIs
  externas desde `main.py`). `logica/geo.py` = UTM->WGS84, point-in-polygon y
  haversine en Python puro (sin pyproj/shapely).
- `web/`: front estatico (`index.html` + `app.css` + `app.js`, MapLibre
  vendorizado). Banner UIA en `web/banner-conaf-uia.jpg` (copia de
  `coipo_master_produccion/identidad/banner3.jpg`). Rutas relativas: la app
  vive bajo `/lab/lemu2-0/`.
- Push a `main` despliega en Forge LAB: `/lab/lemu2-0/`.
- Region piloto: Panguipulli (`REGIONES` en `orquestador.py`: comuna CIREN/SAFF,
  `comudere` SUBDERE, capa CIREN de uso, capa predial y bbox). Ya no hay puntos fijos: se evaluan todos los poligonos
  de bosque CIREN de la comuna.
- Deteccion (`/api/v1/deteccion`): poligonos CIREN `uso='Bosques'` de la
  comuna con `superf_ha >= min_ha` (def. 1 ha) -> NDVI medio t0/t1 por
  poligono en per_api_gee `/v1/zonal/ndvi-change`, lotes de <= 1000,
  concurrencia 3, timeout 180 s. Corre en segundo plano: el endpoint responde
  202 `{estado:'calculando', avance:'3/6 lotes'}` y el front repite cada 5 s.
  NDVI cacheado 6 h por (region, t0, t1, cloud, min_ha); 2 min si fallo GEE.
  Solo se listan los poligonos con cambio (delta mas negativo primero); los
  demas solo se cuentan.
- Zonas CIREN (`logica/zonas_ciren.py`): consulta paginada (maxRecordCount del
  servicio, `maxAllowableOffset=0.0002`, `geometryPrecision=5`), cache en
  memoria y en `datos_cache/` (gitignore) por 7 dias, precarga al arrancar.
  Panguipulli: 5.995 poligonos / 227.005 ha (5.130 / 226.500 ha con >= 1 ha),
  ~6 MB en disco.
- Predios CIREN (`logica/predios_ciren.py`): todos los predios de la comuna
  (`comudere='14108'`) de `IDEMINAGRI/PROPIEDADES_RURALES` capa 11 (Los Rios,
  CIREN 2018), paginas de 2.000, `maxAllowableOffset=0.0002`, cache en memoria
  y `datos_cache/` 7 dias, precarga al arrancar. El predio de un punto se
  busca local (grilla de 0,02 grados + point-in-polygon; si la simplificacion
  traslapa, gana el mas chico). Panguipulli: 7.398 predios (7.156 con rol
  numerico), ~2,9 MB en disco; de los 5.130 centroides de bosque >= 1 ha,
  5.099 caen en un predio y 3.978 en uno con rol numerico (el resto en
  rotulos como «PARQUE NACIONAL VILLARRICA» o «VP-II»).
- Rol (`logica/rol.py`): CIREN trae `rol` como «manzana-predio» y la comuna
  aparte; en SAFF `Rol Avaluo` es texto libre (`COALESCE(VISTA_PREDIO.PREDIO_ROL,
  VISTA_TRAMITACION.ROL_AVALUO, ROL.ROL_NUMERO)` en per_api_saff). Se
  normaliza: sin espacios, guion o barra como separador, sin ceros a la
  izquierda (`00209-00058` -> `209-58`); un prefijo de 5 digitos se toma como
  codigo de comuna y debe coincidir con `comudere`; varios roles por campo se
  aceptan; la comuna se compara sin tildes. Rotulos sin numero (`SR`, rios,
  rutas) no son rol.
- Bases del mapa: Esri (defecto), Bing (protocolo `bing://` -> quadkey),
  Google (`lyrs=s`) y OSM. Bing y Google solo para comparacion en el piloto
  interno. Vista por URL: `?base=sat|bing|google|osm&ciren=0&predios=1#zoom/lat/lon`.
- Capa «Predios rurales (CIREN 2018)»: raster `export` con `dynamicLayers`
  (contorno amarillo, sin relleno ni rotulos), apagada por defecto. Con la
  capa encendida, clic en el mapa -> `/api/v1/predio/punto` (rol, comuna,
  `planes_saff` del rol y contorno exacto consultado a CIREN).
- Indice de deteccion: NDVI (no NDBI — NDBI mide urbanizacion, no perdida de
  vegetacion; ver nota en `app/logica/cambio_vegetacion.py`).
- Solo datos reales. Si una fuente falla se muestra «Sin datos por el
  momento», nunca datos de muestra.

## Fuentes

| Fuente | Uso | Endpoint |
|---|---|---|
| per_api_gee | NDVI medio por poligono t0/t1 y teselas NDVI t0/t1 (ventana de 15 dias; teselas cache 30 min) | `/v1/zonal/ndvi-change`, `/v1/tiles/ndvi` |
| CIREN (publico) | Poligonos de bosque de la comuna (zonas de deteccion), capa raster, leyenda, popup por punto | `USOS_DE_LA_TIERRA__CONAF/MapServer` (capa 12 = Los Rios, CONAF 2024) |
| CIREN / IDE Minagri (publico) | Predios rurales (deslindes SII) de la comuna con rol; capa raster y popup | `IDEMINAGRI/PROPIEDADES_RURALES/MapServer` (capa 11 = Los Rios, CIREN 2018) |
| per_api_saff | Planes de manejo de los ultimos 10 anios, filtrados por `Comuna_Predio` (cache 6 h por comuna, tramos de 1 anio) | `/v1/historico/pm` |
| per_api_sigcra | Rodales (GeoJSON 4326) de los 5 primeros planes de la ficha (los del rol primero, luego los mas cercanos) | `/v1/bosque-co2/rodales?solicitud=` |
| Esri Wayback (front) | Imagen de archivo real con fecha de captura | `waybackconfig.json` + `identify` de metadata |

Datos personales SAFF: `app/clientes/saff.py` deja solo `CAMPOS_PUBLICOS`
(lista blanca) apenas llega la respuesta; ningun nombre/RUT/correo/sexo/etnia
de propietarios llega a la cache ni al front.

Ubicacion de planes: SAFF solo trae el punto de referencia del predio en UTM
(`Norte01`/`Este01`). Si `DATUM01` no declara huso se prueban 18 y 19, se
descartan los que caen fuera de Chile continental y, si quedan ambos, gana el
mas cercano al centro de la region. Datum distinto de WGS84/SIRGAS se marca
«ubicacion aproximada». La cercania es por ese punto, no por el poligono.

## Regla de estado del poligono

Solo para poligonos de bosque CIREN con caida de NDVI medio
(`delta <= -0.15`, `cambio_vegetacion.UMBRAL_CAIDA_NDVI`). Se evalua en el
CENTROIDE del poligono (si el centroide cae fuera, p. ej. forma en C, un punto
interior sobre la misma latitud): predio CIREN que lo contiene, planes SAFF
con el mismo rol y, de respaldo, planes a <= 3 km por punto de referencia
(`lista_negra.evaluar`). «Plan aprobado» = `Estdo Solicitud` contiene «APROB»,
o hay `Nro.Resolucion` y `Super.Aprobada` > 0, con `Fec.Resolucion` <= t1
(`ficha_punto.plan_aprobado`). En este orden:

1. `sin_datos`: SAFF aun no cargado o caido.
2. `en_regla` (solo ficha): el centroide cae dentro de un rodal SIGCRA de un
   plan aprobado a t1 (evidencia adicional; vale aunque no haya predio).
3. `sin_datos`: predios CIREN no cargados/caidos, o sin predio CIREN en el
   centroide.
4. `en_regla`: hay un plan aprobado a t1 con el MISMO rol del predio
   («Plan X aprobado para el rol 209-58»).
5. `revisar`: hay planes con ese rol pero ninguno aprobado a t1.
6. `revisar`: sin plan para el rol (o predio con rol no numerico) pero hay
   planes a <= 3 km («sin plan para el rol; hay planes cercanos»). Si SIGCRA
   no respondio se agrega al motivo.
7. `alerta`: ni plan del rol ni planes a <= 3 km.

- Listado (barato, con las cargas SAFF y de predios en memoria; sin SIGCRA):
  aplica la regla sin el paso 2, asi que ya puede marcar `en_regla` por rol.
  Cada tarjeta muestra el rol del predio.
- Ficha (`evaluar_ficha`, al seleccionar el poligono): muestra «Predio rol
  209-58 · Panguipulli (CIREN 2018)», dibuja el predio, lista primero los
  planes del rol y luego los cercanos, consulta SIGCRA y el front actualiza el
  estado en mapa, lista y conteos.
- Poligonos sin NDVI en t0 o t1 (nubes, sin imagen, lote de GEE caido) se
  cuentan como `sin_ndvi`; si fallan todos los lotes la deteccion completa es
  `sin_datos` («Sin datos por el momento»).

## Pendientes

- Verificar contra datos reales de SAFF los valores de `DATUM01` (formato del
  huso) y la cantidad de planes por comuna; desde el PC de desarrollo las 3
  APIs de Forge LAB no son alcanzables.
- La ruta de rodales de SIGCRA usada es `/v1/bosque-co2/rodales` (la que existe
  en per_api_sigcra); `/v1/rodales` no existe.
- Ampliar regiones (hoy solo Panguipulli). Para comunas con > 2.500 poligonos
  por lote o capas CIREN de otras regiones revisar `ciren_layer` y tiempos.
- `/v1/zonal/ndvi-change` es nuevo en per_api_gee (version 2026.10.08.1): debe
  estar desplegado antes que esta version de Lemu; validar tiempos reales por
  lote (6 lotes en Panguipulli) y ajustar `TAMANO_LOTE`/timeout si GEE corta.
- El `en_regla` por rodal SIGCRA solo aparece tras abrir la ficha; el
  listado solo marca `en_regla` por rol.
- Verificar con datos reales el formato de `Rol Avaluo` en SAFF (el codigo
  no lo fija; la normalizacion acepta varias variantes) y si un prefijo de
  comuna, de venir, es SUBDERE o SII (hoy se compara con `comudere` SUBDERE;
  un codigo SII distinto descartaria el plan).
- La capa predial es CIREN 2018: predios subdivididos despues no estan.
- Predios con rol no numerico (parques, «VP-…», «SR») solo se evaluan por
  cercania.
- per_api_gee solo entrega NDVI coloreado, no color verdadero Sentinel-2.
- Wayback: si t0, t1 y la ultima version comparten la misma captura en el
  punto, solo hay una fecha y no se puede comparar.
