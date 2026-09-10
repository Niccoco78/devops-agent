#!/usr/bin/env python3
"""Interface web de l'agent : on colle l'URL d'un dépôt, on obtient l'analyse.

    python server.py [--port 8080] [--host 0.0.0.0]

Bibliothèque standard uniquement. L'analyse tourne dans un thread ; le navigateur interroge
l'état du job toutes les secondes et affiche l'avancement, puis le rapport.

API :
    GET  /                    l'interface (web/index.html)
    GET  /api/models          backends et modèles disponibles
    POST /api/analyze         {"target": "...", "backend"?: "...", "model"?: "...", "refresh"?: bool} -> {"job_id"}
    GET  /api/jobs/<id>       {"status", "log": [...], "result"?: {...}, "error"?: "..."}
    GET  /api/reports         analyses précédentes (dossier out/)
    GET  /reports/<name>/<f>  fichiers d'un rapport (analysis.html, .md, .json, prompt.txt)
    POST /api/fix             {"name", "risks"?, "target_kind"?, "backend"?, "model"?} -> {"job_id"}  (étape 2)
    GET  /api/providers       fournisseurs d'IA (clés masquées)
    POST /api/providers       {"id", "api_key"?, "models"?, "base_url"?, "set_default"?, "clear"?}
    POST /api/providers/test  {"id", "model"?} -> {"ok", "model", "latency"} ou {"ok": false, "error"}
    GET  /api/cache           dépôts clonés ; POST /api/cache/clear pour les supprimer
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from devops_agent.deployer import DeployError, deploy_state, live_view, remove_loader, run_deploy, undeploy
from devops_agent.platform import install_platform, platform_status, uninstall_platform
from devops_agent.publisher import PublishError, default_repo_name, describe_forges, publish, publish_state, update_forge, whoami
from devops_agent.fixer import TARGETS, RemediationError, run_remediation
from devops_agent.pipeline import PipelineError, default_backend_name, run_analysis
from devops_agent.source import clear_cache, dir_size, empty_dir, list_clones
from devops_agent import cancel, clusters, providers as P
from devops_agent.llm import LLMError, make_backend

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
OUT_DIR = BASE_DIR / "out"

# Analyses simultanées : les modèles gratuits et la machine n'aiment pas la foule.
MAX_PARALLEL = int(os.environ.get("AGENT_MAX_PARALLEL", "2"))
_slots = threading.BoundedSemaphore(MAX_PARALLEL)

# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, params: dict, kind: str = "analyze"):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind            # analyze | fix | deploy
        self.params = params
        self.status = "queued"
        self.log: list[dict] = []
        self.result: dict | None = None
        self.error: str | None = None
        self.created = time.time()
        self.finished: float | None = None
        self.snapshot: dict | None = None
        self.stop = threading.Event()        # « Arrêter » dans l'interface
        self.thread_id: int | None = None
        self.cancelled = False
        self._lock = threading.Lock()

    def push(self, step: str, msg: str) -> None:
        if self.stop.is_set():
            raise cancel.Cancelled()         # chaque ligne de journal est un point d'arrêt
        with self._lock:
            self.log.append({"t": round(time.time() - self.created, 1), "step": step, "msg": msg})

    def set_snapshot(self, snap: dict) -> None:
        """État structuré le plus récent (composants, images, pods…) : ce que le HUD dessine."""
        with self._lock:
            self.snapshot = json.loads(json.dumps(snap, default=str))

    def cancel(self) -> None:
        self.stop.set()
        if self.thread_id:
            cancel.cancel_thread(self.thread_id)     # tue l'appel au modèle ou l'installation en cours

    def to_dict(self) -> dict:
        with self._lock:
            return {"cancelled": self.cancelled, "stopping": self.stop.is_set() and self.status == "running",
                "id": self.id, "kind": self.kind, "status": self.status, "log": list(self.log), "result": self.result,
                "error": self.error, "elapsed": round((self.finished or time.time()) - self.created, 1),
                "params": self.params, "snapshot": self.snapshot,
            }


JOBS: dict[str, Job] = {}


def _run_fix(job: Job) -> None:
    p = job.params
    res = run_remediation(
        p["name"], out_root=OUT_DIR, risk_refs=p.get("risks") or None, target_kind=p.get("target_kind") or "k8s-gitlab",
        backend=p.get("backend"), model=p.get("model"), progress=job.push, snapshot=job.set_snapshot,
    )
    job.result = {
        "name": res.name, "target_kind": res.target_kind, "branch": res.branch, "base_commit": res.base_commit,
        "commit": res.commit, "summary": res.summary, "repo_dir": str(res.repo_dir), "applied_in_repo": res.applied_in_repo,
        "changes": [{"path": c.path, "action": c.action, "purpose": c.purpose, "risk_refs": c.risk_refs,
                     "status": c.status, "note": c.note} for c in res.changes],
        "not_fixed": res.not_fixed, "manual_steps": res.manual_steps, "validations": res.validations,
        "diff": res.diff, "model": res.model, "elapsed": round(res.elapsed, 1), "calls": res.calls, "truncated": res.truncated,
        "files": {k: f"/reports/{res.name}/remediation/{k}" for k in res.files},
    }


def _run_deploy(job: Job) -> None:
    from dataclasses import asdict
    p = job.params
    res = run_deploy(p["name"], out_root=OUT_DIR, backend=p.get("backend"), model=p.get("model"),
                     progress=job.push, snapshot=job.set_snapshot, cluster=p.get("cluster") or "desktop",
                     observability=p.get("observability", True) is not False)
    data = asdict(res)
    data["files"] = {k: f"/reports/{res.name}/deploy/{k}" for k in res.files}
    job.result = data


def _run_platform(job: Job) -> None:
    p = job.params
    if p.get("action") == "uninstall":
        job.result = uninstall_platform(progress=job.push, kind=p.get("cluster") or "desktop")
    else:
        job.result = install_platform(p.get("components") or None, progress=job.push, snapshot=job.set_snapshot,
                                      kind=p.get("cluster") or "desktop")


def _run_job(job: Job) -> None:
    job.thread_id = threading.get_ident()
    cancel.bind(job.stop)
    with _slots:
        job.status = "running"
        p = job.params
        try:
            cancel.check()                   # arrêtée pendant qu'elle attendait son tour
            if job.kind == "fix":
                _run_fix(job)
                job.status = "done"
                return
            if job.kind == "deploy":
                _run_deploy(job)
                job.status = "done"
                return
            if job.kind == "platform":
                _run_platform(job)
                job.status = "done"
                return
            if job.kind == "publish":
                job.result = publish(p["name"], platform=p["platform"], repo_name=p.get("repo") or None,
                                     private=bool(p.get("private", True)), out_root=OUT_DIR, progress=job.push)
                job.status = "done"
                return
            res = run_analysis(
                p["target"], backend=p.get("backend"), model=p.get("model"),
                depth=int(p.get("depth") or 4), budget=int(p.get("budget") or 80_000),
                refresh=bool(p.get("refresh")), out_root=OUT_DIR, progress=job.push, snapshot=job.set_snapshot,
            )
            name = res.out_dir.name
            job.result = {
                "name": name,
                "data": res.data,
                "model": res.model,
                "backend": res.backend,
                "source": res.source,
                "context": res.context,
                "attempts": res.attempts,
                "truncated": res.truncated,
                "elapsed_llm": round(res.elapsed_llm, 1),
                "files": {k: f"/reports/{name}/{k}" for k in res.files},
            }
            job.status = "done"
        except (PipelineError, RemediationError, DeployError, PublishError) as e:
            job.error = str(e)
            job.status = "error"
        except cancel.Cancelled:
            job.cancelled = True
            job.error = "Arrêtée à votre demande."
            job.status = "error"
        except Exception as e:  # noqa: BLE001 — on veut voir l'erreur dans l'interface, pas un thread mort
            job.error = f"Erreur interne : {type(e).__name__}: {e}"
            job.status = "error"
        finally:
            job.finished = time.time()
            cancel.unbind()


def _deployment_info(d: Path) -> dict | None:
    s = deploy_state(d)
    if not s:
        return None
    return {"url": s.get("url"), "ok": s.get("ok"), "deployed": s.get("deployed"), "namespace": s.get("namespace")}


def list_reports() -> list[dict]:
    items = []
    if not OUT_DIR.is_dir():
        return items
    for d in sorted(OUT_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        f = d / "analysis.json"
        if not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        risks = data.get("risks", [])
        meta = {}
        if (d / "meta.json").is_file():
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {}
        items.append({
            "name": d.name,
            "project": data.get("project", {}).get("name", d.name),
            "target": meta.get("target"),
            "fixable": bool(meta.get("target")),
            "remediated": (d / "remediation" / "REMEDIATION.md").is_file(),
            "deployment": _deployment_info(d),
            "published": publish_state(d),
            "repo_name": default_repo_name(d),
            "date": datetime.fromtimestamp(f.stat().st_mtime).strftime("%d/%m/%Y %H:%M"),
            "risks": len(risks),
            "critical": sum(1 for r in risks if r.get("severity") == "critical"),
            "confidence": data.get("confidence", "medium"),
            "files": {n: f"/reports/{d.name}/{n}" for n in ("analysis.html", "analysis.md", "analysis.json", "prompt.txt") if (d / n).is_file()},
            "risk_list": [{"title": r.get("title", ""), "severity": r.get("severity", "medium")} for r in risks],
        })
    return items


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class Handler(BaseHTTPRequestHandler):
    server_version = "devops-agent/1.0"

    # -- utilitaires --------------------------------------------------------
    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, download: bool = False) -> None:
        if not path.is_file():
            self._json({"error": "introuvable"}, HTTPStatus.NOT_FOUND)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if path.suffix in (".md", ".txt"):
            ctype = "text/plain; charset=utf-8"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def log_message(self, fmt, *args):  # journal plus sobre : on tait le polling des jobs
        if args and "/api/jobs/" in str(args[0]):
            return
        super().log_message(fmt, *args)

    # -- routes -------------------------------------------------------------
    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path

        if path in ("/", "/index.html"):
            return self._file(WEB_DIR / "index.html")
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            if ".." in rel:
                return self._json({"error": "chemin invalide"}, HTTPStatus.BAD_REQUEST)
            return self._file(WEB_DIR / rel)

        if path == "/api/platform":
            kind = (parse_qs(url.query).get("cluster") or ["desktop"])[0]
            return self._json(platform_status(kind if kind in clusters.KINDS else "desktop"))
        if path == "/api/clusters":
            from devops_agent.platform import observability_info
            return self._json([{**c, "observability": observability_info(c["id"])} for c in clusters.list_clusters()])
        if path == "/api/git":
            return self._json(describe_forges())
        if path == "/api/cluster":
            name = (parse_qs(url.query).get("name") or [""])[0]
            if not name or not _SAFE_NAME.match(name):
                return self._json({"error": "projet inconnu"}, HTTPStatus.BAD_REQUEST)
            return self._json(live_view(name, out_root=OUT_DIR))
        if path == "/api/jobs":
            # Jobs récents, du plus récent au plus ancien : permet à l'interface de retrouver
            # une analyse en cours après un rechargement de page.
            recent = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)[:20]
            return self._json([{"id": j.id, "kind": j.kind, "status": j.status,
                                "target": j.params.get("target") or j.params.get("name"),
                                "elapsed": round((j.finished or time.time()) - j.created, 1)} for j in recent])
        if path == "/api/models":
            providers = P.describe_all()
            return self._json({
                "default_backend": default_backend_name(),
                "providers": providers,
                "targets": TARGETS,
            })
        if path == "/api/providers":
            return self._json({"providers": P.describe_all(), "default": default_backend_name()})
        if path == "/api/reports":
            return self._json(list_reports())
        if path == "/api/cache":
            clones = list_clones()
            reports = [d for d in OUT_DIR.iterdir() if d.is_dir()] if OUT_DIR.is_dir() else []
            return self._json({"clones": clones, "bytes": sum(c["bytes"] for c in clones),
                               "reports": len(reports), "reports_bytes": sum(dir_size(d) for d in reports)})
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if not job:
                return self._json({"error": "job inconnu"}, HTTPStatus.NOT_FOUND)
            return self._json(job.to_dict())

        if path.startswith("/reports/"):
            parts = path.split("/")[2:]
            # /reports/<nom>/<fichier>, /reports/<nom>/remediation/<fichier> ou /reports/<nom>/deploy/<fichier>
            if len(parts) not in (2, 3) or not all(_SAFE_NAME.match(p) for p in parts) or (len(parts) == 3 and parts[1] not in ("remediation", "deploy")):
                return self._json({"error": "chemin invalide"}, HTTPStatus.BAD_REQUEST)
            return self._file(OUT_DIR.joinpath(*parts), download="download" in url.query)

        self._json({"error": "route inconnue"}, HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:  # sondes des navigateurs et des outils de preview
        self.send_response(200)
        self.end_headers()

    def do_POST(self) -> None:
        route = urlparse(self.path).path
        if route == "/api/fix":
            return self._post_fix()
        if route == "/api/deploy":
            return self._post_deploy()
        if route == "/api/undeploy":
            return self._post_undeploy()
        if route == "/api/platform":
            return self._post_platform()
        m = re.fullmatch(r"/api/jobs/([0-9a-f]{12})/cancel", route)
        if m:
            job = JOBS.get(m.group(1))
            if not job:
                return self._json({"error": "tâche inconnue"}, HTTPStatus.NOT_FOUND)
            if job.status in ("queued", "running"):
                job.cancel()
            return self._json({"ok": True, "status": job.status})
        if route == "/api/clusters/delete":
            if any(j.kind in ("deploy", "platform") and j.status in ("queued", "running") for j in JOBS.values()):
                return self._json({"error": "Un déploiement ou une installation est en cours : attendez sa fin."}, HTTPStatus.CONFLICT)
            try:
                res = clusters.delete_k3s()
            except clusters.ClusterError as e:
                return self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)
            from devops_agent.platform import _save_state
            _save_state({}, "k3s")
            return self._json(res)
        if route == "/api/providers/refresh":
            pid = str(self._body().get("id", ""))
            if pid not in P.PROVIDERS:
                return self._json({"error": "fournisseur inconnu"}, HTTPStatus.BAD_REQUEST)
            P.list_models(pid, refresh=True)
            return self._json({"providers": P.describe_all(), "default": default_backend_name()})
        if route == "/api/git":
            return self._post_git()
        if route == "/api/git/test":
            return self._post_git_test()
        if route == "/api/publish":
            return self._post_publish()
        if route == "/api/providers":
            return self._post_providers()
        if route == "/api/providers/test":
            return self._post_provider_test()
        if route == "/api/cache/clear":
            if any(j.status in ("queued", "running") for j in JOBS.values()):
                return self._json({"error": "Une analyse ou une remédiation est en cours : attendez la fin avant de nettoyer."}, HTTPStatus.CONFLICT)
            # Tout nettoyer : applis déployées, clones ET analyses (rapports, remédiations).
            # Les réglages (data/, clés API) restent.
            apps, errors = 0, []
            for d in (OUT_DIR.iterdir() if OUT_DIR.is_dir() else []):
                s = deploy_state(d) if d.is_dir() else None
                if s and s.get("deployed"):
                    try:
                        undeploy(d.name, out_root=OUT_DIR, purge_images=True)
                        apps += 1
                    except DeployError as e:  # on nettoie quand même le disque, mais on le dit
                        errors.append(f"{d.name} : {e}")
            # Filet de sécurité : tout ce qui porte le label de l'agent sur le cluster, même sans état sur disque.
            from devops_agent.deployer import sweep_cluster
            sw = sweep_cluster(purge_images=True)
            if sw.get("error"):
                errors.append(sw["error"])
            apps = max(apps, len(sw["namespaces"]))
            try:
                clones = clear_cache()
                reports = empty_dir(OUT_DIR)
            except OSError as e:
                return self._json({"error": f"Suppression impossible : {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            JOBS.clear()   # aucun job en cours (vérifié plus haut) : on oublie les résultats qui pointaient vers out/
            return self._json({"clones_removed": clones["removed"], "reports_removed": reports["removed"], "apps_removed": apps,
                               "images_removed": sw["images"], "errors": errors,
                               "freed_bytes": clones["freed_bytes"] + reports["freed_bytes"]})
        if route == "/api/missions/delete":
            return self._post_mission_delete()
        if route != "/api/analyze":
            return self._json({"error": "route inconnue"}, HTTPStatus.NOT_FOUND)
        body = self._body()
        target = str(body.get("target", "")).strip()
        if not target:
            return self._json({"error": "Indiquez l'URL d'un dépôt Git ou un chemin local."}, HTTPStatus.BAD_REQUEST)
        backend = body.get("backend") or None
        if backend is not None and backend not in P.PROVIDERS:
            return self._json({"error": "backend inconnu"}, HTTPStatus.BAD_REQUEST)
        job = Job({
            "target": target, "backend": backend, "model": body.get("model") or None,
            "depth": body.get("depth") or 4, "budget": body.get("budget") or 80_000,
            "refresh": bool(body.get("refresh")),
        })
        JOBS[job.id] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        self._json({"job_id": job.id}, HTTPStatus.ACCEPTED)

    def _post_mission_delete(self) -> None:
        """Supprime définitivement une mission : appli retirée du cluster, analyse, usine et clone local."""
        body = self._body()
        name = str(body.get("name", "")).strip()
        if not name or not _SAFE_NAME.match(name) or not (OUT_DIR / name).is_dir():
            return self._json({"error": "Mission inconnue."}, HTTPStatus.BAD_REQUEST)
        busy = [j for j in JOBS.values() if j.status in ("queued", "running")
                and (j.params.get("name") == name or j.kind == "analyze")]
        if busy:
            return self._json({"error": "Une tâche tourne sur cette mission : attendez sa fin."}, HTTPStatus.CONFLICT)
        errors = []
        s = deploy_state(OUT_DIR / name)
        if s and s.get("deployed"):
            try:
                undeploy(name, out_root=OUT_DIR, purge_images=True)
            except DeployError as e:
                errors.append(f"appli non retirée du cluster : {e}")
        from devops_agent.source import CACHE_DIR, _rmtree
        try:
            _rmtree(OUT_DIR / name)
            if (CACHE_DIR / name).exists():
                _rmtree(CACHE_DIR / name)
        except OSError as e:
            return self._json({"error": f"Suppression impossible : {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        for jid in [k for k, j in JOBS.items() if j.params.get("name") == name or (j.result or {}).get("name") == name]:
            JOBS.pop(jid, None)
        self._json({"deleted": name, "errors": errors})

    def _post_deploy(self) -> None:
        """Étape 3 : déploie la branche corrigée sur le Kubernetes local (job de fond)."""
        body = self._body()
        name = str(body.get("name", "")).strip()
        if not name or not _SAFE_NAME.match(name) or not (OUT_DIR / name / "remediation" / "changes.json").is_file():
            return self._json({"error": "Aucune remédiation pour ce projet : lancez d'abord « Corriger les risques »."}, HTTPStatus.BAD_REQUEST)
        if any(j.kind == "deploy" and j.params.get("name") == name and j.status in ("queued", "running") for j in JOBS.values()):
            return self._json({"error": "Un déploiement de ce projet est déjà en cours."}, HTTPStatus.CONFLICT)
        backend = body.get("backend") or None
        if backend is not None and backend not in P.PROVIDERS:
            return self._json({"error": "backend inconnu"}, HTTPStatus.BAD_REQUEST)
        cluster = body.get("cluster") or "desktop"
        if cluster not in clusters.KINDS:
            return self._json({"error": "cluster inconnu (desktop | k3s)"}, HTTPStatus.BAD_REQUEST)
        job = Job({"name": name, "backend": backend, "model": body.get("model") or None, "cluster": cluster,
                   "observability": body.get("observability", True) is not False}, kind="deploy")
        JOBS[job.id] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        self._json({"job_id": job.id}, HTTPStatus.ACCEPTED)

    def _post_undeploy(self) -> None:
        body = self._body()
        name = str(body.get("name", "")).strip()
        if not name or not _SAFE_NAME.match(name):
            return self._json({"error": "projet inconnu"}, HTTPStatus.BAD_REQUEST)
        if any(j.kind == "deploy" and j.params.get("name") == name and j.status in ("queued", "running") for j in JOBS.values()):
            return self._json({"error": "Un déploiement est en cours : attendez sa fin."}, HTTPStatus.CONFLICT)
        try:
            self._json(undeploy(name, out_root=OUT_DIR))
        except DeployError as e:
            self._json({"error": str(e)}, HTTPStatus.BAD_REQUEST)

    def _post_platform(self) -> None:
        """Installe ou retire la plateforme locale (observabilité, entrée, logs) — job de fond."""
        body = self._body()
        action = body.get("action", "install")
        if action not in ("install", "uninstall"):
            return self._json({"error": "action inconnue (install | uninstall)"}, HTTPStatus.BAD_REQUEST)
        if any(j.kind in ("platform", "deploy") and j.status in ("queued", "running") for j in JOBS.values()):
            return self._json({"error": "Une installation ou un déploiement est en cours : attendez sa fin."}, HTTPStatus.CONFLICT)
        comps = [c for c in (body.get("components") or []) if isinstance(c, str)]
        cluster = body.get("cluster") or "desktop"
        if cluster not in clusters.KINDS:
            return self._json({"error": "cluster inconnu (desktop | k3s)"}, HTTPStatus.BAD_REQUEST)
        job = Job({"action": action, "components": comps, "name": "plateforme", "cluster": cluster}, kind="platform")
        JOBS[job.id] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        self._json({"job_id": job.id}, HTTPStatus.ACCEPTED)

    def _post_git(self) -> None:
        """Jetons GitHub / GitLab : enregistrés côté serveur, jamais renvoyés en clair."""
        body = self._body()
        try:
            update_forge(str(body.get("id")), token=body.get("token"), url=body.get("url"), clear=bool(body.get("clear")))
        except KeyError:
            return self._json({"error": "forge inconnue (github | gitlab)"}, HTTPStatus.BAD_REQUEST)
        self._json(describe_forges())

    def _post_git_test(self) -> None:
        body = self._body()
        try:
            me = whoami(str(body.get("id")))
        except KeyError:
            return self._json({"error": "forge inconnue"}, HTTPStatus.BAD_REQUEST)
        except PublishError as e:
            return self._json({"ok": False, "error": str(e)})
        self._json({"ok": True, **me})

    def _post_publish(self) -> None:
        """Étape 4 : crée le dépôt sur GitHub ou GitLab, pousse la branche corrigée, ouvre la PR / MR."""
        body = self._body()
        name = str(body.get("name", "")).strip()
        platform = str(body.get("platform", "")).strip()
        if not name or not _SAFE_NAME.match(name) or not (OUT_DIR / name / "remediation" / "changes.json").is_file():
            return self._json({"error": "Aucune usine construite pour ce projet."}, HTTPStatus.BAD_REQUEST)
        if platform not in ("github", "gitlab"):
            return self._json({"error": "plateforme inconnue (github | gitlab)"}, HTTPStatus.BAD_REQUEST)
        repo = str(body.get("repo") or "").strip()
        if repo and not re.fullmatch(r"[A-Za-z0-9._-]{1,90}", repo):
            return self._json({"error": "nom de dépôt invalide (lettres, chiffres, . _ -)"}, HTTPStatus.BAD_REQUEST)
        if any(j.kind == "publish" and j.params.get("name") == name and j.status in ("queued", "running") for j in JOBS.values()):
            return self._json({"error": "Une publication de ce projet est déjà en cours."}, HTTPStatus.CONFLICT)
        job = Job({"name": name, "platform": platform, "repo": repo, "private": body.get("private", True) is not False},
                  kind="publish")
        JOBS[job.id] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        self._json({"job_id": job.id}, HTTPStatus.ACCEPTED)

    def _post_providers(self) -> None:
        """Enregistre les réglages d'un fournisseur. La clé n'est jamais renvoyée en clair."""
        body = self._body()
        pid = str(body.get("id", ""))
        if pid not in P.PROVIDERS:
            return self._json({"error": "fournisseur inconnu"}, HTTPStatus.BAD_REQUEST)
        models = body.get("models")
        if isinstance(models, str):
            models = [m for m in models.replace("\n", ",").split(",")]
        try:
            P.update(pid, api_key=body.get("api_key") if "api_key" in body else None, models=models,
                     base_url=body.get("base_url") if "base_url" in body else None, clear=bool(body.get("clear")),
                     default_model=body.get("default_model") if "default_model" in body else None)
            if body.get("set_default"):
                P.set_default(pid)
            elif body.get("clear") and P._load().get("default") == pid:
                P.set_default(None)
        except OSError as e:
            return self._json({"error": f"Enregistrement impossible : {e}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
        self._json({"providers": P.describe_all(), "default": default_backend_name()})

    def _post_provider_test(self) -> None:
        """Un appel minimal au fournisseur pour vérifier clé, URL et modèle."""
        body = self._body()
        pid = str(body.get("id", ""))
        if pid not in P.PROVIDERS:
            return self._json({"error": "fournisseur inconnu"}, HTTPStatus.BAD_REQUEST)
        t0 = time.time()
        try:
            backend = make_backend(pid, body.get("model") or None)
            res = backend.complete("Tu réponds uniquement en JSON.", 'Réponds exactement {"ok": true}', expected_keys=("ok",))
            self._json({"ok": True, "model": res.model, "latency": round(time.time() - t0, 1),
                        "tokens": (res.input_tokens or 0) + (res.output_tokens or 0)})
        except LLMError as e:
            self._json({"ok": False, "error": str(e).splitlines()[0][:400], "latency": round(time.time() - t0, 1)})
        except Exception as e:  # noqa: BLE001
            self._json({"ok": False, "error": f"{type(e).__name__}: {e}"[:400], "latency": round(time.time() - t0, 1)})

    def _post_fix(self) -> None:
        body = self._body()
        name = str(body.get("name", "")).strip()
        if not name or not _SAFE_NAME.match(name) or not (OUT_DIR / name / "analysis.json").is_file():
            return self._json({"error": "Analyse introuvable : lancez d'abord une analyse."}, HTTPStatus.BAD_REQUEST)
        target_kind = body.get("target_kind") or "k8s-gitlab"
        if target_kind not in TARGETS:
            return self._json({"error": "cible inconnue"}, HTTPStatus.BAD_REQUEST)
        risks = [int(r) for r in (body.get("risks") or []) if str(r).isdigit()]
        job = Job({"name": name, "risks": risks, "target_kind": target_kind,
                   "backend": body.get("backend") or None, "model": body.get("model") or None}, kind="fix")
        JOBS[job.id] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        self._json({"job_id": job.id}, HTTPStatus.ACCEPTED)


def main() -> None:
    p = argparse.ArgumentParser(description="Interface web de l'agent DevOps")
    p.add_argument("--host", default=os.environ.get("AGENT_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("AGENT_PORT", "8080")))
    args = p.parse_args()
    OUT_DIR.mkdir(exist_ok=True)
    # la liste des modèles opencode (lente à obtenir) est demandée dès le démarrage
    threading.Thread(target=P.list_models, args=("opencode",), daemon=True).start()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Agent DevOps — interface sur http://{args.host}:{args.port}  (Ctrl+C pour arrêter)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
