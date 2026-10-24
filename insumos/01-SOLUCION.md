# Solución — cómo está resuelto Lemu 2.0

Detalle fino (umbrales, cachés, comparador) en [`../AGENTS.md`](../AGENTS.md); aquí el mapa. VERIFICADO = visto en código o en la salida de pruebas del 2026-10-24 (73 OK).

## Arquitectura
FastAPI (`app/main.py`, versión 0.4.0) sirve la API y el front estático (`web/`). Sin base de datos: caché en memoria y en `datos_cache/` (7 días para capas CIREN; 6 h SAFF y NDVI; 30 min teselas).

## Componentes
| Pieza | Rol |
|---|---|
| `clientes/gee.py` | API GEE: NDVI zonal t0/t1 con Cloud Score+, teselas NDVI y RGB |
| `clientes/ciren.py`, `logica/zonas_ciren.py`, `logica/predios_ciren.py` | Polígonos de bosque (capa 12, CONAF 2024) y predios (capa 11, CIREN 2018) de CIREN/IDE Minagri |
| `clientes/saff.py`, `logica/rol.py` | Planes de manejo SAFF (10 años por comuna), normalización del rol, lista blanca de campos |
| `clientes/sigcra.py` | Rodales de los planes de la ficha |
| `logica/orquestador.py` | `REGIONES`, trabajo en segundo plano (202 «calculando», el front consulta cada 5 s) |
| `logica/lista_negra.py`, `ficha_punto.py` | Regla de estado del polígono |
| `logica/comparar.py` | Comparador Sentinel-2 antes/después |

## Flujo de datos
Polígonos CIREN (≥ `min_ha`) → NDVI medio t0/t1 por lotes ≤ 1000 → los con `delta <= -0.15` (`cambio_vegetacion.UMBRAL_CAIDA_NDVI`) → predio del centroide → planes SAFF del mismo rol o ≤ 3 km → rodales SIGCRA al abrir la ficha → estado. Polígonos con nubes sobre más de la mitad no entran a la regla (`sin_imagen_limpia`).

## Fuentes y variables
GEE, SAFF y SIGCRA internas (variables `*_BASE_URL` y `*_API_KEY`); CIREN públicas; Esri Wayback desde el front.

## Decisiones (AGENTS.md)
NDVI y no NDBI; geometría en Python puro; solo datos reales; lista blanca de campos SAFF; la geometría exacta se pide a CIREN solo para polígonos con cambio.

## Despliegue
`Dockerfile` multi-etapa (las pruebas corren en el build), puerto 8080, healthcheck `/health`. Ramas `uat` y `main`. El mecanismo de despliegue y la instancia final: no los comprobé.

## Estado y pendientes
Estado: región Panguipulli operativa en código. Pendientes (de AGENTS.md): validar con datos reales SAFF el formato de `DATUM01` y `Rol Avaluo`; ampliar regiones; el `en_regla` por rodal SIGCRA solo aparece tras abrir la ficha; la capa predial es de 2018. No hay CI en el repo.
