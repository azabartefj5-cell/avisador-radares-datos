# Base de radares para el avisador

Este repositorio contiene la base de radares fijos y de tramo de España que usa la app
Avisador de radares, y la tarea que la mantiene al día.

- `datos/version.json`: la fecha de la base, el número de radares y la huella sha256.
- `datos/radares.json.gz`: la base completa, con los caminos de aproximación de cada radar.
- `herramientas/generar_radares.py`: cruza las listas oficiales (DGT y Servei Català de Trànsit) con el mapa de OpenStreetMap.
- `herramientas/publicar.py`: comprueba la base nueva y, si tiene sentido, la publica.
- `herramientas/cache/`: última copia buena de las listas oficiales, por si sus servidores no responden.
- `.github/workflows/actualizar.yml`: la tarea programada (lunes y jueves).

La app consulta `version.json` una vez al día. Si la base ha cambiado, la descarga, la
comprueba y la instala. Si algo falla, sigue usando la base que ya tenía.

## Para lanzarla a mano

Ve a la pestaña *Actions*, abre **Actualizar radares** y pulsa **Run workflow**.

## Fuentes y licencia

- Radares de la DGT: Punto de Acceso Nacional de tráfico ([nap.dgt.es](https://nap.dgt.es)),
  datos reutilizables citando la fuente.
- Radares fijos de Cataluña: Servei Català de Trànsit
  ([transit.gencat.cat](https://transit.gencat.cat)), datos abiertos de la Generalitat de Catalunya.
- Radares, carreteras e hitos kilométricos: © colaboradores de
  [OpenStreetMap](https://www.openstreetmap.org/copyright), bajo licencia
  [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).

La base derivada (`datos/`) se publica con la misma licencia ODbL 1.0.

Solo se incluyen radares fijos y de tramo. Cada radar indica si su posición es oficial
(`"o": 1`, de la DGT o del SCT) o si solo viene de OpenStreetMap; en ese caso, la app lo
anuncia como «posible radar». También se anuncian como posibles los sentidos dudosos
(`ap2`). No se incluyen radares móviles, radares de semáforo ni controles policiales.
