#!/usr/bin/env python3
"""
Genera radares.json, la base de datos que usa el avisador.

Fuentes:
  * DGT (Punto de Acceso Nacional, DATEX II): cabinas de radar fijo y radares de tramo.
    No incluye Cataluña, País Vasco ni radares municipales.                   -> OFICIAL
  * Servei Català de Trànsit (transit.gencat.cat): radares fijos de Cataluña.  -> OFICIAL
  * OpenStreetMap (highway=speed_camera): completa los huecos. Son datos de voluntarios:
    la app los anuncia como «posible radar» salvo que coincidan con un radar oficial.
    Se descartan los que el propio mapa marca como móviles, temporales, pedagógicos,
    de semáforo en rojo, de cinturón o retirados.
  * OpenStreetMap (carreteras e hitos kilométricos, del fichero de Geofabrik): sirven para
      - saber en qué sentido vigila cada radar, y
      - por qué tramos de carretera se llega a él ("aproximaciones").
    Con las aproximaciones el móvil solo avisa si vas por esa carretera y en ese sentido.

Sentido vigilado, de más a menos fiable:
  1. tramo DGT (del inicio al final) o PK oficial DGT + hitos kilométricos;
  2. etiqueta direction de OSM o radar sobre una calzada de sentido único;
  3. calzada de sentido único junto al radar (autovías y avenidas con mediana);
  4. lado de la calzada donde está dibujado el poste (en España, a la derecha del
     tráfico que vigila): acierta ~83 %, así que el sentido contrario se guarda como
     «dudoso» (la app avisa de él como posible radar);
  5. sin pistas: los dos sentidos, ambos dudosos.

Uso:
    python herramientas/generar_radares.py --pbf spain-latest.osm.pbf
    python herramientas/generar_radares.py --pbf madrid-latest.osm.pbf --zona 40.3,-3.9,40.6,-3.2   # pruebas

El fichero .osm.pbf se descarga de https://download.geofabrik.de/europe/spain-latest.osm.pbf

Licencias: datos DGT y Generalitat reutilizables citando la fuente; datos OSM © colaboradores
de OpenStreetMap bajo ODbL (la base derivada debe compartirse con la misma licencia).
"""
import argparse
import json
import math
import os
import re
import shutil
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(RAIZ, "herramientas", "cache")
SALIDA = os.path.join(RAIZ, "build", "radares.json")  # la app la incluye copiándola a app/src/main/assets
URL_DGT = "https://nap.dgt.es/datex2/dgt/PredefinedLocationsPublication/radares/content.xml"
URL_SCT = "https://transit.gencat.cat/web/.content/documents/seguretat_viaria/radars.txt"
AGENTE = "avisador-radares/0.3"
VIAS = "^(motorway|trunk|primary|secondary|tertiary|unclassified|residential|living_street)(_link)?$"

LONG_APROXIMACION = 1300   # metros de carretera guardados antes de cada radar
RADIO_RED = 1700           # carreteras que se cargan alrededor de cada radar
MAX_DIST_RADAR_VIA = 45    # distancia máxima radar-carretera para considerarlo "en" esa vía
MAX_DIST_OFICIAL = 80      # las coordenadas oficiales a veces están algo apartadas de la vía
MIN_APROXIMACION = 30      # una aproximación corta (radar justo después de una rotonda) también sirve
MIN_RAMA = 50              # ramas laterales más cortas (salidas de garajes...) no se guardan
MAX_RAMAS = 6              # caminos de aproximación por sentido (carretera principal + accesos)
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


def descargar(url, datos=None, timeout=120):
    peticion = urllib.request.Request(url, data=datos, headers={"User-Agent": AGENTE})
    with urllib.request.urlopen(peticion, timeout=timeout) as r:
        return r.read()


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


# ----------------------------------------------------------------------------- Cataluña (SCT)


def utm_a_latlon(x, y, huso=31):
    """ETRS89 / UTM huso norte a (latitud, longitud). Serie clásica de Snyder, precisión < 1 m."""
    a, f, k0 = 6378137.0, 1 / 298.257222101, 0.9996
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    x -= 500000.0
    mu = (y / k0) / (a * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    fi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu) + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
           + (151 * e1 ** 3 / 96) * math.sin(6 * mu) + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    s, c, t = math.sin(fi1), math.cos(fi1), math.tan(fi1)
    n1 = a / math.sqrt(1 - e2 * s * s)
    t1, c1 = t * t, ep2 * c * c
    r1 = a * (1 - e2) / (1 - e2 * s * s) ** 1.5
    d = x / (n1 * k0)
    lat = fi1 - (n1 * t / r1) * (d ** 2 / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
                                 + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720)
    lon = math.radians((huso - 1) * 6 - 180 + 3) + (d - (1 + 2 * t1 + c1) * d ** 3 / 6
                                                    + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120) / c
    return math.degrees(lat), math.degrees(lon)


def _coordenada_utm(texto, minimo, maximo):
    """El fichero oficial trae coordenadas sin separador decimal ("42552972" = 425529,72): se reescalan."""
    valor = float(texto.replace(",", "."))
    for _ in range(6):
        if minimo <= valor <= maximo:
            return valor
        valor /= 10
    return None


def leer_sct(ruta):
    """
    Radares fijos del Servei Català de Trànsit: «Via PK Velocitat X Y» (UTM 31N, ETRS89).

    En los radares de tramo el PK viene como «inicio-fin» (p. ej. «501,5-506,5»): el orden
    de los kilómetros indica el sentido vigilado.
    """
    radares = []
    patron = re.compile(r"^(.*?)\s+(\d+(?:,\d+)?)\s*(?:-\s*(\d+(?:,\d+)?))?$")
    with open(ruta, encoding="latin-1") as f:
        for linea in f:
            partes = linea.split()
            if len(partes) < 5:
                continue
            try:
                limite = int(float(partes[-3].replace(",", ".")))
                x = _coordenada_utm(partes[-2], 250000, 530000)
                y = _coordenada_utm(partes[-1], 4480000, 4760000)
            except ValueError:
                continue  # cabecera
            m = patron.match(" ".join(partes[:-3]))
            if m is None or x is None or y is None:
                continue
            via = m.group(1)
            pk = float(m.group(2).replace(",", "."))
            pk_fin = float(m.group(3).replace(",", ".")) if m.group(3) else None
            relativo = None if pk_fin is None or pk_fin == pk else ("positive" if pk_fin > pk else "negative")
            lat, lon = utm_a_latlon(x, y)
            radares.append({"id": f"SCT-{via}-{pk:g}", "tipo": "F", "fuente": "SCT", "punto": (lat, lon),
                            "carretera": via.split()[0], "pk": pk, "relativo": relativo, "sentido": None,
                            "limite_oficial": limite})
    # La misma vía y PK puede salir dos veces (un radar por sentido): ids únicos
    vistos = Counter()
    for r in radares:
        vistos[r["id"]] += 1
        if vistos[r["id"]] > 1:
            r["id"] += f"-{vistos[r['id']]}"
    return radares


def descargar_con_copia(url, ruta, comprobar, nombre):
    """Descarga una lista oficial; si el servidor falla, usa la última copia buena."""
    try:
        datos = descargar(url)
        if not comprobar(datos):
            raise RuntimeError("contenido inesperado")
        with open(ruta, "wb") as f:
            f.write(datos)
    except Exception as e:  # noqa: BLE001
        if not os.path.exists(ruta):
            raise SystemExit(f"No se pudo descargar la lista de {nombre}: {e}")
        print(f"  {nombre} no respondió ({str(e)[:80]}); se usa la copia anterior.", flush=True)


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


def caminos_aproximacion(p, arista, entrantes, coords, clave):
    """
    Caminos por los que se llega al radar, recorriendo la red hacia atrás desde él.

    Primero la carretera principal: en cada cruce se sigue la continuación más natural
    (la misma carretera y el menor giro), atravesando rotondas y enlaces. Después, por orden
    de cercanía al radar, los accesos laterales de esos cruces (otra calle o un ramal de
    entrada que desemboca antes del radar), cada uno con su propia continuación.
    """
    _, t, u, v, _via = arista
    inicio = interpolar(coords[u], coords[v], t)

    def seguir(nodo, siguiente, recorrido, puntos, nodos, visitados):
        cruces = []
        while recorrido < LONG_APROXIMACION:
            opciones = [(w, via) for (w, via) in entrantes.get(nodo, []) if w != siguiente and w not in visitados]
            if not opciones:
                break
            salida = rumbo(coords[nodo], coords[siguiente])

            def coste(op):
                w, via = op
                giro = dif_angulo(rumbo(coords[w], coords[nodo]), salida)
                return (giro > 120, 0 if misma_carretera(via.get("tags", {}), clave) else 1, giro)

            opciones.sort(key=coste)
            cruces.append((len(puntos), recorrido, nodo, opciones[1:]))
            w = opciones[0][0]
            recorrido += distancia(coords[w], coords[nodo])
            puntos.append(coords[w])
            nodos.append(w)
            visitados.add(w)
            siguiente, nodo = nodo, w
        return puntos, nodos, recorrido, cruces

    principal, nodos, _, cruces = seguir(u, v, distancia(coords[u], inicio), [inicio, coords[u]], [None, u], {u, v})
    caminos = [principal]
    for idx, recorrido, nodo, alternativas in cruces:
        if len(caminos) >= MAX_RAMAS or recorrido > LONG_APROXIMACION - MIN_RAMA:
            break
        for w, _via in alternativas:
            if len(caminos) >= MAX_RAMAS:
                break
            hasta = recorrido + distancia(coords[w], coords[nodo])
            puntos, _, total, _ = seguir(w, nodo, hasta, principal[:idx] + [coords[w]], nodos[:idx] + [w],
                                         set(nodos[1:idx]) | {v, w})
            if total - recorrido >= MIN_RAMA:
                caminos.append(puntos)
    resultado = []
    for camino in caminos:
        camino = list(reversed(camino))  # de lejos hacia el radar
        if sum(distancia(a, b) for a, b in zip(camino, camino[1:])) >= MIN_APROXIMACION:
            resultado.append(codificar_polilinea(simplificar(camino)))
    return resultado


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
    if d.replace(".", "").isdigit() and d != "1":
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


# ----------------------------------------------------------------------------- OpenStreetMap (fichero .osm.pbf)


def _etiquetas(objeto):
    return {t.k: t.v for t in objeto.tags}


def leer_pbf_nodos(ruta):
    """Radares (highway=speed_camera) e hitos kilométricos (highway=milestone)."""
    import osmium
    camaras, hitos = [], []
    filtro = osmium.filter.TagFilter(("highway", "speed_camera"), ("highway", "milestone"))
    for n in osmium.FileProcessor(ruta, osmium.osm.NODE).with_filter(filtro):
        if not n.location.valid():
            continue
        e = {"type": "node", "id": n.id, "lat": n.location.lat, "lon": n.location.lon, "tags": _etiquetas(n)}
        (camaras if e["tags"].get("highway") == "speed_camera" else hitos).append(e)
    return camaras, hitos


def leer_pbf_vias(ruta, centros, radio):
    """Carreteras de coche con algún punto a menos de ~radio metros de alguno de los centros."""
    import osmium
    patron = re.compile(VIAS)
    celda = 0.01
    zona = set()
    for lat, lon in centros:
        n_lat = int(radio / 111000 / celda) + 1
        n_lon = int(radio / (111000 * max(0.2, math.cos(math.radians(lat)))) / celda) + 1
        ci, cj = math.floor(lat / celda), math.floor(lon / celda)
        for i in range(ci - n_lat, ci + n_lat + 1):
            for j in range(cj - n_lon, cj + n_lon + 1):
                zona.add((i, j))
    vias = []
    procesador = (osmium.FileProcessor(ruta, osmium.osm.NODE | osmium.osm.WAY)
                  .with_locations()
                  .with_filter(osmium.filter.EntityFilter(osmium.osm.WAY))
                  .with_filter(osmium.filter.KeyFilter("highway")))
    for w in procesador:
        if not patron.match(w.tags.get("highway", "")) or w.tags.get("area") == "yes":
            continue
        if w.tags.get("access") in ("no", "private") or w.tags.get("motor_vehicle") in ("no", "private"):
            continue
        if not all(nd.location.valid() for nd in w.nodes):
            continue  # vía cortada por el borde del fichero
        puntos = [(nd.location.lat, nd.location.lon) for nd in w.nodes]
        if not any((math.floor(la / celda), math.floor(lo / celda)) in zona for la, lo in puntos):
            continue
        vias.append({"type": "way", "id": w.id, "nodes": [nd.ref for nd in w.nodes], "tags": _etiquetas(w),
                     "geometry": [{"lat": la, "lon": lo} for la, lo in puntos]})
    return vias


# Radares del mapa que no son radares fijos de velocidad (o ya no existen).
PALABRAS_DESCARTE = [
    ("móvil o remolque", r"m[oó]vil|remolc|remolque|itinerante"),
    ("pedagógico", r"pedag[oòó]gic|no multa|informativ"),
    ("semáforo en rojo", r"sem[aàá]for|paso en rojo|pas en vermell|en rojo"),
    ("cinturón", r"cintur"),
    ("retirado o erróneo", r"no es un radar|no existe|ya no hay|retirad|desmontad|eliminad|obviously wrong|position is wrong"),
]


def motivo_descarte(tags):
    if tags.get("temporary") == "yes":
        return "móvil o remolque"
    if tags.get("enforcement") == "traffic_signals":
        return "semáforo en rojo"
    if any(k.split(":")[0] in ("disused", "removed", "abandoned", "demolished", "was") for k in tags):
        return "retirado o erróneo"
    texto = " ".join(v for k, v in tags.items() if k.split(":")[0] in ("note", "description", "fixme", "name") or k.startswith("note"))
    for motivo, patron in PALABRAS_DESCARTE:
        if re.search(patron, texto, re.IGNORECASE):
            return motivo
    return None


def lado_de_la_calzada(p, arista, coords):
    """Sentido de la arista en el que el punto queda a la derecha (o None si está sobre la vía)."""
    d, _, u, v, _ = arista
    if d < 2.5:
        return None
    a, b = coords[u], coords[v]
    ax, ay = a_metros(a, b)
    px, py = a_metros(a, p)
    izquierda = ax * py - ay * px > 0
    rb = rumbo(a, b)
    return (rb + 180) % 360 if izquierda else rb


def unico_sentido_cercano(p, lista, coords, cercana):
    """Si todas las calzadas junto al radar van en el mismo sentido (calzada de sentido único), ese rumbo."""
    rb = rumbo(coords[cercana[2]], coords[cercana[3]])
    junto = [a for a in lista if proyeccion(p, coords[a[0]], coords[a[1]])[0] <= cercana[0] + 12]
    if all(dif_angulo(rumbo(coords[u], coords[v]), rb) <= 60 for u, v, _ in junto):
        return rb
    return None


# ----------------------------------------------------------------------------- proceso principal


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argp = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    argp.add_argument("--pbf", required=True, help="fichero .osm.pbf de OpenStreetMap (Geofabrik)")
    argp.add_argument("--salida", default=SALIDA, help="dónde escribir radares.json")
    argp.add_argument("--zona", help="solo los radares dentro de lat1,lon1,lat2,lon2 (para pruebas con un fichero regional)")
    argp.add_argument("--sin-descargas", action="store_true", help="usa las copias guardadas de la DGT y del SCT")
    args = argp.parse_args()
    os.makedirs(CACHE, exist_ok=True)
    inicio = time.monotonic()

    def reloj():
        return f"[{(time.monotonic() - inicio) / 60:.1f} min]"

    # 1. Listas oficiales
    ruta_dgt = os.path.join(CACHE, "dgt_radares.xml")
    ruta_sct = os.path.join(CACHE, "sct_radares.txt")
    if not args.sin_descargas:
        print("Descargando radares de la DGT y del Servei Català de Trànsit…", flush=True)
        descargar_con_copia(URL_DGT, ruta_dgt, lambda d: b"predefinedLocation" in d, "la DGT")
        descargar_con_copia(URL_SCT, ruta_sct, lambda d: d.count(b"\n") > 50, "el SCT")
    radares, publicado_dgt = leer_dgt(ruta_dgt)
    sct = leer_sct(ruta_sct)
    print(f"DGT: {sum(r['tipo'] == 'F' for r in radares)} fijos, {sum(r['tipo'] == 'T' for r in radares)} tramos "
          f"(publicado {publicado_dgt}) · SCT: {len(sct)} fijos")
    radares += sct

    # 2. OSM: radares e hitos
    print(f"{reloj()} Leyendo radares e hitos del mapa…", flush=True)
    camaras, nodos_hito = leer_pbf_nodos(args.pbf)
    descartes = Counter()
    validas = []
    for c in camaras:
        motivo = motivo_descarte(c["tags"])
        if motivo:
            descartes[motivo] += 1
        else:
            validas.append(c)
    camaras = validas
    print(f"OSM: {len(camaras)} radares válidos; descartados {sum(descartes.values())}: {dict(descartes)}")

    if args.zona:
        la1, lo1, la2, lo2 = (float(x) for x in args.zona.split(","))

        def dentro(q):
            return min(la1, la2) <= q[0] <= max(la1, la2) and min(lo1, lo2) <= q[1] <= max(lo1, lo2)

        radares = [r for r in radares if dentro(r["punto"])]
        camaras = [c for c in camaras if dentro((c["lat"], c["lon"]))]

    hitos = indice_hitos(nodos_hito)
    # Los propios radares oficiales son "hitos": coordenadas con su punto kilométrico oficial.
    for r in radares:
        if r.get("carretera") and r.get("pk") is not None:
            hitos[normalizar_ref(r["carretera"])].append((r["punto"], r["pk"]))
            if r["tipo"] == "T" and r.get("pk_fin") is not None:
                hitos[normalizar_ref(r["carretera"])].append((r["fin"], r["pk_fin"]))
    comprobar_tramos([r for r in radares if r["fuente"] == "DGT"], hitos)

    # 3. Carreteras alrededor de todos los radares
    print(f"{reloj()} Leyendo las carreteras alrededor de los radares…", flush=True)
    centros = [r["punto"] for r in radares] + [r["fin"] for r in radares if r.get("fin")] + [(c["lat"], c["lon"]) for c in camaras]
    red = Red()
    red.anadir(leer_pbf_vias(args.pbf, centros, RADIO_RED))
    print(f"{reloj()} {len(red.vias)} tramos de carretera cargados")
    ids_camara = {c["id"] for c in camaras}
    via_de_nodo = {}
    for via in red.vias.values():
        for n in via["nodes"]:
            if n in ids_camara:
                via_de_nodo.setdefault(n, via)

    # 4. Emparejar radares oficiales con OSM (mismo lugar y sentido compatible); el resto de OSM, «posibles»
    rumbo_camara = {c["id"]: rumbo_osm(c, via_de_nodo, red.coords) for c in camaras}
    usadas = set()
    for r in radares:
        r["oficial"] = True
        r["rumbo_aprox"] = rumbo_dgt_aproximado(r, hitos)  # DGT y tramos del SCT: PK oficial + hitos

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
        radares.append({"id": f"OSM-{c['id']}", "tipo": "F", "fuente": "OSM", "punto": (c["lat"], c["lon"]),
                        "carretera": None, "pk": None, "relativo": None, "sentido": None, "osm": c,
                        "oficial": False, "rumbo_aprox": None})
    for r in radares:
        r["limite"] = r.get("limite_oficial") or (numero(r["osm"]["tags"].get("maxspeed")) if "osm" in r else None)
    print(f"Total: {len(radares)} radares ({sum(r['oficial'] for r in radares)} oficiales, "
          f"{sum(not r['oficial'] for r in radares)} posibles, solo en OSM)")

    # 5. Carretera, sentido, límite y caminos de aproximación de cada radar
    print(f"{reloj()} Calculando sentidos y aproximaciones…", flush=True)
    metodos = Counter()
    salida = []
    for r in radares:
        p = r["punto"]
        entrantes, lista = aristas_dirigidas(red.vias_cerca(p, RADIO_RED))
        via_osm = via_de_nodo.get(r["osm"]["id"]) if "osm" in r else None
        if r.get("carretera"):
            clave = ("ref", frozenset({normalizar_ref(r["carretera"])}))
        elif via_osm is not None:
            clave = clave_carretera(via_osm.get("tags", {}))
        else:
            cercana = arista_mas_cercana(p, [a for a in lista if clave_carretera(a[2].get("tags", {}))], red.coords)
            clave = clave_carretera(cercana[4].get("tags", {})) if cercana else None
        propia = [a for a in lista if misma_carretera(a[2].get("tags", {}), clave)] if clave else lista
        max_dist = MAX_DIST_OFICIAL if r["oficial"] else MAX_DIST_RADAR_VIA
        if not arista_mas_cercana(p, propia, red.coords, max_dist=max_dist):
            propia = lista  # la carretera indicada no está a mano: cualquier vía junto al radar
        cercana = arista_mas_cercana(p, propia, red.coords, max_dist=max_dist)
        if r.get("carretera") is None and cercana is not None:
            tags = cercana[4].get("tags", {})
            r["carretera"] = (tags.get("ref") or "").split(";")[0] or tags.get("name")

        # Sentido vigilado: uno seguro (o el más probable) y, si hay dudas, el otro como «dudoso»
        rumbo_osm_c = rumbo_camara.get(r["osm"]["id"]) if "osm" in r else None
        seguro, dudosos, metodo = None, [], "desconocido"
        if r["tipo"] == "T":
            seguro, metodo = rumbo(r["punto"], r["fin"]), "tramo"
        elif r["rumbo_aprox"] is not None:
            seguro, metodo = r["rumbo_aprox"], "dgt_pk"
            if rumbo_osm_c is not None and dif_angulo(rumbo_osm_c, seguro) > 90:
                dudosos.append(rumbo_osm_c)  # el mapa dice otra cosa: se avisa también, como posible
                metodo = "dgt_pk_con_dudas"
        elif rumbo_osm_c is not None:
            seguro, metodo = rumbo_osm_c, "osm"
        elif cercana is not None:
            unico = unico_sentido_cercano(p, propia, red.coords, cercana)
            if unico is not None:
                seguro, metodo = unico, "sentido_unico"
            else:
                rb = rumbo(red.coords[cercana[2]], red.coords[cercana[3]])
                lado = lado_de_la_calzada(p, cercana, red.coords)
                if lado is not None:
                    seguro, metodo = lado, "lado_calzada"
                    dudosos.append((lado + 180) % 360)
                else:
                    dudosos += [rb, (rb + 180) % 360]
        if seguro is not None and cercana is not None:  # alinear con la carretera real junto al radar
            alineada = arista_mas_cercana(p, propia, red.coords, rumbo_deseado=seguro, tolerancia=90, max_dist=max_dist)
            if alineada:
                seguro = rumbo(red.coords[alineada[2]], red.coords[alineada[3]])
        metodos[metodo] += 1

        def aproximaciones(rb):
            arista = arista_mas_cercana(p, propia, red.coords, rumbo_deseado=rb, tolerancia=60, max_dist=max_dist)
            if arista is None:
                return [], None
            tags = arista[4].get("tags", {})
            adelante = arista[4]["nodes"].index(arista[2]) < arista[4]["nodes"].index(arista[3])
            limite = numero(tags.get("maxspeed:forward" if adelante else "maxspeed:backward")) or numero(tags.get("maxspeed"))
            return caminos_aproximacion(p, arista, entrantes, red.coords, clave), limite

        ap, limite_via = aproximaciones(seguro) if seguro is not None else ([], None)
        ap2 = []
        for rb in dudosos:
            if seguro is not None and dif_angulo(rb, seguro) < 60:
                continue
            caminos, lim = aproximaciones(rb)
            ap2 += caminos
            limite_via = limite_via or lim
        limite = r.get("limite") or limite_via

        item = {"id": r["id"], "t": r["tipo"], "la": round(p[0], 6), "lo": round(p[1], 6), "f": r["fuente"]}
        if r["oficial"]:
            item["o"] = 1
        if r.get("carretera"):
            item["c"] = r["carretera"]
        if r.get("pk") is not None:
            item["pk"] = round(r["pk"], 2)
        if r.get("sentido"):
            item["s"] = r["sentido"]
        if seguro is not None:
            item["r"] = round(seguro, 1)
        if limite:
            item["lim"] = limite
        if r["tipo"] == "T":
            item["fla"], item["flo"] = round(r["fin"][0], 6), round(r["fin"][1], 6)
            if r.get("pk_fin") is not None and r.get("pk") is not None:
                item["len"] = int(abs(r["pk_fin"] - r["pk"]) * 1000)
            else:
                item["len"] = int(distancia(p, r["fin"]))
        if ap:
            item["ap"] = ap
        if ap2:
            item["ap2"] = ap2
        salida.append(item)

    # 6. Guardar
    ruta_salida = os.path.abspath(args.salida)
    os.makedirs(os.path.dirname(ruta_salida), exist_ok=True)
    resultado = {
        "version": 1,
        "generado": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "dgt_publicado": publicado_dgt,
        "atribucion": "Radares: DGT (nap.dgt.es), Servei Català de Trànsit y © colaboradores de OpenStreetMap (ODbL)",
        "radares": salida,
    }
    temporal = ruta_salida + ".tmp"
    with open(temporal, "w", encoding="utf-8") as f:
        json.dump(resultado, f, ensure_ascii=False, separators=(",", ":"))
    shutil.move(temporal, ruta_salida)

    con_ap = sum(1 for s in salida if "ap" in s or "ap2" in s)
    print(f"\n{reloj()} Resumen")
    print(f"  Radares: {len(salida)}  (fijos {sum(s['t'] == 'F' for s in salida)}, tramos {sum(s['t'] == 'T' for s in salida)}, "
          f"oficiales {sum('o' in s for s in salida)})")
    print(f"  Con camino de aproximación: {con_ap} ({100 * con_ap // max(1, len(salida))} %)")
    print(f"  Con sentido seguro o probable: {sum('ap' in s for s in salida)} · solo dudosos: "
          f"{sum('ap2' in s and 'ap' not in s for s in salida)}")
    print(f"  Con límite de velocidad: {sum(1 for s in salida if 'lim' in s)}")
    print(f"  Origen del sentido vigilado: {dict(metodos)}")
    print(f"  Fichero: {ruta_salida} ({os.path.getsize(ruta_salida) // 1024} KB)")


if __name__ == "__main__":
    main()
