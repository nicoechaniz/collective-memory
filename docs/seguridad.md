# Seguridad

Este documento dice qué protege el sistema, cómo, y —sobre todo— qué **no**
protege. La parte incómoda está al final y es la que más conviene leer.

## Modelo de amenaza

El activo es el corpus: documentos que no deberían salir de la máquina. Las tres
vías por las que podrían salir son un servicio expuesto a internet, un proceso
con demasiado acceso, y un dato privado que termina publicado por error.

## 1. Los servicios no quedan expuestos

Dos capas, y conviene saber cuál es la que manda.

**Capa 1 — `safe_bind()`, en Python.** Rechaza siempre los comodines (`0.0.0.0`,
`::`, la cadena vacía y `"0"`, que también escuchan en todas las interfaces) y
cualquier dirección globalmente ruteable, normalizando antes las IPv4 mapeadas
en IPv6. El default es loopback. Escuchar en una interfaz de red exige
`MAPA_BIND_ALLOW_LAN=1`.

**Capa 2 — el filtro de red de systemd.** `IPAddressDeny=any` más un
`IPAddressAllow` acotado. Esta es la garantía real: actúa en el kernel, no
depende de que el proceso se porte bien.

### Una trampa que vale la pena conocer

**Una dirección RFC1918 no significa privada.** En AWS, GCP y Azure la IP
primaria de la máquina es RFC1918 (`10.x`, `172.31.x`) con NAT 1:1 hacia una IP
pública. Bindear ahí deja el servicio accesible desde internet, aunque la
dirección parezca interna. Por eso el bind no-loopback pide confirmación
explícita en vez de aceptarse por parecer privado.

### Dos detalles operativos

`IPAddressAllow` gobierna **entrada y salida**. Por eso las plantillas separan la
red desde la que se alcanza el servicio de la red del proveedor de modelo: con un
solo valor para ambas cosas, el acceso al modelo se bloquea a nivel kernel y el
fallo es difícil de diagnosticar.

Y el filtro **solo aplica con cgroup v2 unificado y BPF**. Donde no hay soporte
—contenedores, algunas VMs, `systemd --user`— systemd registra un aviso y arranca
igual, sin filtrar: la unidad parece bien y la garantía no existe. El preflight
lo verifica y falla cerrado cuando el bind no es loopback.

## 2. Separación de procesos

Los servicios corren con un usuario sin shell y sin privilegios, con
`ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp` y el corpus montado como
solo-lectura.

`ProtectHome=yes` deja `/home` y `/root` **vacíos** dentro del namespace. Es la
razón por la que el instalador rechaza un corpus ahí adentro: los servicios no
lo verían, y el síntoma sería un índice inexplicablemente vacío.

## 3. Identidades del registro

El registro de hallazgos distingue al dueño, al director y a la marca de
procedencia **solo por el texto** del campo `reviewer`, y ese campo decide
autorización: únicamente el dueño puede confirmar un hallazgo.

Esos nombres están reservados: no se puede registrar un usuario con ninguno de
ellos. Pero la puerta que importa no es el alta sino el **import**: el servicio
web escribe en una base sandbox, y de ahí las filas se copian al registro del
dueño. Un `reviewer` reservado que llegue por ese camino se **reetiqueta** con su
origen. Se reetiqueta y no se rechaza a propósito: rechazar el import completo
permitiría fabricar un candidato imposible de importar, y con eso bloquear el
canal de propuestas.

## 4. El botón del director

El Lab puede disparar al director dejando un archivo en un directorio que vigila
un proceso con privilegios. Ese proceso verifica **en código** que el archivo lo
haya escrito el usuario del servicio web, y si no puede resolver ese usuario, no
dispara. Depender solo de los permisos del directorio sería frágil: cualquiera
que pudiera escribir ahí escalaría a root.

## 5. El director — lo que NO está protegido

Acá está lo importante, y no tiene eufemismo posible.

El director corre un CLI de agente con **shell**. La contención es de procesos:
un usuario propio, y un namespace donde el corpus está enmascarado. Eso significa
que ese proceso **no puede leer tu corpus**.

Pero:

- **Puede leer el resto del host.** `ProtectSystem=strict` hace el sistema
  solo-lectura, **no invisible**: `/etc`, `/opt`, `/srv`, `/var/lib`, otros
  montajes y cualquier corpus que tengas fuera del indexado siguen siendo
  legibles.
- **Puede salir a internet, sin restricción.** El filtro de red no se le aplica.
  No es un olvido: `IPAddressAllow` acepta únicamente IP y CIDR literales, y un
  CLI de agente habla con la API de su proveedor a través de una CDN con
  direcciones rotativas. No hay rango estable que permitir, así que no se
  promete un aislamiento que no se puede escribir.

Dicho de una vez: **el cerebro del director puede leer todo el host salvo el
corpus, y puede hablar con internet.** La contención real es que no tiene tus
documentos a la vista, no que esté encerrado.

Por eso viene apagado, la instalación deja sus unidades deshabilitadas, y
encenderlo requiere pasos manuales. Quien lo habilite asume ese riesgo con
conocimiento.

## 6. Fuga en el propio repositorio

Este repositorio se construye por **allowlist**: un manifiesto declara qué
archivos existen, y `tools/leak_check.py` falla ante cualquier archivo que no
esté declarado, además de revisar rutas del host, IPs públicas, credenciales por
regla estructural, patrones de secreto y tamaño.

Dos criterios que salieron de equivocarse:

**Ninguna regla se enumera a mano.** Las listas escritas a mano fallaron dos
veces —dejaron pasar la ruta de otro disco y el nombre del proyecto más grande
del corpus—, así que los términos privados se derivan mecánicamente del sistema
de origen en tiempo de build.

**Esa lista no viaja en el repositorio.** Publicar la lista de términos
prohibidos sería exactamente la fuga que la regla evita. Vive fuera del árbol, y
por eso esas dos reglas corren solo en el build local; la integración continua
cubre las demás como segunda red.

El chequeo incluye una **prueba negativa**: siembra un archivo sucio y verifica
que falle. Un guardia que nunca se probó fallando no es un guardia.
