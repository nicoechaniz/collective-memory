#!/usr/bin/env python3
"""leak_check.py — guardia de fuga del repo público.

Filosofía: DENY-BY-DEFAULT. Falla ante cualquier archivo del árbol que no esté
en el manifiesto, y además ante ocho reglas de contenido. Un archivo aprobado a
mano sigue pasando por las reglas; un archivo no aprobado ni siquiera llega.

Reglas
  1. Path absoluto cuya primera componente cae fuera de una allowlist corta de
     FHS. Generalizada a propósito: una lista de literales prohibidos ya falló
     (dejó pasar rutas de otros discos del host). No aplica a docs/ ni *.example,
     que necesitan mostrar rutas de ejemplo verosímiles.
  2. IP del host e IPs públicas literales.
  3. Términos privados DERIVADOS del sistema de origen (nunca escritos a mano:
     una lista enumerada dejó pasar el proyecto más grande del disco).
  3b. Vocabulario de infraestructura privada (VPN, host, tenant), también derivado.
  4. Credenciales por regla estructural (no por lista de nombres).
  5. Patrones de secreto.
  6. Archivos > 1 MB.
  7. El .git del repo no puede ser el del sistema de origen.

Reglas 3 y 3b necesitan la lista de términos privados del sistema de origen.
Publicar esa lista en claro sería la fuga que la regla evita, así que:
  - build local (--src): deriva los términos del sistema privado y matchea por
    substring (más potente).
  - CI (sin --src): usa tools/leakcheck_terms.hashed —hashes sha256 que NO
    revelan los valores— y matchea por token. Así CI caza una regresión (que
    alguien re-agregue un nombre privado) sin la lista en claro en el repo.

Uso:
  leak_check.py --repo <dir> [--src <MAPA_SRC>] [--deep] [--self-test]
Salida: 0 limpio, 1 hallazgos, 2 error de uso.
"""
import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys

MAX_BYTES = 1024 * 1024
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")
HASHES_FILE = "tools/leakcheck_terms.hashed"


def _sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

# Primeras componentes de path aceptables. Todo lo demás (/mnt, /home, /srv,
# /media, /data, /root...) es topología del host de alguien.
ALLOWED_ROOTS = {
    "usr", "etc", "var", "run", "opt", "tmp", "proc", "sys", "dev",
    "bin", "sbin", "lib", "lib64",
}

# Rutas HTTP del propio servicio. Se parecen a paths absolutos pero no son
# topología del host: son la API pública de la aplicación.
ROUTE_ROOTS = {
    "health", "search", "doc", "ui", "atlas", "wiki", "assets", "api", "lab",
    "pg", "static", "favicon.ico", "src", "node_modules", "@vite", "@react-refresh",
}

# El lookbehind excluye `*` además de `/` y `:` para no enganchar el interior de
# un glob relativo (`*/node_modules/**`) ni de una URL, que no son rutas del host.
ABS_PATH_RE = re.compile(r"(?<![\w.\-/:*])/([A-Za-z][A-Za-z0-9_.-]*)((?:/[A-Za-z0-9_.*@%+-]+)+)")
# Contextos donde el `/` que sigue continúa una variable o una concatenación de
# strings, no una ruta absoluta: f-strings (`}`), expansión de shell (`$VAR`),
# placeholders (`@X@`), un `)` previo, o una SUMA de strings (`+ "`).
# OJO: la coma NO cuenta. `foo(x, "/ruta")` es un path pasado como argumento y
# debe marcarse — incluirla eximía la clase entera de rutas-como-2do-argumento
# (así se colaba un path pasado como 2º argumento de una función).
PRE_INTERP_RE = re.compile(r"""(\}|\)|@[A-Z0-9_]+@|\$\{?\w+\}?["']?|\+\s*["'])$""")

IPV4_RE = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])")
TEXT_EXT = {
    ".py", ".sh", ".ts", ".tsx", ".js", ".mjs", ".json", ".md", ".txt", ".css",
    ".html", ".yml", ".yaml", ".in", ".service", ".path", ".example", ".cfg", "",
}

SECRET_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "clave estilo sk-"),
    (re.compile(r"\bghp_[A-Za-z0-9]{20,}"), "token de GitHub"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "PAT de GitHub"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"), "Authorization: Bearer con valor"),
    (re.compile(r"(?<![A-Za-z0-9])[a-f0-9]{64}(?![A-Za-z0-9])"), "hex de 64 (sha256/token)"),
    (re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{60,}={0,2}(?![A-Za-z0-9+/=])"), "base64 largo"),
]

# Regla 4: estructural. No anclada a un directorio padre — la ubicación canónica
# de las credenciales ya se movió una vez y la regla anclada dejó de aplicar.
CRED_DIR_RE = re.compile(r"(^|/)[A-Za-z0-9_-]+-config(/|$)")
CRED_NAME_RE = re.compile(
    r"(^|/)(\.credentials\.json|\.claude\.json.*|auth\.json|mcp_token.*|"
    r"installation_id|.*\.key|.*\.pem|.*\.db|.*\.sqlite.*|.*\.log|mapa\.env)$"
)
CRED_PATH_PREFIXES = ("var/lib/mapa-director",)


class Finding:
    def __init__(self, rule, path, detail, line=None):
        self.rule, self.path, self.detail, self.line = rule, path, detail, line

    def __str__(self):
        loc = f"{self.path}:{self.line}" if self.line else self.path
        return f"  [regla {self.rule}] {loc} — {self.detail}"


# ---------- manifiesto ----------

def load_manifest(repo):
    """Devuelve (paths_permitidos, globs_permitidos, excepciones)."""
    mf = os.path.join(repo, "tools", "manifest_allowlist.txt")
    if not os.path.isfile(mf):
        sys.exit(f"[leak-check] falta el manifiesto: {mf}")
    allowed, globs, excepts = set(), [], {}
    for raw in open(mf, encoding="utf-8"):
        line = raw.split("#")[0].strip() if not raw.strip().startswith("#") else ""
        if not line:
            continue
        parts = line.split()
        if parts[0] == "EXCEPT" and len(parts) >= 3:
            rules = parts[2].split("=", 1)[1] if "=" in parts[2] else ""
            excepts[parts[1]] = {r.strip() for r in rules.split(",") if r.strip()}
        elif parts[0] in ("COPY", "AUTHOR") and len(parts) >= 3:
            dst = parts[2]
            (globs.append(dst[:-2]) if dst.endswith("**") else allowed.add(dst))
    return allowed, globs, excepts


def walk_repo(repo):
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "__pycache__")]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            yield full, os.path.relpath(full, repo)


# ---------- términos derivados (reglas 3 / 3b) ----------

GENERIC = {
    "mapa", "memoria-colectiva", "inbox", "docs", "src", "web", "test", "tests",
    "datasets", "skills",
}
# Componentes de nombres compuestos que por sí solos son demasiado genéricos
# para marcar (evita falsos positivos masivos).
GENERIC_PARTS = {
    "editorial", "data", "andy", "server", "tts", "gpt", "kit", "mc", "open",
    "webui", "core", "agent", "code", "conciencia", "ventana",
}


def _split_compound(name):
    """De un nombre compuesto `aaaa-bbbb` saca también el componente `bbbb`.

    Una lista enumerada dejó pasar la org porque solo estaba el compuesto y el
    match es por substring: el compuesto no matchea el componente suelto.
    Descomponer captura la parte distintiva sin escribir nada a mano.
    """
    out = set()
    for part in re.split(r"[-_]", name):
        if len(part) > 4 and part not in GENERIC_PARTS:
            out.add(part)
    return out


def derive_private_terms(src):
    """Deriva términos privados e IPs del host desde el sistema de origen.

    NUNCA se escribe al repo. Devuelve (terms, host_ips). Fuentes: directorios de
    primer nivel del corpus, claves `project` de los índices vivos, hostname, y
    el archivo externo de términos (infra + lo que no se deriva solo, como el
    nombre del dueño y las IPs de su red).
    """
    terms, root = set(), os.path.dirname(os.path.abspath(src.rstrip("/")))
    try:
        for name in os.listdir(root):
            if name.startswith("."):
                continue
            if os.path.isdir(os.path.join(root, name)) and len(name) > 3:
                terms.add(name.lower())
                terms |= _split_compound(name.lower())
    except OSError as e:
        sys.exit(f"[leak-check] no se pudo listar {root}: {e}")

    for rel in ("manifest.json", "corpus_audit.json"):
        p = os.path.join(src, rel)
        if not os.path.isfile(p):
            continue
        try:
            data = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        for proj in _collect_projects(data):
            if len(proj) > 3:
                terms.add(proj.lower())
                terms |= _split_compound(proj.lower())

    try:
        terms.add(os.uname().nodename.lower())
    except Exception:
        pass

    # Archivo externo: infra (VPN/host/tenant), nombre del dueño y las IPs del
    # host. Vive FUERA del repo — escribir acá los términos prohibidos sería la
    # fuga que la regla evita. Las líneas que parecen IP/CIDR van al set de IPs.
    extra, drop, host_ips = load_terms_file(os.path.join(src, "leakcheck_terms.local"))
    terms |= extra
    return (terms - drop - GENERIC), host_ips


def load_terms_file(path):
    """Términos extra (uno por línea) y exclusiones con prefijo `!`.

    Las exclusiones existen porque la derivación mecánica captura de más: un
    directorio del disco puede ser el nombre de un producto público (un runtime
    de modelos, una librería) que el código legítimamente necesita nombrar.
    Que el criterio viva acá —y no en el fuente del checker— lo mantiene junto
    al sistema privado, que es donde se sabe qué es propio y qué es de terceros.

    Las lineas que parecen IP o CIDR van a un set aparte (regla 2). El resto son
    terminos de texto (regla 3). `!term` excluye.

    Devuelve (agregados, exclusiones, host_ips).
    """
    add, drop, ips = set(), set(), set()
    if not path or not os.path.isfile(path):
        return add, drop, ips
    for line in open(path, encoding="utf-8"):
        t = line.split("#")[0].strip()
        if not t:
            continue
        if t.startswith("!"):
            drop.add(t[1:].strip().lower())
            continue
        base = t.split("/")[0]  # tolera CIDR
        try:
            ipaddress.ip_address(base)
            ips.add(base)
        except ValueError:
            add.add(t.lower())
    return add, drop, ips


def _collect_projects(obj, out=None):
    out = out if out is not None else []
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "project" and isinstance(v, str):
                out.append(v)
            else:
                _collect_projects(v, out)
    elif isinstance(obj, list):
        for it in obj:
            _collect_projects(it, out)
    return out


# ---------- reglas de contenido ----------

def scan_text(rel, text, terms, host_ips, term_hashes, host_ip_hashes, excepts):
    ex = excepts.get(rel, set())
    findings = []
    # La regla 1 no aplica a documentación ni a plantillas de ejemplo: ahí hay
    # que poder mostrar un MAPA_ROOT verosímil y advertir sobre /home.
    doc_like = rel.startswith("docs/") or rel.endswith(".example") or rel == "README.md"

    for i, line in enumerate(text.splitlines(), 1):
        if not doc_like and "1" not in ex:
            for m in ABS_PATH_RE.finditer(line):
                # Un sufijo pegado a una variable o a una concatenación no es un
                # path del host: `f"{DATA}/sub"`, `"$VAR"/sub`, `base + "/api/x"`.
                if PRE_INTERP_RE.search(line[:m.start()]):
                    continue
                if m.group(1) not in ALLOWED_ROOTS and m.group(1) not in ROUTE_ROOTS:
                    findings.append(Finding(1, rel, f"path absoluto fuera de FHS: /{m.group(1)}{m.group(2)}", i))
        if "2" not in ex:
            for m in IPV4_RE.finditer(line):
                try:
                    ip = ipaddress.ip_address(m.group(1))
                except ValueError:
                    continue
                # Pública (siempre) O la IP del host: una IP privada del host
                # (p.ej. la de su VPN) es topología igual de sensible, y is_global
                # no la marca. Las IPs del host se derivan, no se hardcodean.
                if ip.is_global:
                    findings.append(Finding(2, rel, f"IP pública literal: {ip}", i))
                elif str(ip) in host_ips or _sha(str(ip)) in host_ip_hashes:
                    findings.append(Finding(2, rel, f"IP privada del host: {ip}", i))
        low = line.lower()
        if terms:                      # modo local: términos en claro (substring)
            for t in terms:
                if t in low:
                    findings.append(Finding(3, rel, f"término privado derivado: {t!r}", i))
        elif term_hashes:              # modo CI: solo hashes (tokens completos)
            for tok in set(TOKEN_RE.findall(low)):
                if len(tok) > 3 and _sha(tok) in term_hashes:
                    findings.append(Finding(3, rel, f"token que matchea un término privado (hash): {tok!r}", i))
        if "5" not in ex:
            for pat, why in SECRET_PATTERNS:
                if pat.search(line):
                    findings.append(Finding(5, rel, why, i))
    return findings


def check_path_rules(rel):
    out = []
    if CRED_DIR_RE.search(rel) or CRED_NAME_RE.search(rel) or \
            any(rel.startswith(p) for p in CRED_PATH_PREFIXES):
        out.append(Finding(4, rel, "path de credenciales/estado por regla estructural"))
    return out


def check_git(repo, src, deep):
    out = []
    repo_git = os.path.join(repo, ".git")
    if src:
        src_root = os.path.dirname(os.path.abspath(src.rstrip("/")))
        if os.path.exists(os.path.join(src_root, ".git")) and \
                os.path.isdir(os.path.join(src_root, ".git")) and \
                os.listdir(os.path.join(src_root, ".git")):
            out.append(Finding(7, "<git>", f"{src_root}/.git está inicializado: nunca correr git ahí"))
        try:
            if os.path.samefile(repo_git, os.path.join(src_root, ".git")):
                out.append(Finding(7, "<git>", "el .git del repo es el del sistema de origen"))
        except OSError:
            pass
    if deep and os.path.isdir(repo_git):
        try:
            names = subprocess.run(
                ["git", "-C", repo, "log", "--diff-filter=A", "--name-only",
                 "--pretty=format:", "--all"],
                capture_output=True, text=True, timeout=120).stdout
            for p in {n.strip() for n in names.splitlines() if n.strip()}:
                out.extend(f for f in check_path_rules(p))
        except Exception as e:
            out.append(Finding(7, "<git>", f"no se pudo revisar la historia: {e}"))
    return out


# ---------- main ----------

def load_hashes(repo):
    """Hashes de términos/IPs privados, materializados en el repo para que CI
    ejerza las reglas 3/3b sin la lista en claro. Devuelve (term_hashes, ip_hashes)."""
    p = os.path.join(repo, HASHES_FILE)
    th, ih = set(), set()
    if not os.path.isfile(p):
        return th, ih
    for line in open(p, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if ":" in line:
            kind, h = line.split(":", 1)
            (ih if kind == "ip" else th).add(h.strip())
    return th, ih


def emit_hashes(repo, src):
    """Escribe tools/leakcheck_terms.hashed desde el sistema de origen.

    Los hashes no revelan los términos, así que el archivo SÍ puede vivir en el
    repo — su propósito es que CI cace regresiones (que alguien re-agregue un
    nombre privado), no defender contra fuerza bruta con un diccionario de nombres.
    """
    terms, host_ips = derive_private_terms(src)
    lines = sorted({f"term:{_sha(t)}" for t in terms if len(t) > 3})
    lines += sorted({f"ip:{_sha(ip)}" for ip in host_ips})
    out = os.path.join(repo, HASHES_FILE)
    with open(out, "w", encoding="utf-8") as f:
        f.write("# Hashes de terminos/IPs privados (sha256). No revelan los\n"
                "# valores; permiten que CI ejerza las reglas 3/3b sin la lista\n"
                "# en claro. Se regenera con: leak_check.py --emit-hashes --src <MAPA_SRC>\n")
        f.write("\n".join(lines) + "\n")
    print(f"[leak-check] {len(lines)} hashes -> {HASHES_FILE}")


def run(repo, src, deep):
    allowed, globs, excepts = load_manifest(repo)
    if src:
        terms, host_ips = derive_private_terms(src)
        term_hashes, ip_hashes = set(), set()
    else:
        terms, host_ips = set(), set()
        term_hashes, ip_hashes = load_hashes(repo)   # modo CI
    findings, seen = [], 0

    for full, rel in walk_repo(repo):
        seen += 1
        if rel not in allowed and not any(rel.startswith(g) for g in globs):
            findings.append(Finding(0, rel, "NO está en el manifiesto (deny-by-default)"))
            # Sin `continue`: un archivo no listado igual se escanea por contenido.
        findings.extend(check_path_rules(rel))
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        if size > MAX_BYTES:
            findings.append(Finding(6, rel, f"archivo de {size/1024/1024:.1f} MB (máximo 1 MB)"))
        if os.path.splitext(rel)[1].lower() in TEXT_EXT:
            try:
                findings.extend(scan_text(
                    rel, open(full, encoding="utf-8", errors="replace").read(),
                    terms, host_ips, term_hashes, ip_hashes, excepts))
            except OSError:
                pass

    findings.extend(check_git(repo, src, deep))
    return findings, seen, terms, term_hashes


def self_test(repo):
    """Prueba negativa: si sembrar un archivo sucio NO falla, el check no sirve."""
    import tempfile
    # En `mapa/`, no en `docs/`: docs está exento de la regla 1 a propósito, así
    # que una sonda ahí nunca ejercería esa regla (y el test daría un falso OK).
    probe = os.path.join(repo, "mapa", ".leakprobe.py")
    os.makedirs(os.path.dirname(probe), exist_ok=True)
    # Armado por partes a propósito: si el literal viviera en este archivo, el
    # propio checker dispararía las reglas 1 y 2 sobre su fuente.
    # Incluye un path como 2º argumento de función: es justo la forma que un bug
    # de la heurística de interpolación dejó pasar una vez (regresión guard).
    dirty = ("SRC = '" + "/" + "mnt/algun-disco-privado/data'\n"
             + "IP = '" + "8.8" + ".8.8'\n"
             + "import sys; sys.path.insert(0, '" + "/" + "mnt/otro-disco/secreto')\n")
    open(probe, "w").write(dirty)
    try:
        findings, _, _, _ = run(repo, None, False)
        hits = [f for f in findings if f.path.endswith(".leakprobe.py")]
        rules = {f.rule for f in hits}
        ok = 0 in rules and 1 in rules and 2 in rules
        print(f"[self-test] archivo sembrado → reglas disparadas: {sorted(rules) or 'NINGUNA'}")
        print("[self-test] " + ("OK: el check detecta lo que debe." if ok else
                                "FALLO: el check NO detecta un archivo sucio."))
        return 0 if ok else 1
    finally:
        os.path.exists(probe) and os.remove(probe)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--src", help="MAPA_SRC: habilita las reglas 3/3b (build local)")
    ap.add_argument("--deep", action="store_true", help="revisar también la historia git")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--emit-hashes", action="store_true",
                    help="regenera tools/leakcheck_terms.hashed desde --src")
    a = ap.parse_args()

    if not os.path.isdir(a.repo):
        sys.exit(2)
    if a.emit_hashes:
        if not a.src:
            sys.exit("[leak-check] --emit-hashes necesita --src")
        emit_hashes(a.repo, a.src)
        return 0
    if a.self_test:
        sys.exit(self_test(a.repo))

    findings, seen, terms, thashes = run(a.repo, a.src, a.deep)
    if terms:
        mode = f", {len(terms)} términos privados derivados (reglas 3/3b activas, modo local)"
    elif thashes:
        mode = f", {len(thashes)} hashes de términos privados (reglas 3/3b por hash, modo CI)"
    else:
        mode = ", reglas 3/3b INACTIVAS (sin --src ni archivo de hashes)"
    print(f"[leak-check] {seen} archivos revisados" + mode)
    if not findings:
        print("[leak-check] LIMPIO")
        return 0
    print(f"[leak-check] {len(findings)} HALLAZGOS:")
    for f in sorted(findings, key=lambda f: (f.rule, f.path, f.line or 0)):
        print(f)
    return 1


if __name__ == "__main__":
    sys.exit(main())
