"""Detecta cambio de vegetacion entre t0 y t1 usando NDVI (Sentinel-2).

Nota de diseno: el audio original hablaba de "NDBI", pero NDBI (Normalized
Difference Built-up Index) mide urbanizacion/construccion, no perdida de
vegetacion. Para detectar corta/cambio de cobertura boscosa el indice
correcto es NDVI (o NBR para zonas quemadas); per_api_gee ya expone ambos.
Usamos NDVI por defecto; queda documentado para que Alfredo lo confirme.
"""
from __future__ import annotations

from app.clientes import gee

UMBRAL_CAIDA_NDVI = 0.15  # caida absoluta minima para considerar "cambio"


class CambioVegetacion:
    def __init__(self, lat: float, lon: float, ndvi_t0: float | None, ndvi_t1: float | None):
        self.lat = lat
        self.lon = lon
        self.ndvi_t0 = ndvi_t0
        self.ndvi_t1 = ndvi_t1

    @property
    def delta(self) -> float | None:
        if self.ndvi_t0 is None or self.ndvi_t1 is None:
            return None
        return self.ndvi_t1 - self.ndvi_t0

    @property
    def cambio_detectado(self) -> bool:
        d = self.delta
        return d is not None and d <= -UMBRAL_CAIDA_NDVI

    def to_dict(self) -> dict:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "ndvi_t0": self.ndvi_t0,
            "ndvi_t1": self.ndvi_t1,
            "delta_ndvi": self.delta,
            "cambio_detectado": self.cambio_detectado,
        }


def _extraer_ndvi(respuesta: dict) -> float | None:
    """Extrae el valor NDVI de la respuesta de /v1/query/point."""
    valor = respuesta.get("ndvi")
    if valor is None:
        valores = respuesta.get("values") or {}
        valor = valores.get("ndvi")
    return valor


async def detectar_cambio_punto(
    lat: float,
    lon: float,
    t0_inicio: str,
    t0_fin: str,
    t1_inicio: str,
    t1_fin: str,
    cloud: int = 40,
) -> CambioVegetacion:
    """Consulta NDVI en t0 y t1 para un punto y calcula el cambio."""
    try:
        resp_t0 = await gee.query_point_index(lat, lon, t0_inicio, t0_fin, cloud)
    except gee.GeeError:
        resp_t0 = {}
    try:
        resp_t1 = await gee.query_point_index(lat, lon, t1_inicio, t1_fin, cloud)
    except gee.GeeError:
        resp_t1 = {}
    return CambioVegetacion(lat, lon, _extraer_ndvi(resp_t0), _extraer_ndvi(resp_t1))
