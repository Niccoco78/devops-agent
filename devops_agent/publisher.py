"""Étape 4 : publier l'usine logicielle sur GitHub OU GitLab.

    publish(name, platform="github"|"gitlab", repo_name="quiz-app", private=True)

1. identifie le compte du jeton (GET /user)
2. crée le dépôt s'il n'existe pas (ou réutilise celui du même nom)
3. pousse la branche de base puis la branche corrigée
4. ouvre la pull request (GitHub) ou la merge request (GitLab) : c'est elle qui déclenche le pipeline

Rien ne part sans un clic explicite dans l'interface. Les jetons restent sur le serveur
(data/settings.json, section « git ») ; ils passent à git par l'environnement du processus,
jamais dans une URL ni dans la ligne de commande.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

from .providers import _load, _save, mask

ProgressCb = Callable[[str, str], None]
_lock = threading.Lock()

FORGES: dict[str, dict] = {
    "github": {"label": "GitHub", "env": "GITHUB_TOKEN", "url": "https://github.com", "api": "https://api.github.com",
               "key_hint": "ghp_… ou github_pat_…",
               "scopes": "jeton classique : repo + workflow · jeton fin : Administration, Contents, Pull requests, Workflows en écriture"},
    "gitlab": {"label": "GitLab", "env": "GITLAB_TOKEN", "url": "https://gitlab.com", "api": None, "key_hint": "glpat-…",
               "scopes": "portée api (création du projet, push, merge request)", "editable_url": True},
}


class PublishError(RuntimeError):
    pass


# --------------------------------------------------------------------------- réglages
def forge_config(fid: str) -> dict:
    base = FORGES[fid]
    saved = _load().get("git", {}).get(fid, {})
    token = saved.get("token") or os.environ.get(base["env"]) or ""
    url = (saved.get("url") or os.environ.get(f"{fid.upper()}_URL") or base["url"]).rstrip("/")
    api = base["api"] or f"{url}/api/v4"
    return {"id": fid, "token": token, "url": url, "api": api, "configured": bool(token),
            "source": "settings" if saved.get("token") else ("env" if token else "")}


def describe_forges() -> list[dict]:
    """Ce que l'interface a le droit de voir : jamais le jeton en clair."""
    out = []
    for fid, base in FORGES.items():
        cfg = forge_config(fid)
        out.append({"id": fid, "label": base["label"], "configured": cfg["configured"], "source": cfg["source"],
                     "token_masked": mask(cfg["token"]), "url": cfg["url"], "env": base["env"],
                     "key_hint": base["key_hint"], "scopes": base["scopes"], "editable_url": base.get("editable_url", False)})
    return out


def update_forge(fid: str, *, token: str | None = None, url: str | None = None, clear: bool = False) -> None:
    if fid not in FORGES:
        raise KeyError(fid)
    with _lock:
        settings = _load()
        git = settings.setdefault("git", {})
        if clear:
            git.pop(fid, None)
        else:
            entry = git.setdefault(fid, {})
            if token is not None:
                if token.strip():
                    entry["token"] = token.strip()
                else:
                    entry.pop("token", None)
            if url is not None:
                if url.strip():
                    entry["url"] = url.strip().rstrip("/")
                else:
                    entry.pop("url", None)
            if not entry:
                git.pop(fid, None)
        _save(settings)


# --------------------------------------------------------------------------- API REST
def _api(cfg: dict, method: str, path: str, body: dict | None = None) -> tuple[int, dict | list]:
    url = path if path.startswith("http") else cfg["api"] + path
    headers = {"Accept": "application/json", "User-Agent": "devops-agent", "Content-Type": "application/json"}
    if cfg["id"] == "github":
        headers.update({"Authorization": f"Bearer {cfg['token']}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"})
    else:
        headers["PRIVATE-TOKEN"] = cfg["token"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(txt) if txt.strip() else {})
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(txt)
        except json.JSONDecodeError:
            payload = {"message": txt[:300]}
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise PublishError(f"{FORGES[cfg['id']]['label']} injoignable : {e}") from e


def _msg(payload) -> str:
    if isinstance(payload, dict):
        m = payload.get("message") or payload.get("error") or payload
        if payload.get("errors"):
            m = f"{m} — {payload['errors']}"
        return str(m)[:400]
    return str(payload)[:400]


def whoami(fid: str) -> dict:
    cfg = forge_config(fid)
    if not cfg["token"]:
        raise PublishError(f"Aucun jeton {FORGES[fid]['label']} : ajoutez-le dans les réglages (⚙).")
    code, me = _api(cfg, "GET", "/user")
    if code == 401:
        raise PublishError(f"Jeton {FORGES[fid]['label']} refusé (401) : expiré ou invalide.")
    if code != 200 or not isinstance(me, dict):
        raise PublishError(f"{FORGES[fid]['label']} : {_msg(me)}")
    return {"login": me.get("login") or me.get("username"), "name": me.get("name"),
            "url": me.get("html_url") or me.get("web_url")}


# --------------------------------------------------------------------------- git
def _git(repo: Path, *args: str, env: dict | None = None, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env)


def _auth_env(cfg: dict) -> dict:
    """Le jeton passe par GIT_CONFIG_* (environnement du processus) : ni dans l'URL, ni dans `ps`."""
    user = "x-access-token" if cfg["id"] == "github" else "oauth2"
    basic = base64.b64encode(f"{user}:{cfg['token']}".encode()).decode()
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "2",
                "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
                "GIT_CONFIG_KEY_1": "credential.helper", "GIT_CONFIG_VALUE_1": ""})
    return env


def _scrub(text: str, cfg: dict) -> str:
    return text.replace(cfg["token"], "***") if cfg["token"] else text


def _base_ref(repo: Path) -> str:
    """Nom de la branche de base : celle par défaut du dépôt d'origine (souvent main)."""
    p = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if p.returncode == 0 and p.stdout.strip():
        return p.stdout.strip().split("/", 1)[-1]
    for cand in ("main", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", cand).returncode == 0:
            return cand
    return "main"


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-.")
    return s[:90] or "usine-logicielle"


def default_repo_name(out_dir: Path) -> str:
    try:
        meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}
    target = str(meta.get("target") or out_dir.name).rstrip("/")
    return _slug(target.split("/")[-1].removesuffix(".git"))


# --------------------------------------------------------------------------- publication
def publish(name: str, *, platform: str, repo_name: str | None = None, private: bool = True,
            out_root: Path = Path("out"), progress: ProgressCb | None = None) -> dict:
    def say(step: str, msg: str) -> None:
        if progress:
            progress(step, msg)

    if platform not in FORGES:
        raise PublishError("plateforme inconnue (github | gitlab)")
    label = FORGES[platform]["label"]
    out_dir = out_root / name
    try:
        changes = json.loads((out_dir / "remediation" / "changes.json").read_text(encoding="utf-8"))
        meta = json.loads((out_dir / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PublishError("Aucune usine construite pour ce projet : lancez d'abord la correction.") from e
    # Comme le déployeur : le clone se retrouve par la cible (meta["root"] peut venir d'un autre environnement,
    # par exemple le conteneur, quand out/ est partagé).
    from .source import resolve
    try:
        repo = resolve(meta["target"], refresh=False).resolve()
    except (RuntimeError, KeyError, FileNotFoundError) as e:
        raise PublishError(f"Clone du dépôt introuvable : {e}") from e
    branch = changes.get("branch")
    if not branch or not (repo / ".git").exists():
        raise PublishError("Branche corrigée introuvable dans le clone local : relancez la correction.")
    if _git(repo, "rev-parse", "--verify", "--quiet", branch).returncode != 0:
        raise PublishError(f"La branche {branch} n'existe plus dans le clone local.")
    cfg = forge_config(platform)
    warnings: list[str] = []
    files = {c.get("path", "") for c in changes.get("changes", [])}
    has_gh = any(p.startswith(".github/workflows/") for p in files)
    has_gl = any(p.endswith("gitlab-ci.yml") for p in files)
    if platform == "github" and has_gl and not has_gh:
        warnings.append("L'usine a été générée pour GitLab CI : sur GitHub, le pipeline ne se lancera pas. "
                        "Reconstruisez l'usine avec la cible « GitHub Actions ».")
    if platform == "gitlab" and has_gh and not has_gl:
        warnings.append("L'usine a été générée pour GitHub Actions : sur GitLab, le pipeline ne se lancera pas. "
                        "Reconstruisez l'usine avec la cible « GitLab CI ».")

    # 1. compte ------------------------------------------------------------------
    say("auth", f"vérification du jeton {label} …")
    owner = whoami(platform)["login"]
    say("auth", f"connecté en tant que {owner}")

    # 2. dépôt -------------------------------------------------------------------
    rname = _slug(repo_name) if repo_name else default_repo_name(out_dir)
    say("repo", f"dépôt {owner}/{rname} ({'privé' if private else 'public'}) …")
    created = False
    desc = "Usine logicielle générée par l'agent DevOps"
    if platform == "github":
        code, r = _api(cfg, "POST", "/user/repos", {"name": rname, "private": private, "auto_init": False, "description": desc})
        if code == 201:
            created = True
        elif code == 422:
            code, r = _api(cfg, "GET", f"/repos/{owner}/{rname}")
            if code != 200:
                raise PublishError(f"Création du dépôt refusée : {_msg(r)}")
        else:
            raise PublishError(f"Création du dépôt refusée ({code}) : {_msg(r)}")
        web, clone, pid = r["html_url"], r["clone_url"], f"{owner}/{rname}"
    else:
        code, r = _api(cfg, "POST", "/projects", {"name": rname, "path": rname, "description": desc,
                                                  "visibility": "private" if private else "public",
                                                  "initialize_with_readme": False})
        if code == 201:
            created = True
        elif code == 400 and "taken" in json.dumps(r):
            code, r = _api(cfg, "GET", "/projects/" + urllib.parse.quote(f"{owner}/{rname}", safe=""))
            if code != 200:
                raise PublishError(f"Création du projet refusée : {_msg(r)}")
        else:
            raise PublishError(f"Création du projet refusée ({code}) : {_msg(r)}")
        web, clone, pid = r["web_url"], r["http_url_to_repo"], r["id"]
    say("repo", ("dépôt créé : " if created else "dépôt existant réutilisé : ") + web)

    # 3. push ----------------------------------------------------------------------
    env = _auth_env(cfg)
    base = _base_ref(repo)
    base_sha = changes.get("base_commit") or base
    say("push", f"envoi de la branche de base {base} …")
    p = _git(repo, "push", clone, f"{base_sha}:refs/heads/{base}", env=env)
    if p.returncode != 0:
        err = _scrub(p.stderr, cfg)
        if "rejected" in err or "non-fast-forward" in err:
            warnings.append(f"La branche {base} du dépôt distant a un autre historique : elle est laissée intacte.")
        else:
            raise PublishError(f"Envoi de {base} refusé : {err.strip()[-500:]}")
    say("push", f"envoi de la branche corrigée {branch} …")
    p = _git(repo, "push", "--force", clone, f"refs/heads/{branch}:refs/heads/{branch}", env=env)
    if p.returncode != 0:
        err = _scrub(p.stderr, cfg).strip()
        hint = (" — le jeton GitHub doit avoir la portée « workflow » pour pousser .github/workflows/."
                if platform == "github" and "workflow" in err else "")
        raise PublishError(f"Envoi de {branch} refusé : {err[-500:]}{hint}")
    sha = _git(repo, "rev-parse", branch).stdout.strip()
    say("push", f"{branch} @ {sha[:8]} envoyée")
    if platform == "github" and created:
        _api(cfg, "PATCH", f"/repos/{pid}", {"default_branch": base})

    # 4. PR / MR ---------------------------------------------------------------------
    title = "Usine logicielle : correction des risques DevOps"
    body = ((changes.get("summary") or "").strip()
            + "\n\n---\nGénéré par l'agent DevOps. Le détail des corrections est dans REMEDIATION.md et le pipeline "
              "se lance sur cette demande de fusion. Le job de déploiement attend le secret KUBE_CONFIG.")
    pr = None
    if platform == "github":
        code, r = _api(cfg, "POST", f"/repos/{pid}/pulls", {"title": title, "head": branch, "base": base, "body": body})
        if code == 201:
            pr = r["html_url"]
        else:
            code2, lst = _api(cfg, "GET", f"/repos/{pid}/pulls?head={owner}:{urllib.parse.quote(branch)}&state=open")
            pr = lst[0]["html_url"] if code2 == 200 and isinstance(lst, list) and lst else None
            if not pr:
                warnings.append(f"Pull request non ouverte : {_msg(r)}")
        pipelines, settings_url = f"{web}/actions", f"{web}/settings/secrets/actions"
    else:
        code, r = _api(cfg, "POST", f"/projects/{pid}/merge_requests",
                       {"source_branch": branch, "target_branch": base, "title": title, "description": body})
        if code == 201:
            pr = r["web_url"]
        else:
            code2, lst = _api(cfg, "GET", f"/projects/{pid}/merge_requests?source_branch={urllib.parse.quote(branch)}&state=opened")
            pr = lst[0]["web_url"] if code2 == 200 and isinstance(lst, list) and lst else None
            if not pr:
                warnings.append(f"Merge request non ouverte : {_msg(r)}")
        pipelines, settings_url = f"{web}/-/pipelines", f"{web}/-/settings/ci_cd"
    if pr:
        say("pr", ("pull request : " if platform == "github" else "merge request : ") + pr)

    result = {"platform": platform, "label": label, "owner": owner, "repo": rname, "private": private,
              "created": created, "web_url": web, "pr_url": pr, "pipelines_url": pipelines,
              "settings_url": settings_url, "branch": branch, "base": base, "commit": sha, "warnings": warnings,
              "published_at": datetime.now().isoformat(timespec="seconds")}
    (out_dir / "publish.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    say("done", f"publié sur {label} : {web}")
    return result


def publish_state(out_dir: Path) -> dict | None:
    try:
        return json.loads((out_dir / "publish.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
