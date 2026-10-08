"""Utilidades geograficas en Python puro (sin pyproj ni shapely).

- UTM (WGS84/SIRGAS, hemisferio sur) -> lat/lon WGS84 con la serie estandar
  de Snyder (USGS PP 1395), error sub-metrico dentro del huso.
- Point-in-polygon (ray casting) para GeoJSON Polygon/MultiPolygon con hoyos.
- Distancia haversine en km.
"""
from __future__ import annotations

import math

_A = 6378137.0
_F = 1 / 298.257223563
_K0 = 0.9996
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)

# Chile continental (criterio para inferir el huso cuando el dato no lo trae).
CHILE_LAT = (-56.0, -17.0)
CHILE_LON = (-76.0, -66.0)


def utm_a_wgs84(este: float, norte: float, huso: int, sur: bool = True) -> tuple[float, float]:
    """Convierte coordenadas UTM a (lat, lon) en grados decimales."""
    x = este - 500_000.0
    y = norte - 10_000_000.0 if sur else norte
    m = y / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2**2 / 64 - 5 * _E2**3 / 256))
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1**3 / 32) * math.sin(2 * mu)
        + (21 * e1**2 / 16 - 55 * e1**4 / 32) * math.sin(4 * mu)
        + (151 * e1**3 / 96) * math.sin(6 * mu)
        + (1097 * e1**4 / 512) * math.sin(8 * mu)
    )
    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    n1 = _A / math.sqrt(1 - _E2 * sin1**2)
    t1 = tan1**2
    c1 = _EP2 * cos1**2
    r1 = _A * (1 - _E2) / (1 - _E2 * sin1**2) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * tan1 / r1) * (
        d**2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1**2 - 9 * _EP2) * d**4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1**2 - 252 * _EP2 - 3 * c1**2) * d**6 / 720
    )
    lon = (
        d
        - (1 + 2 * t1 + c1) * d**3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1**2 + 8 * _EP2 + 24 * t1**2) * d**5 / 120
    ) / cos1
    lon0 = (huso - 1) * 6 - 180 + 3
    return math.degrees(lat), lon0 + math.degrees(lon)


def en_chile(lat: float, lon: float) -> bool:
    return CHILE_LAT[0] <= lat <= CHILE_LAT[1] and CHILE_LON[0] <= lon <= CHILE_LON[1]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _en_anillo(lon: float, lat: float, anillo: list) -> bool:
    dentro = False
    n = len(anillo)
    j = n - 1
    for i in range(n):
        xi, yi = anillo[i][0], anillo[i][1]
        xj, yj = anillo[j][0], anillo[j][1]
        if (yi > lat) != (yj > lat):
            x_corte = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_corte:
                dentro = not dentro
        j = i
    return dentro


def _en_poligono(lon: float, lat: float, anillos: list) -> bool:
    if not anillos or not _en_anillo(lon, lat, anillos[0]):
        return False
    return not any(_en_anillo(lon, lat, hoyo) for hoyo in anillos[1:])


def _poligonos(geometria: dict | None) -> list:
    if not geometria:
        return []
    if geometria.get("type") == "Polygon":
        return [geometria.get("coordinates") or []]
    if geometria.get("type") == "MultiPolygon":
        return geometria.get("coordinates") or []
    return []


def _area_centroide_anillo(anillo: list) -> tuple[float, float, float]:
    """Area con signo (grados^2) y centroide de un anillo (formula del shoelace)."""
    a = cx = cy = 0.0
    for i in range(len(anillo) - 1):
        x0, y0 = anillo[i][0], anillo[i][1]
        x1, y1 = anillo[i + 1][0], anillo[i + 1][1]
        cruz = x0 * y1 - x1 * y0
        a += cruz
        cx += (x0 + x1) * cruz
        cy += (y0 + y1) * cruz
    a /= 2
    if a == 0:
        return 0.0, anillo[0][0], anillo[0][1]
    return a, cx / (6 * a), cy / (6 * a)


def bbox(geometria: dict | None) -> tuple[float, float, float, float] | None:
    """(oeste, sur, este, norte) de un Polygon/MultiPolygon."""
    xs = [p[0] for poli in _poligonos(geometria) for anillo in poli for p in anillo]
    ys = [p[1] for poli in _poligonos(geometria) for anillo in poli for p in anillo]
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def centroide(geometria: dict | None) -> tuple[float, float] | None:
    """(lon, lat) del centroide de area (los hoyos restan).

    Si el centroide cae fuera del poligono (formas en C o con hoyos) se usa un
    punto interior: el medio del tramo interior mas largo sobre la horizontal
    del centroide. Asi el cruce con rodales siempre evalua un punto del bosque.
    """
    area = sx = sy = 0.0
    for poli in _poligonos(geometria):
        for k, anillo in enumerate(poli):
            if len(anillo) < 4:
                continue
            a, cx, cy = _area_centroide_anillo(anillo)
            a = abs(a) if k == 0 else -abs(a)
            area += a
            sx += cx * a
            sy += cy * a
    if area <= 0:
        caja = bbox(geometria)
        return ((caja[0] + caja[2]) / 2, (caja[1] + caja[3]) / 2) if caja else None
    lon, lat = sx / area, sy / area
    if punto_en_geometria(lon, lat, geometria):
        return lon, lat
    return _punto_interior(geometria, lat) or (lon, lat)


def _punto_interior(geometria: dict, lat: float) -> tuple[float, float] | None:
    cortes = []
    for poli in _poligonos(geometria):
        for anillo in poli:
            for i in range(len(anillo) - 1):
                (x0, y0), (x1, y1) = anillo[i][:2], anillo[i + 1][:2]
                if (y0 > lat) != (y1 > lat):
                    cortes.append(x0 + (lat - y0) * (x1 - x0) / (y1 - y0))
    cortes.sort()
    tramos = [(cortes[i], cortes[i + 1]) for i in range(0, len(cortes) - 1, 2)]
    if not tramos:
        return None
    a, b = max(tramos, key=lambda t: t[1] - t[0])
    return (a + b) / 2, lat


def punto_en_geometria(lon: float, lat: float, geometria: dict | None) -> bool:
    """True si (lon, lat) cae dentro de un GeoJSON Polygon/MultiPolygon."""
    if not geometria:
        return False
    tipo = geometria.get("type")
    coords = geometria.get("coordinates") or []
    if tipo == "Polygon":
        return _en_poligono(lon, lat, coords)
    if tipo == "MultiPolygon":
        return any(_en_poligono(lon, lat, poli) for poli in coords)
    if tipo == "GeometryCollection":
        return any(punto_en_geometria(lon, lat, g) for g in geometria.get("geometries") or [])
    return False
