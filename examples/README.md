# Corpus de ejemplo

Quince documentos sinteticos para ver el sistema funcionando sin usar material
propio. Estan armados para que el recorrido tenga algo que mostrar:

- **proyecto-faro** — sensores costeros; el problema es la *deriva de calibracion*.
- **proyecto-telar** — anotacion colaborativa; el problema es la *deriva de criterio*.
- **proyecto-mirador** — visualizacion; el problema es la *legibilidad* por densidad.
- **biblioteca** — tres textos conceptuales que cruzan los tres.

Los dos primeros proyectos no se mencionan entre si, pero comparten el mismo
problema con distinto vocabulario. Es exactamente el caso que el minero de
descubrimiento busca: un puente latente entre cosas que no se citan.

## Probarlo

El corpus se **copia** a un directorio de trabajo aparte antes de indexar, para
no mezclar el corpus con el directorio de instalacion:

    mkdir -p /tmp/demo && cp -a examples/corpus/. /tmp/demo/
    ./install.sh --root /tmp/demo

    export MAPA_ROOT=/tmp/demo
    <venv>/bin/python <code-home>/tier1.py index --scope total
    <venv>/bin/python <code-home>/tier1.py search "deriva"
    <venv>/bin/python <code-home>/serve.py        # atlas en http://127.0.0.1:8899/atlas/

La busqueda de "deriva" deberia traer documentos de los dos proyectos, aunque
solo uno use esa palabra en el titulo: ahi se ve la parte semantica del indice
haciendo su trabajo.
