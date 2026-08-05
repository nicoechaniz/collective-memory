# Frontera de intercambio v1

Estado: implementada y validada sobre corpus sintéticos aislados. No hay una
migración ni un despliegue sobre un corpus vivo.

`mapa/exchange.py` define dos contratos deliberadamente separados. El primero
publica una generación inmutable, atribuible y verificable para lectores
externos. El segundo recibe un derivado ya consentido y revisado, vuelve a
verificar la evidencia contra los bytes finales, y lo publica bajo la misma
frontera single-writer que usa el bibliotecario.

Matrix.org no participa de esta integración. “Matrix” en los documentos de
integración significa el proyecto Daimon Matrix.

## Autoridad y separación

| Responsabilidad | Dueño |
| --- | --- |
| Corpus, generaciones, índice, mapa y Atlas | collective-memory |
| Identidad `/me`, eventos, clasificación, consentimiento y política de memoria | Daimon Matrix |
| Generación de exportación y tombstones del corpus | export-reader |
| Render final de una publicación y target lógico allowlisted | reviewed-publisher |
| Consentimiento del sujeto | clave Ed25519 con rol `subject-consent` |
| Revisión independiente | otra clave Ed25519 con rol `independent-review` |

Las dos capacidades tienen archivos, roles, scopes, secretos, directorios de
estado, generations, idempotencia y receipts distintos:

```text
.mapa/exchange/v1/
├── export-reader/
│   ├── generations/
│   ├── pending/
│   └── current -> generations/...
└── publisher/
    ├── generations/
    ├── transactions/
    ├── publication.fence.json
    └── current -> generations/...
```

No comparten una cola ni una base SQLite. `index.db` y `ui_v2.db` siguen siendo
proyecciones propias del corpus, no estado del adapter y nunca cruzan la API.
Poseer una capacidad de lectura no permite invocar preview, plan, apply,
reconcile ni recover. Poseer una capacidad de publicación no permite crear,
paginar ni leer una exportación.

## Contratos cerrados

El schema agregado está en
`schemas/exchange/v1/contracts.schema.json`. Los documentos públicos aceptan
solamente JSON UTF-8, sin claves duplicadas, floats, valores no finitos ni campos
desconocidos. Los enteros quedan dentro del rango interoperable exacto de JSON
(`±(2^53-1)`) y base64url debe ser canónico, sin padding. Los hashes y firmas se
calculan sobre JSON minificado con claves ordenadas y sin floats.

El contrato no acepta ni devuelve:

- paths del host, handles SQLite, SQL o nombres de tablas;
- comandos, argumentos de shell, executables, templates o imports;
- URLs que deban resolverse o ejecutarse;
- claves privadas, tokens, archivos de capability o configuración;
- prompts, sesiones, discovery privado, estado del director o caches de modelo;
- DB, WAL, SHM, snapshots de un corpus vivo o inventario del host.

Los paths que aparecen en `collective-export-catalog/v1` son configuración local
del productor, cargada mediante `MAPA_EXCHANGE_CATALOG`, y nunca forman parte de
la operación externa ni aparecen en el manifest exportado. El reader sólo pide
un `scope_id`; no puede elegir paths o artifacts. Los targets de
publicación entran como IDs lógicos; `collective-exchange-config/v1` los resuelve
internamente a paths relativos allowlisted. Traversal, symlinks en cualquier
ancestro, target existente sin receipt y alias de dos IDs al mismo path fallan
cerrado.

## Exportación inmutable

Un catálogo local declara, en orden canónico, el conjunto completo aceptado:

- `artifact_id` inmutable y `logical_id` estable;
- autorías/principals y referencias de origen con hashes;
- licencia, consentimiento, clasificación y media type;
- predecessor exacto y estado `active` o `tombstone`;
- path local sólo para un artifact activo.

En v1, `consent_scope` y `classification` de cada artifact deben coincidir con
el scope aceptado del catálogo. Un reader no puede convertir material privado o
excluido en una exportación pública cambiando metadata en la solicitud.

`ExportBoundary.create` toma un shared lock sobre `.mapa/lock`, por lo que no
puede mezclar el snapshot anterior con el siguiente flip del bibliotecario.
Verifica archivos regulares sin symlinks, UTF-8, tamaños, hashes, IDs únicos,
orden, continuidad y la ausencia de eliminaciones implícitas. Luego materializa
objetos por SHA-256, un manifest cerrado y un directorio de generación completo;
el único publish es el reemplazo atómico de `export-reader/current`.
El manifest liga explícitamente las generaciones observadas de índice y UI; si
ambas existen pero no coinciden, falla cerrado. Cuando una proyección no existe,
su generación es `null`, nunca una inferencia silenciosa.

Una repetición del mismo estado devuelve byte por byte el mismo manifest aunque
el operador proponga otro timestamp. Un cambio requiere que cada artifact
modificado extienda al artifact vigente. Un artifact que desaparece necesita un
successor explícito `tombstone`; la ausencia nunca significa revocación.

`ExportBoundary.page` produce páginas acotadas ligadas a `generation_id` y
`manifest_hash`. El cursor contiene generación, manifest, offset y checksum.
Reutilizarlo contra otra generación falla con `mixed_generation`.
`ExportBoundary.manifest(generation_id)` recupera también un manifest histórico
inmutable. Un consumidor que estuvo offline recorre `predecessor_generation`
hacia atrás hasta su high-water aceptado, verifica la cadena completa y recién
entonces la aplica en orden ascendente. La lectura conserva el mismo capability
y scope y no muta el corpus ni el export cache.
`object_bytes` entrega únicamente un `sha256:*` declarado por ese manifest y
reverifica los bytes antes de responder.

El importer de Daimon Matrix debe además autenticar al productor configurado,
verificar el manifest completo y los objetos, y convertirlos en claims DM-015
en cuarentena. Recibir una exportación no es adoptar su contenido ni convertirlo
en autobiografía `/me`.

## Publicación revisada

La secuencia soportada es:

1. `publication-preview` valida el draft, resuelve el target lógico, observa el
   predecessor actual, renderiza todos los bytes y ejecuta el scanner final. No
   escribe ningún target, plan ni estado.
2. El sujeto firma consentimiento Ed25519 y otra persona/rol firma review
   independiente. Ambas evidencias ligan action, target, sujeto, requester,
   source checkpoint, clasificación, policy, preview y hash de contenido.

El adaptador/transporte que invoque la boundary debe autenticar `requester_id` y
la instancia productora; esos IDs no son bearer tokens autoafirmados. La
boundary verifica las evidencias y la separación de roles, mientras Daimon
Matrix liga la sesión autenticada con el requester declarado.
3. `publication-plan` verifica firmas, roles, vigencia, revocación, separación
   del reviewer y estado actual contra el reloj UTC del proceso; devuelve un
   plan determinista. El caller no puede suministrar ni retrotraer ese reloj.
4. `publication-apply` vuelve a calcular request, preview, plan, predecessor y
   evidencia. Bajo el mismo exclusive `.mapa/lock`, guarda rollback, instala los
   bytes, reconstruye el índice real y Atlas, verifica el postcondition y recién
   entonces prepara el receipt y la nueva generación de estado.
5. El reemplazo atómico de `publisher/current` acepta receipt, idempotencia y
   target a la vez. `publication-reconcile` vuelve a observar archivo, hash,
   índice, Atlas y generaciones antes de decir `verified`.

La representación Markdown es determinista: una línea de metadata canónica,
título y body normalizados a LF. El scanner corre sobre esos bytes, no sobre el
draft. Detecta al menos private-key blocks, Bearer/token/API-key patterns,
credenciales GitHub/AWS y URLs con userinfo. No hay redacción automática: un
match rechaza el artifact completo.

`publish` requiere un target sin historia ni archivo preexistente. `successor` y
`tombstone` requieren el receipt ID/hash exactamente vigente y sólo avanzan esa
línea. El tombstone es un artifact visible, revisado y receipted; no borra el
archivo ni la historia. Un overwrite manual, un revoke no registrado o un
predecessor viejo es drift y falla cerrado.

## Idempotencia y effect truth

El idempotency key está ligado al hash de todo el request, incluidas ambas
evidencias. Repetir el mismo request devuelve el receipt original sólo después
de observar nuevamente:

- hash y longitud de los bytes del target;
- fila exacta en `index.db`;
- presencia exacta en `ui_v2.db`;
- igualdad entre las generaciones de índice y Atlas.

Un idempotency key con otro request es `idempotency_conflict`. Un receipt cuyo
efecto ya no se observa es `effect_truth_discrepancy`; nunca se reproduce a
ciegas. La expiración posterior del consentimiento/review no invalida el replay
de un efecto ya aceptado, pero tampoco autoriza un successor nuevo.

## Ventanas de crash y lecturas

Cada transacción journaliza estas etapas durables:

```text
snapshot-staged
  → prepared
  → target-published
  → index-published
  → ui-published
  → projections-published
  → receipt-staged
  → state-published
  → committed
```

Desde `prepared` hasta el final existe `publication.fence.json`. Las superficies
soportadas de búsqueda y UI devuelven indisponibilidad mientras existe, en vez de
mostrar target nuevo con índice viejo o receipt ausente.

Recovery bajo el writer lock usa una regla única:

- si `publisher/current` ya apunta al `new_state_hash`, completa el journal y
  conserva all-new;
- si todavía no apunta, restaura bytes, índice, manifest/audit y UI exactos del
  snapshot de rollback y conserva all-old.

El crash después del efecto pero antes del receipt vuelve all-old. El crash
después del state flip vuelve all-new y un retry recupera el mismo receipt. Los
tests inyectan muerte en cada etapa, además de response loss, errores de
proyección y retry.

## Configuración y CLI

El CLI es `mapa/exchange_cli.py`. Requiere:

```text
MAPA_ROOT
MAPA_DATA
MAPA_EXCHANGE_CONFIG
MAPA_EXCHANGE_CATALOG
MAPA_EXCHANGE_READER_CAPABILITY
MAPA_EXCHANGE_PUBLISHER_CAPABILITY
MAPA_EXCHANGE_TRUST
```

Los capability files deben ser archivos regulares, sin symlink, propiedad del
UID efectivo y modo owner-only. Contienen exactamente schema, capability ID,
rol, scopes y un secreto base64url de al menos 32 bytes. Nunca se pasan en argv,
request, receipt o logs.

Comandos:

```bash
python3 mapa/exchange_cli.py export-create --scope public
python3 mapa/exchange_cli.py export-page --generation '<logical-generation-id>' --limit 100
python3 mapa/exchange_cli.py export-object --generation '<logical-generation-id>' --content-ref 'sha256:<digest>'

python3 mapa/exchange_cli.py publication-preview --draft draft.json
python3 mapa/exchange_cli.py publication-plan --request request.json
python3 mapa/exchange_cli.py publication-apply --request request.json --plan plan.json
python3 mapa/exchange_cli.py publication-reconcile --receipt '<logical-receipt-id>'
python3 mapa/exchange_cli.py publication-recover
```

Los paths de `--draft`, `--request` y `--plan` son transporte local del CLI; no
forman parte del documento contractual ni aparecen en outputs. El catálogo es
configuración del productor tomada del entorno y no un argumento de la operación.

## Integración de Daimon Matrix

DM-036 debe usar estas operaciones como capacidades inyectadas, no importar el
módulo ni conocer sus directorios internos:

- inbound: create/page/object → verificar → DM-015 publication/claim/quarantine;
- outbound: preview → consentimiento/review Matrix → plan/apply/reconcile →
  `publication.receipted`;
- guardar en Matrix sólo IDs lógicos, hashes, source/checkpoint, clasificación,
  predecessor, evidencia y receipt;
- usar adaptadores, capabilities, stores, queues y receipts distintos por
  dirección;
- reconstruir la proyección inbound desde el ledger Matrix sin leer SQLite del
  productor;
- nunca interpretar un receipt outbound como confianza inbound ni un manifest
  inbound como consentimiento outbound.

Los vectors canónicos están en `vectors/exchange/v1/` y se regeneran con:

```bash
python3 tools/generate_exchange_vectors.py
python3 tools/generate_exchange_vectors.py --check
```

Incluyen catálogo/manifest/page, draft/preview, consentimiento/review Ed25519,
request/plan/receipt/reconcile y un draft negativo con `host_path`.

## Rollback y límites

Antes de cualquier uso real, rollback elimina el adapter no configurado. Después
de una importación o publicación aceptada, rollback deshabilita la capability y
usa successors/tombstones explícitos; no borra manifests, receipts, ledger ni
high-waters. Una transacción incompleta se recupera con
`publication-recover` antes de reabrir lecturas.

Esta versión no define transporte remoto, descubrimiento de peers, adopción
Matrix, migración de bases, sincronización de credenciales, firma de tags ni
deploy. No se debe apuntar a un corpus real hasta que la integración DM-036 y su
canary tengan autorización y evidencia separadas.
