"""Filtra cambios de vegetacion contra la mascara de uso de suelo CIREN.

Descarta cambios que caen en usos no forestales (agricola, urbano, agua,
etc.) para reducir falsos positivos.
"""
from __future__ import annotations

from app.clientes import ciren


class ClasificacionUso:
    def __init__(self, uso: str | None, subuso: str | None, es_forestal: bool):
        self.uso = uso
        self.subuso = subuso
        self.es_forestal = es_forestal

    def to_dict(self) -> dict:
        return {"uso": self.uso, "subuso": self.subuso, "es_forestal": self.es_forestal}


async def clasificar_punto(lat: float, lon: float) -> ClasificacionUso:
    """Determina el uso de suelo CIREN en el punto dado."""
    try:
        capa = await ciren.identify_layer(lon, lat)
    except ciren.CirenError:
        return ClasificacionUso(None, None, False)
    if not capa or capa.get("layer_id") is None:
        return ClasificacionUso(None, None, False)
    try:
        features = await ciren.query_uso_suelo(capa["layer_id"], lon, lat, radio_m=200)
    except ciren.CirenError:
        return ClasificacionUso(None, None, False)
    if not features:
        return ClasificacionUso(None, None, False)
    props = features[0].get("properties") or {}
    uso = props.get("uso")
    subuso = props.get("subuso")
    return ClasificacionUso(uso, subuso, ciren.es_uso_forestal(uso, subuso))
