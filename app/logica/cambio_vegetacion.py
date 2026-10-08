"""Regla de cambio de vegetacion entre t0 y t1 usando NDVI (Sentinel-2).

Nota de diseno: el audio original hablaba de "NDBI", pero NDBI (Normalized
Difference Built-up Index) mide urbanizacion/construccion, no perdida de
vegetacion. Para detectar corta/cambio de cobertura boscosa el indice
correcto es NDVI (o NBR para zonas quemadas); per_api_gee ya expone ambos.
Usamos NDVI por defecto; queda documentado para que Alfredo lo confirme.

El NDVI de cada poligono CIREN es el promedio de la mediana Sentinel-2 de
cada ventana (per_api_gee /v1/zonal/ndvi-change).
"""
from __future__ import annotations

UMBRAL_CAIDA_NDVI = 0.15  # caida absoluta minima para considerar "cambio"


def delta(ndvi_t0: float | None, ndvi_t1: float | None) -> float | None:
    if ndvi_t0 is None or ndvi_t1 is None:
        return None
    return ndvi_t1 - ndvi_t0


def hay_cambio(delta_ndvi: float | None) -> bool:
    return delta_ndvi is not None and delta_ndvi <= -UMBRAL_CAIDA_NDVI
