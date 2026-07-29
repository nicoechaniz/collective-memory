# Interfaz V3

La V3 organiza la lectura y el descubrimiento en dos superficies separadas:

- **Atlas** (`/atlas-v3/`): grafo 3D inmersivo, filtros por proyecto y tipo de
  relacion, busqueda, archivo y lector de documentos.
- **Lab** (`/lab-v3/descubrir`): campañas, bandeja privada, evidencia y, en el
  perfil completo, director.

Las interfaces V1 siguen disponibles en `/atlas/` y `/lab/`. El Atlas V3
reutiliza el grafo 3D de V1, que conserva su comportamiento de nubes dinamicas,
pero lo integra con navegacion y lectura mas claras.

## Proyeccion visual

`ui_v2_store.py` deriva `ui_v2.db` desde `index.db` y los artefactos JSON de la
generacion vigente. Contiene solo metadatos de navegacion: proyectos,
descripciones, documentos, comunidades, grafos y evidencia agregada. No es una
segunda fuente de verdad y se puede borrar y regenerar.

`ui_builder.py` la crea antes de publicar cada generacion. `serve.py` y
`playground.py` exponen `/ui/v2/*` en modo lectura. La navegacion no carga el
embedder, no reserva VRAM y no necesita CUDA.

Se puede ajustar el catalogo humano con un archivo opcional en
`$MAPA_DATA/ui-v2-private/catalog.json`; el ejemplo sanitizado esta en
`config/ui_v2_catalog.example.json`.

## Perfiles

### Completo

Es el default. El Lab ofrece operadores, falsificacion LLM y director cuando
esos componentes fueron configurados expresamente.

### Estructural

Sirve para demos o instalaciones donde el corpus debe poder explorarse sin
habilitar inferencia en vivo. El backend admite solamente `latent_bridge`,
`cluster_frontier` y `outlier`, bloquea el modo agente y el director, y marca su
health como `structural-only`.

Variables de runtime:

```bash
MAPA_STRUCTURAL_ONLY=1
MAPA_DISCOVERY_NO_LLM=1
MAPA_DIRECTOR=0
```

El bundle Lab debe compilarse con la misma intencion para que la interfaz lo
explique y no muestre controles inutiles:

```bash
VITE_LAB_STRUCTURAL_ONLY=1 npm run build:v3:lab
```

La restriccion de seguridad esta en el backend. La variable de Vite no se toma
como control de acceso.

## Origenes separados

Atlas y Lab suelen correr en puertos distintos. Los enlaces cruzados usan
8899/8898 por defecto y se pueden fijar durante el build:

```bash
VITE_ATLAS_ORIGIN=http://host-privado:8899 \
VITE_LAB_ORIGIN=http://host-privado:8898 \
npm run build:v3
```

Esto es obligatorio cuando una instalacion aislada convive en el mismo host con
otra memoria: evita que un enlace visual salte por accidente al corpus vecino.

## Build y publicacion

```bash
cd web
npm install
npm run build:all
```

El instalador publica los cuatro bundles solo despues de que todos compilan. En
produccion conviene conservar releases fechados y mover symlinks, nunca copiar
archivos sobre una version servida.

## Sesiones del Lab

El login intercambia el token inicial por una cookie de sesion `HttpOnly` y usa
un token CSRF para escrituras. Los tokens originales siguen hasheados en el
servidor. Atlas permanece read-only y no requiere sesion.

El modo estructural no elimina el sandbox multiusuario: cada persona conserva
su bandeja y sus candidatos, aunque el calculo se limite a operadores sin LLM.
