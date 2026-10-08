"""Decide el estado de un cambio forestal cruzando SAFF (planes) y SIGCRA (rodales).

Se aplica al centroide de cada poligono de bosque CIREN con caida de NDVI.
El listado usa evaluar_cercania (solo SAFF en memoria); la ficha usa
evaluar_ficha (con rodales SIGCRA):
- sin_datos: SAFF no respondio, o SIGCRA no respondio para los planes cercanos
  y por eso no se puede confirmar el cruce.
- en_regla: el punto cae dentro de un rodal SIGCRA de un plan con resolucion
  aprobada (Estdo Solicitud contiene "APROB", o hay Nro.Resolucion y
  Super.Aprobada > 0) y Fec.Resolucion <= t1.
- alerta: no hay ningun plan SAFF con punto de referencia a <= radio_km.
- revisar: hay planes cercanos pero el punto no cae en un rodal aprobado a t1
  (fuera de los rodales consultados, plan sin aprobar o sin rodales en SIGCRA).
  No se marca alerta porque la cercania es por punto de referencia del predio.
"""
from __future__ import annotations

ESTADO_ALERTA = "alerta"
ESTADO_EN_REGLA = "en_regla"
ESTADO_REVISAR = "revisar"
ESTADO_SIN_DATOS = "sin_datos"


class ResultadoListaNegra:
    def __init__(self, estado: str, motivo: str, plan_manejo: dict | None = None):
        self.estado = estado
        self.motivo = motivo
        self.plan_manejo = plan_manejo

    def to_dict(self) -> dict:
        return {"estado": self.estado, "motivo": self.motivo, "plan_manejo": self.plan_manejo}


def evaluar_cercania(planes_cercanos: int | None, radio_km: float) -> ResultadoListaNegra:
    """Estado preliminar del listado, solo con la carga SAFF en memoria (sin SIGCRA).

    None = SAFF aun no cargado o caido. Con planes cercanos queda «revisar»
    hasta abrir la ficha, que consulta los rodales y aplica evaluar_ficha.
    """
    radio = f"{radio_km:g}".replace(".", ",")
    if planes_cercanos is None:
        return ResultadoListaNegra(ESTADO_SIN_DATOS, "SAFF sin datos por el momento")
    if planes_cercanos == 0:
        return ResultadoListaNegra(ESTADO_ALERTA, f"Sin planes de manejo SAFF a menos de {radio} km")
    return ResultadoListaNegra(
        ESTADO_REVISAR,
        f"{planes_cercanos} plan(es) a menos de {radio} km; abre la ficha para cruzar con rodales SIGCRA",
    )


def evaluar_ficha(ficha: dict) -> ResultadoListaNegra:
    """Aplica la regla de estado sobre la salida de ficha_punto.ficha_punto (con t1)."""
    if (ficha.get("saff") or {}).get("estado") != "ok":
        return ResultadoListaNegra(ESTADO_SIN_DATOS, "SAFF no respondió")
    dentro = ficha.get("dentro_de_rodal")
    if dentro and dentro.get("aprobado_a_t1"):
        return ResultadoListaNegra(
            ESTADO_EN_REGLA,
            f"Dentro del rodal {dentro['rodal']} del plan {dentro['nro_solicitud']} (aprobado a t1)",
            plan_manejo={"nro_solicitud": dentro["nro_solicitud"], "rodal": dentro["rodal"]},
        )
    planes = ficha.get("planes") or []
    if not planes:
        return ResultadoListaNegra(ESTADO_ALERTA, f"Sin planes de manejo SAFF a menos de {ficha.get('radio_km')} km")
    sigcra_estado = (ficha.get("sigcra") or {}).get("estado")
    if not dentro and sigcra_estado in ("sin_datos", "parcial"):
        return ResultadoListaNegra(ESTADO_SIN_DATOS, "SIGCRA no respondió para los planes cercanos")
    if dentro:
        motivo = f"Dentro del rodal {dentro['rodal']} del plan {dentro['nro_solicitud']}, sin resolución aprobada a t1"
    else:
        motivo = f"{len(planes)} plan(es) cercano(s), pero el punto no cae en sus rodales SIGCRA"
    return ResultadoListaNegra(ESTADO_REVISAR, motivo)
