#!/usr/bin/env python3
"""El perfil estructural debe bloquear toda ruta que pueda invocar un LLM."""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def request(url, token=None, body=None):
    headers = {}
    data = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, headers=headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.load(exc)


def main():
    with tempfile.TemporaryDirectory(prefix="mapa-structural-") as tmp:
        root = os.path.join(tmp, "corpus")
        data = os.path.join(tmp, "state")
        discovery = os.path.join(data, "discovery")
        os.makedirs(root)
        os.makedirs(discovery)
        token = "fixture-structural-token"
        with open(os.path.join(discovery, "users.json"), "w", encoding="utf-8") as handle:
            json.dump({"fixture": {"sha256": hashlib.sha256(token.encode()).hexdigest(),
                                    "disabled": False}}, handle)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = os.environ.copy()
        env.update({
            "MAPA_ROOT": root,
            "MAPA_DATA": data,
            "MAPA_BIND": "127.0.0.1",
            "MAPA_PG_PORT": str(port),
            "MAPA_STRUCTURAL_ONLY": "1",
            "MAPA_DISCOVERY_NO_LLM": "1",
            "MAPA_DIRECTOR": "0",
        })
        proc = subprocess.Popen(
            [sys.executable, os.path.join(REPO, "mapa", "playground.py"), "serve"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(60):
                try:
                    code, health = request(base + "/pg/health")
                    if code == 200:
                        break
                except (OSError, ValueError):
                    time.sleep(0.1)
            else:
                raise AssertionError("playground estructural no inicio")
            assert health["mode"] == "structural-only"
            assert health["llm_screening"] is False
            assert set(health["operators"]) == {"latent_bridge", "cluster_frontier", "outlier"}

            code, operators = request(base + "/pg/operators", token)
            assert code == 200 and operators["judges"] == []
            assert operators["default_judge"] == ""

            code, _ = request(base + "/pg/run", token, {"task": "usa un agente"})
            assert code == 503
            code, _ = request(base + "/pg/run", token, {"operators": ["tension"]})
            assert code == 400
            code, _ = request(base + "/pg/director", token, {"password": "irrelevante"})
            assert code == 503
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
    print("structural_mode: guards backend OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
