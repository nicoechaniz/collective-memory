# Bibliotecario del mapa de memoria colectiva

Rol: **único escritor** del mapa del corpus. Compila señales crudas (cambios del disco + notas del inbox) en el mapa markdown, de forma incremental y atómica. Se invoca **a mano**: como `codex exec` corriendo el harness, o disparado por Claude vía el MCP de Codex (`sandbox: workspace-write` acotado a la raíz del corpus).

## Dos capas

- **Mecánica (determinista):** `scripts/librarian.sh` — `flock` (single-writer), ingest del inbox (cola durable), manifest de hashes por nodo, `status.md`/`log.md`, publish por **snapshot swap atómico** (staging → `mapa.<run_id>/` → flip del symlink `mapa`), poda con grace. Es **idempotente**.
- **Semántica (LLM):** compilar/actualizar cada `proyectos/<id>.md` a partir de sus `source_paths`. Lo hace el agente (Codex) cuando un hash cambió o llega una nota relevante. El primer sub-mapa compilado sirve de **molde** a imitar (frontmatter + secciones). Las fuentes compiladas viven en `.mapa/overlays/mapa/` (`index.md` y `proyectos/**/*.md`); el harness las instala en su staging bajo `flock` antes de publicar.

## Ciclo (diseño §6: PLAN_MVP_memoria_colectiva.md)

1. **Ingest** — detectar cambios por **hash de contenido** contra `.mapa/manifest.json` (git diff como acelerador donde haya repo; mtime solo hint). Respetar el **alcance** con exclusiones: `.git venv node_modules __pycache__ data models results hf-cache dist build` y **áreas privadas** (dirs `drwx------`, p.ej. una `Biblioteca/` privada). Ante `EACCES`: registrar el path como no-inspeccionable en `status.md`, **no abortar**. Consumir `inbox/new/`.
2. **Compile** — recompilar solo el sub-mapa del proyecto afectado en `.mapa/overlays/mapa/proyectos/`; `index.md` entero (barato) en `.mapa/overlays/mapa/index.md`. Declarar backlinks en el frontmatter (`links:`).
3. **Lint** — dedup, contradicciones, huérfanos, enlaces rotos.
4. **Publish** — vía `librarian.sh` (atómico; nunca in-place).
5. **Index** — `manifest.json` (MVP). SQLite+FTS5+Model2Vec = fase 2.

## Reglas de oro

- Nunca editar el snapshot vivo in-place; compilar en staging y publicar por flip del symlink.
- Nunca correr dos veces en paralelo (`flock` lo impide).
- La identidad de nodo es el `id` (frontmatter), no el path físico del snapshot.
- El mapa es la única fuente **compartida**; las memorias privadas de cada agente son aparte.

## Escalar del piloto a los 17

Con el primer sub-mapa validado como molde: por cada proyecto pendiente del `index.md`, correr el paso semántico (leer sus docs/estructura respetando exclusiones y privados) → escribir `proyectos/<id>.md` con el mismo schema → `librarian.sh` publica. Una corrida por proyecto.

## Barrios semánticos (F21 — Leiden)

El build **`ui_builder.py build --mode full`** ahora emite además `ui/communities.json`
(+ `.gz`): la partición **Leiden** de los ~2.3k doc-vectors sobre el grafo knn colapsado a
no dirigido, con `partition_hash` determinista (seed fijo + renumeración canónica). Viaja en
el mismo rename atómico de la generación. Lo consumen dos lados, ambos **fail-open**: (1) el
Atlas colorea los "barrios" con la partición servida (`/ui/communities`) y cae al Louvain
client-side si falta; (2) el minero usa un **prior de inesperadez** (pares que cruzan barrios
distintos y poco conectados se evalúan primero) en `op_tension`/`op_latent_bridge` — si el
artefacto falta o es incoherente, el prior queda inactivo y el minero corre igual. Un build
`--mode fast` no lo emite. Idea inspirada en Graphify (github.com/Graphify-Labs/graphify).

## Director autónomo (F19 + backend Codex F20)

Tras un run OK, el librarian puede disparar al **director de descubrimiento**
(`director.sh` — desde F20 con backend **Codex `gpt-5.6-terra`**; corre
campañas/tareas, juzga candidatos falsados como `reviewer=director:<backend>:<model>`
y deja un digest en `director/digests/`). Es **opt-in**: solo con `MAPA_DIRECTOR=1`,
y solo si el run fue `MAPA_UI_MODE=full` con Tier-1 y UI OK y `ui/doc_vectors.db`
coherente (con índice FTS-only el trigger no dispara y lo dice en el log). Típico:

    MAPA_DIRECTOR=1 MAPA_UI_MODE=full bash scripts/librarian.sh

**Arquitectura F20 (dos unidades):** el mediador (`director_mcp.py`, ledger + GPU,
sin internet) corre en la unidad A (`mapa-director-mcp`); el modelo corre en la
unidad B (`mapa-director-brain`) con un namespace sin ningún dato del disco — solo
su config y el socket del mediador. El promote sigue siendo manual del dueño: el
director llega hasta `interesting`/`discarded`/abstención, trazado en
`candidate_reviews`. Config, lock, policy y digests viven en el control plane
root-owned `.mapa/director/`; logs por corrida en `director/logs/<run>/`.

**Formas de correrlo:** manual `bash director.sh --backend codex [--dry] [--judge-only]`;
desde el Lab con el **botón "🎬 Director"** (contraseña que setea el dueño con
`playground.py director-pass set`); o automático por el trigger del librarian.
El botón deja un request que un watcher root (`mapa-director-button.path`) consume
y dispara con parámetros fijos — la web nunca ejecuta nada privilegiado.
