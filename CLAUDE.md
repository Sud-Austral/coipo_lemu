# coipo_lemu — guía para agentes

Lee primero [`AGENTS.md`](AGENTS.md): es la especificación técnica vigente (reglas de estado, cachés, fuentes, pendientes). Este archivo es el resumen operativo. Visión general en [`README.md`](README.md) e [`insumos/`](insumos/).

## Resumen
FastAPI + front estático. Detecta caída de NDVI en polígonos de bosque CIREN y cruza con predios CIREN (rol) y planes SAFF/SIGCRA. Región única: Panguipulli.

## Estructura
`app/main.py` (rutas) · `app/clientes/` (HTTP por fuente) · `app/logica/` (reglas) · `web/` (front sin build, MapLibre vendorizado) · `tests/`.

## Comandos (VERIFICADOS)
```bash
python -m pytest tests -q      # 73 OK el 2026-10-24
uvicorn app.main:app --port 8080
```
No hay lint ni build del front. No hay `requirements.txt`: las dependencias están fijadas en el `Dockerfile` (fastapi 0.115, uvicorn 0.32, httpx 0.27; pytest 8, pytest-asyncio 0.24 en la etapa de prueba).

## Convenciones
- `main.py` no llama directamente a las APIs externas: pasa por `clientes/` y `logica/`.
- Solo datos reales: si una fuente falla se responde «Sin datos por el momento», jamás datos de muestra. El comparador nunca responde 500.
- Geometría sin pyproj/shapely (`logica/geo.py`).
- Rutas del front relativas (debe funcionar bajo cualquier subruta).
- Índice de detección: NDVI, no NDBI (nota en `cambio_vegetacion.py`).

## Trampas
- **Datos personales SAFF**: `clientes/saff.py` conserva solo `CAMPOS_PUBLICOS` (lista blanca); no agregar nombre/RUT/correo/sexo/etnia de propietarios.
- Secretos solo por variables de entorno (`.env.example` trae nombres); nunca versionar `.env` ni llaves.
- `datos_cache/` está en `.gitignore`; no versionarlo.
- Cambiar `cloud` cambia la clave de caché (recalcula).
- El `Dockerfile` ejecuta los tests en el build: una prueba rota rompe la imagen.
- Ramas: `uat` = UAT, `main` = producción solo por PR aprobado.
- `AGENTS.md` fija umbrales (p. ej. `delta <= -0.15`, `min_ha` 1, nubes 80 %); si cambias código que los define, actualiza ese documento.
