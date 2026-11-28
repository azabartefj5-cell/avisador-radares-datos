#!/usr/bin/env python3
"""
Prepara la base de radares para que la descarguen los móviles.

Lee el radares.json que acaba de generar generar_radares.py, comprueba que tiene
sentido comparándolo con la versión ya publicada y, si es así, escribe en datos/:

    radares.json.gz   la base comprimida
    version.json      fecha, número de radares y huella (sha256) del .gz

La app solo consulta version.json (unos cientos de bytes) y baja el .gz cuando cambia.

Si el fichero nuevo parece roto (pocos radares, coordenadas fuera de España, muchos
menos caminos de aproximación que antes...) NO se publica y el script termina con
código 2: los móviles siguen con la versión anterior.

Uso:
    python herramientas/publicar.py [--entrada build/radares.json] [--destino datos]
"""
import argparse
import gzip
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MINIMO_RADARES = 1500          # España tiene ~2.700: menos de esto es un fallo seguro
CAIDA_MAXIMA_RADARES = 0.15    # perder más del 15 % de radares de golpe no es normal
CAIDA_MAXIMA_APROXIMACION = 0.10
LAT, LON = (27.0, 44.5), (-18.5, 4.5)  # España con Canarias


def ahora():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def huella_contenido(base):
    """Huella de los radares sin la fecha de generación: detecta si algo cambió de verdad."""
    return hashlib.sha256(json.dumps(base["radares"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def problemas(base, anterior):
    """Lista de motivos para NO publicar (vacía si todo está bien)."""
    fallos = []
    if base.get("version") != 1 or not isinstance(base.get("radares"), list):
        return ["formato desconocido"]
    radares = base["radares"]
    if len(radares) < MINIMO_RADARES:
        fallos.append(f"solo {len(radares)} radares (mínimo {MINIMO_RADARES})")
    ids = set()
    for r in radares:
        if not isinstance(r.get("id"), str) or r.get("t") not in ("F", "T"):
            fallos.append(f"radar mal formado: {str(r)[:80]}")
            break
        if not (LAT[0] < r.get("la", 0) < LAT[1] and LON[0] < r.get("lo", 99) < LON[1]):
            fallos.append(f"radar {r['id']} fuera de España ({r.get('la')}, {r.get('lo')})")
            break
        if r["id"] in ids:
            fallos.append(f"radar repetido: {r['id']}")
            break
        ids.add(r["id"])
    if anterior:
        n0, n1 = anterior["radares"], len(radares)
        if n1 < n0 * (1 - CAIDA_MAXIMA_RADARES):
            fallos.append(f"de {n0} a {n1} radares de golpe")
        a0, a1 = anterior.get("con_aproximacion", 0), sum(1 for r in radares if r.get("ap") or r.get("ap2"))
        if a1 < a0 * (1 - CAIDA_MAXIMA_APROXIMACION):
            fallos.append(f"de {a0} a {a1} radares con camino de aproximación (¿falló la lectura del mapa?)")
    return fallos


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argp = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    argp.add_argument("--entrada", default=os.path.join(RAIZ, "build", "radares.json"))
    argp.add_argument("--destino", default=os.path.join(RAIZ, "datos"))
    args = argp.parse_args()

    with open(args.entrada, encoding="utf-8") as f:
        texto = f.read()
    base = json.loads(texto)
    ruta_version = os.path.join(args.destino, "version.json")
    ruta_gz = os.path.join(args.destino, "radares.json.gz")
    anterior = None
    if os.path.exists(ruta_version) and os.path.exists(ruta_gz):
        with open(ruta_version, encoding="utf-8") as f:
            anterior = json.load(f)

    fallos = problemas(base, anterior)
    if fallos:
        print("NO se publica la base nueva:")
        for m in fallos:
            print(f"  - {m}")
        sys.exit(2)

    contenido = huella_contenido(base)
    os.makedirs(args.destino, exist_ok=True)
    if anterior and anterior.get("contenido") == contenido:
        # Sin cambios: solo se anota la comprobación (y el repositorio registra actividad,
        # lo que evita que GitHub desactive la tarea programada por inactividad).
        anterior["comprobado"] = ahora()
        version = anterior
        print("Los radares no han cambiado; se anota la comprobación.")
    else:
        # mtime=0: el mismo contenido produce siempre el mismo .gz
        datos_gz = gzip.compress(texto.encode("utf-8"), compresslevel=9, mtime=0)
        with open(ruta_gz + ".tmp", "wb") as f:
            f.write(datos_gz)
        os.replace(ruta_gz + ".tmp", ruta_gz)
        version = {
            "formato": 1,
            "generado": base["generado"],
            "dgt_publicado": base.get("dgt_publicado"),
            "radares": len(base["radares"]),
            "con_aproximacion": sum(1 for r in base["radares"] if r.get("ap") or r.get("ap2")),
            "oficiales": sum(1 for r in base["radares"] if r.get("o")),
            "archivo": "radares.json.gz",
            "bytes": len(datos_gz),
            "sha256": hashlib.sha256(datos_gz).hexdigest(),
            "contenido": contenido,
            "comprobado": ahora(),
        }
        print(f"Publicada la base del {version['generado']}: {version['radares']} radares, "
              f"{version['con_aproximacion']} con camino de aproximación, {version['bytes'] // 1024} KB.")
    with open(ruta_version + ".tmp", "w", encoding="utf-8") as f:
        json.dump(version, f, ensure_ascii=False, indent=1)
    os.replace(ruta_version + ".tmp", ruta_version)


if __name__ == "__main__":
    main()
