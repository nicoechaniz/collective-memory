# Instalacion

## Requisitos

El `preflight.sh` los verifica todos y dice exactamente que falta. Vale correrlo
primero, porque varios no son obvios:

| | Para que |
|---|---|
| `python3` con **extensiones cargables de sqlite** y **FTS5** | el indice usa `sqlite-vec` y FTS5; varios builds de Python (el del sistema en macOS, algunos `pyenv`, imagenes slim) vienen sin eso, y el fallo recien aparece al indexar |
| `node` + `npm` | no solo para compilar el frontend: el pipeline **ejecuta node en cada corrida** para calcular el layout del grafo |
| `poppler-utils` (`pdftotext`) | extraer texto de PDF |
| `util-linux` (`flock`) | el lock de escritor unico |
| GPU | opcional; sin ella el indexado inicial es bastante mas lento |
| systemd + root | solo si vas a instalar los servicios |

## Nucleo

```bash
./scripts/preflight.sh
./install.sh --root <dir-del-corpus>
```

Esto crea un entorno virtual, instala las dependencias, descarga el modelo de
embeddings una unica vez, escribe la configuracion y compila Atlas y Lab V1/V3. No pide
privilegios ni toca systemd.

Los bundles se publican como un release bajo `$MAPA_DATA/web/releases/` y cuatro
symlinks (`dist`, `dist-lab`, `dist-v3-atlas`, `dist-v3-lab`) cambian solo cuando
todos los builds terminaron. Un fallo de frontend no pisa la version anterior.

Para una demostracion que permita explorar y ejecutar solo descubrimientos
estructurales, sin LLM ni director:

```bash
./install.sh --root <dir-del-corpus> --structural-only
```

**Donde poner el corpus.** No bajo `/home` ni `/root`: las unidades systemd traen
`ProtectHome=yes`, que deja esos arboles vacios dentro del namespace, y los
servicios no verian nada. El instalador lo rechaza con ese mensaje.

## Servicios

```bash
sudo ./install.sh --root <dir-del-corpus> --with-systemd
```

Instala las unidades y activa el servicio de lectura. Las del director quedan
instaladas pero **deshabilitadas**.

Para escuchar fuera de la maquina hace falta `--bind <ip>`, y el instalador pide
confirmacion escrita. La razon esta en [seguridad.md](seguridad.md): una IP
RFC1918 no garantiza que sea privada.

Si el proveedor de modelo corre en otra maquina de la red, `--llm-cidr <cidr>`:
el filtro de red gobierna tambien la salida, asi que sin eso el acceso al modelo
queda bloqueado a nivel kernel.

## Director

Apagado por defecto. Tres pasos, todos manuales:

```bash
sudo ./scripts/setup_director_users.sh
sudo ./scripts/setup_director_backend.sh <codex|claude>   # necesita sesion ya autenticada
sudo systemctl enable --now mapa-director-button.path      # solo si querés el boton del Lab
```

Leer [seguridad.md](seguridad.md) antes. Habilitarlo cambia el modelo de amenaza
de forma que conviene entender.

## Primer uso

```bash
export MAPA_ROOT=<dir-del-corpus>
<venv>/bin/python <code-home>/tier1.py index --scope total
<venv>/bin/python <code-home>/tier1.py search "tu consulta"
```

La primera indexacion de un corpus grande tarda: con GPU, minutos; sin GPU,
bastante mas. Despues es incremental (solo lo que cambio de hash).

Para probar sin material propio: [../examples/README.md](../examples/README.md).
La operacion y las rutas de la interfaz V3 estan en [interfaz-v3.md](interfaz-v3.md).

## Actualizar

El repositorio es un *fork* del sistema de referencia y la sincronizacion es
unidireccional. No hay camino de vuelta automatizado: si adaptaste el codigo,
las actualizaciones se integran a mano.
