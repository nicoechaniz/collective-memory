# Arquitectura

Cinco capas. Cada una consume la anterior y ninguna necesita a la siguiente:
el sistema es util quedandose en la primera.

## 1. Indice

`tier1.py` recorre el corpus segun una **politica auditable**
(`corpus_policy.json`): que extensiones entran, con que peso, que directorios se
excluyen y que archivos nunca se tocan (claves, credenciales, tokens).

Cada documento se parte en chunks y se indexa dos veces: en **FTS5** para
busqueda lexica, y como **vectores** de un modelo de embeddings local, guardados
en la misma base con `sqlite-vec`. La consulta corre las dos y las combina con
*reciprocal rank fusion*, ponderando por tipo de documento.

Codigo y configuracion se indexan solo lexicamente: agruparlos por embeddings
los junta por tokens superficiales y ensucia la vecindad semantica.

## 2. Mapa

`librarian.sh` compila un mapa markdown —un nodo por proyecto— y lo publica.
Tres propiedades que definen el diseño:

**Un solo escritor.** Todo corre bajo `flock`. Dos corridas simultaneas no se
pisan porque la segunda no arranca.

**Publicacion atomica.** Se compila en un directorio de staging, se renombra a un
snapshot con identificador de corrida, y recien ahi se mueve el symlink. Un
lector ve la version vieja o la nueva, nunca una a medias.

**Idempotente.** El manifiesto guarda un hash por nodo; recompilar sin cambios da
el mismo resultado.

La parte mecanica es determinista. La parte semantica —redactar el contenido de
cada nodo— la hace un agente, y sus resultados entran como *overlays* que el
harness instala en el staging bajo el mismo lock.

## 3. Atlas

`ui_builder.py` proyecta el indice a JSON estatico: nodos, aristas de vecindad
semantica, arbol de proyectos y estadisticas. Todo viaja en el mismo renombre
atomico que el resto de la generacion.

Sobre el grafo de vecinos corre **Leiden** para detectar comunidades. La
particion se calcula una vez por corrida completa y se sirve como artefacto, con
un hash determinista: misma entrada, misma particion. Si el artefacto falta o no
coincide con la generacion publicada, el frontend cae a una deteccion local — el
atlas se degrada, no se rompe.

`serve.py` sirve todo eso en modo estrictamente lectura: no hay un solo endpoint
de escritura, y los documentos salen de la base, no del filesystem.

## 4. Descubrimiento

`discover_operators.py` implementa seis operadores sobre el espacio vectorial:
puentes latentes, fronteras entre proyectos, outliers, tensiones, analogias y
evidencia fresca que contradice lo viejo.

Cada candidato pasa por un filtro LLM local y una etapa de **falsacion**: se le
pide al modelo que intente refutarlo. Los que sobreviven quedan en un registro
privado con estado `candidate`. Nada se publica sin revision humana, y la
promocion exige un minimo de fuentes primarias trazables.

Hay un **prior de inesperadez**: los pares que cruzan comunidades poco conectadas
entre si se evaluan primero. Reordena, no filtra; si el artefacto de comunidades
falta, el prior simplemente no actua.

## 5. Director (opcional, apagado)

Un agente que corre campañas sin supervision. Dos unidades con usuarios
distintos: el **mediador** habla con el indice, la GPU y el registro, sin
internet; el **modelo** razona en un namespace donde el corpus no existe.

La frontera es de **procesos**, no de configuracion del CLI: el CLI de agente
tiene shell y no hay forma de quitarsela, asi que la contencion tiene que estar
afuera. Leer [seguridad.md](seguridad.md) antes de encenderlo — hay riesgos que
esta arquitectura no cubre y conviene conocerlos.

El director nunca confirma un hallazgo: llega hasta marcarlo como interesante o
descartarlo. La confirmacion es del dueño, y eso es un guard en codigo.

## Dos raices

`MAPA_ROOT` son los datos; `CODE_HOME` es el codigo. Estan separados porque en el
sistema original eran el mismo directorio, y eso hacia imposible instalarlo en
otro lado. El control plane del director vive fuera de ambos, en `/var/lib`.
