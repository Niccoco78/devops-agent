"""Plateforme locale : le socle de l'usine logicielle, installé sur le cluster par Helm.

    metriques  kube-prometheus-stack — Prometheus, Alertmanager, Grafana, kube-state-metrics, node-exporter
    entree     Traefik — contrôleur Ingress : http://grafana.localhost, http://<appli>.localhost
    logs       Loki (binaire unique, stockage fichier) + Alloy (collecte des logs de tous les pods)

Charts officiels, versions épinglées. L'ordre compte : kube-prometheus-stack installe les CRD
(ServiceMonitor…) dont Traefik a besoin pour être lui-même surveillé.
Une fois la plateforme en place, chaque application déployée par l'agent est :
  - publiée par un Ingress sur http://<namespace>.localhost ;
  - collectée par Alloy (logs) et visible dans le tableau de bord « Applications déployées par l'agent ».
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

import yaml

from . import cancel, clusters
from .deployer import IN_DOCKER, DeployError, Kube, _setup_kube
from .providers import DATA_DIR

ProgressCb = Callable[[str, str], None]
STATE_FILE = DATA_DIR / "platform.json"
HELM_TIMEOUT = "15m"


def _helm_exe() -> str:
    for cand in (os.environ.get("HELM_BIN"), shutil.which("helm"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "helm", "helm.exe")):
        if cand and os.path.isfile(cand):
            return cand
    raise DeployError("Helm est introuvable (voir README : installation de Helm).")


def _state_all() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if "installed" in data or "grafana_password" in data:       # ancien format : un seul cluster
        data = {"desktop": data}
    return data


def _state(kind: str = "desktop") -> dict:
    """État de la plateforme sur un cluster (mot de passe Grafana, port d'entrée, composants installés)."""
    return dict(_state_all().get(kind) or {})


def _save_state(state: dict, kind: str = "desktop") -> None:
    data = _state_all()
    data[kind] = state
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def releases_for(kind: str) -> list[dict]:
    # k3s embarque déjà Traefik : en installer un second serait inutile et conflictuel.
    return [r for r in RELEASES if not (kind == "k3s" and r["id"] == "ingress")]


def web_port(kind: str = "desktop", st: dict | None = None) -> int:
    """Port de l'entrée HTTP (Traefik) du cluster sur la machine."""
    if kind == "k3s":
        return clusters.K3S_WEB_PORT
    return int((st if st is not None else _state(kind)).get("web_port", 80))


def observability_info(kind: str = "desktop") -> dict:
    st = _state(kind)
    port = web_port(kind, st)
    suffix = "" if port == 80 else f":{port}"
    return {"kind": kind, "installed": st.get("installed", []), "user": "admin", "password": st.get("grafana_password"),
            "grafana": f"http://grafana.localhost{suffix}", "prometheus": f"http://prometheus.localhost{suffix}",
            "alertmanager": f"http://alertmanager.localhost{suffix}"}


def ensure_platform(kind: str = "desktop", progress: ProgressCb | None = None,
                    snapshot: Callable[[dict], None] | None = None) -> dict:
    """Étape « Observabilité » du déploiement : installe ce qui manque sur ce cluster, sinon vérifie seulement."""
    def say(step: str, msg: str) -> None:
        if progress:
            progress(step, msg)

    status = platform_status(kind)
    if status.get("error"):
        raise DeployError(status["error"])
    missing = [c for c in status["components"] if not c["installed"]]
    if missing:
        say("platform", "observabilité : installation de " + ", ".join(c["label"].lower() for c in missing)
            + " (première fois sur ce cluster, environ 5 minutes) …")
        install_platform([c["id"] for c in missing], progress=progress, snapshot=snapshot, kind=kind)
    else:
        say("platform", "observabilité déjà en place : Prometheus, Grafana, Loki")
        st = _state(kind)
        st["installed"] = sorted(c["id"] for c in status["components"])
        _save_state(st, kind)
    return observability_info(kind)


def _port_free(port: int) -> bool:
    host = "host.docker.internal" if IN_DOCKER else "127.0.0.1"
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


# ---------------------------------------------------------------------------
# Composants
# ---------------------------------------------------------------------------

def _values_metrics(st: dict) -> dict:
    return {
        "grafana": {
            "adminPassword": st["grafana_password"],
            "ingress": {"enabled": True, "ingressClassName": "traefik", "hosts": ["grafana.localhost"], "path": "/"},
            "additionalDataSources": [{"name": "Loki", "type": "loki", "uid": "loki", "access": "proxy",
                                       "url": "http://loki.logging.svc.cluster.local:3100"}],
            "defaultDashboardsTimezone": "browser",
            "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}},
        },
        "prometheus": {
            "ingress": {"enabled": True, "ingressClassName": "traefik", "hosts": ["prometheus.localhost"], "paths": ["/"]},
            "prometheusSpec": {
                "retention": "2d",
                # Surveiller TOUT le cluster, pas seulement les objets de cette release (les applis déployées aussi).
                "serviceMonitorSelectorNilUsesHelmValues": False,
                "podMonitorSelectorNilUsesHelmValues": False,
                "ruleSelectorNilUsesHelmValues": False,
                "probeSelectorNilUsesHelmValues": False,
                "resources": {"requests": {"cpu": "100m", "memory": "400Mi"}},
            },
        },
        "alertmanager": {
            "ingress": {"enabled": True, "ingressClassName": "traefik", "hosts": ["alertmanager.localhost"], "paths": ["/"]},
        },
        # Injoignables dans un cluster kind : les laisser actifs ne produit que des alertes de faux positif.
        "kubeEtcd": {"enabled": False},
        "kubeScheduler": {"enabled": False},
        "kubeControllerManager": {"enabled": False},
        "kubeProxy": {"enabled": False},
    }


def _values_ingress(st: dict) -> dict:
    return {
        "ingressClass": {"enabled": True, "isDefaultClass": True},
        "ports": {"web": {"exposedPort": st["web_port"]}, "websecure": {"expose": {"default": False}}},
        "providers": {"kubernetesIngress": {"publishedService": {"enabled": True}}},
        "metrics": {"prometheus": {"serviceMonitor": {"enabled": True}}},
        "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}},
    }


def _values_loki(st: dict) -> dict:
    return {
        "deploymentMode": "SingleBinary",
        "loki": {
            "auth_enabled": False,
            "commonConfig": {"replication_factor": 1},
            "storage": {"type": "filesystem"},
            "schemaConfig": {"configs": [{"from": "2024-04-01", "store": "tsdb", "object_store": "filesystem",
                                          "schema": "v13", "index": {"prefix": "index_", "period": "24h"}}]},
            "limits_config": {"retention_period": "72h"},
        },
        "singleBinary": {"replicas": 1, "persistence": {"enabled": True, "size": "5Gi"},
                         "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}}},
        "read": {"replicas": 0}, "write": {"replicas": 0}, "backend": {"replicas": 0},
        "chunksCache": {"enabled": False}, "resultsCache": {"enabled": False},
        "lokiCanary": {"enabled": False}, "test": {"enabled": False}, "gateway": {"enabled": False},
        "minio": {"enabled": False},
    }


ALLOY_CONFIG = """
discovery.kubernetes "pods" {
  role = "pod"
}

discovery.relabel "pods" {
  targets = discovery.kubernetes.pods.targets
  rule {
    source_labels = ["__meta_kubernetes_namespace"]
    target_label  = "namespace"
  }
  rule {
    source_labels = ["__meta_kubernetes_pod_name"]
    target_label  = "pod"
  }
  rule {
    source_labels = ["__meta_kubernetes_pod_container_name"]
    target_label  = "container"
  }
  rule {
    source_labels = ["__meta_kubernetes_pod_label_app_kubernetes_io_name", "__meta_kubernetes_pod_label_app"]
    separator     = ""
    target_label  = "app"
  }
}

loki.source.kubernetes "pods" {
  targets    = discovery.relabel.pods.output
  forward_to = [loki.write.default.receiver]
}

loki.write "default" {
  endpoint {
    url = "http://loki.logging.svc.cluster.local:3100/loki/api/v1/push"
  }
}
"""


def _values_alloy(st: dict) -> dict:
    # Collecte par l'API Kubernetes : un seul réplica suffit (un DaemonSet dupliquerait les logs).
    return {"alloy": {"configMap": {"create": True, "content": ALLOY_CONFIG},
                      "resources": {"requests": {"cpu": "20m", "memory": "64Mi"}}},
            "controller": {"type": "deployment", "replicas": 1}}


RELEASES = [
    {"id": "metrics", "label": "Métriques et alertes", "detail": "Prometheus · Alertmanager · Grafana",
     "release": "monitoring", "namespace": "monitoring", "chart": "kube-prometheus-stack", "version": "90.0.0",
     "repo": "https://prometheus-community.github.io/helm-charts", "values": _values_metrics},
    {"id": "ingress", "label": "Entrée", "detail": "Traefik · Ingress *.localhost",
     "release": "traefik", "namespace": "traefik", "chart": "traefik", "version": "41.5.0",
     "repo": "https://traefik.github.io/charts", "values": _values_ingress},
    {"id": "logs", "label": "Logs", "detail": "Loki · stockage des logs",
     "release": "loki", "namespace": "logging", "chart": "loki", "version": "7.3.0", "shared_namespace": True,
     "repo": "https://grafana.github.io/helm-charts", "values": _values_loki},
    {"id": "collector", "label": "Collecte", "detail": "Alloy · logs de tous les pods",
     "release": "alloy", "namespace": "logging", "chart": "alloy", "version": "1.12.1", "shared_namespace": True,
     "repo": "https://grafana.github.io/helm-charts", "values": _values_alloy},
]


# ---------------------------------------------------------------------------
# Tableau de bord Grafana : les applications déployées par l'agent
# ---------------------------------------------------------------------------

def _dashboard() -> dict:
    ns = '{namespace=~"$namespace"}'
    prom = {"type": "prometheus", "uid": "prometheus"}

    def ts(title, expr, legend, unit, x, y, w=12, h=8):
        return {"type": "timeseries", "title": title, "datasource": prom, "gridPos": {"x": x, "y": y, "w": w, "h": h},
                "fieldConfig": {"defaults": {"unit": unit}}, "targets": [{"expr": expr, "legendFormat": legend, "refId": "A"}]}

    return {
        "uid": "devops-agent-apps", "title": "Applications déployées par l'agent", "tags": ["devops-agent"],
        "timezone": "browser", "refresh": "10s", "time": {"from": "now-30m", "to": "now"}, "schemaVersion": 39,
        "templating": {"list": [{
            "name": "namespace", "label": "Application (namespace)", "type": "query", "datasource": prom,
            "query": {"query": 'label_values(kube_namespace_labels{label_devops_agent_deploy!=""}, namespace)', "refId": "ns"},
            "definition": 'label_values(kube_namespace_labels{label_devops_agent_deploy!=""}, namespace)',
            "includeAll": True, "multi": True, "refresh": 2}]},
        "panels": [
            {"type": "stat", "title": "Pods prêts", "datasource": prom, "gridPos": {"x": 0, "y": 0, "w": 6, "h": 5},
             "targets": [{"expr": f'sum(kube_pod_status_ready{{condition="true",namespace=~"$namespace"}})', "refId": "A"}]},
            {"type": "stat", "title": "Pods non prêts", "datasource": prom, "gridPos": {"x": 6, "y": 0, "w": 6, "h": 5},
             "fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}]}}},
             "targets": [{"expr": f'sum(kube_pod_status_ready{{condition="false",namespace=~"$namespace"}}) or vector(0)', "refId": "A"}]},
            {"type": "stat", "title": "Redémarrages (1 h)", "datasource": prom, "gridPos": {"x": 12, "y": 0, "w": 6, "h": 5},
             "fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "orange", "value": 1}]}}},
             "targets": [{"expr": f'sum(increase(kube_pod_container_status_restarts_total{ns}[1h])) or vector(0)', "refId": "A"}]},
            {"type": "stat", "title": "Alertes actives", "datasource": prom, "gridPos": {"x": 18, "y": 0, "w": 6, "h": 5},
             "targets": [{"expr": f'count(ALERTS{{alertstate="firing",namespace=~"$namespace"}}) or vector(0)', "refId": "A"}]},
            ts("CPU par pod", f'sum by (pod) (rate(container_cpu_usage_seconds_total{{container!="",namespace=~"$namespace"}}[2m]))', "{{pod}}", "cores", 0, 5),
            ts("Mémoire par pod", f'sum by (pod) (container_memory_working_set_bytes{{container!="",namespace=~"$namespace"}})', "{{pod}}", "bytes", 12, 5),
            ts("Trafic réseau reçu", f'sum by (pod) (rate(container_network_receive_bytes_total{ns}[2m]))', "{{pod}}", "Bps", 0, 13),
            ts("Redémarrages par conteneur", f'sum by (pod, container) (kube_pod_container_status_restarts_total{ns})', "{{pod}}/{{container}}", "short", 12, 13),
            {"type": "logs", "title": "Logs de l'application", "datasource": {"type": "loki", "uid": "loki"},
             "gridPos": {"x": 0, "y": 21, "w": 24, "h": 12},
             "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"},
             "targets": [{"expr": '{namespace=~"$namespace"}', "refId": "A"}]},
        ],
    }


def _dashboard_configmap() -> str:
    cm = {"apiVersion": "v1", "kind": "ConfigMap",
          "metadata": {"name": "devops-agent-apps-dashboard", "namespace": "monitoring",
                       "labels": {"grafana_dashboard": "1", "app.kubernetes.io/managed-by": "devops-agent"}},
          "data": {"devops-agent-apps.json": json.dumps(_dashboard(), ensure_ascii=False)}}
    return yaml.safe_dump(cm, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# Installation, état, désinstallation
# ---------------------------------------------------------------------------

def _pods_ready(kube: Kube, ns: str, instance: str | None = None) -> tuple[int, int, list[dict]]:
    args = ["get", "pods", "-n", ns] + (["-l", f"app.kubernetes.io/instance={instance}"] if instance else [])
    try:
        items = kube.json(*args, timeout=30)["items"]
    except DeployError:
        return 0, 0, []
    pods = []
    for p in items:
        cs = (p.get("status") or {}).get("containerStatuses") or []
        ready = bool(cs) and all(c.get("ready") for c in cs)
        if (p.get("status") or {}).get("phase") == "Succeeded":
            continue
        reason = next((((c.get("state") or {}).get("waiting") or {}).get("reason") for c in cs
                       if ((c.get("state") or {}).get("waiting") or {}).get("reason")), "")
        pods.append({"name": p["metadata"]["name"], "ready": ready, "phase": (p.get("status") or {}).get("phase", "?"), "reason": reason})
    return sum(1 for p in pods if p["ready"]), len(pods), pods


def install_platform(components: list[str] | None = None, progress: ProgressCb | None = None,
                     snapshot: Callable[[dict], None] | None = None, kind: str = "desktop") -> dict:
    def say(step: str, msg: str) -> None:
        if progress:
            progress(step, msg)

    helm = _helm_exe()
    wanted = [r for r in releases_for(kind) if not components or r["id"] in components]
    st = _state(kind)
    st.setdefault("grafana_password", "grafana-" + secrets.token_urlsafe(12))
    with tempfile.TemporaryDirectory() as tmp:
        say("prereq", "connexion au cluster …")
        kube, version = _setup_kube(Path(tmp), kind)
        cfg = str(Path(tmp) / "kubeconfig")
        if kind == "desktop" and "web_port" not in st:
            st["web_port"] = 80 if _port_free(80) else 18000
        say("prereq", f"cluster {clusters.KINDS[kind]['label']} · Kubernetes {version} · entrée publiée sur le port {web_port(kind, st)}")
        _save_state(st, kind)
        status = {r["id"]: {"state": "en attente", "ready": 0, "total": 0} for r in wanted}

        def push() -> None:
            if snapshot:
                snapshot({"components": status})

        push()
        for r in wanted:
            values = Path(tmp) / f"{r['release']}.yaml"
            values.write_text(yaml.safe_dump(r["values"](st), sort_keys=False, allow_unicode=True), encoding="utf-8")
            args = [helm, "upgrade", "--install", r["release"], r["chart"], "--repo", r["repo"], "--version", r["version"],
                    "--namespace", r["namespace"], "--create-namespace", "--kubeconfig", cfg, "-f", str(values),
                    "--wait", "--timeout", HELM_TIMEOUT]
            say(r["id"], f"{r['label']} : installation de {r['chart']} {r['version']} …")
            status[r["id"]]["state"] = "installation"
            push()
            t0 = time.time()
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
            last = ""
            cancel.register(proc)           # « Arrêter » tue aussi l'installation Helm en cours
            while proc.poll() is None:
                cancel.check()
                ready, total, _ = _pods_ready(kube, r["namespace"], r["release"] if r.get("shared_namespace") else None)
                status[r["id"]].update(ready=ready, total=total)
                msg = f"{r['label']} : {ready}/{total} pod(s) prêt(s) · {time.time() - t0:.0f} s"
                if msg.split(" · ")[0] != last:
                    say(r["id"], msg)
                    last = msg.split(" · ")[0]
                push()
                time.sleep(4)
            out, err = proc.communicate()
            if proc.returncode != 0:
                status[r["id"]]["state"] = "échec"
                push()
                raise DeployError(f"{r['label']} ({r['chart']}) : {err.strip()[-800:] or out.strip()[-400:]}")
            ready, total, _ = _pods_ready(kube, r["namespace"], r["release"] if r.get("shared_namespace") else None)
            status[r["id"]].update(state="prêt", ready=ready, total=total)
            push()
            say(r["id"], f"{r['label']} prêt en {time.time() - t0:.0f} s ({ready}/{total} pods)")

        if any(r["id"] == "metrics" for r in wanted):
            kube.run("apply", "-f", "-", input=_dashboard_configmap(), check=True)
            say("dashboard", "tableau de bord « Applications déployées par l'agent » ajouté à Grafana")
        st["installed"] = sorted(set(st.get("installed", [])) | {r["id"] for r in wanted})
        st["installed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        _save_state(st, kind)
    return platform_status(kind)


def platform_status(kind: str = "desktop") -> dict:
    """État de la plateforme : composants, pods prêts, adresses. Ne lève pas si le cluster est arrêté."""
    st = _state(kind)
    port = web_port(kind, st)
    suffix = "" if port == 80 else f":{port}"
    out = {"kind": kind, "installed": st.get("installed", []), "web_port": port, "grafana_user": "admin",
           "grafana_password": st.get("grafana_password"), "cluster": None, "components": [],
           "urls": {"grafana": f"http://grafana.localhost{suffix}", "prometheus": f"http://prometheus.localhost{suffix}",
                    "alertmanager": f"http://alertmanager.localhost{suffix}"}}
    try:
        helm = _helm_exe()
    except DeployError as e:
        out["error"] = str(e)
        return out
    with tempfile.TemporaryDirectory() as tmp:
        try:
            kube, version = _setup_kube(Path(tmp), kind)
        except DeployError as e:
            out["error"] = str(e)
            return out
        out["cluster"] = version
        proc = subprocess.run([helm, "list", "-A", "-o", "json", "--kubeconfig", str(Path(tmp) / "kubeconfig")],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        releases = {r["name"]: r for r in json.loads(proc.stdout or "[]")} if proc.returncode == 0 else {}
        for r in releases_for(kind):
            rel = releases.get(r["release"])
            ready, total, pods = _pods_ready(kube, r["namespace"], r["release"] if r.get("shared_namespace") else None) if rel else (0, 0, [])
            out["components"].append({"id": r["id"], "label": r["label"], "detail": r["detail"], "chart": f"{r['chart']} {r['version']}",
                                      "namespace": r["namespace"], "installed": bool(rel), "status": rel.get("status") if rel else None,
                                      "ready": ready, "total": total, "pods": pods})
    return out


def uninstall_platform(progress: ProgressCb | None = None, kind: str = "desktop") -> dict:
    helm = _helm_exe()
    with tempfile.TemporaryDirectory() as tmp:
        kube, _ = _setup_kube(Path(tmp), kind)
        cfg = str(Path(tmp) / "kubeconfig")
        kube.run("delete", "configmap", "devops-agent-apps-dashboard", "-n", "monitoring", "--ignore-not-found")
        for r in reversed(releases_for(kind)):
            if progress:
                progress(r["id"], f"désinstallation de {r['release']} …")
            subprocess.run([helm, "uninstall", r["release"], "-n", r["namespace"], "--kubeconfig", cfg, "--ignore-not-found"],
                           capture_output=True, text=True, timeout=300)
        for ns in ("monitoring", "logging") + (("traefik",) if kind == "desktop" else ()):
            kube.run("delete", "namespace", ns, "--wait=false", "--ignore-not-found")
    st = _state(kind)
    st["installed"] = []
    _save_state(st, kind)
    return {"removed": True}


def ingress_available(kube: Kube) -> bool:
    """La classe d'Ingress Traefik existe-t-elle ? (les applis sont alors publiées sur <ns>.localhost)"""
    return kube.run("get", "ingressclass", "traefik").returncode == 0
