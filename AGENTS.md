# per_lemu2.0

Deteccion de cambios de cobertura forestal (corta no autorizada) cruzando
NDVI Sentinel-2 (per_api_gee), uso de suelo CIREN, planes de manejo SAFF
(per_api_saff) y rodales SIGCRA (per_api_sigcra).

- `app/`: backend FastAPI. `clientes/` = un cliente HTTP por fuente externa;
  `logica/` = orquestacion y reglas de cruce (sin tocar directamente las APIs
  externas desde `main.py`). `logica/geo.py` = UTM->WGS84, point-in-polygon y
  haversine en Python puro (sin pyproj/shapely).
- `web/`: front estatico (`index.html` + `app.css` + `app.js`, MapLibre
  vendorizado). Banner UIA en `web/banner-conaf-uia.jpg` (copia de
  `coipo_master_produccion/identidad/banner3.jpg`). Rutas relativas: la app
  vive bajo `/lab/lemu2-0/`.
- Push a `main` despliega en Forge LAB: `/lab/lemu2-0/`.
- Region piloto: Panguipulli (puntos fijos en `orquestador.py`,
  `REGIONES_PILOTO`, con comuna SAFF, capa CIREN y bbox).
- Indice de deteccion: NDVI (no NDBI — NDBI mide urbanizacion, no perdida de
  vegetacion; ver nota en `app/logica/cambio_vegetacion.py`).
- Solo datos reales. Si una fuente falla se muestra «Sin datos por el
  momento», nunca datos de muestra.

## Fuentes

| Fuente | Uso | Endpoint |
|---|---|---|
| per_api_gee | NDVI por punto y teselas NDVI t0/t1 (ventana de 15 dias, cache 30 min) | `/v1/query/point`, `/v1/tiles/ndvi` |
| CIREN (publico) | Mascara forestal, capa raster, leyenda, popup por punto | `USOS_DE_LA_TIERRA__CONAF/MapServer` (capa 12 = Los Rios, CONAF 2024) |
| per_api_saff | Planes de manejo de los ultimos 10 anios, filtrados por `Comuna_Predio` (cache 6 h por comuna, tramos de 1 anio) | `/v1/historico/pm` |
| per_api_sigcra | Rodales (GeoJSON 4326) de los 5 planes mas cercanos | `/v1/bosque-co2/rodales?solicitud=` |
| Esri Wayback (front) | Imagen de archivo real con fecha de captura | `waybackconfig.json` + `identify` de metadata |

Datos personales SAFF: `app/clientes/saff.py` deja solo `CAMPOS_PUBLICOS`
(lista blanca) apenas llega la respuesta; ningun nombre/RUT/correo/sexo/etnia
de propietarios llega a la cache ni al front.

Ubicacion de planes: SAFF solo trae el punto de referencia del predio en UTM
(`Norte01`/`Este01`). Si `DATUM01` no declara huso se prueban 18 y 19, se
descartan los que caen fuera de Chile continental y, si quedan ambos, gana el
mas cercano al centro de la region. Datum distinto de WGS84/SIRGAS se marca
«ubicacion aproximada». La cercania es por ese punto, no por el poligono.

## Regla de estado del punto

Solo para puntos con caida de NDVI (`delta <= -0.15`) en uso forestal CIREN
(`app/logica/lista_negra.py`, radio 3 km):

- `sin_datos`: SAFF no respondio; o hay planes cercanos y SIGCRA no respondio
  (total o parcialmente) sin que se haya confirmado el cruce. Tambien si no hay
  NDVI en t0 o t1 (GEE caido o sin imagen).
- `en_regla`: el punto cae dentro de un rodal SIGCRA de un plan aprobado
  (`Estdo Solicitud` contiene «APROB», o hay `Nro.Resolucion` y
  `Super.Aprobada` > 0) con `Fec.Resolucion` <= t1.
- `alerta`: no hay ningun plan SAFF a <= 3 km.
- `revisar`: hay planes cercanos pero el punto no cae en un rodal aprobado a
  t1 (fuera de los rodales, plan no aprobado o sin rodales en SIGCRA).
- Sin caida de NDVI: `sin_cambio`; uso no forestal: `descartado_no_forestal`.

## Pendientes

- Verificar contra datos reales de SAFF los valores de `DATUM01` (formato del
  huso) y la cantidad de planes por comuna; desde el PC de desarrollo las 3
  APIs de Forge LAB no son alcanzables.
- La ruta de rodales de SIGCRA usada es `/v1/bosque-co2/rodales` (la que existe
  en per_api_sigcra); `/v1/rodales` no existe.
- Ampliar regiones / grilla real de puntos (hoy 3 puntos piloto).
- per_api_gee solo entrega NDVI coloreado, no color verdadero Sentinel-2.
- Wayback: si t0, t1 y la ultima version comparten la misma captura en el
  punto, solo hay una fecha y no se puede comparar.
