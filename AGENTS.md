# per_lemu2.0

Deteccion de cambios de cobertura forestal (corta no autorizada) cruzando
4 fuentes: NDVI Sentinel-2 (per_api_gee), uso de suelo CIREN, predios/rodales
(per_api_sigcra) y planes de manejo vigentes (per_api_saff).

- `app/`: backend FastAPI. `clientes/` = un cliente HTTP por fuente externa;
  `logica/` = orquestacion y reglas de cruce (sin tocar directamente las APIs
  externas desde `main.py`).
- `web/`: front estatico (MapLibre + selector de fechas t0/t1 variable).
- Push a `main` despliega en Forge LAB: `/lab/lemu2.0/`.
- Region piloto: Panguipulli (puntos de muestreo fijos en `orquestador.py`,
  REGIONES_PILOTO). Ampliar a mas regiones/grilla real es iteracion futura.
- Indice de deteccion: NDVI (no NDBI — NDBI mide urbanizacion, no perdida de
  vegetacion; ver nota en `app/logica/cambio_vegetacion.py`).
- Datos reales de 3 APIs Forge LAB; CIREN es publico sin clave. Sin datos
  inventados, pero el cruce predio<->codigo_predio aun es parcial (SIGCRA no
  esta conectado end-to-end en esta primera iteracion; `lista_negra.py` recibe
  `codigo_predio=None` para los puntos piloto hasta conectar SIGCRA).
