"""Action : envoyer le prompt à un modèle et récupérer un JSON exploitable.

Deux backends, même interface `complete(system, user) -> LLMResult` :

  - AnthropicBackend : SDK officiel `anthropic`, sortie structurée garantie par le schéma JSON.
  - OpencodeBackend  : CLI `opencode run` en sous-processus (modèles gratuits « opencode zen »).
                       Le prompt passe par stdin (pas de limite de taille d'argument sous Windows),
                       la réponse est lue dans le flux d'événements JSON du CLI.
"""

from __future__ import annotations

from . import cancel

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass


@dataclass
class LLMResult:
    data: dict            # le JSON parsé
    raw_text: str         # le texte brut renvoyé par le modèle (pour le débogage)
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class LLMError(RuntimeError):
    """Erreur d'appel au modèle, avec un message actionnable pour l'utilisateur.

    `raw` : la réponse brute du modèle quand il y en a une (JSON invalide, mauvaise structure…),
    pour pouvoir la sauvegarder et la relire.
    """

    def __init__(self, message: str, raw: str | None = None):
        super().__init__(message)
        self.raw = raw


# ---------------------------------------------------------------------------
# Utilitaires communs
# ---------------------------------------------------------------------------

# Clés qui identifient la réponse d'analyse. « project » n'en fait pas partie : certains modèles
# aplatissent name/purpose au premier niveau, et report.validate() sait remettre ça d'équerre.
EXPECTED_KEYS = ("architecture", "risks")


def _json_objects(text: str):
    """Énumère tous les objets JSON valides trouvés dans un texte (du dernier au premier).

    Un modèle bavard peut produire du texte, recopier le schéma, puis donner la vraie réponse :
    on veut pouvoir choisir le bon objet, pas seulement « du premier { au dernier } ».
    """
    decoder = json.JSONDecoder()
    results = []
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            results.append(obj)
        pos = end
    return reversed(results)


def _closers_for(text: str) -> str | None:
    """Calcule les fermetures manquantes (`"`, `]`, `}`) d'un JSON coupé net. None si le texte est incohérent."""
    stack: list[str] = []
    in_string = escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack or stack[-1] != ch:
                return None
            stack.pop()
    return ('"' if in_string else "") + "".join(reversed(stack))


def repair_truncated_json(text: str, expected_keys=EXPECTED_KEYS) -> dict | None:
    """Tente de récupérer un objet JSON dont la fin manque (sortie coupée par le modèle).

    On recule jusqu'au dernier `}` (fin d'un élément complet), on referme ce qui est ouvert,
    et on garde le premier résultat qui porte les clés attendues. Les éléments partiels sont perdus,
    le reste du rapport est sauvé.
    """
    start = text.find("{")
    if start == -1:
        return None
    body = text[start:]
    pos = len(body)
    for _ in range(40):
        pos = body.rfind("}", 0, pos)
        if pos <= 0:
            return None
        candidate = body[: pos + 1]
        closers = _closers_for(candidate)
        if closers is None:
            continue
        try:
            obj = json.loads(candidate + closers)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and all(k in obj for k in expected_keys):
            obj["_truncated"] = True
            return obj
    return None


_QUOTE_SLIP = re.compile(r"'(\s*[,:]\s*)'")     # ','  ':'  →  ","  ":"


def lenient_loads(text: str, max_fixes: int = 30) -> dict | None:
    """json.loads tolérant aux fautes de frappe des modèles : un `'` à la place d'un `"` autour
    d'une virgule ou d'un deux-points (`…du quiz','evidence":…`). À chaque échec, on corrige dans
    la fenêtre précédant l'erreur et on réessaie ; on abandonne si rien ne change."""
    for _ in range(max_fixes):
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError as e:
            start = max(0, e.pos - 600)
            window, fixed = text[start:e.pos + 1], None
            fixed = _QUOTE_SLIP.sub(r'"\1"', window)
            if fixed == window:
                return None
            text = text[:start] + fixed + text[e.pos + 1:]
    return None


def extract_json(text: str, expected_keys=EXPECTED_KEYS) -> dict:
    """Récupère la réponse JSON attendue (celle qui porte `expected_keys`), même noyée dans des fences,
    du bavardage, mal typographiée, ou coupée."""
    cleaned = re.sub(r"```(?:json)?", "", text)
    fallback = None
    for obj in _json_objects(cleaned):
        if all(k in obj for k in expected_keys):
            return obj                      # le bon objet : celui qui a les clés attendues
        fallback = fallback or obj
    # JSON complet mais mal typographié (guillemets simples) : décodage tolérant.
    first, last = cleaned.find("{"), cleaned.rfind("}")
    if first != -1 and last > first:
        obj = lenient_loads(cleaned[first:last + 1])
        if obj is not None and all(k in obj for k in expected_keys):
            return obj
    repaired = repair_truncated_json(cleaned, expected_keys)
    if repaired is not None:
        return repaired
    if fallback is not None:
        # Un objet JSON existe mais n'a pas la bonne forme (souvent : le schéma recopié).
        raise LLMError(
            "Le modèle a renvoyé du JSON, mais pas la structure demandée "
            f"(clés trouvées : {', '.join(list(fallback)[:6])}).\n--- début de la réponse ---\n{text[:800]}",
            raw=text,
        )
    raise LLMError(f"Aucun JSON trouvé dans la réponse du modèle.\n--- début de la réponse ---\n{text[:800]}", raw=text)


# ---------------------------------------------------------------------------
# Backend Anthropic
# ---------------------------------------------------------------------------

_UNSUPPORTED_BOUNDS = ("maxItems", "minLength", "maxLength", "minimum", "maximum",
                       "exclusiveMinimum", "exclusiveMaximum", "multipleOf")


def anthropic_schema(schema):
    """Copie du schéma acceptée par la sortie structurée d'Anthropic.

    L'API refuse `minItems` au-delà de 1 (« au moins 3 risques ») et les bornes numériques ou de longueur.
    On les retire de la copie envoyée ; la règle reste tenue ailleurs : le prompt demande au moins trois
    risques et la validation du rapport relance le modèle si la réponse est incomplète.
    """
    if isinstance(schema, dict):
        out = {k: anthropic_schema(v) for k, v in schema.items() if k not in _UNSUPPORTED_BOUNDS}
        if isinstance(out.get("minItems"), int) and out["minItems"] > 1:
            out["minItems"] = 1
        return out
    if isinstance(schema, list):
        return [anthropic_schema(v) for v in schema]
    return schema


class AnthropicBackend:
    DEFAULT_MODEL = "claude-opus-5"

    def __init__(self, model: str | None = None, schema: dict | None = None, api_key: str | None = None):
        try:
            import anthropic  # import local : le SDK est optionnel
        except ImportError as e:
            raise LLMError("Le SDK Anthropic n'est pas installé : pip install anthropic") from e
        self._anthropic = anthropic
        # Clé des réglages si fournie ; sinon le SDK lit ANTHROPIC_API_KEY ou un profil `ant auth login`.
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model or self.DEFAULT_MODEL
        self.schema = anthropic_schema(schema) if schema is not None else None

    def complete(self, system: str, user: str, expected_keys=EXPECTED_KEYS) -> LLMResult:
        a = self._anthropic
        kwargs: dict = {}
        if self.schema is not None:
            # Sortie structurée : le premier bloc texte est garanti conforme au schéma.
            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": self.schema}}
        try:
            # Streaming : l'entrée peut être longue (README + configs), on évite les timeouts HTTP.
            with self.client.messages.stream(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                **kwargs,
            ) as stream:
                # Chaque événement du flux est un point d'arrêt : « Arrêter » coupe la réponse en cours
                # (la sortie du `with` ferme la connexion) au lieu d'attendre la fin de la génération.
                for _ in stream:
                    if cancel.requested():
                        raise cancel.Cancelled()
                response = stream.get_final_message()
        except a.AuthenticationError as e:
            raise LLMError("Clé API Anthropic absente ou invalide (variable ANTHROPIC_API_KEY).") from e
        except a.RateLimitError as e:
            raise LLMError("Quota Anthropic atteint, réessayez plus tard.") from e
        except a.APIStatusError as e:
            raise LLMError(f"Erreur API Anthropic ({e.status_code}) : {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError("Impossible de joindre l'API Anthropic (réseau ?).") from e

        if response.stop_reason == "refusal":
            raise LLMError("Le modèle a refusé la requête (stop_reason=refusal).")
        if response.stop_reason == "max_tokens":
            raise LLMError("Réponse tronquée (max_tokens atteint) : augmentez max_tokens ou réduisez le contexte.")

        text = next((b.text for b in response.content if b.type == "text"), "")
        return LLMResult(
            data=extract_json(text, expected_keys),
            raw_text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )


# ---------------------------------------------------------------------------
# Backend opencode (CLI, modèles gratuits)
# ---------------------------------------------------------------------------

class OpencodeBackend:
    DEFAULT_MODEL = "opencode/big-pickle"

    def __init__(self, model: str | None = None, timeout: int = 600):
        self.exe = self._find_executable()
        if not self.exe:
            raise LLMError("La commande `opencode` est introuvable dans le PATH (npm install -g opencode-ai).")
        self.model = model or self.DEFAULT_MODEL
        self.timeout = timeout

    @staticmethod
    def _find_executable() -> str | None:
        """Cherche `opencode` dans l'ordre : variable OPENCODE_BIN, PATH, emplacements connus.

        Piège Windows : le Python du « Python install manager » tourne dans un conteneur MSIX
        qui ne voit pas `%APPDATA%\\npm` (où npm installe ses shims). D'où les emplacements
        de repli, dont `%LOCALAPPDATA%\\Programs\\opencode\\opencode.exe` (une copie du binaire
        autonome y suffit — voir README).
        """
        env = os.environ.get("OPENCODE_BIN")
        if env and os.path.isfile(env):
            return env
        for name in ("opencode", "opencode.cmd", "opencode.exe"):
            found = shutil.which(name)
            if found:
                return found
        candidates = []
        if os.environ.get("APPDATA"):
            candidates.append(os.path.join(os.environ["APPDATA"], "npm", "opencode.cmd"))
        if os.environ.get("LOCALAPPDATA"):
            candidates.append(os.path.join(os.environ["LOCALAPPDATA"], "Programs", "opencode", "opencode.exe"))
        candidates.append(os.path.expanduser("~/.opencode/bin/opencode"))
        candidates.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "opencode.exe"))
        for c in candidates:
            if os.path.isfile(c):
                return c
        return None

    # Agent opencode sur mesure, sans aucun outil : le CLI est un agent complet (lecture, grep, bash…),
    # or ici c'est NOTRE agent qui a déjà fait la perception. On veut un simple appel de modèle.
    # Ce fichier est écrit dans le dossier de travail temporaire, où opencode le lit automatiquement.
    AGENT_CONFIG = {
        "$schema": "https://opencode.ai/config.json",
        "agent": {
            "analyst": {
                "description": "Analyse uniquement à partir du prompt, sans aucun outil",
                "mode": "primary",
                "tools": {name: False for name in (
                    "write", "edit", "patch", "bash", "read", "glob", "grep", "list",
                    "webfetch", "todowrite", "todoread", "task", "skill",
                )},
            }
        },
    }

    # Agents essayés dans l'ordre. « analyst » (sans outils) est le plus propre, mais certains
    # environnements (Python conteneurisé sous Windows) le font échouer côté serveur opencode ;
    # « plan » (outils en lecture seule, inutiles dans un dossier vide) sert alors de repli.
    AGENTS = ("analyst", "plan")

    def complete(self, system: str, user: str, expected_keys=EXPECTED_KEYS) -> LLMResult:
        # Pas de canal « system » dans `opencode run` : on le place en tête du message.
        prompt = f"{system}\n\n---\n\n{user}"
        last_error: LLMError | None = None
        for agent in self.AGENTS:
            try:
                return self._run(agent, prompt, expected_keys)
            except LLMError as e:
                last_error = e
                if "aucun texte" not in str(e):
                    raise           # JSON invalide, timeout… : changer d'agent n'y changera rien
        assert last_error is not None
        raise last_error

    def _run(self, agent: str, prompt: str, expected_keys=EXPECTED_KEYS) -> LLMResult:
        cmd = [self.exe, "run", "--agent", agent, "--format", "json", "-m", self.model]

        # cwd = dossier temporaire vide : l'agent CLI ne doit ni lire ni modifier le projet analysé,
        # il ne travaille qu'à partir du prompt qu'on lui donne.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "opencode.json"), "w", encoding="utf-8") as fh:
                json.dump(self.AGENT_CONFIG, fh)
            # Popen enregistré : « Arrêter » dans l'interface tue l'appel en cours sans attendre sa fin.
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", errors="replace", cwd=tmp)
            cancel.register(p)
            try:
                out, err = p.communicate(input=prompt, timeout=self.timeout)
            except subprocess.TimeoutExpired as e:
                p.kill()
                p.communicate()
                raise LLMError(f"opencode n'a pas répondu en {self.timeout}s.") from e
            finally:
                cancel.unregister(p)
            cancel.check()
            proc = subprocess.CompletedProcess(cmd, p.returncode, out, err)

        if proc.returncode != 0 and not proc.stdout.strip():
            raise LLMError(f"opencode a échoué (code {proc.returncode}) :\n{proc.stderr[-1500:]}")

        texts: list[str] = []
        errors: list[str] = []
        in_tok = out_tok = None
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            part = event.get("part", {})
            if kind == "text":
                texts.append(part.get("text", ""))
            elif kind == "step_finish":
                tokens = part.get("tokens") or {}
                in_tok = (in_tok or 0) + int(tokens.get("input", 0))
                out_tok = (out_tok or 0) + int(tokens.get("output", 0))
            elif kind == "error":
                err = event.get("error") or {}
                errors.append(f"{err.get('name', 'Error')}: {(err.get('data') or {}).get('message', '')}")

        text = "\n".join(texts)
        if not text.strip():
            detail = "\n".join(errors) or proc.stderr[-1500:] or "(aucun détail)"
            raise LLMError(f"opencode n'a produit aucun texte.\n{detail}")
        return LLMResult(data=extract_json(text, expected_keys), raw_text=text, model=self.model,
                         input_tokens=in_tok, output_tokens=out_tok)


# ---------------------------------------------------------------------------
# Backends HTTP : OpenAI et compatibles, Gemini — bibliothèque standard uniquement
# ---------------------------------------------------------------------------

import urllib.error
import urllib.request

HTTP_TIMEOUT = 600


def _http_json(url: str, body: dict, headers: dict, label: str) -> dict:
    """POST JSON -> dict, avec des erreurs lisibles (401, 404 modèle, 429, réseau…)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "devops-agent/1.0", **headers})
    import threading

    # L'appel part dans un fil à part : la tâche, elle, vérifie toutes les demi-secondes si on lui a
    # demandé de s'arrêter. « Arrêter » abandonne alors l'appel ; sa réponse éventuelle est ignorée.
    box: dict = {}

    def work() -> None:
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                box["data"] = resp.read()
        except BaseException as e:  # noqa: BLE001 — relancée plus bas, dans le fil de la tâche
            box["err"] = e

    import time

    # Surcharge passagère (502, 503, 504, 529) : deux nouveaux essais espacés avant d'abandonner.
    for attempt in range(3):
        box.clear()
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        while worker.is_alive():
            worker.join(0.5)
            cancel.check()
        err = box.get("err")
        if isinstance(err, urllib.error.HTTPError) and err.code in (502, 503, 504, 529) and attempt < 2:
            for _ in range(20 * (attempt + 1)):          # 10 s puis 20 s, en restant interruptible
                time.sleep(0.5)
                cancel.check()
            continue
        break
    try:
        if "err" in box:
            raise box["err"]
        return json.loads(box["data"].decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
            err = payload.get("error")
            msg = err.get("message") if isinstance(err, dict) else err or payload.get("message")
        except Exception:  # noqa: BLE001
            msg = None
        hint = {401: "clé API absente ou invalide", 403: "clé refusée pour ce modèle ou cette ressource",
                404: "modèle ou URL introuvable", 429: "quota ou limite de débit atteint"}.get(e.code, "")
        raise LLMError(f"{label} : HTTP {e.code}" + (f" — {hint}" if hint else "") + (f" — {str(msg)[:300]}" if msg else "")) from e
    except urllib.error.URLError as e:
        raise LLMError(f"{label} : impossible de joindre {url} ({e.reason})") from e
    except TimeoutError as e:
        raise LLMError(f"{label} : délai dépassé ({HTTP_TIMEOUT}s)") from e


class OpenAICompatibleBackend:
    """OpenAI, Mistral, Ollama, OpenRouter, Groq, DeepSeek… : l'API /chat/completions."""

    def __init__(self, base_url: str, api_key: str, model: str, label: str = "OpenAI"):
        if not base_url:
            raise LLMError(f"{label} : URL de base manquante")
        self.base_url, self.api_key, self.model, self.label = base_url.rstrip("/"), api_key, model, label

    def complete(self, system: str, user: str, expected_keys=EXPECTED_KEYS) -> LLMResult:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        body = {"model": self.model, "temperature": 0.2,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"}}
        try:
            payload = _http_json(f"{self.base_url}/chat/completions", body, headers, self.label)
        except LLMError as e:
            # Certains serveurs compatibles ne connaissent pas response_format : on réessaie sans.
            if "HTTP 400" not in str(e):
                raise
            body.pop("response_format")
            payload = _http_json(f"{self.base_url}/chat/completions", body, headers, self.label)
        try:
            text = payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self.label} : réponse inattendue ({str(payload)[:200]})") from e
        usage = payload.get("usage") or {}
        return LLMResult(data=extract_json(text, expected_keys), raw_text=text, model=payload.get("model") or self.model,
                         input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"))


class GeminiBackend:
    """Google Gemini : l'API generateContent (clé dans l'en-tête x-goog-api-key)."""

    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise LLMError("Gemini : clé API manquante (GEMINI_API_KEY ou réglages)")
        self.api_key, self.model = api_key, model

    def complete(self, system: str, user: str, expected_keys=EXPECTED_KEYS) -> LLMResult:
        body = {"system_instruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}}
        payload = _http_json(f"{self.BASE}/models/{self.model}:generateContent", body,
                             {"x-goog-api-key": self.api_key}, "Gemini")
        try:
            parts = payload["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError) as e:
            reason = (payload.get("candidates") or [{}])[0].get("finishReason") or payload.get("promptFeedback")
            raise LLMError(f"Gemini : réponse vide ou bloquée ({reason})") from e
        usage = payload.get("usageMetadata") or {}
        return LLMResult(data=extract_json(text, expected_keys), raw_text=text, model=self.model,
                         input_tokens=usage.get("promptTokenCount"), output_tokens=usage.get("candidatesTokenCount"))


# ---------------------------------------------------------------------------
# Sélection
# ---------------------------------------------------------------------------

def make_backend(name: str | None, model: str | None, schema: dict | None = None):
    """Instancie le backend d'un fournisseur du registre (providers.py), avec sa clé et son modèle."""
    from . import providers as P
    name = name or P.default_provider()
    if name not in P.PROVIDERS:
        raise LLMError(f"Backend inconnu : {name} (attendu : {' | '.join(P.PROVIDERS)})")
    cfg = P.provider_config(name)
    label = P.PROVIDERS[name]["label"].split(" (")[0]
    if P.PROVIDERS[name]["needs_key"] and not cfg["api_key"]:
        raise LLMError(f"{label} : aucune clé API — saisissez-la dans « Fournisseurs d'IA » ou via {P.PROVIDERS[name]['env']}.")
    model = model or cfg["models"][0]
    kind = cfg["kind"]
    if kind == "opencode":
        return OpencodeBackend(model=model)
    if kind == "anthropic":
        return AnthropicBackend(model=model, schema=schema, api_key=cfg["api_key"] if cfg["source"] == "settings" else None)
    if kind == "openai":
        return OpenAICompatibleBackend(cfg["base_url"], cfg["api_key"], model, label=label)
    if kind == "gemini":
        return GeminiBackend(cfg["api_key"], model)
    raise LLMError(f"Type de fournisseur inconnu : {kind}")
