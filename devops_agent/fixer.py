"""Étape 2 — Remédiation : l'agent corrige les risques qu'il a trouvés.

Boucle agentique en plusieurs appels, parce qu'un seul appel ne suffit pas (les modèles coupent les
longues sorties, et un plan écrit avant le code donne de meilleurs fichiers) :

    1. PLAN      : quelles modifications, dans quels fichiers, pour quels risques (un appel)
    2. FICHIERS  : le contenu complet de chaque fichier, par lots de 3 (n appels)
    3. APPLIQUER : écriture dans une branche Git dédiée du dépôt (jamais sur la branche courante)
    4. VALIDER   : JSON / YAML / Dockerfile / `kubectl --dry-run` quand disponible
    5. RAPPORT   : REMEDIATION.md + patch.diff + changes.json dans out/<projet>/remediation/

Cible par défaut : Kubernetes + pipeline GitLab CI — l'« usine logicielle ».
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .explorer import explore
from .llm import LLMError, make_backend
from .source import resolve

ProgressCb = Callable[[str, str], None]

TARGETS = {
    "k8s-gitlab": "Kubernetes (manifests dans k8s/, assemblés par kustomize, observabilité dans k8s/monitoring/) + pipeline "
                  "GitLab CI complet (.gitlab-ci.yml) : lint → tests → secrets (gitleaks) → SAST (semgrep) → build des images "
                  "→ scan Trivy (bloquant sur CRITICAL) → SBOM (syft) → push vers le registre GitLab → déploiement kubectl -k "
                  "(branche principale, seulement si KUBE_CONFIG est défini) → test de fumée",
    "k8s-github": "Kubernetes (manifests dans k8s/, assemblés par kustomize, observabilité dans k8s/monitoring/) + GitHub Actions "
                  "complet (.github/workflows/ci.yml) : lint → tests → secrets (gitleaks) → SAST (semgrep) → build des images "
                  "→ scan Trivy (bloquant sur CRITICAL) → SBOM (syft) → push vers GHCR → déploiement kubectl -k "
                  "(branche principale, seulement si le secret KUBE_CONFIG est défini) → test de fumée",
    "compose": "Docker Compose durci (secrets externalisés, healthchecks, limites de ressources, images de production), sans Kubernetes",
}

MAX_CHANGES = 25
FILES_PER_BATCH = 3
MAX_FILE_CHARS = 30_000

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

FIX_SYSTEM = """Tu es un ingénieur DevOps senior. Tu as audité un dépôt et listé ses risques. On te demande
maintenant de LES CORRIGER en modifiant le dépôt, avec un objectif : le rendre déployable dans une usine
logicielle (CI/CD) sur la cible indiquée.

Règles, non négociables :
1. Tu ne modifies que la configuration, l'infrastructure et le déploiement (Dockerfile, compose, CI, manifests
   Kubernetes, .dockerignore, .gitignore, fichiers d'exemple d'environnement, documentation d'exploitation).
   Tu ne réécris pas le code applicatif ; si un risque l'exige, tu le signales comme non corrigé avec la raison.
2. Chaque fichier que tu produis est COMPLET et directement utilisable : pas de « … », pas de « à compléter »
   au milieu d'un fichier. Les valeurs que tu ne peux pas connaître (domaine, registry, noms de secrets) sont
   des variables clairement nommées (${VARIABLE} ou une valeur d'exemple explicitement marquée « CHANGE_ME »).
3. Aucun secret en clair. Les secrets vont dans des variables de CI, des Secrets Kubernetes fournis à part,
   ou un fichier .env non versionné dont tu fournis le .env.example.
4. Kubernetes, quand c'est la cible : un namespace, un Deployment + un Service par composant, probes de
   liveness/readiness, requests/limits de ressources, securityContext non-root, ConfigMap pour la config,
   Secret en gabarit, Ingress pour l'entrée, et un fichier k8s/kustomization.yaml qui les assemble. Chaque
   composant porte le label app.kubernetes.io/name. Un conteneur non-root qui écrit sur disque reçoit des
   volumes emptyDir pour ces dossiers (nginx : /var/cache/nginx, /var/run, /tmp). Tout nom référencé
   (ConfigMap, Secret, Service, générateur kustomize) correspond EXACTEMENT à un objet défini. Les sondes
   visent une route de santé exclue de toute limitation de débit (rate limit) ; si l'appli limite toutes
   ses routes, utilise une sonde tcpSocket sur le port du conteneur.
5. Observabilité, quand Kubernetes est la cible : dans k8s/monitoring/, avec son propre kustomization.yaml
   (NON référencé par k8s/kustomization.yaml, car il exige Prometheus Operator) : une PrometheusRule (pods qui
   redémarrent, déploiement indisponible, composant injoignable) et un ServiceMonitor pour chaque composant qui
   expose réellement des métriques. N'invente pas d'endpoint /metrics absent du code.
6. Pipeline CI, quand c'est la cible : lint → tests → détection de secrets (gitleaks) → SAST (semgrep) → build
   des images (tag = SHA du commit, jamais « latest » seul) → scan des images (Trivy, bloquant sur CRITICAL) →
   SBOM (syft) → push vers le registre de la plateforme → déploiement `kubectl apply -k k8s/` sur la branche
   principale uniquement, et seulement si le secret KUBE_CONFIG est défini (sinon l'étape est ignorée proprement)
   → test de fumée. Images d'outils officielles et épinglées ; variables et secrets attendus documentés en
   commentaire en tête du fichier. Le fichier doit être valide tel quel pour la plateforme (GitLab ou GitHub).
7. Dockerfiles : multi-étapes s'il y a une compilation, utilisateur non-root, et chaque fichier copié (COPY/ADD)
   existe réellement dans le dépôt — sans lockfile présent, pas de `npm ci` mais `npm install`.
8. Tu restes cohérent avec ce que le dépôt contient réellement (noms de services, ports, chemins) : tu les
   reprends des extraits fournis, tu n'en inventes pas.
9. Tu réponds en français et UNIQUEMENT dans le format JSON demandé."""

PLAN_INSTRUCTIONS = """## Ta tâche maintenant : le PLAN
Ne produis PAS encore le contenu des fichiers. Liste les modifications à faire, une entrée par fichier,
avec pour chacune : le chemin relatif, l'action (create | modify | delete), à quoi elle sert, et les numéros
des risques qu'elle corrige. {max_changes} entrées maximum. Indique aussi les risques que tu ne corrigeras
pas (et pourquoi) et les étapes manuelles qui resteront à l'équipe (créer un secret, pointer un DNS…).

Réponds UNIQUEMENT avec cet objet JSON :
{{
 "branch": "nom-de-branche-git",
 "summary": "ce que fait la remédiation, en 2-3 phrases",
 "changes": [
  {{"path": "k8s/namespace.yaml", "action": "create", "purpose": "...", "risk_refs": [1, 4]}}
 ],
 "not_fixed": [{{"risk_ref": 3, "why": "..."}}],
 "manual_steps": ["..."]
}}"""

FILES_INSTRUCTIONS = """## Ta tâche maintenant : le CONTENU de {n} fichier(s) du plan
Produis le contenu COMPLET des fichiers listés ci-dessous, conformément au plan et aux règles.
Pour une action « modify », le contenu actuel du fichier est fourni : renvoie le fichier entier modifié,
pas seulement les lignes changées.

Fichiers à produire dans cette réponse :
{files}

Réponds UNIQUEMENT avec cet objet JSON (le contenu est une chaîne JSON, les retours à la ligne sont \\n) :
{{"files": [{{"path": "chemin/relatif", "content": "contenu complet du fichier"}}]}}"""


# ---------------------------------------------------------------------------
# Résultat
# ---------------------------------------------------------------------------

@dataclass
class Change:
    path: str
    action: str
    purpose: str
    risk_refs: list[int]
    content: str | None = None
    status: str = "planned"      # planned | written | deleted | skipped | failed
    note: str = ""


@dataclass
class RemediationResult:
    name: str
    target_kind: str
    branch: str | None
    base_commit: str | None
    commit: str | None
    summary: str
    changes: list[Change]
    not_fixed: list[dict]
    manual_steps: list[str]
    validations: list[dict]
    diff: str
    out_dir: Path
    files: dict[str, Path]
    repo_dir: Path
    applied_in_repo: bool
    model: str
    elapsed: float
    calls: int
    truncated: bool = False


class RemediationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

_SAFE_PATH = re.compile(r"^[A-Za-z0-9._/\-]+$")


def _safe_rel_path(path: str) -> str | None:
    """Chemin relatif sûr (pas d'absolu, pas de « .. », pas de .git/), ou None."""
    p = path.strip().replace("\\", "/")
    while p.startswith("./"):          # « ./k8s/x.yaml » -> « k8s/x.yaml », sans toucher au point de « .gitlab-ci.yml »
        p = p[2:]
    p = p.lstrip("/")
    if not p or not _SAFE_PATH.match(p) or ".." in p.split("/") or p.startswith(".git/") or p == ".git":
        return None
    return p


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", "-c", "user.name=devops-agent", "-c", "user.email=devops-agent@local", *args],
        cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and proc.returncode != 0:
        raise RemediationError(f"git {' '.join(args[:2])} a échoué : {proc.stderr.strip()[-400:]}")
    return proc


def _is_git_repo(path: Path) -> bool:
    return shutil.which("git") is not None and (path / ".git").exists()


def _base_branch(repo: Path) -> str | None:
    """La branche de départ : celle par défaut du dépôt d'origine (origin/HEAD), sinon la branche courante
    — sauf si celle-ci est déjà une branche de remédiation d'un passage précédent."""
    proc = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if proc.returncode == 0 and proc.stdout.strip().startswith("origin/"):
        return proc.stdout.strip()[len("origin/"):]
    current = _git(repo, "branch", "--show-current", check=False).stdout.strip()
    if current and not current.startswith(("remediation/", "fix/")):
        return current
    for candidate in ("main", "master", "dev"):
        if _git(repo, "rev-parse", "--verify", "-q", candidate, check=False).returncode == 0:
            return candidate
    return None


def _risks_block(risks: list[dict], selected: list[int]) -> str:
    lines = []
    for i, r in enumerate(risks, 1):
        if i not in selected:
            continue
        lines.append(f"### Risque {i} — [{r.get('severity', '?')}] {r.get('title', '')} ({r.get('category', '')})\n"
                     f"{r.get('description', '')}\nPreuve : {r.get('evidence', '')}\nRecommandation : {r.get('recommendation', '')}")
    return "\n\n".join(lines)


def _context_block(analysis: dict, ctx) -> str:
    a = analysis.get("architecture", {})
    comps = "\n".join(f"- {c.get('name')} : {c.get('role')} ({c.get('technology')})" for c in a.get("components", []))
    stores = "\n".join(f"- {s.get('name')} ({s.get('technology')})" for s in a.get("data_stores", []))
    files = "\n".join(f"\n### {f.path}\n```\n{f.content.rstrip()}\n```" for f in ctx.key_files)
    return (f"## Le dépôt : {analysis.get('project', {}).get('name', ctx.name)}\n"
            f"{analysis.get('project', {}).get('purpose', '')}\n\n"
            f"### Composants identifiés\n{comps or '(aucun)'}\n\n### Données\n{stores or '(aucune)'}\n\n"
            f"### Arborescence\n```\n{ctx.tree}\n```\n\n## Fichiers de configuration actuels\n{files or '(aucun)'}")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate_file(path: Path, rel: str) -> dict:
    """Vérification locale, sans dépendance obligatoire. Retourne {path, check, ok, detail}."""
    suffix = path.suffix.lower()
    name = path.name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        return {"path": rel, "check": "lecture", "ok": False, "detail": str(e)}
    if suffix == ".json":
        try:
            json.loads(text)
            return {"path": rel, "check": "JSON", "ok": True, "detail": "syntaxe valide"}
        except json.JSONDecodeError as e:
            return {"path": rel, "check": "JSON", "ok": False, "detail": str(e)}
    if suffix in (".yml", ".yaml"):
        if "\t" in text:
            return {"path": rel, "check": "YAML", "ok": False, "detail": "tabulations interdites en YAML"}
        try:
            import yaml  # optionnel
            list(yaml.safe_load_all(text))
            return {"path": rel, "check": "YAML", "ok": True, "detail": "syntaxe valide (PyYAML)"}
        except ImportError:
            return {"path": rel, "check": "YAML", "ok": True, "detail": "pas de tabulation (PyYAML absent : syntaxe non vérifiée)"}
        except Exception as e:  # noqa: BLE001
            return {"path": rel, "check": "YAML", "ok": False, "detail": str(e).splitlines()[0][:200]}
    if name.startswith("Dockerfile"):
        first = next((l for l in text.splitlines() if l.strip() and not l.strip().startswith("#")), "")
        if not first.upper().startswith(("FROM", "ARG")):
            return {"path": rel, "check": "Dockerfile", "ok": False, "detail": f"première instruction inattendue : {first[:60]}"}
        # Chaque fichier copié doit exister : à la racine du dépôt ou à côté du Dockerfile (contextes usuels).
        root = path
        for _ in Path(rel).parts:
            root = root.parent
        contexts = [root, path.parent]
        missing = []
        for line in re.sub(r"\\\s*\n", " ", text).splitlines():
            m = re.match(r"^\s*(?:COPY|ADD)\s+((?:--\S+\s+)*)(.+)$", line, re.I)
            if not m or "--from" in m.group(1):
                continue
            try:
                parts = shlex.split(m.group(2))
            except ValueError:
                continue
            for s in parts[:-1]:
                if any(ch in s for ch in "*?[$") or s.startswith(("http://", "https://")):
                    continue
                if not any((c / s.lstrip("/")).exists() for c in contexts):
                    missing.append(s)
        if missing:
            return {"path": rel, "check": "Dockerfile", "ok": False,
                    "detail": "COPY/ADD de fichier(s) absent(s) du dépôt : " + ", ".join(dict.fromkeys(missing))
                              + " — retirez-les ou utilisez un motif (ex. package*.json) ; sans lockfile, npm install au lieu de npm ci"}
        return {"path": rel, "check": "Dockerfile", "ok": True, "detail": "FROM en tête, fichiers copiés présents"}
    return {"path": rel, "check": "écriture", "ok": True, "detail": f"{len(text):,} caractères"}


_OFFLINE_MARKERS = ("failed to download openapi", "could not find the requested resource", "connection refused",
                    "unable to connect", "no such host", "did you specify the right host", "actively refused")


def _kubectl_checks(repo: Path, changes: list[Change]) -> list[dict]:
    """Vérifie les manifests Kubernetes avec kubectl, sans exiger de cluster.

    - dossier avec kustomization.yaml : `kubectl kustomize` (rendu pur, hors ligne) — attrape les
      références cassées, les YAML invalides, les doublons ;
    - dossier de manifests simples : `kubectl apply --dry-run=client --validate=false` ; si aucun cluster
      n'est joignable, le résultat est « non vérifiable » (ok=None), pas un échec.
    Les sous-dossiers couverts par une kustomization parente ne sont pas revérifiés séparément.
    """
    if not shutil.which("kubectl"):
        return []
    dirs = sorted({Path(c.path).parent.as_posix() for c in changes if c.status == "written"
                   and c.path.lower().endswith((".yaml", ".yml"))
                   and any(k in c.path.lower() for k in ("k8s", "kube", "manifests", "deploy/"))})
    kustom = [d for d in dirs if (repo / d / "kustomization.yaml").is_file()]
    plain = [d for d in dirs if d not in kustom and not any(d.startswith(k + "/") for k in kustom)]
    out: list[dict] = []
    for d in kustom:
        proc = subprocess.run(["kubectl", "kustomize", str(repo / d)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=120)
        errs = "\n".join(l for l in proc.stderr.splitlines() if not l.lstrip("# ").lower().startswith("warning"))
        out.append({"path": d, "check": "kubectl kustomize", "ok": proc.returncode == 0,
                    "detail": ("rendu OK" if proc.returncode == 0 else errs.strip()[-500:] or proc.stdout[-300:])
                              + ("" if not proc.stderr.strip() or proc.returncode else " · " + proc.stderr.strip().splitlines()[0][:160])})
    for d in plain:
        proc = subprocess.run(["kubectl", "apply", "--dry-run=client", "--validate=false", "-f", str(repo / d)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        text = (proc.stdout + proc.stderr).strip()
        if proc.returncode != 0 and any(m in text.lower() for m in _OFFLINE_MARKERS):
            out.append({"path": d, "check": "kubectl --dry-run", "ok": None, "detail": "aucun cluster joignable : non vérifié"})
        else:
            out.append({"path": d, "check": "kubectl --dry-run", "ok": proc.returncode == 0, "detail": text[-500:]})
    return out


REPAIR_INSTRUCTIONS = """## Ta tâche maintenant : CORRIGER un fichier qui échoue à la validation
Le fichier ci-dessous a été validé par un outil et l'outil a renvoyé une erreur. Corrige le fichier
pour que la validation passe, sans changer son intention. Renvoie le fichier ENTIER corrigé.

Fichier : {path}
Outil : {check}
Erreur :
```
{error}
```
Contenu actuel :
```
{content}
```

Réponds UNIQUEMENT avec cet objet JSON :
{{"files": [{{"path": "{path}", "content": "contenu complet corrigé"}}]}}"""

MAX_REPAIRS = 3


def _repair_validations(repo: Path, changes: list[Change], validations: list[dict], llm, say) -> tuple[list[dict], int]:
    """Une passe de réparation : chaque validation en échec est renvoyée au modèle avec l'erreur.

    Retourne (validations mises à jour, nombre d'appels). Au plus MAX_REPAIRS fichiers.
    """
    by_path = {c.path: c for c in changes}
    calls = 0
    for v in [v for v in validations if v["ok"] is False][:MAX_REPAIRS]:
        # Une erreur kustomize pointe le dossier : le fichier fautif est sa kustomization.
        path = v["path"] if v["check"] != "kubectl kustomize" else f"{v['path']}/kustomization.yaml"
        change = by_path.get(path)
        target = repo / path
        if not target.is_file():
            continue
        say("validate", f"réparation de {path} ({v['check']}) …")
        try:
            content = target.read_text(encoding="utf-8")[:MAX_FILE_CHARS]
            res = llm.complete(FIX_SYSTEM, REPAIR_INSTRUCTIONS.format(path=path, check=v["check"], error=v["detail"][:1500], content=content),
                               expected_keys=("files",))
        except (LLMError, OSError) as e:
            say("validate", f"réparation impossible pour {path} : {str(e).splitlines()[0][:120]}")
            continue
        calls += 1
        new = next((f.get("content") for f in res.data.get("files") or [] if _safe_rel_path(str(f.get("path", ""))) == path), None)
        if not isinstance(new, str) or not new.strip():
            continue
        target.write_text(new if new.endswith("\n") else new + "\n", encoding="utf-8")
        if change:
            change.note = (change.note + " · " if change.note else "") + "réparé après validation"
        # Re-validation du fichier, puis de la kustomization si c'était elle.
        fresh = _validate_file(target, path) if v["check"] != "kubectl kustomize" else \
            next((k for k in _kubectl_checks(repo, [c for c in changes if c.path.startswith(v["path"] + "/") or c.path == path]) if k["check"] == "kubectl kustomize"), None)
        if fresh:
            v.update(fresh)
            v["detail"] = ("réparé — " if fresh["ok"] else "toujours en échec après réparation — ") + fresh["detail"]
    return validations, calls


# ---------------------------------------------------------------------------
# La boucle
# ---------------------------------------------------------------------------

def run_remediation(
    name: str,
    *,
    out_root: Path = Path("out"),
    risk_refs: list[int] | None = None,
    target_kind: str = "k8s-gitlab",
    backend: str | None = None,
    model: str | None = None,
    depth: int = 4,
    budget: int = 60_000,
    progress: ProgressCb | None = None,
    snapshot: Callable[[dict], None] | None = None,
) -> RemediationResult:
    def say(step: str, msg: str) -> None:
        if progress:
            progress(step, msg)

    live: dict = {"stage": "plan", "target_kind": target_kind, "branch": None, "summary": "", "changes": [],
                  "validations": [], "commit": None}

    def snap(**kw) -> None:
        """Instantané pour le HUD : l'usine logicielle qui se construit, fichier par fichier."""
        if snapshot:
            live.update(kw)
            live["changes"] = [{"path": c.path, "action": c.action, "status": c.status, "purpose": c.purpose,
                                "risk_refs": c.risk_refs, "note": c.note} for c in live.get("_changes", [])]
            snapshot({k: v for k, v in live.items() if k != "_changes"})

    t_start = time.time()
    out_dir = out_root / name
    analysis_file, meta_file = out_dir / "analysis.json", out_dir / "meta.json"
    if not analysis_file.is_file():
        raise RemediationError(f"Aucune analyse trouvée pour « {name} » : lancez d'abord l'analyse.")
    analysis = json.loads(analysis_file.read_text(encoding="utf-8"))
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
    target = meta.get("target")
    if not target:
        raise RemediationError("L'analyse ne mémorise pas sa source (analyse trop ancienne) : relancez l'analyse avant de corriger.")
    if target_kind not in TARGETS:
        raise RemediationError(f"Cible inconnue : {target_kind}")

    risks = analysis.get("risks", [])
    selected = sorted(set(risk_refs or range(1, len(risks) + 1)))
    selected = [i for i in selected if 1 <= i <= len(risks)]
    if not selected:
        raise RemediationError("Aucun risque sélectionné.")

    # 0. Le dépôt --------------------------------------------------------------
    try:
        repo = resolve(target, refresh=False, log=lambda m: say("plan", m.strip())).resolve()
    except RuntimeError as e:
        raise RemediationError(str(e)) from e
    # Un dépôt Git est remis sur sa branche de base AVANT l'exploration : sinon les fichiers d'une
    # remédiation précédente seraient lus comme s'ils faisaient partie du projet.
    applied_in_repo = _is_git_repo(repo)
    base = None
    if applied_in_repo:
        _git(repo, "checkout", "-q", "--", ".", check=False)
        _git(repo, "clean", "-qfd", check=False)
        base = _base_branch(repo)
        if base:
            _git(repo, "checkout", "-q", base)
    ctx = explore(repo, max_depth=depth, total_budget=budget)
    say("plan", f"dépôt : {repo} · {len(ctx.key_files)} fichiers de configuration en contexte · {len(selected)} risque(s) à corriger")

    try:
        llm = make_backend(backend, model)
    except LLMError as e:
        raise RemediationError(str(e)) from e
    calls = 0
    truncated = False

    # 1. PLAN ------------------------------------------------------------------
    base_user = (
        f"## Cible de déploiement\n{TARGETS[target_kind]}\n\n"
        f"{_context_block(analysis, ctx)}\n\n## Risques à corriger\n{_risks_block(risks, selected)}\n\n"
    )
    say("plan", f"appel du modèle {llm.model} pour le plan …")
    try:
        res = llm.complete(FIX_SYSTEM, base_user + PLAN_INSTRUCTIONS.format(max_changes=MAX_CHANGES), expected_keys=("changes",))
    except LLMError as e:
        raise RemediationError(f"Plan impossible : {e}") from e
    calls += 1
    plan = res.data
    truncated |= bool(plan.pop("_truncated", False))
    raw_changes = plan.get("changes") or []
    changes: list[Change] = []
    for c in raw_changes[:MAX_CHANGES]:
        rel = _safe_rel_path(str(c.get("path", "")))
        action = str(c.get("action", "create")).lower()
        if not rel or action not in ("create", "modify", "delete"):
            continue
        refs = [int(x) for x in c.get("risk_refs", []) if str(x).isdigit()]
        changes.append(Change(path=rel, action=action, purpose=str(c.get("purpose", "")), risk_refs=refs))
    if not changes:
        raise RemediationError("Le plan ne contient aucune modification exploitable.")
    branch = re.sub(r"[^A-Za-z0-9._/\-]+", "-", str(plan.get("branch") or "fix/devops-agent")).strip("-/") or "fix/devops-agent"
    say("plan", f"plan : {len(changes)} modification(s) — " + ", ".join(f"{c.action} {c.path}" for c in changes[:8]) + (" …" if len(changes) > 8 else ""))
    snap(stage="plan", branch=branch, summary=str(plan.get("summary", "")), _changes=changes)

    # 2. FICHIERS, par lots ------------------------------------------------------
    to_write = [c for c in changes if c.action != "delete"]
    plan_text = "\n".join(f"- {c.action} {c.path} — {c.purpose} (risques {', '.join(map(str, c.risk_refs)) or '—'})" for c in changes)
    for start in range(0, len(to_write), FILES_PER_BATCH):
        batch = to_write[start:start + FILES_PER_BATCH]
        say("files", f"fichiers {start + 1}–{start + len(batch)} / {len(to_write)} : " + ", ".join(c.path for c in batch))
        for c in batch:
            if c.status == "planned":
                c.status = "generating"
        snap(stage="files")
        descr = []
        for c in batch:
            entry = f"- {c.path} ({c.action}) — {c.purpose}"
            current = repo / c.path
            if c.action == "modify" and current.is_file():
                try:
                    cur = current.read_text(encoding="utf-8", errors="replace")[:MAX_FILE_CHARS]
                    entry += f"\n  Contenu actuel :\n```\n{cur}\n```"
                except OSError:
                    pass
            descr.append(entry)
        user = base_user + f"## Le plan retenu\n{plan_text}\n\n" + FILES_INSTRUCTIONS.format(n=len(batch), files="\n".join(descr))
        try:
            res = llm.complete(FIX_SYSTEM, user, expected_keys=("files",))
        except LLMError as e:
            for c in batch:
                c.status, c.note = "failed", f"le modèle n'a pas produit ce fichier : {str(e).splitlines()[0][:160]}"
            continue
        calls += 1
        truncated |= bool(res.data.pop("_truncated", False))
        produced = {}
        for f in res.data.get("files") or []:
            rel = _safe_rel_path(str(f.get("path", "")))
            if rel and isinstance(f.get("content"), str):
                produced[rel] = f["content"]
        for c in batch:
            if c.path in produced and produced[c.path].strip():
                c.content = produced[c.path]
                c.status = "generated"
            else:
                c.status, c.note = "failed", "absent de la réponse du modèle"
        snap(stage="files")

    # 3. APPLIQUER --------------------------------------------------------------
    base_commit = commit = None
    if applied_in_repo:
        base_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "checkout", "-q", "-B", branch)
        say("apply", f"branche {branch} créée depuis {base or 'HEAD'} ({base_commit[:8]})")
        write_root = repo
    else:
        write_root = out_dir / "remediation" / "tree"
        say("apply", f"la cible n'est pas un dépôt Git : fichiers écrits dans {write_root}")

    for c in changes:
        dest = write_root / c.path
        try:
            if c.action == "delete":
                if (repo / c.path).exists() and applied_in_repo:
                    dest.unlink()
                    c.status = "deleted"
                else:
                    c.status, c.note = "skipped", "fichier absent"
            elif c.content is not None:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(c.content if c.content.endswith("\n") else c.content + "\n", encoding="utf-8")
                c.status = "written"
        except OSError as e:
            c.status, c.note = "failed", str(e)

    snap(stage="apply")

    # 4. VALIDER ----------------------------------------------------------------
    validations = [_validate_file(write_root / c.path, c.path) for c in changes if c.status == "written"]
    kube = _kubectl_checks(write_root, changes)
    validations.extend(kube)
    n_ok = sum(1 for v in validations if v["ok"] is True)
    n_ko = sum(1 for v in validations if v["ok"] is False)
    say("validate", f"{n_ok} vérification(s) passée(s), {n_ko} en échec, {len(validations) - n_ok - n_ko} non vérifiable(s)"
                    + ("" if shutil.which("kubectl") else " · kubectl absent : manifests non vérifiés"))
    if n_ko:
        validations, repair_calls = _repair_validations(write_root, changes, validations, llm, say)
        calls += repair_calls
        n_ko_after = sum(1 for v in validations if v["ok"] is False)
        say("validate", f"après réparation : {n_ko - n_ko_after} corrigé(s), {n_ko_after} restant(s)")
    snap(stage="validate", validations=validations)

    diff = ""
    if applied_in_repo:
        _git(repo, "add", "-A")
        msg = f"DevOps agent : remédiation ({len([c for c in changes if c.status in ('written', 'deleted')])} fichiers)\n\n{plan.get('summary', '')}"
        proc = _git(repo, "commit", "-q", "-m", msg, check=False)
        if proc.returncode == 0:
            commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
            diff = _git(repo, "diff", f"{base_commit}..{commit}", "--stat", check=False).stdout
            diff += "\n" + _git(repo, "diff", f"{base_commit}..{commit}", check=False).stdout
            say("apply", f"commit {commit[:8]} sur {branch}")
        else:
            say("apply", "rien à committer : aucun fichier écrit")

    # 5. RAPPORT ----------------------------------------------------------------
    rem_dir = out_dir / "remediation"
    rem_dir.mkdir(parents=True, exist_ok=True)
    summary = str(plan.get("summary", ""))
    not_fixed = [nf for nf in (plan.get("not_fixed") or []) if isinstance(nf, dict)]
    manual = [str(m) for m in (plan.get("manual_steps") or [])]
    files: dict[str, Path] = {}
    (rem_dir / "changes.json").write_text(json.dumps({
        "branch": branch, "base_commit": base_commit, "commit": commit, "summary": summary,
        "changes": [c.__dict__ | {"content": None} for c in changes], "not_fixed": not_fixed,
        "manual_steps": manual, "validations": validations,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    files["changes.json"] = rem_dir / "changes.json"
    if diff.strip():
        (rem_dir / "patch.diff").write_text(diff, encoding="utf-8")
        files["patch.diff"] = rem_dir / "patch.diff"
    (rem_dir / "REMEDIATION.md").write_text(_render_md(name, risks, selected, branch, base_commit, commit, summary, changes,
                                                       not_fixed, manual, validations, repo, applied_in_repo, llm.model), encoding="utf-8")
    files["REMEDIATION.md"] = rem_dir / "REMEDIATION.md"
    say("report", f"rapport de remédiation écrit dans {rem_dir}")
    snap(stage="done", commit=commit)

    return RemediationResult(
        name=name, target_kind=target_kind, branch=branch if applied_in_repo else None, base_commit=base_commit, commit=commit,
        summary=summary, changes=changes, not_fixed=not_fixed, manual_steps=manual, validations=validations,
        diff=diff[:200_000], out_dir=rem_dir, files=files, repo_dir=repo, applied_in_repo=applied_in_repo,
        model=llm.model, elapsed=time.time() - t_start, calls=calls, truncated=truncated,
    )


def _render_md(name, risks, selected, branch, base, commit, summary, changes, not_fixed, manual, validations, repo, in_repo, model) -> str:
    md = [f"# Remédiation — {name}\n", f"_Générée le {datetime.now():%d/%m/%Y %H:%M} par `{model}`._\n", summary, ""]
    if in_repo:
        md += ["## Où sont les modifications\n", f"Branche `{branch}` du dépôt `{repo}`" + (f", commit `{commit[:8]}` (base `{base[:8]}`)." if commit else " (aucun commit)."),
               "", "```bash", f"cd \"{repo}\"", f"git log --oneline {base[:8] if base else ''}..{branch}", f"git push -u origin {branch}   # puis ouvrir une merge request", "```", ""]
    md.append("## Modifications\n")
    md.append("| Fichier | Action | Statut | Risques | Objet |\n|---|---|---|---|---|")
    for c in changes:
        md.append(f"| `{c.path}` | {c.action} | {c.status}{(' — ' + c.note) if c.note else ''} | {', '.join(map(str, c.risk_refs)) or '—'} | {c.purpose} |")
    md.append("\n## Risques traités\n")
    for i in selected:
        r = risks[i - 1]
        touched = [c.path for c in changes if i in c.risk_refs and c.status in ("written", "deleted")]
        md.append(f"- **{i}. {r.get('title')}** — " + (", ".join(f"`{p}`" for p in touched) if touched else "_aucun fichier écrit_"))
    if not_fixed:
        md.append("\n## Non corrigés\n")
        md += [f"- Risque {nf.get('risk_ref')} : {nf.get('why')}" for nf in not_fixed]
    if manual:
        md.append("\n## Étapes manuelles restantes\n")
        md += [f"1. {m}" for m in manual]
    md.append("\n## Validations\n")
    md += [f"- {'✅' if v['ok'] is True else '⚠️' if v['ok'] is None else '❌'} `{v['path']}` — {v['check']} : {v['detail']}" for v in validations]
    md.append("\n> Relisez chaque fichier avant de le fusionner : l'agent propose, l'équipe décide.")
    return "\n".join(md) + "\n"
