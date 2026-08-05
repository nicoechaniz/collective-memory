# Operacion

## Ciclo normal

```bash
MAPA_ROOT=<corpus> bash scripts/librarian.sh
```

Consume las notas del inbox, recompila el manifiesto, publica un snapshot nuevo y
reindexa. Es idempotente: correrlo dos veces sin cambios da lo mismo.

Dos modos de atlas:

- `MAPA_UI_MODE=fast` (default) — reusa las aristas semanticas existentes.
- `MAPA_UI_MODE=full` — las recalcula y emite el artefacto de comunidades. Usa
  GPU si hay.

Solo el modo completo emite las comunidades. Despues de una corrida rapida el
atlas cae a deteccion local y el prior de inesperadez del minero queda inactivo:
ninguna de las dos cosas rompe nada, pero explica por que a veces los colores
cambian.

Cada publicacion genera tambien `ui_v2.db` dentro de la misma generacion del
atlas. Es una proyeccion de lectura para la V3; si hiciera falta reconstruirla
sin reindexar ni usar GPU:

```bash
MAPA_ROOT=<corpus> MAPA_DATA=<corpus>/.mapa \
  <venv>/bin/python <code-home>/ui_v2_store.py
```

## Notas al inbox

Un archivo por nota en `<corpus>/inbox/new/`, con nombre unico. El bibliotecario
las consume en la proxima corrida y las mueve a `done/`. Nunca se edita el mapa
publicado a mano: lo reescribe el proximo snapshot.

## Proveedor de modelo (providers.json)

La capa de descubrimiento y el Lab leen `$MAPA_DATA/director/providers.json` y
son **fail-closed**: sin ese archivo, cualquier tarea LLM aborta con `RuntimeError`
(a propósito — nunca degradan a un default silencioso). `install.sh` lo instala
desde el ejemplo con ollama local por defecto; editalo para apuntar a otro
proveedor. El indexado y la búsqueda del núcleo **no** dependen de esto.

## Descubrimiento

```bash
<venv>/bin/python <code-home>/discover.py run --operators tension --limit 20
<venv>/bin/python <code-home>/discover.py list --status candidate
<venv>/bin/python <code-home>/discover.py review <id> --status interesting
```

Los candidatos viven en un registro privado y **nunca** se sirven. Promover a
hallazgo publicado exige revision del dueño y un minimo de fuentes primarias
trazables.

### Identidad del dueño

`MAPA_OWNER_REVIEWER` (default `owner`) no es una etiqueta: es un guard de
autorizacion. Solo ese reviewer puede confirmar hallazgos; el resto queda
limitado a marcar interesante o descartado.

Si venis de una base anterior con otro nombre de dueño, poné ese valor en la
configuracion en vez de renombrar filas — el guard compara contra la constante.

## Director

```bash
bash scripts/director.sh --backend codex --dry
```

`--dry` acota lo que el modelo puede hacer, pero **no** evita las escrituras del
wrapper: crea su directorio de logs, reescribe la politica de la corrida, escribe
tokens y publica un digest. Para probarlo sin tocar una instalacion en uso hay
que levantar un corpus y un code-home aparte, con sus propios usuarios.

El director llega hasta marcar interesante o descartar. La confirmacion es
siempre humana.

## Tests

```bash
python3 tests/test_safe_bind.py            # invariante de bind; corre en CI
python3 tests/test_ui_v2.py                 # proyeccion, API y sesiones V3
python3 tests/test_structural_mode.py       # el perfil demo bloquea LLM/director
python3 tests/test_inyeccion_indirecta.py  # requiere modelo local + GPU
```

El segundo verifica las defensas del agente de descubrimiento contra inyeccion
indirecta —documentos hostiles dentro del corpus—: que cite identificadores
reales, que no siga instrucciones incrustadas, que respete su presupuesto y que
no filtre su prompt. Necesita un modelo corriendo, asi que no va en CI.

## Cuando algo falla

**El indice sale vacio y no hay error.** Probablemente el corpus este bajo
`/home` o `/root` con los servicios en systemd: `ProtectHome=yes` los deja vacios
dentro del namespace.

**El atlas no muestra el grafo.** Falta `node`, que el pipeline ejecuta en cada
corrida para el layout.

**El minero no encuentra el modelo.** Si instalaste con systemd y el proveedor
esta en otra maquina, revisá el CIDR de salida: el filtro de red gobierna
ingress y egress a la vez.

**Nadie puede autenticarse en el Lab.** El grupo de servicio no existe o no
coincide. Esto antes fallaba en silencio; ahora corta con un mensaje explicito.

## Exchange v1

La exportación y la publicación revisada se operan con
`mapa/exchange_cli.py`. Configure capacidades distintas owner-only y el trust
store Ed25519 fuera del repositorio. Si existe
`.mapa/exchange/v1/publisher/publication.fence.json` después de un reinicio,
ejecute `publication-recover` antes de reabrir búsqueda o Atlas. No borre el
fence ni edite journals manualmente: recovery decide all-old o all-new contra el
state hash vigente. Véase [exchange-v1.md](exchange-v1.md).
