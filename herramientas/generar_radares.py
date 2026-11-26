#!/usr/bin/env python3
"""
Genera radares.json, la base de datos que usa el avisador (por defecto en build/).

Fuentes:
  * DGT (Punto de Acceso Nacional, DATEX II): cabinas de radar fijo y radares de tramo.
    No incluye Cataluña, País Vasco ni radares municipales.
  * OpenStreetMap (highway=speed_camera): completa esos huecos.
  * OpenStreetMap (carreteras e hitos kilométricos): sirve para saber
      - en qué sentido vigila cada radar (rumbo), y
      - por qué tramo de carretera se llega a él ("aproximación").
    Con la aproximación, el móvil solo avisa si vas por esa carretera y en ese sentido.

Uso:
    python herramientas/generar_radares.py            # usa la caché si existe
    python herramientas/generar_radares.py --refrescar # vuelve a descargarlo todo
    python herramientas/generar_radares.py --actualizar --minutos 300
        # actualización periódica (la usa GitHub Actions): renueva la lista de radares,
        # reutiliza las carreteras ya descargadas y no aborta si Overpass falla

Licencias: datos DGT reutilizables citando la fuente; datos OSM © colaboradores de
OpenStreetMap bajo ODbL (la base derivada debe compartirse con la misma licencia).
"""
import argparse
import glob
import hashlib
import json
import math
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(RAIZ, "herramientas", "cache")
SALIDA = os.path.join(RAIZ, "build", "radares.json")  # la app la incluye copiándola a app/src/main/assets
URL_DGT = "https://nap.dgt.es/datex2/dgt/PredefinedLocationsPublication/radares/content.xml"
SERVIDORES_OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]
AGENTE = "avisador-radares-prototipo/0.1"
VIAS = "^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street)(_link)?$"

LONG_APROXIMACION = 1300   # metros de carretera guardados antes de cada radar
RADIO_CARRETERA = 1400     # radio de búsqueda de la carretera alrededor del radar
MAX_DIST_RADAR_VIA = 45    # distancia máxima radar-carretera para considerarlo "en" esa vía
MAX_RAMAS = 6              # caminos de aproximación por radar (bifurcaciones)
RADIO_HITOS = 20000        # hasta dónde buscar puntos kilométricos conocidos de la misma carretera
R_TIERRA = 6371008.8
NS = "{http://datex2.eu/schema/1_0/1_0}"

# ----------------------------------------------------------------------------- geometría


def distancia(a, b):
    """Distancia en metros entre dos puntos (lat, lon)."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R_TIERRA * math.asin(min(1.0, math.sqrt(h)))


def rumbo(a, b):
    """Rumbo inicial de a hacia b, en grados (0 = norte, 90 = este)."""
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    y = math.sin(lo2 - lo1) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(lo2 - lo1)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def dif_angulo(a, b):
    return abs((a - b + 180) % 360 - 180)


def a_metros(origen, p):
    """Proyección local equirectangular (suficiente para distancias de pocos km)."""
    k = math.pi / 180 * R_TIERRA
    return ((p[1] - origen[1]) * k * math.cos(math.radians(origen[0])), (p[0] - origen[0]) * k)


def proyeccion(p, a, b):
    """Distancia de p al segmento ab y fracción t (0..1) del punto más cercano."""
    ax, ay = a_metros(p, a)
    bx, by = a_metros(p, b)
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    t = 0.0 if l2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / l2))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(qx, qy), t


def interpolar(a, b, t):
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def simplificar(puntos, tolerancia=4.0):
    """Douglas-Peucker en metros: menos vértices, misma forma."""
    if len(puntos) < 3:
        return puntos
    origen = puntos[0]
    xy = [a_metros(origen, p) for p in puntos]
    conservar = [False] * len(puntos)
    conservar[0] = conservar[-1] = True
    pila = [(0, len(puntos) - 1)]
    while pila:
        i, j = pila.pop()
        (x1, y1), (x2, y2) = xy[i], xy[j]
        dx, dy = x2 - x1, y2 - y1
        norma = math.hypot(dx, dy) or 1e-9
        peor, indice = 0.0, -1
        for k in range(i + 1, j):
            d = abs(dy * (xy[k][0] - x1) - dx * (xy[k][1] - y1)) / norma
            if d > peor:
                peor, indice = d, k
        if peor > tolerancia:
            conservar[indice] = True
            pila += [(i, indice), (indice, j)]
    return [p for p, c in zip(puntos, conservar) if c]


def codificar_polilinea(puntos):
    """Formato 'encoded polyline' (precisión 1e-5, ~1 m). El móvil lo decodifica."""
    salida, plat, plon = [], 0, 0
    for lat, lon in puntos:
        ilat, ilon = round(lat * 1e5), round(lon * 1e5)
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                salida.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            salida.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(salida)


# ----------------------------------------------------------------------------- descargas


def descargar(url, datos=None, timeout=300):
    peticion = urllib.request.Request(url, data=datos, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(peticion, timeout=timeout) as r:
        return r.read()


def overpass(consulta, etiqueta, refrescar=False, tolerante=False, intentos=12):
    """Consulta Overpass con caché en disco y reintentos en varios servidores.

    Con tolerante=True, si ningún servidor responde devuelve la copia de la caché
    (aunque se pidiera refrescar) o None si no la hay, en vez de abortar.
    """
    os.makedirs(CACHE, exist_ok=True)
    clave = hashlib.sha1(consulta.encode()).hexdigest()[:16]
    ruta = os.path.join(CACHE, f"op_{etiqueta}_{clave}.json")

    def leer_cache():
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)

    if os.path.exists(ruta) and not refrescar:
        return leer_cache()
    cuerpo = urllib.parse.urlencode({"data": consulta}).encode()
    espera = 15
    for intento in range(intentos):
        servidor = SERVIDORES_OVERPASS[intento % len(SERVIDORES_OVERPASS)]
        try:
            datos = json.loads(descargar(servidor, cuerpo, timeout=360))
            aviso = datos.get("remark", "")
            if "error" in aviso.lower():
                raise RuntimeError(aviso[:160])
            with open(ruta, "w", encoding="utf-8") as f:
                json.dump(datos, f)
            time.sleep(2)  # cortesía con el servidor público
            return datos
        except Exception as e:  # noqa: BLE001 - cualquier fallo de red se reintenta
            print(f"    {servidor.split('/')[2]} no respondió ({str(e)[:90]}); reintento en {espera}s", flush=True)
            time.sleep(espera)
            espera = min(espera * 2, 180)
    if tolerante:
        if os.path.exists(ruta):
            print(f"    Sin respuesta para '{etiqueta}': se usa la copia anterior de la caché.", flush=True)
            return leer_cache()
        return None
    raise SystemExit(f"No se pudo completar la consulta '{etiqueta}'. Vuelve a ejecutar más tarde (la caché conserva lo ya descargado).")


# ----------------------------------------------------------------------------- DGT


def texto(el, ruta):
    x = el.find(ruta)
    return x.text.strip() if x is not None and x.text else None


def leer_punto(el):
    c = el.find(f".//{NS}pointCoordinates")
    return (float(texto(c, f"{NS}latitude")), float(texto(c, f"{NS}longitude")))


def leer_referencia(rp):
    return {
        "carretera": texto(rp, f".//{NS}roadNumber"),
        "pk": (float(texto(rp, f".//{NS}referencePointDistance")) / 1000) if texto(rp, f".//{NS}referencePointDistance") else None,
        "relativo": texto(rp, f".//{NS}directionRelative"),
        "sentido": texto(rp, f".//{NS}directionNamed"),
    }


def leer_dgt(ruta):
    raiz = ET.parse(ruta).getroot()
    publicado = texto(raiz, f".//{NS}publicationTime")
    radares = []
    for conjunto in raiz.iter(f"{NS}predefinedLocationSet"):
        es_tramo = "VelocidadMedia" in (conjunto.get("id") or "")
        for loc in conjunto.findall(f"{NS}predefinedLocation"):
            ident = (loc.get("id") or "").replace("GUID_", "")
            refs = [leer_referencia(rp) for rp in loc.iter(f"{NS}referencePoint")]
            if es_tramo:
                ini, fin = loc.find(f".//{NS}from"), loc.find(f".//{NS}to")
                if ini is None or fin is None or len(refs) < 2:
                    continue
                radares.append({
                    "id": "DGT-" + ident, "tipo": "T", "fuente": "DGT",
                    "punto": leer_punto(ini), "fin": leer_punto(fin),
                    "carretera": refs[0]["carretera"], "pk": refs[0]["pk"], "pk_fin": refs[1]["pk"],
                    "relativo": refs[0]["relativo"], "sentido": refs[0]["sentido"],
                })
            else:
                r = refs[0] if refs else {}
                radares.append({
                    "id": "DGT-" + ident.replace("CABINACINEMOMETRO_", ""), "tipo": "F", "fuente": "DGT",
                    "punto": leer_punto(loc), "carretera": r.get("carretera"), "pk": r.get("pk"),
                    "relativo": r.get("relativo"), "sentido": r.get("sentido"),
                })
    return radares, publicado


# ----------------------------------------------------------------------------- OSM: utilidades


def normalizar_ref(ref):
    return ref.upper().replace(" ", "").replace("-", "") if ref else None


def refs_de(tags):
    return {normalizar_ref(r) for r in (tags.get("ref") or "").split(";") if r.strip()}


def numero(valor):
    """Límite de velocidad de OSM a km/h (acepta '120', 'ES:urban', '50 mph'...)."""
    if not valor:
        return None
    tipos = {"ES:URBAN": 50, "ES:RURAL": 90, "ES:MOTORWAY": 120, "ES:TRUNK": 100, "ES:LIVING_STREET": 20, "ES:ZONE30": 30}
    v = valor.strip().upper()
    if v in tipos:
        return tipos[v]
    try:
        n = float(v.split()[0].split(";")[0])
        return int(round(n * 1.609)) if "MPH" in v else int(n)
    except ValueError:
        return None


CARDINALES = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5, "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
              "S": 180, "SSW": 202.5, "SSO": 202.5, "SW": 225, "SO": 225, "WSW": 247.5, "OSO": 247.5,
              "W": 270, "O": 270, "WNW": 292.5, "ONO": 292.5, "NW": 315, "NO": 315, "NNW": 337.5, "NNO": 337.5}


def sentido_via(tags):
    """1 = solo en el sentido de dibujo, -1 = solo al revés, 0 = doble sentido."""
    ow = (tags.get("oneway") or "").lower()
    if ow in ("yes", "true", "1"):
        return 1
    if ow in ("-1", "reverse"):
        return -1
    if ow == "no":
        return 0
    if tags.get("highway") in ("motorway", "motorway_link") or tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


class Red:
    """Grafo dirigido de carreteras de OSM con índice espacial por celdas."""

    CELDA = 0.01  # grados (~1 km)

    def __init__(self):
        self.vias = {}
        self.coords = {}
        self.celdas = defaultdict(set)

    def anadir(self, elementos):
        for e in elementos:
            if e.get("type") != "way" or "geometry" not in e or e["id"] in self.vias:
                continue
            if len(e["geometry"]) != len(e.get("nodes", [])):
                continue
            self.vias[e["id"]] = e
            for nodo, g in zip(e["nodes"], e["geometry"]):
                self.coords[nodo] = (g["lat"], g["lon"])
                self.celdas[(int(g["lat"] / self.CELDA), int(g["lon"] / self.CELDA))].add(e["id"])

    def vias_cerca(self, p, radio):
        n = int(radio / 1000 / 111 / self.CELDA) + 1
        ci, cj = int(p[0] / self.CELDA), int(p[1] / self.CELDA)
        ids = set()
        for i in range(ci - n, ci + n + 1):
            for j in range(cj - n, cj + n + 1):
                ids |= self.celdas.get((i, j), set())
        return [self.vias[i] for i in ids]


def clave_carretera(tags):
    if tags.get("ref"):
        return ("ref", frozenset(refs_de(tags)))
    if tags.get("name"):
        return ("name", tags["name"])
    return None


def misma_carretera(tags, clave):
    if clave is None:
        return True
    tipo, valor = clave
    if tipo == "ref":
        return bool(refs_de(tags) & set(valor))
    return tags.get("name") == valor


def aristas_dirigidas(vias):
    """Devuelve (salientes, entrantes, lista_aristas) con aristas (u, v, via)."""
    lista = []
    for via in vias:
        s = sentido_via(via.get("tags", {}))
        nodos = via["nodes"]
        for u, v in zip(nodos, nodos[1:]):
            if s >= 0:
                lista.append((u, v, via))
            if s <= 0:
                lista.append((v, u, via))
    entrantes = defaultdict(list)
    for u, v, via in lista:
        entrantes[v].append((u, via))
    return entrantes, lista


def arista_mas_cercana(p, lista, coords, rumbo_deseado=None, tolerancia=60, max_dist=MAX_DIST_RADAR_VIA):
    mejor = None
    for u, v, via in lista:
        a, b = coords[u], coords[v]
        d, t = proyeccion(p, a, b)
        if d > max_dist:
            continue
        if rumbo_deseado is not None and dif_angulo(rumbo(a, b), rumbo_deseado) > tolerancia:
            continue
        if mejor is None or d < mejor[0]:
            mejor = (d, t, u, v, via)
    return mejor


def caminos_aproximacion(p, arista, entrantes, coords):
    """Recorre la carretera hacia atrás desde el radar: caminos por los que se llega a él."""
    _, t, u, v, _ = arista
    inicio = interpolar(coords[u], coords[v], t)
    caminos = []

    def explorar(nodo, siguiente, recorrido, puntos, visitados):
        if len(caminos) >= MAX_RAMAS:
            return
        if recorrido >= LONG_APROXIMACION:
            caminos.append(puntos)
            return
        opciones = [(w, via) for (w, via) in entrantes.get(nodo, []) if w != siguiente and w not in visitados]
        if not opciones:
            caminos.append(puntos)
            return
        for w, _via in opciones[:3]:
            explorar(w, nodo, recorrido + distancia(coords[w], coords[nodo]), puntos + [coords[w]], visitados | {w})

    explorar(u, v, distancia(coords[u], inicio), [inicio, coords[u]], {u, v})
    resultado = []
    for camino in caminos:
        camino = list(reversed(camino))  # de lejos hacia el radar
        if sum(distancia(a, b) for a, b in zip(camino, camino[1:])) >= 150:
            resultado.append(codificar_polilinea(simplificar(camino)))
    return resultado


def cargar_cache(red, prefijo):
    """Añade a la red todas las carreteras descargadas en ejecuciones anteriores."""
    for ruta in sorted(glob.glob(os.path.join(CACHE, f"op_{prefijo}_*.json"))):
        with open(ruta, encoding="utf-8") as f:
            red.anadir(json.load(f).get("elements", []))


RUTA_COBERTURA = os.path.join(CACHE, "cobertura.json")


def leer_cobertura(refrescar):
    """Radares cuya carretera ya se consultó (aunque la consulta no devolviera nada)."""
    if refrescar or not os.path.exists(RUTA_COBERTURA):
        return {"vias": set(), "trazado": set()}
    with open(RUTA_COBERTURA, encoding="utf-8") as f:
        datos = json.load(f)
    return {k: set(datos.get(k, [])) for k in ("vias", "trazado")}


def guardar_cobertura(cobertura, etapa, lote):
    cobertura[etapa].update(r["id"] for r in lote)
    with open(RUTA_COBERTURA, "w", encoding="utf-8") as f:
        json.dump({k: sorted(v) for k, v in cobertura.items()}, f)


# ----------------------------------------------------------------------------- hitos kilométricos


def indice_hitos(elementos):
    hitos = defaultdict(list)
    for e in elementos:
        tags = e.get("tags", {})
        try:
            km = float((tags.get("distance") or "").replace(",", "."))
        except ValueError:
            continue
        for r in refs_de(tags):
            hitos[r].append(((e["lat"], e["lon"]), km))
    return hitos


def rumbo_creciente_pk(p, pk, ref, hitos):
    """Rumbo aproximado en el que crecen los kilómetros de la carretera junto al radar."""
    candidatos = [(distancia(p, q), q, km) for q, km in hitos.get(normalizar_ref(ref), [])]
    candidatos = [c for c in candidatos if c[0] < RADIO_HITOS and abs(c[2] - pk) >= 0.3]
    candidatos.sort()
    votos_x = votos_y = 0.0
    for d, q, km in candidatos[:4]:
        b = math.radians(rumbo(p, q))
        signo = 1 if km > pk else -1
        votos_x += signo * math.sin(b) / max(d, 200)
        votos_y += signo * math.cos(b) / max(d, 200)
    if not candidatos:
        return None
    return (math.degrees(math.atan2(votos_x, votos_y)) + 360) % 360


def rumbo_dgt_aproximado(r, hitos):
    """Sentido que vigila un radar DGT, a partir de sus datos oficiales y los hitos de OSM."""
    if r["tipo"] == "T":
        return rumbo(r["punto"], r["fin"])  # un tramo se recorre siempre del inicio al final
    if r.get("pk") is None or r.get("relativo") not in ("positive", "negative") or not r.get("carretera"):
        return None
    creciente = rumbo_creciente_pk(r["punto"], r["pk"], r["carretera"], hitos)
    if creciente is None:
        return None
    return creciente if r["relativo"] == "positive" else (creciente + 180) % 360


def rumbo_osm(c, via_de_nodo, coords):
    """Sentido que vigila un radar de OSM (etiqueta direction o calzada de sentido único)."""
    d = (c.get("tags", {}).get("direction") or "").strip().upper()
    if d.replace(".", "").isdigit():
        return float(d) % 360
    if d in CARDINALES:
        return CARDINALES[d]
    via = via_de_nodo.get(c["id"])
    if via is None or not (d in ("FORWARD", "BACKWARD") or sentido_via(via.get("tags", {})) != 0):
        return None
    i = via["nodes"].index(c["id"])
    a = coords[via["nodes"][max(0, i - 1)]]
    b = coords[via["nodes"][min(len(via["nodes"]) - 1, i + 1)]]
    rb = rumbo(a, b)
    if d == "BACKWARD" or (d != "FORWARD" and sentido_via(via["tags"]) == -1):
        rb = (rb + 180) % 360
    return rb


def comprobar_tramos(radares, hitos):
    """Control de calidad: el punto 'from' de la DGT debe estar en el PK de inicio."""
    bien = mal = 0
    for r in radares:
        if r["tipo"] != "T" or r.get("pk") is None or r.get("pk_fin") is None:
            continue
        lista = hitos.get(normalizar_ref(r["carretera"]), [])
        if not lista:
            continue
        km_ini = min(lista, key=lambda h: distancia(h[0], r["punto"]))[1]
        km_fin = min(lista, key=lambda h: distancia(h[0], r["fin"]))[1]
        if abs(km_ini - r["pk"]) + abs(km_fin - r["pk_fin"]) <= abs(km_ini - r["pk_fin"]) + abs(km_fin - r["pk"]):
            bien += 1
        else:
            mal += 1
    print(f"Control de tramos (inicio DGT coincide con el PK de los hitos): {bien} bien, {mal} dudosos")


# ----------------------------------------------------------------------------- proceso principal


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argp = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    argp.add_argument("--refrescar", action="store_true", help="ignora la caché y lo descarga todo de nuevo")
    argp.add_argument("--contar", action="store_true", help="solo dice cuántos radares faltan por descargar")
    argp.add_argument("--sin-trazado", action="store_true",
                      help="versión rápida: sin descargar carreteras (la app usa la regla de respaldo: radar delante y en tu sentido)")
    argp.add_argument("--actualizar", action="store_true",
                      help="actualización periódica: vuelve a bajar la lista de radares (DGT y OSM) pero reutiliza "
                           "las carreteras ya descargadas; si un servidor falla, sigue con lo que haya")
    argp.add_argument("--minutos", type=float, default=None,
                      help="tiempo máximo descargando carreteras; lo que falte se pide en la siguiente ejecución")
    argp.add_argument("--salida", default=SALIDA, help="dónde escribir radares.json")
    args = argp.parse_args()
    os.makedirs(CACHE, exist_ok=True)
    inicio = time.monotonic()
    # En una actualización se renuevan la lista de radares y los hitos, nunca las carreteras.
    renovar_listas = args.refrescar or args.actualizar

    # 1. DGT
    ruta_dgt = os.path.join(CACHE, "dgt_radares.xml")
    if renovar_listas or not os.path.exists(ruta_dgt):
        print("Descargando radares de la DGT…", flush=True)
        try:
            datos = descargar(URL_DGT)
            if b"predefinedLocation" not in datos:
                raise RuntimeError("la DGT no devolvió el fichero de radares esperado")
            with open(ruta_dgt, "wb") as f:
                f.write(datos)
        except Exception as e:  # noqa: BLE001
            if not (args.actualizar and os.path.exists(ruta_dgt)):
                raise SystemExit(f"No se pudo descargar la lista de la DGT: {e}")
            print(f"  La DGT no respondió ({str(e)[:90]}); se usa la copia anterior.", flush=True)
    radares, publicado_dgt = leer_dgt(ruta_dgt)
    print(f"DGT: {sum(r['tipo'] == 'F' for r in radares)} fijos, {sum(r['tipo'] == 'T' for r in radares)} tramos (publicado {publicado_dgt})")

    # 2. OSM: radares, vías que los contienen e hitos kilométricos
    area = 'area["ISO3166-1"="ES"][admin_level=2]->.es;'
    print("Descargando radares de OpenStreetMap…", flush=True)
    osm = overpass(f"[out:json][timeout:300];{area}node[highway=speed_camera](area.es)->.c;.c out body;way(bn.c)[highway]->.w;.w out body geom;",
                   "camaras", renovar_listas, tolerante=args.actualizar)
    print("Descargando hitos kilométricos de OpenStreetMap…", flush=True)
    datos_hitos = overpass(f"[out:json][timeout:300];{area}node[highway=milestone](area.es);out body;",
                           "hitos", renovar_listas, tolerante=args.actualizar)
    if osm is None or datos_hitos is None:
        raise SystemExit("OpenStreetMap no respondió y no hay copia anterior de los radares o de los hitos.")
    hitos = indice_hitos(datos_hitos["elements"])
    camaras = [e for e in osm["elements"] if e["type"] == "node"]
    red = Red()
    red.anadir(osm["elements"])
    via_de_nodo = {}
    for via in red.vias.values():
        for n in via["nodes"]:
            via_de_nodo.setdefault(n, via)
    # Los propios radares DGT son "hitos": coordenadas con su punto kilométrico oficial.
    for r in radares:
        if r.get("carretera") and r.get("pk") is not None:
            hitos[normalizar_ref(r["carretera"])].append((r["punto"], r["pk"]))
            if r["tipo"] == "T" and r.get("pk_fin") is not None:
                hitos[normalizar_ref(r["carretera"])].append((r["fin"], r["pk_fin"]))
    print(f"OSM: {len(camaras)} radares, hitos en {len(hitos)} carreteras")
    comprobar_tramos(radares, hitos)

    # 3. Emparejar radares DGT con OSM (mismo lugar y sentido compatible) y añadir los que solo están en OSM
    rumbo_camara = {c["id"]: rumbo_osm(c, via_de_nodo, red.coords) for c in camaras}
    usadas = set()
    for r in radares:
        r["rumbo_aprox"] = rumbo_dgt_aproximado(r, hitos)

        def compatible(c):
            rc = rumbo_camara[c["id"]]
            return rc is None or r["rumbo_aprox"] is None or dif_angulo(rc, r["rumbo_aprox"]) <= 90

        if r["tipo"] == "T":
            for c in camaras:
                q = (c["lat"], c["lon"])
                if compatible(c) and min(distancia(q, r["punto"]), distancia(q, r["fin"])) < 150:
                    usadas.add(c["id"])
            continue
        candidatas = sorted((distancia((c["lat"], c["lon"]), r["punto"]), c["id"], c) for c in camaras
                            if c["id"] not in usadas and compatible(c) and abs(c["lat"] - r["punto"][0]) < 0.002)
        candidatas = [x for x in candidatas if x[0] < 120]
        if candidatas:
            r["osm"] = candidatas[0][2]
            usadas.add(candidatas[0][1])
    for c in camaras:
        if c["id"] in usadas:
            continue
        tags = c.get("tags", {})
        radares.append({"id": f"OSM-{c['id']}", "tipo": "F", "fuente": "OSM", "punto": (c["lat"], c["lon"]),
                        "carretera": None, "pk": None, "relativo": None, "sentido": None, "osm": c,
                        "limite_osm": numero(tags.get("maxspeed"))})
    for r in radares:
        if "osm" in r and r["osm"].get("tags", {}).get("maxspeed"):
            r["limite_osm"] = numero(r["osm"]["tags"]["maxspeed"])
    print(f"Total: {len(radares)} radares ({sum(r['fuente'] == 'OSM' for r in radares)} solo en OSM)")

    # 4. Identificar la carretera de cada radar (ref o nombre)
    sin_via = [r for r in radares if not r.get("carretera") and not ("osm" in r and r["osm"]["id"] in via_de_nodo)]
    if args.sin_trazado:
        sin_via = []
    # Se reaprovecha todo lo descargado antes y solo se pide lo que falta.
    cobertura = leer_cobertura(args.refrescar)
    if not args.refrescar:
        cargar_cache(red, "vias_cercanas")

    def tiene_via_cerca(r):
        _, lista = aristas_dirigidas(red.vias_cerca(r["punto"], 100))
        return arista_mas_cercana(r["punto"], lista, red.coords) is not None

    def queda_tiempo():
        return args.minutos is None or (time.monotonic() - inicio) / 60 < args.minutos

    sin_via = [r for r in sin_via if r["id"] not in cobertura["vias"] and not tiene_via_cerca(r)]
    print(f"Buscando la carretera de {len(sin_via)} radares sin vía conocida…", flush=True)
    for i in range(0, 0 if args.contar else len(sin_via), 100):
        lote = sin_via[i:i + 100]
        sentencias = "".join(f'way(around:{MAX_DIST_RADAR_VIA},{r["punto"][0]:.6f},{r["punto"][1]:.6f})[highway~"{VIAS}"];' for r in lote)
        # El nombre lleva una huella de los radares del lote: así un lote nuevo nunca pisa uno anterior.
        huella = hashlib.sha1("".join(r["id"] for r in lote).encode()).hexdigest()[:8]
        respuesta = overpass(f"[out:json][timeout:240];({sentencias});out body geom;", f"vias_cercanas_{huella}",
                             args.refrescar, tolerante=args.actualizar, intentos=6 if args.actualizar else 12)
        if respuesta is None:
            print("  Overpass no responde: los radares que faltan se buscarán en la próxima actualización.", flush=True)
            break
        red.anadir(respuesta["elements"])
        guardar_cobertura(cobertura, "vias", lote)
        print(f"  {min(i + 100, len(sin_via))}/{len(sin_via)}", flush=True)
    for r in radares:
        if r.get("carretera"):
            r["clave"] = ("ref", frozenset({normalizar_ref(r["carretera"])}))
            continue
        via = via_de_nodo.get(r["osm"]["id"]) if "osm" in r else None
        if via is None:
            _, lista = aristas_dirigidas([v for v in red.vias_cerca(r["punto"], 100) if clave_carretera(v.get("tags", {}))])
            cercana = arista_mas_cercana(r["punto"], lista, red.coords)
            via = cercana[4] if cercana else None
        r["clave"] = clave_carretera(via.get("tags", {})) if via else None
        if via is not None:
            r["carretera"] = (via["tags"].get("ref") or "").split(";")[0] or via["tags"].get("name")

    # 5. Descargar el trazado de cada carretera alrededor de su radar
    con_clave = [] if args.sin_trazado else [r for r in radares if r.get("clave")]
    if not args.refrescar:
        cargar_cache(red, "trazado")

    def trazado_cubierto(r):
        """¿Ya hay carretera descargada junto al radar y hasta ~1 km antes y después?"""
        cerca = lejos = False
        for v in red.vias_cerca(r["punto"], RADIO_CARRETERA):
            if not misma_carretera(v.get("tags", {}), r["clave"]):
                continue
            for g in v["geometry"]:
                d = distancia(r["punto"], (g["lat"], g["lon"]))
                cerca = cerca or d < 100
                lejos = lejos or 900 < d < RADIO_CARRETERA
            if cerca and lejos:
                return True
        return False

    con_clave = [r for r in con_clave if r["id"] not in cobertura["trazado"] and not trazado_cubierto(r)]
    print(f"Descargando el trazado de la carretera de {len(con_clave)} radares…", flush=True)
    if args.contar:
        return
    # Por zonas (celdas de ~10 km): los radares de un lote quedan juntos y cada consulta
    # cubre también a sus vecinos, que entonces ya no hace falta pedir.
    pendientes = sorted(con_clave, key=lambda r: (round(r["punto"][0], 1), round(r["punto"][1], 1), r["punto"]))
    total, hechos, consulta = len(pendientes), 0, 0
    lote_n = 25
    while pendientes:
        lote = []
        while pendientes and len(lote) < lote_n:
            r = pendientes.pop(0)
            hechos += 1
            if consulta == 0 or not trazado_cubierto(r):
                lote.append(r)
        if not lote:
            break
        sentencias = []
        for r in lote:
            tipo, valor = r["clave"]
            lat, lon = r["punto"]
            if tipo == "ref":
                # OSM escribe "A-2" o "A-2;E-90": el patrón admite el guion opcional y listas con ';'
                alternativas = "|".join("-?".join(_partes(v)) for v in sorted(valor))
                filtro = f'[ref~"(^|;) *({alternativas}) *(;|$)"]'
            else:
                filtro = '[name="' + valor.replace('"', '\\"') + '"]'
            sentencias.append(f'way(around:{RADIO_CARRETERA},{lat:.6f},{lon:.6f})[highway~"{VIAS}"]{filtro};')
        respuesta = overpass(f"[out:json][timeout:300];({''.join(sentencias)});out body geom;", f"trazado_z{consulta}",
                             args.refrescar, tolerante=args.actualizar, intentos=6 if args.actualizar else 12)
        if respuesta is None:
            print(f"  Overpass no responde: {total - hechos + len(lote)} radares se completarán en la próxima actualización "
                  "(mientras, la app usa con ellos la regla de respaldo).", flush=True)
            break
        red.anadir(respuesta["elements"])
        guardar_cobertura(cobertura, "trazado", lote)
        consulta += 1
        print(f"  {hechos}/{total} radares revisados ({consulta} consultas)", flush=True)
        if not queda_tiempo():
            print(f"  Tiempo agotado: {len(pendientes)} radares quedan para la próxima actualización.", flush=True)
            break

    # 6. Sentido vigilado, límite y caminos de aproximación
    metodos = Counter()
    salida = []
    for r in radares:
        p = r["punto"]
        vias = [v for v in red.vias_cerca(p, RADIO_CARRETERA) if misma_carretera(v.get("tags", {}), r.get("clave"))]
        entrantes, lista = aristas_dirigidas(vias)
        # Sentido vigilado. Orden de preferencia:
        #   a) DGT: punto kilométrico creciente/decreciente orientado con los hitos de OSM
        #      (en un tramo, del inicio hacia el final)
        #   b) OSM: etiqueta direction o radar dibujado sobre una calzada de sentido único
        rumbo_vigilado, metodo = r.get("rumbo_aprox"), ("dgt_pk" if r.get("rumbo_aprox") is not None else "desconocido")
        if rumbo_vigilado is None and r.get("osm") is not None:
            rumbo_vigilado = rumbo_camara[r["osm"]["id"]]
            metodo = "osm" if rumbo_vigilado is not None else metodo
        if rumbo_vigilado is not None:  # alinear con la carretera real junto al radar
            cercana = arista_mas_cercana(p, lista, red.coords, max_dist=80)
            if cercana:
                rb = rumbo(red.coords[cercana[2]], red.coords[cercana[3]])
                rumbo_vigilado = rb if dif_angulo(rb, rumbo_vigilado) <= 90 else (rb + 180) % 360
        metodos[metodo] += 1

        # caminos de aproximación (uno por sentido vigilado; si no se sabe, ambos)
        aproximaciones, limite = [], r.get("limite_osm")
        rumbos = [rumbo_vigilado] if rumbo_vigilado is not None else [None]
        if rumbo_vigilado is None:
            primera = arista_mas_cercana(p, lista, red.coords)
            if primera:
                rb = rumbo(red.coords[primera[2]], red.coords[primera[3]])
                rumbos = [rb, (rb + 180) % 360]
        for rb in rumbos:
            if rb is None or args.sin_trazado:
                continue
            arista = arista_mas_cercana(p, lista, red.coords, rumbo_deseado=rb, tolerancia=60)
            if arista is None:
                continue
            aproximaciones += caminos_aproximacion(p, arista, entrantes, red.coords)
            if limite is None:
                tags = arista[4].get("tags", {})
                adelante = tags.get("maxspeed:forward") if arista[4]["nodes"].index(arista[2]) < arista[4]["nodes"].index(arista[3]) else tags.get("maxspeed:backward")
                limite = numero(adelante) or numero(tags.get("maxspeed"))

        item = {"id": r["id"], "t": r["tipo"], "la": round(p[0], 6), "lo": round(p[1], 6), "f": r["fuente"]}
        if r.get("carretera"):
            item["c"] = r["carretera"]
        if r.get("pk") is not None:
            item["pk"] = round(r["pk"], 2)
        if r.get("sentido"):
            item["s"] = r["sentido"]
        if rumbo_vigilado is not None:
            item["r"] = round(rumbo_vigilado, 1)
        if limite:
            item["lim"] = limite
        if r["tipo"] == "T":
            item["fla"], item["flo"] = round(r["fin"][0], 6), round(r["fin"][1], 6)
            if r.get("pk_fin") is not None and r.get("pk") is not None:
                item["len"] = int(abs(r["pk_fin"] - r["pk"]) * 1000)
            else:
                item["len"] = int(distancia(p, r["fin"]))
        if aproximaciones:
            item["ap"] = aproximaciones
        salida.append(item)

    # 7. Guardar
    ruta_salida = os.path.abspath(args.salida)
    os.makedirs(os.path.dirname(ruta_salida), exist_ok=True)
    resultado = {
        "version": 1,
        "generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dgt_publicado": publicado_dgt,
        "atribucion": "Radares: DGT (nap.dgt.es) y © colaboradores de OpenStreetMap (ODbL)",
        "radares": salida,
    }
    temporal = ruta_salida + ".tmp"
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, separators=(",", ":"))
    shutil.move(temporal, ruta_salida)

    con_ap = sum(1 for s in salida if "ap" in s)
    print("\nResumen")
    print(f"  Radares: {len(salida)}  (fijos {sum(s['t'] == 'F' for s in salida)}, tramos {sum(s['t'] == 'T' for s in salida)})")
    print(f"  Con camino de aproximación: {con_ap} ({100 * con_ap // max(1, len(salida))} %)")
    print(f"  Con límite de velocidad: {sum(1 for s in salida if 'lim' in s)}")
    print(f"  Origen del sentido vigilado: {dict(metodos)}")
    print(f"  Fichero: {ruta_salida} ({os.path.getsize(ruta_salida) // 1024} KB)")


def _partes(ref_normalizada):
    """'A2' -> ['A', '2'] para construir el patrón 'A-?2' (OSM usa 'A-2')."""
    letras = "".join(ch for ch in ref_normalizada if not ch.isdigit()).rstrip()
    resto = ref_normalizada[len(letras):]
    return [letras, resto] if letras and resto else [ref_normalizada]


if __name__ == "__main__":
    main()
