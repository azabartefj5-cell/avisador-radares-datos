# Base de radares para el avisador

Este repositorio contiene la base de radares fijos y de tramo de España que usa la app
Avisador de radares, y la tarea que la mantiene al día.

- `datos/version.json`: la fecha de la base, el número de radares y la huella sha256.
- `datos/radares.json.gz`: la base completa, con los caminos de aproximación de cada radar.
- `herramientas/generar_radares.py`: descarga y cruza los datos de la DGT y de OpenStreetMap.
- `herramientas/publicar.py`: comprueba la base nueva y, si tiene sentido, la publica.
- `herramientas/cache/`: copia de lo ya descargado, para no pedirlo otra vez.
- `.github/workflows/actualizar.yml`: la tarea programada (lunes y jueves).

La app consulta `version.json` una vez al día. Si la base ha cambiado, la descarga, la
comprueba y la instala. Si algo falla, sigue usando la base que ya tenía.

## Para lanzarla a mano

Ve a la pestaña *Actions*, abre **Actualizar radares** y pulsa **Run workflow**.

## Fuentes y licencia

- Radares de la DGT: Punto de Acceso Nacional de tráfico ([nap.dgt.es](https://nap.dgt.es)),
  datos reutilizables citando la fuente.
- Radares, carreteras e hitos kilométricos: © colaboradores de
  [OpenStreetMap](https://www.openstreetmap.org/copyright), bajo licencia
  [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).

La base derivada (`datos/`) se publica con la misma licencia ODbL 1.0.

Solo se incluyen radares fijos y de tramo, en ubicaciones publicadas. No se incluyen
radares móviles ni controles policiales.
