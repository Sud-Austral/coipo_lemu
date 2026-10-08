"""Decide si una alerta de cambio de vegetacion tiene plan de manejo vigente.

Cruza contra el historico de solicitudes de plan de manejo de SAFF
(/v1/historico/pm). Si el predio aparece con una resolucion de aprobacion
reciente, se considera "en_regla"; si no aparece, queda como "alerta".

Nota: SAFF no tiene geometria, por lo que el cruce real predio<->punto se
hace via SIGCRA (codigo_solicitud / codigo_predio). Para el piloto, el
cruce se hace por codigo de predio cuando esta disponible.
"""
from __future__ import annotations

from datetime import date

from app.clientes import saff

ESTADO_ALERTA = "alerta"
ESTADO_EN_REGLA = "en_regla"
ESTADO_SIN_DATOS = "sin_datos"


class ResultadoListaNegra:
    def __init__(self, estado: str, plan_manejo: dict | None = None):
        self.estado = estado
        self.plan_manejo = plan_manejo

    def to_dict(self) -> dict:
        return {"estado": self.estado, "plan_manejo": self.plan_manejo}


async def evaluar_predio(
    codigo_predio: str | None,
    fecha_desde: date,
    fecha_hasta: date,
) -> ResultadoListaNegra:
    """Busca si el predio tiene un plan de manejo resuelto en el rango dado."""
    if not codigo_predio:
        return ResultadoListaNegra(ESTADO_SIN_DATOS)
    try:
        data = await saff.historico_pm(fecha_desde, fecha_hasta, limit=2000)
    except saff.SaffError:
        return ResultadoListaNegra(ESTADO_SIN_DATOS)
    items = data.get("items") or []
    for item in items:
        if str(item.get("Cod. Predio") or item.get("cod_predio") or "") == str(codigo_predio):
            return ResultadoListaNegra(ESTADO_EN_REGLA, plan_manejo=item)
    return ResultadoListaNegra(ESTADO_ALERTA)
