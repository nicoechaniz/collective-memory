#!/usr/bin/env python3
"""bootstrap_model.py — descarga el modelo de embeddings, una sola vez.

En runtime el sistema trabaja **offline**: `HF_HUB_OFFLINE=1` y el modelo se
carga con `local_files_only=True`. Eso es deliberado — nada del corpus sale de
la maquina, y el indexado no depende de que haya red. Pero implica que una
instalacion nueva no arranca hasta que el modelo este en cache.

Este script es el unico lugar donde la descarga esta permitida: levanta el modo
offline solo mientras baja, y despues todo vuelve a la normalidad.

Uso:  tools/bootstrap_model.py [--model <id>]
"""
import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "mapa"))

# Se levanta ANTES de importar mapa_config, que es quien fija el modo offline.
os.environ["MAPA_ALLOW_MODEL_DOWNLOAD"] = "1"
os.environ.pop("HF_HUB_OFFLINE", None)
os.environ.pop("TRANSFORMERS_OFFLINE", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    a = ap.parse_args()

    os.environ.setdefault("MAPA_ROOT", os.environ.get("MAPA_ROOT", "/tmp"))
    import mapa_config as cfg

    model_id = a.model or cfg.MODEL_ID
    cache = cfg.HF_HOME
    os.makedirs(cache, exist_ok=True)

    print(f"[bootstrap] modelo : {model_id}")
    print(f"[bootstrap] cache  : {cache}")
    print("[bootstrap] descargando (son ~2 GB la primera vez)…")

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        sys.exit("[bootstrap] falta sentence-transformers. Corré antes: "
                 "pip install -r requirements.txt")

    try:
        m = SentenceTransformer(model_id, cache_folder=os.path.join(cache, "hub"))
    except Exception as e:
        sys.exit(f"[bootstrap] no se pudo descargar {model_id}: {e}\n"
                 "  Revisá la conexion, o bajalo a mano al cache de HuggingFace.")

    dims = m.get_sentence_embedding_dimension()
    print(f"[bootstrap] listo. Dimension de los vectores: {dims}")
    print("[bootstrap] a partir de ahora el sistema corre offline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
