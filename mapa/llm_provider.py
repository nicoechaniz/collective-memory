#!/usr/bin/env python3
"""Provider LLM abstracto para la capa de discovery (F14).

Default: ollama local (127.0.0.1:11434). Bisagra: providers
"openai_compatible" (API externa) configurables en
.mapa/discovery/llm_providers.json — ese dir está EXCLUIDO del corpus por
policy, así que puede contener api_key sin riesgo de indexarse.

Invariantes:
  - Opener sin proxies (HTTP(S)_PROXY del entorno no puede desviar tráfico).
  - Bajo el unit systemd del playground, un provider remoto queda BLOQUEADO
    por IPAddressDeny; habilitarlo requiere un drop-in de egress deliberado
    (20-egress.conf). El error se reporta claro, no falla mudo.
"""
import json
import re
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from mapa_config import DMAPA  # noqa: E402
# Canonical en el control plane del director (F20): parent root-owned — la web
# (grupo mapa) lo LEE pero no puede reemplazarlo. SIN fallback al path legacy de
# discovery/ (web-escribible): un canonical ausente/ilegible es fail-closed.
PROVIDERS_PATH = os.path.join(DMAPA, "director", "providers.json")

DEFAULT_PROVIDERS = {
    "default": {"type": "ollama", "url": "http://127.0.0.1:11434", "model": "qwen3:8b"},
}

# Sin proxies: LLM local-only por default; el egress remoto es una decisión
# explícita del provider config + drop-in systemd, jamás una env var heredada.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_providers():
    # Fail-closed (F20): sin el canonical no hay providers — jamás degradar a un
    # default silencioso ni leer el path legacy (discovery/ es web-escribible).
    try:
        with open(PROVIDERS_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"[llm_provider] providers.json canonical ilegible ({PROVIDERS_PATH}): {e} "
                           "— fail-closed, no hay fallback") from e
    if not isinstance(cfg, dict) or not cfg:
        raise RuntimeError(f"[llm_provider] providers.json inválido: se esperaba un dict no vacío")
    merged = dict(DEFAULT_PROVIDERS)
    merged.update(cfg)
    return merged


def get_provider(name="default", model_override=None):
    provs = load_providers()
    p = provs.get(name)
    if not p:
        raise ValueError(f"provider desconocido: {name} (configurados: {sorted(provs)})")
    p = dict(p)
    if model_override:
        p["model"] = model_override
    p["name"] = name
    return p


def provider_label(p):
    return f"{p.get('name', '?')}:{p.get('model', '?')}"


def _parse_json_content(text):
    """json.loads robusto: gemma4 envuelve en fences ```json …``` aunque se pida format:json."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", t, re.S)  # último recurso: primer bloque {...}
        if m:
            return json.loads(m.group(0))
        raise


def _post_json(url, payload, headers, timeout):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def chat_json(system, user, provider="default", model=None, timeout=120):
    """Chat con salida JSON estricta. Devuelve dict parseado o None (2 intentos).

    provider: nombre en llm_providers.json, o un dict ya resuelto por get_provider().
    """
    p = provider if isinstance(provider, dict) else get_provider(provider, model)
    for attempt in (1, 2):
        try:
            if p["type"] == "ollama":
                out = _post_json(
                    p["url"].rstrip("/") + "/api/chat",
                    {"model": p["model"],
                     "messages": [{"role": "system", "content": system},
                                  {"role": "user", "content": user}],
                     "stream": False, "think": False, "format": "json",
                     "options": {"temperature": 0.2}},
                    {}, timeout)
                return _parse_json_content(out["message"]["content"])
            if p["type"] == "openai_compatible":
                out = _post_json(
                    p["base_url"].rstrip("/") + "/chat/completions",
                    {"model": p["model"],
                     "messages": [{"role": "system", "content": system},
                                  {"role": "user", "content": user}],
                     "response_format": {"type": "json_object"},
                     "temperature": 0.2},
                    {"Authorization": f"Bearer {p.get('api_key', '')}"}, timeout)
                return _parse_json_content(out["choices"][0]["message"]["content"])
            raise ValueError(f"tipo de provider desconocido: {p['type']}")
        except urllib.error.URLError as e:
            if attempt == 2:
                print(f"  [llm:{provider_label(p)}] fallo de red: {e} "
                      "(si es un provider remoto bajo el unit del playground: "
                      "está bloqueado por IPAddressDeny — falta el drop-in 20-egress.conf)",
                      file=sys.stderr)
        except Exception as e:
            if attempt == 2:
                print(f"  [llm:{provider_label(p)}] fallo definitivo: {e}", file=sys.stderr)
    return None


def provider_supports_tools(provider="default"):
    """True salvo que la config lo declare explícitamente sin soporte (supports_tools:false).
    Default True: gemma4/qwen3.6 y las APIs openai soportan tools; solo modelos viejos
    (gemma3) lo declaran false, y ahí el loop usa el fallback ReAct-JSON."""
    p = provider if isinstance(provider, dict) else get_provider(provider)
    return p.get("supports_tools", True) is not False


def _norm_tool_calls(raw_calls):
    """Normaliza tool_calls de ollama u openai a [{id, name, args:dict}]."""
    out = []
    for i, tc in enumerate(raw_calls or []):
        fn = tc.get("function", tc) or {}
        args = fn.get("arguments")
        if isinstance(args, str):  # openai devuelve arguments como string JSON
            try:
                args = json.loads(args)
            except Exception:
                args = {}
        out.append({"id": tc.get("id") or f"call_{i}", "name": fn.get("name"), "args": args or {}})
    return out


def chat_tools(messages, tools, provider="default", model=None, num_predict=1024, timeout=120):
    """Chat con tool-calling nativo (Gemma 4 / Qwen 3.6 / APIs openai).

    messages: historial multiturno [{role: system|user|assistant|tool, content, tool_calls?, tool_call_id?}].
    tools: [{type:"function", function:{name, description, parameters}}].
    Devuelve dict normalizado {content, tool_calls:[{id,name,args}]} o None (2 intentos).
    num_predict acota tokens por llamada (cap del loop del agente).
    """
    p = provider if isinstance(provider, dict) else get_provider(provider, model)
    for attempt in (1, 2):
        try:
            if p["type"] == "ollama":
                out = _post_json(
                    p["url"].rstrip("/") + "/api/chat",
                    {"model": p["model"], "messages": messages, "tools": tools,
                     "stream": False, "think": False,
                     "options": {"temperature": 0.2, "num_predict": num_predict}},
                    {}, timeout)
                msg = out.get("message", {})
                return {"content": msg.get("content") or "", "tool_calls": _norm_tool_calls(msg.get("tool_calls"))}
            if p["type"] == "openai_compatible":
                out = _post_json(
                    p["base_url"].rstrip("/") + "/chat/completions",
                    {"model": p["model"], "messages": messages, "tools": tools,
                     "temperature": 0.2, "max_tokens": num_predict},
                    {"Authorization": f"Bearer {p.get('api_key', '')}"}, timeout)
                msg = out["choices"][0]["message"]
                return {"content": msg.get("content") or "", "tool_calls": _norm_tool_calls(msg.get("tool_calls"))}
            raise ValueError(f"tipo de provider desconocido: {p['type']}")
        except urllib.error.URLError as e:
            if attempt == 2:
                print(f"  [llm:{provider_label(p)}] fallo de red en chat_tools: {e}", file=sys.stderr)
        except Exception as e:
            if attempt == 2:
                print(f"  [llm:{provider_label(p)}] fallo en chat_tools: {e}", file=sys.stderr)
    return None


def provider_up(provider="default", timeout=5):
    try:
        p = provider if isinstance(provider, dict) else get_provider(provider)
    except ValueError:
        return False
    try:
        if p["type"] == "ollama":
            _OPENER.open(p["url"].rstrip("/") + "/api/tags", timeout=timeout)
            return True
        if p["type"] == "openai_compatible":
            # Sin endpoint estándar barato: probamos /models (openai lo expone).
            req = urllib.request.Request(p["base_url"].rstrip("/") + "/models",
                                         headers={"Authorization": f"Bearer {p.get('api_key', '')}"})
            _OPENER.open(req, timeout=timeout)
            return True
    except Exception:
        return False
    return False
