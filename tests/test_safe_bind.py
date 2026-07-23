#!/usr/bin/env python3
"""Test del invariante de bind: el servicio nunca queda expuesto a internet.

El sistema original garantizaba esto con una IP hardcodeada. Al hacerlo portable
la garantía pasa a depender de `safe_bind()`, así que hay que ejercerla.

NOTA: este archivo contiene literales de IP pública a propósito — son los
fixtures sin los cuales no se puede probar el rechazo. Los rangos reservados
para documentación NO sirven: `ipaddress` los clasifica como no-globales, así
que un test escrito con ellos pasaría sin ejercer nada. Por eso el manifiesto le
declara a este path una excepción acotada a la regla 2 del leak-check.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MAPA = os.path.join(os.path.dirname(HERE), "mapa")


def try_bind(value, allow_lan=False):
    """Corre safe_bind en un proceso aparte (usa sys.exit para fallar cerrado).

    Devuelve (ok, salida).
    """
    env = dict(os.environ)
    env["MAPA_ROOT"] = "/tmp"
    env.pop("MAPA_BIND_ALLOW_LAN", None)
    if allow_lan:
        env["MAPA_BIND_ALLOW_LAN"] = "1"
    code = (
        "import mapa_config as c, sys;"
        "sys.stdout.write(c.safe_bind(%r))" % value
    )
    p = subprocess.run([sys.executable, "-c", code], cwd=MAPA, env=env,
                       capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


REJECT = [
    ("0.0.0.0", "comodín IPv4"),
    ("::", "comodín IPv6"),
    ("", "cadena vacía (bindea todas las interfaces)"),
    ("0", "cero (bindea todas las interfaces)"),
    ("8.8.8.8", "IP pública"),
    ("::ffff:8.8.8.8", "IP pública mapeada en IPv6"),
    ("no-una-ip", "no es una IP literal"),
]

ACCEPT_ALWAYS = [("127.0.0.1", "loopback")]
NEEDS_OPT_IN = [("192.168.42.7", "RFC1918"), ("10.0.0.5", "RFC1918")]


def main():
    fails = []

    for value, why in REJECT:
        ok, out = try_bind(value)
        print(f"  rechaza {value!r:22} ({why}) … {'OK' if not ok else 'FALLA'}")
        if ok:
            fails.append(f"aceptó {value!r} ({why}) — debía rechazarlo. Salida: {out}")

    for value, why in ACCEPT_ALWAYS:
        ok, out = try_bind(value)
        print(f"  acepta  {value!r:22} ({why}) … {'OK' if ok else 'FALLA'}")
        if not ok:
            fails.append(f"rechazó {value!r} ({why}) — debía aceptarlo. Salida: {out}")

    for value, why in NEEDS_OPT_IN:
        ok_without, _ = try_bind(value, allow_lan=False)
        ok_with, out = try_bind(value, allow_lan=True)
        good = (not ok_without) and ok_with
        print(f"  {value!r:22} ({why}) exige opt-in … {'OK' if good else 'FALLA'}")
        if ok_without:
            fails.append(f"aceptó {value!r} sin MAPA_BIND_ALLOW_LAN=1")
        if not ok_with:
            fails.append(f"rechazó {value!r} incluso con el opt-in. Salida: {out}")

    print()
    if fails:
        print(f"FALLÓ ({len(fails)}):")
        for f in fails:
            print("  - " + f)
        return 1
    print("safe_bind: todos los casos OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
