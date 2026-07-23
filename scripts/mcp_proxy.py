#!/usr/bin/env python3
"""Proxy MCP stdio→unix-socket del director (F20). stdlib-only.

Corre DENTRO de la unidad B (el cerebro) como `command` del MCP config del
backend: bombea stdin→socket y socket→stdout. Primera línea al conectar = token
(leído de $CREDENTIALS_DIRECTORY/mcp_token o del archivo pasado como argv[2]).

Uso: mcp_proxy.py <sock_path> [token_file]
"""
import os
import socket
import sys
import threading


def main():
    sock_path = sys.argv[1]
    token_file = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.environ.get("CREDENTIALS_DIRECTORY", ""), "mcp_token")
    with open(token_file) as f:
        token = f.read().strip()

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sock_path)
    s.sendall((token + "\n").encode())

    def pump_out():
        buf = b""
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
        os._exit(0)

    t = threading.Thread(target=pump_out, daemon=True)
    t.start()
    # readline() explícito, NO `for line in`: el iterador de archivo hace read-ahead
    # y NO entrega una línea suelta (p.ej. un tool-call que el modelo manda tras
    # razonar) hasta llenar su buffer → el server nunca la ve y el cliente cancela.
    inp = sys.stdin.buffer
    while True:
        line = inp.readline()
        if not line:
            break
        s.sendall(line)
    s.shutdown(socket.SHUT_WR)
    t.join(timeout=5)


if __name__ == "__main__":
    main()
