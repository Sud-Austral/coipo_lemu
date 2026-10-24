# coipo_lemu — Lemu 2.0: detección de cambios de cobertura forestal

Aplicación web que ayuda a detectar posibles **cortas no autorizadas** de bosque: toma los polígonos de bosque de CIREN de una comuna, mide la caída de NDVI Sentinel-2 entre dos fechas (t0 y t1) y cruza cada polígono con el predio (rol SII) y con los planes de manejo de SAFF para clasificarlo como `alerta`, `revisar`, `en_regla` o `sin_datos`. Usuarios finales exactos: pendiente de confirmar por el equipo (INFERIDO: personal de fiscalización forestal de CONAF; el banner del front es de CONAF-UIA).

Región configurada hoy: **Panguipulli** (única en `REGIONES`, `app/logica/orquestador.py`).

## Stack (VERIFICADO)
- Backend: Python 3.12, FastAPI, uvicorn, httpx (`Dockerfile`). Geometría en Python puro (`app/logica/geo.py`).
- Front estático sin build: `web/` (HTML, CSS, JS, MapLibre vendorizado).
- Sin base de datos: caché en memoria y en `datos_cache/` (ignorado por git).

## Estructura
- `app/main.py`: API y montaje del front en `/`.
- `app/clientes/`: un cliente HTTP por fuente (`gee`, `saff`, `sigcra`, `ciren`).
- `app/logica/`: orquestación y reglas (detección, ficha de punto, estado del polígono, rol, comparador).
- `web/`: interfaz. `tests/`: pruebas pytest.
- `AGENTS.md`: documentación técnica detallada (reglas, cachés, fuentes, pendientes).

## Endpoints (VERIFICADO en `app/main.py`)
`/health`, `/ready` (503 si falla GEE, SIGCRA o SAFF), `/api/v1/regiones`, `/api/v1/deteccion`, `/api/v1/sentinel/tiles`, `/api/v1/comparar/sentinel`, `/api/v1/ciren/punto`, `/api/v1/punto/ficha`, `/api/v1/predio/punto`.

## Configuración
Variables (solo nombres; ver `.env.example`): `GEE_BASE_URL`, `GEE_API_KEY`, `SAFF_BASE_URL`, `SAFF_API_KEY`, `SIGCRA_BASE_URL`, `SIGCRA_API_KEY`; opcional `LEMU_CACHE_DIR` (por defecto `datos_cache`). Sin URL, esa fuente responde «Sin datos por el momento». Las llaves nunca se versionan.

## Ejecución local
```bash
pip install fastapi uvicorn httpx pytest pytest-asyncio   # no hay requirements.txt; versiones en el Dockerfile
uvicorn app.main:app --port 8080
```
Las APIs internas (GEE, SAFF, SIGCRA) no son alcanzables desde un PC de desarrollo (AGENTS.md), así que ahí la app muestra «Sin datos por el momento».

## Pruebas
```bash
python -m pytest tests -q
```
Ejecutado el 2026-10-24: 73 pruebas OK. El `Dockerfile` corre las pruebas en una etapa `prueba` y falla el build si no pasan.

## Despliegue
`Dockerfile` (puerto 8080, `HEALTHCHECK` en `/health`). Ramas según AGENTS.md: `uat` = ambiente UAT, `main` = producción solo vía PR aprobado. No hay `docker-compose` ni workflows de CI en el repo; el mecanismo de despliegue no lo comprobé.

## Más documentación
[`CLAUDE.md`](CLAUDE.md) · [`AGENTS.md`](AGENTS.md) · [`insumos/00-PROBLEMA.md`](insumos/00-PROBLEMA.md) · [`insumos/01-SOLUCION.md`](insumos/01-SOLUCION.md)
