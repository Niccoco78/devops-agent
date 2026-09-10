"""Clusters cibles du déploiement local.

    desktop  le Kubernetes intégré à Docker Desktop (à activer dans ses réglages)
    k3s      un cluster k3s léger, créé et géré par l'agent avec k3d (k3s dans des conteneurs Docker)

k3s embarque déjà Traefik (Ingress) et un stockage local : l'agent n'a qu'à le créer. Les ports sont
publiés sur la machine par l'équilibreur de k3d, fixés à la création du cluster :
    6550         API Kubernetes
    18200-18209  applis (Services LoadBalancer)
    18280        Ingress Traefik : http://<appli>.localhost:18280, http://grafana.localhost:18280
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import cancel
from .providers import DATA_DIR

ProgressCb = Callable[[str, str], None]

K3D_CLUSTER = "devops-agent"
K3S_KUBECONFIG = DATA_DIR / "k3s.kubeconfig"
K3S_API_PORT = 6550
K3S_APP_PORTS = list(range(18200, 18210))
K3S_WEB_PORT = 18280
DESKTOP_PORTS = list(range(18080, 18200))

KINDS = {
    "desktop": {"label": "Docker Desktop", "detail": "le Kubernetes intégré à Docker Desktop"},
    "k3s": {"label": "k3s", "detail": "cluster léger créé par l'agent avec k3d, dans Docker"},
}


class ClusterError(RuntimeError):
    pass


def k3d_exe() -> str | None:
    for cand in (os.environ.get("K3D_BIN"), shutil.which("k3d"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "k3d", "k3d.exe")):
        if cand and os.path.isfile(cand):
            return cand
    return None


def _k3d(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    exe = k3d_exe()
    if not exe:
        raise ClusterError("k3d est introuvable : il est fourni dans l'image Docker de l'agent (voir README).")
    proc = subprocess.Popen([exe, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    cancel.register(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        proc.communicate()
        raise ClusterError(f"k3d {args[0]} : délai dépassé ({timeout} s)") from e
    finally:
        cancel.unregister(proc)
    cancel.check()
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def desktop_kubeconfig() -> str | None:
    src = os.environ.get("KUBECONFIG")
    if src and Path(src).is_file():
        return src
    for cand in (Path("/root/.kube-host/config"), Path.home() / ".kube" / "config"):
        if cand.is_file():
            return str(cand)
    return None


def kubeconfig_path(kind: str) -> str | None:
    if kind == "k3s":
        return str(K3S_KUBECONFIG) if K3S_KUBECONFIG.is_file() else None
    return desktop_kubeconfig()


def k3s_info() -> dict:
    """{available: k3d présent, exists: cluster créé, running: serveur démarré}"""
    info = {"available": bool(k3d_exe()), "exists": False, "running": False, "error": None}
    if not info["available"]:
        return info
    try:
        proc = _k3d("cluster", "list", "-o", "json", timeout=30)
        for c in json.loads(proc.stdout or "[]"):
            if c.get("name") == K3D_CLUSTER:
                info["exists"] = True
                info["running"] = int(c.get("serversRunning", 0) or 0) > 0
    except (ClusterError, json.JSONDecodeError) as e:
        info["error"] = str(e)
    return info


def ensure_k3s(say: ProgressCb) -> None:
    """Crée (ou redémarre) le cluster k3s de l'agent, puis écrit son kubeconfig."""
    info = k3s_info()
    if not info["available"]:
        raise ClusterError("k3d est introuvable : reconstruisez l'image de l'agent (docker compose up -d --build).")
    if not info["exists"]:
        say("prereq", "création du cluster k3s « devops-agent » avec k3d (première fois, environ une minute) …")
        args = ["cluster", "create", K3D_CLUSTER, "--api-port", f"0.0.0.0:{K3S_API_PORT}",
                "--k3s-arg", "--tls-san=host.docker.internal@server:*",
                "-p", f"{K3S_WEB_PORT}:80@loadbalancer"]
        for p in K3S_APP_PORTS:
            args += ["-p", f"{p}:{p}@loadbalancer"]
        args += ["--wait", "--timeout", "300s"]
        proc = _k3d(*args, timeout=420)
        if proc.returncode != 0:
            raise ClusterError(f"Création du cluster k3s impossible : {(proc.stderr or proc.stdout).strip()[-600:]}")
        say("prereq", "cluster k3s créé")
    elif not info["running"]:
        say("prereq", "démarrage du cluster k3s …")
        proc = _k3d("cluster", "start", K3D_CLUSTER, "--wait", timeout=300)
        if proc.returncode != 0:
            raise ClusterError(f"Démarrage du cluster k3s impossible : {(proc.stderr or proc.stdout).strip()[-600:]}")
    proc = _k3d("kubeconfig", "get", K3D_CLUSTER, timeout=60)
    if proc.returncode != 0 or "server:" not in proc.stdout:
        raise ClusterError(f"kubeconfig k3s introuvable : {proc.stderr.strip()[-300:]}")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    K3S_KUBECONFIG.write_text(proc.stdout, encoding="utf-8")


def _platform() -> str:
    import platform
    return "linux/" + ("arm64" if platform.machine().lower() in ("aarch64", "arm64") else "amd64")


def import_images(images: list[str], say: ProgressCb) -> None:
    """Charge des images locales dans le containerd du nœud k3s, puis vérifie qu'elles y sont.

    Pas `k3d image import` : il importe « toutes les plateformes », or le stockage d'images de Docker
    Desktop ne contient que celle de la machine — l'import échoue dans le nœud et k3d annonce quand même
    un succès. On exporte donc la seule plateforme locale et on importe nous-mêmes.
    """
    if not images:
        return
    node = f"k3d-{K3D_CLUSTER}-server-0"
    for img in images:
        say("load", f"import de {img} dans le nœud k3s")
        save_cmd = ["docker", "save", "--platform", _platform(), img]
        probe = subprocess.run(save_cmd[:2] + ["--help"], capture_output=True, text=True)
        if "--platform" not in probe.stdout:
            save_cmd = ["docker", "save", img]           # CLI Docker ancienne : pas de filtre de plateforme
        save = subprocess.Popen(save_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        cancel.register(save)
        try:
            imp = subprocess.run(["docker", "exec", "-i", node, "ctr", "-n", "k8s.io", "images", "import", "-"],
                                 stdin=save.stdout, capture_output=True, timeout=900)
            save.stdout.close()
            save.wait(timeout=60)
        finally:
            cancel.unregister(save)
        cancel.check()
        if save.returncode != 0 or imp.returncode != 0:
            err = (imp.stderr or b"").decode("utf-8", "replace") + (save.stderr.read() if save.stderr else b"").decode("utf-8", "replace")
            raise ClusterError(f"Import de {img} dans k3s impossible : {err.strip()[-500:]}")
    listed = subprocess.run(["docker", "exec", node, "ctr", "-n", "k8s.io", "images", "ls", "-q"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60).stdout
    missing = [i for i in images if not any(line.endswith(i) for line in listed.splitlines())]
    if missing:
        raise ClusterError("Images absentes du nœud k3s après import : " + ", ".join(missing))


def delete_k3s() -> dict:
    info = k3s_info()
    if info["exists"]:
        proc = _k3d("cluster", "delete", K3D_CLUSTER, timeout=300)
        if proc.returncode != 0:
            raise ClusterError(f"Suppression du cluster k3s impossible : {(proc.stderr or proc.stdout).strip()[-400:]}")
    K3S_KUBECONFIG.unlink(missing_ok=True)
    return {"deleted": info["exists"]}


def list_clusters() -> list[dict]:
    """Ce que l'interface propose au moment de déployer."""
    out = []
    kc = desktop_kubeconfig()
    ready, note = False, "activez Kubernetes dans Docker Desktop (Settings, Kubernetes)"
    if kc and shutil.which("kubectl"):
        from .deployer import DeployError, _setup_kube
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            try:
                _, version = _setup_kube(Path(tmp), "desktop", quick=True)
                ready, note = True, f"Kubernetes {version}"
            except DeployError:
                pass
    out.append({"id": "desktop", **KINDS["desktop"], "available": bool(kc), "ready": ready, "note": note})
    info = k3s_info()
    if not info["available"]:
        note = "k3d absent de cette installation"
    elif not info["exists"]:
        note = "créé au premier déploiement, environ une minute"
    elif not info["running"]:
        note = "arrêté, redémarré au déploiement"
    else:
        note = "en marche"
    out.append({"id": "k3s", **KINDS["k3s"], "available": info["available"], "ready": info["running"],
                "exists": info["exists"], "note": note})
    return out
