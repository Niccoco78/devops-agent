"""Fournisseurs d'IA : registre, clés API et modèles.

Une clé vient, par priorité : des réglages saisis dans l'interface (data/settings.json), sinon de la
variable d'environnement du fournisseur. Les clés ne sortent jamais du serveur autrement que masquées.

    python server.py            -> panneau « Fournisseurs d'IA » en bas de page
    ANTHROPIC_API_KEY=...       -> ou par l'environnement (Docker, CI)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"
_lock = threading.Lock()

# kind : comment on parle au fournisseur (voir llm.py). base_url modifiable pour « compatible ».
PROVIDERS: dict[str, dict] = {
    "opencode": {
        "label": "opencode (gratuit, sans clé)", "kind": "opencode", "needs_key": False,
        "models": ["opencode/big-pickle", "opencode/nemotron-3-ultra-free", "opencode/nemotron-3.5-lightning-free",
                   "opencode/mimo-v2.5-free", "opencode/ling-3.0-flash-fin-free",
                   "opencode/muse-spark-1.3-contributor-free", "opencode/muse-spark-1.2-contributor-free"],
    },
    "anthropic": {
        "label": "Anthropic (Claude)", "kind": "anthropic", "needs_key": True, "env": "ANTHROPIC_API_KEY",
        "models": ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"], "key_hint": "sk-ant-…",
    },
    "openai": {
        "label": "OpenAI", "kind": "openai", "needs_key": True, "env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1", "models": ["gpt-5", "gpt-5-mini", "gpt-4.1"], "key_hint": "sk-…",
    },
    "gemini": {
        "label": "Google Gemini", "kind": "gemini", "needs_key": True, "env": "GEMINI_API_KEY",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"], "key_hint": "AIza…",
    },
    "mistral": {
        "label": "Mistral", "kind": "openai", "needs_key": True, "env": "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1", "models": ["mistral-large-latest", "codestral-latest"], "key_hint": "clé Mistral",
    },
    "compatible": {
        "label": "Compatible OpenAI (Ollama, OpenRouter, Groq, DeepSeek…)", "kind": "openai", "needs_key": False,
        "env": "OPENAI_COMPAT_API_KEY", "base_url": "http://localhost:11434/v1", "models": ["llama3.1", "qwen2.5-coder"],
        "key_hint": "clé (vide pour Ollama)", "editable_url": True,
    },
}

# Ordre de préférence pour le backend par défaut quand plusieurs sont configurés.
PRIORITY = ("anthropic", "openai", "gemini", "mistral", "compatible", "opencode")


def _load() -> dict:
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)


def provider_config(pid: str) -> dict:
    """Configuration effective d'un fournisseur : registre + réglages + environnement."""
    base = PROVIDERS[pid]
    saved = _load().get("providers", {}).get(pid, {})
    key = saved.get("api_key") or (os.environ.get(base["env"]) if base.get("env") else None) or ""
    base_url = saved.get("base_url") or os.environ.get(f"{pid.upper()}_BASE_URL") or base.get("base_url", "")
    # Modèles : ceux que le fournisseur annonce réellement (voir list_models), le modèle par défaut en tête.
    # La liste du registre ne sert que de repli interne, jamais d'affichage.
    found = (_models.get(pid) or {}).get("models") or []
    models = _ordered(found, saved.get("default_model")) or saved.get("models") or base["models"]
    # « configuré » : opencode toujours ; un fournisseur à clé dès qu'il a une clé ; « compatible »
    # (Ollama & co, clé facultative) dès que l'utilisateur a enregistré quelque chose pour lui.
    configured = pid == "opencode" or bool(key) or (not base["needs_key"] and bool(saved))
    return {"id": pid, "kind": base["kind"], "api_key": key, "base_url": base_url, "models": list(models),
            "source": "settings" if saved else ("env" if key else ""),
            "configured": configured}


def mask(key: str) -> str:
    if not key:
        return ""
    return key[:4] + "…" + key[-4:] if len(key) > 12 else "•" * len(key)


def describe_all(refresh: bool = False) -> list[dict]:
    """Ce que l'interface a le droit de voir : jamais la clé en clair. Les modèles sont ceux que chaque
    fournisseur configuré annonce réellement (interrogé en parallèle, mémorisé 15 minutes)."""
    configured = [pid for pid in PROVIDERS if provider_config(pid)["configured"]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        found = dict(zip(configured, pool.map(lambda p: list_models(p, refresh=refresh), configured)))
    out = []
    for pid, base in PROVIDERS.items():
        cfg = provider_config(pid)
        saved = _load().get("providers", {}).get(pid, {})
        lm = found.get(pid) or {"models": [], "error": None, "loading": False}
        out.append({
            "id": pid, "label": base["label"], "kind": base["kind"], "needs_key": base["needs_key"],
            "env": base.get("env"), "key_hint": base.get("key_hint", ""), "editable_url": base.get("editable_url", False),
            "configured": cfg["configured"], "source": cfg["source"], "key_masked": mask(cfg["api_key"]),
            "base_url": cfg["base_url"], "models": _ordered(lm["models"], saved.get("default_model")),
            "models_error": lm["error"], "models_loading": lm["loading"], "default_model": saved.get("default_model"),
        })
    return out


# ---------------------------------------------------------------------------
# Modèles disponibles : demandés à chaque fournisseur, pas supposés
# ---------------------------------------------------------------------------

MODELS_TTL = 900
_models: dict[str, dict] = {}          # pid -> {"at", "models", "error"}
_models_lock = threading.Lock()
_loading: set[str] = set()
_NOT_CHAT = re.compile(r"embedding|tts|whisper|audio|realtime|transcribe|image|dall-e|moderation|search|similarity|"
                       r"babbage|davinci|computer-use", re.I)


def _ordered(models: list[str], default: str | None) -> list[str]:
    return ([default] + [m for m in models if m != default]) if default and default in models else list(models)


def _get_json(url: str, headers: dict, timeout: int = 12):
    req = urllib.request.Request(url, headers={"User-Agent": "devops-agent", "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code in (400, 401, 403):
            raise RuntimeError(f"clé refusée par le fournisseur (HTTP {e.code})") from e
        raise RuntimeError(f"HTTP {e.code} : {e.read().decode('utf-8', 'replace')[:200]}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"fournisseur injoignable : {getattr(e, 'reason', e)}") from e


def _version_key(mid: str):
    m = re.search(r"(\d+(?:\.\d+)?)", mid)
    return (-(float(m.group(1)) if m else 0), bool(re.search(r"preview|exp", mid)), len(mid), mid)


def _discover(pid: str) -> list[str]:
    cfg, kind = provider_config(pid), PROVIDERS[pid]["kind"]
    key = cfg["api_key"]
    if kind == "opencode":
        from .llm import OpencodeBackend
        exe = OpencodeBackend._find_executable()
        if not exe:
            raise RuntimeError("opencode est introuvable")
        proc = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90)
        found = [l.strip() for l in proc.stdout.splitlines() if l.strip().startswith("opencode/")]
        if not found:
            raise RuntimeError("opencode ne liste aucun modèle " + proc.stderr.strip()[-200:])
        return sorted(found, key=lambda m: (0 if "big-pickle" in m else 1 if m.endswith("-free") else 2, m))
    if kind == "anthropic":
        data = _get_json("https://api.anthropic.com/v1/models?limit=1000", {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return [m["id"] for m in data.get("data", []) if m.get("id")]          # déjà du plus récent au plus ancien
    if kind == "gemini":
        data = _get_json("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", {"x-goog-api-key": key})
        ids = {m["name"].split("/", 1)[-1] for m in data.get("models", [])
               if "generateContent" in (m.get("supportedGenerationMethods") or []) and m.get("name", "").startswith("models/gemini")
               and not re.search(r"embedding|tts|image|live|audio|aqa|robotics|computer|transcribe", m["name"], re.I)}
        return sorted(ids, key=_version_key)
    data = _get_json(cfg["base_url"].rstrip("/") + "/models", {"Authorization": f"Bearer {key}"} if key else {})
    items = [m for m in (data.get("data", []) if isinstance(data, dict) else []) if isinstance(m, dict) and m.get("id")]
    if pid == "openai":
        items = [m for m in items if re.match(r"(gpt-|o\d|chatgpt)", m["id"]) and not _NOT_CHAT.search(m["id"])]
    elif pid == "mistral":
        items = [m for m in items if (m.get("capabilities") or {}).get("completion_chat", True) and not m.get("deprecation")]
    items.sort(key=lambda m: -(m.get("created") or 0))
    return list(dict.fromkeys(m["id"] for m in items))


def _refresh(pid: str) -> None:
    try:
        models, err = _discover(pid), None
    except Exception as e:  # noqa: BLE001 — l'erreur est montrée dans l'interface
        models, err = [], str(e)[:300]
    with _models_lock:
        prev = _models.get(pid) or {}
        if err and prev.get("models"):
            models = prev["models"]             # panne passagère : on garde la dernière liste connue
        _models[pid] = {"at": time.time(), "models": models, "error": err}
        _loading.discard(pid)


def list_models(pid: str, refresh: bool = False) -> dict:
    """Modèles réellement disponibles chez ce fournisseur. opencode (lent) est interrogé en tâche de fond."""
    if not provider_config(pid)["configured"]:
        return {"models": [], "error": None, "loading": False}
    with _models_lock:
        entry = _models.get(pid)
        stale = refresh or not entry or time.time() - entry["at"] > MODELS_TTL
        start_bg = stale and PROVIDERS[pid]["kind"] == "opencode" and pid not in _loading
        if start_bg:
            _loading.add(pid)
    if stale and PROVIDERS[pid]["kind"] != "opencode":
        _refresh(pid)
    elif start_bg:
        threading.Thread(target=_refresh, args=(pid,), daemon=True).start()
    entry = _models.get(pid) or {}
    return {"models": entry.get("models", []), "error": entry.get("error"), "loading": pid in _loading}


def update(pid: str, *, api_key: str | None = None, models: list[str] | None = None,
           base_url: str | None = None, clear: bool = False, default_model: str | None = None) -> None:
    """Enregistre (ou efface) les réglages d'un fournisseur."""
    if pid not in PROVIDERS:
        raise KeyError(pid)
    with _lock:
        settings = _load()
        providers = settings.setdefault("providers", {})
        if clear:
            providers.pop(pid, None)
        else:
            entry = providers.setdefault(pid, {})
            if api_key is not None:
                if api_key.strip():
                    entry["api_key"] = api_key.strip()
                else:
                    entry.pop("api_key", None)
            if models is not None:
                cleaned = [m.strip() for m in models if m.strip()]
                if cleaned:
                    entry["models"] = cleaned
                else:
                    entry.pop("models", None)
            if base_url is not None:
                if base_url.strip():
                    entry["base_url"] = base_url.strip().rstrip("/")
                else:
                    entry.pop("base_url", None)
            if default_model is not None:
                if default_model.strip():
                    entry["default_model"] = default_model.strip()
                else:
                    entry.pop("default_model", None)
            if not entry:
                providers.pop(pid, None)
        _save(settings)
    if clear or api_key is not None or base_url is not None:
        with _models_lock:
            _models.pop(pid, None)             # nouvelle clé ou nouvelle URL : la liste des modèles est à redemander


def set_default(pid: str | None) -> None:
    with _lock:
        settings = _load()
        if pid:
            settings["default"] = pid
        else:
            settings.pop("default", None)
        _save(settings)


def default_provider() -> str:
    """Le backend par défaut : celui choisi dans les réglages s'il est configuré, sinon le premier
    configuré par ordre de préférence, sinon opencode."""
    chosen = _load().get("default")
    if chosen in PROVIDERS and provider_config(chosen)["configured"]:
        return chosen
    for pid in PRIORITY:
        if pid != "opencode" and provider_config(pid)["configured"]:
            return pid
    return "opencode"
