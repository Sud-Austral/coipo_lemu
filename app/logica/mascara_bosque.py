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
        props = await ciren.query_punto(capa["layer_id"], lon, lat)
    except ciren.CirenError:
        return ClasificacionUso(None, None, False)
    if not props:
        return ClasificacionUso(None, None, False)
    uso = props.get("uso")
    subuso = props.get("subuso")
    return ClasificacionUso(uso, subuso, ciren.es_uso_forestal(uso, subuso))
