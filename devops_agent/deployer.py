"""Étape 3 — Déploiement local : la version corrigée tourne sur le Kubernetes de Docker Desktop.

    1. PRÉREQUIS   kubectl + cluster joignable, Docker joignable
    2. SOURCES     la branche de remédiation, extraite telle quelle (git archive) — le clone n'est pas modifié
    3. IMAGES      docker build de chaque image du projet (commandes lues dans le pipeline CI généré),
                   puis import dans le containerd du nœud (le mode kind de Docker Desktop ne voit pas
                   les images locales)
    4. MANIFESTS   kubectl kustomize, puis adaptation au local : images locales, secrets générés,
                   StorageClass par défaut, Service LoadBalancer d'entrée sur localhost
    5. DÉPLOIEMENT kubectl apply, puis surveillance des pods (échec détecté tôt : CrashLoop, ImagePull…)
    6. RÉPARATION  si la construction, le rendu ou le démarrage échoue : diagnostic (pods, événements,
                   journaux) -> le modèle corrige les fichiers -> commit sur la branche -> on recommence
    7. ACCÈS       http://localhost:<port>

Tout ce qui est créé est étiqueté `devops-agent/deploy=<projet>` ; `undeploy()` le retire.
"""

from __future__ import annotations

import base64
import collections
import io
import json
import os
import posixpath
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import cancel, clusters
from .fixer import FIX_SYSTEM, _safe_rel_path
from .llm import LLMError, make_backend
from .providers import DATA_DIR
from .source import _rmtree, resolve

ProgressCb = Callable[[str, str], None]

IN_DOCKER = os.path.exists("/.dockerenv") or os.environ.get("AGENT_IN_DOCKER") == "1"
SYSTEM_NS = "devops-agent-system"
LABEL = "devops-agent/deploy"
PORT_RANGE = range(18080, 18200)
ROLLOUT_TIMEOUT = 300
MAX_REPAIRS = 3
REPAIR_TIMEOUT = 420          # un modèle gratuit peut être lent : on borne chaque réparation
WORKLOADS = ("Deployment", "StatefulSet", "DaemonSet")
CLUSTER_SCOPED = {"Namespace", "ClusterRole", "ClusterRoleBinding", "PersistentVolume", "StorageClass",
                  "IngressClass", "CustomResourceDefinition", "PriorityClass", "APIService",
                  "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}
FAIL_REASONS = {"CrashLoopBackOff", "ErrImagePull", "ImagePullBackOff", "ErrImageNeverPull", "InvalidImageName",
                "CreateContainerConfigError", "CreateContainerError", "RunContainerError"}
PLACEHOLDER = re.compile(r"change[_-]?me|replace[_-]?me|<[^>]*>|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|^your[_-]|^x{3,}$|^$", re.I)
CI_FILES = (".gitlab-ci.yml", "gitlab-ci.yml", ".github/workflows/*.yml", ".github/workflows/*.yaml", "Jenkinsfile")
WORKSPACE_VAR = re.compile(r"^\$\{?(CI_PROJECT_DIR|GITHUB_WORKSPACE|WORKSPACE|PWD)\}?/?")


class DeployError(RuntimeError):
    """Prérequis manquant ou état incohérent : rien à réparer côté dépôt."""


class _Failure(Exception):
    """Échec réparable par le modèle : construction, manifests ou démarrage."""

    def __init__(self, stage: str, diagnostic: str, build: "Build | None" = None):
        super().__init__(diagnostic[:300])
        self.stage, self.diagnostic, self.build = stage, diagnostic, build


# ---------------------------------------------------------------------------
# Outils : processus, kubectl, git
# ---------------------------------------------------------------------------

def _run(args: list[str], *, input=None, timeout: int = 120, binary: bool = False, cwd=None) -> subprocess.CompletedProcess:
    kw: dict = {"capture_output": True, "timeout": timeout, "cwd": cwd, "input": input}
    if not binary:
        kw.update(text=True, encoding="utf-8", errors="replace")
    try:
        return subprocess.run(args, **kw)
    except subprocess.TimeoutExpired as e:
        raise DeployError(f"{args[0]} {' '.join(args[1:3])} : délai dépassé ({timeout} s)") from e
    except FileNotFoundError as e:
        raise DeployError(f"commande introuvable : {args[0]}") from e


class Kube:
    def __init__(self, kubeconfig: str):
        self.base = ["kubectl", "--kubeconfig", kubeconfig]

    TRANSIENT = ("Unable to connect to the server", "connection refused", "TLS handshake timeout", "i/o timeout",
                 "failed to respond", "connection reset", "EOF", "the server is currently unable to handle the request",
                 "Unauthorized")

    def run(self, *args: str, input=None, timeout: int = 120, check: bool = False, binary: bool = False):
        # Une API Kubernetes locale chargée a des coupures passagères : on retente avant de conclure à un échec.
        for attempt in range(4):
            proc = _run(self.base + list(args), input=input, timeout=timeout, binary=binary)
            err = proc.stderr if isinstance(proc.stderr, str) else (proc.stderr or b"").decode("utf-8", "replace")
            if proc.returncode == 0 or attempt == 3 or not any(t in err for t in self.TRANSIENT):
                break
            time.sleep(5 * (attempt + 1))
        if check and proc.returncode != 0:
            err = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode("utf-8", "replace")
            raise DeployError(f"kubectl {' '.join(args[:3])} : {err.strip()[-600:]}")
        return proc

    def json(self, *args: str, timeout: int = 60) -> dict:
        return json.loads(self.run(*args, "-o", "json", timeout=timeout, check=True).stdout)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    proc = _run(["git", "-c", "user.name=devops-agent", "-c", "user.email=devops-agent@local", "-C", str(repo), *args])
    if check and proc.returncode != 0:
        raise DeployError(f"git {' '.join(args[:2])} : {proc.stderr.strip()[-400:]}")
    return proc


def _slug(name: str, maxlen: int = 40) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return s[:maxlen].strip("-") or "app"


def _setup_kube(tmp: Path, kind: str = "desktop", quick: bool = False) -> tuple[Kube, str]:
    """Prépare un kubeconfig autonome pour le cluster visé ; dans Docker, réécrit l'adresse de l'API."""
    if not shutil.which("kubectl"):
        raise DeployError("kubectl est introuvable.")
    if kind not in clusters.KINDS:
        raise DeployError(f"Cluster inconnu : {kind}")
    src = clusters.kubeconfig_path(kind)
    if not src:
        raise DeployError("Le cluster k3s n'existe pas encore : il est créé au premier déploiement sur k3s." if kind == "k3s" else
                          "Aucun kubeconfig trouvé : activez Kubernetes dans Docker Desktop "
                          "(Settings → Kubernetes → Enable Kubernetes), puis relancez.")
    context = os.environ.get("KUBE_CONTEXT") if kind == "desktop" else None
    args = ["kubectl", "--kubeconfig", src, "config", "view", "--raw", "--minify", "--flatten"]
    if context:
        args += ["--context", context]
    proc = _run(args)
    if proc.returncode != 0 or "server:" not in proc.stdout:
        raise DeployError(f"kubeconfig inutilisable ({src}) : {proc.stderr.strip()[-300:] or 'aucun contexte courant'}")
    cfg = tmp / "kubeconfig"
    cfg.write_text(proc.stdout, encoding="utf-8")
    kube = Kube(str(cfg))
    server = kube.run("config", "view", "-o", "jsonpath={.clusters[0].cluster.server}").stdout.strip()
    cluster = kube.run("config", "view", "-o", "jsonpath={.clusters[0].name}").stdout.strip()
    u = urlparse(server)
    if u.hostname in ("127.0.0.1", "localhost", "kubernetes.docker.internal", "0.0.0.0") and (IN_DOCKER or u.hostname == "0.0.0.0"):
        # L'API écoute sur la boucle locale de la machine : depuis le conteneur, c'est host.docker.internal.
        # Le certificat ne couvre pas forcément ce nom : on présente localhost (toujours dans ses noms).
        host = "host.docker.internal" if IN_DOCKER else "127.0.0.1"
        tls = "localhost" if u.hostname in ("127.0.0.1", "0.0.0.0") else u.hostname
        kube.run("config", "set-cluster", cluster, f"--server=https://{host}:{u.port or 443}",
                 f"--tls-server-name={tls}", check=True)
    if quick:
        ver = _run(kube.base + ["version", "-o", "json", "--request-timeout=5s"], timeout=12)
    else:
        ver = kube.run("version", "-o", "json", timeout=25)
    if ver.returncode != 0:
        hint = ("Le cluster k3s ne répond pas : relancez un déploiement sur k3s pour le redémarrer." if kind == "k3s" else
                "Le cluster Kubernetes ne répond pas. Vérifiez dans Docker Desktop que Kubernetes est démarré (voyant vert).")
        raise DeployError(f"{hint} Détail : {ver.stderr.strip()[-300:]}")
    try:
        server_version = json.loads(ver.stdout).get("serverVersion", {}).get("gitVersion", "?")
    except json.JSONDecodeError:
        server_version = "?"
    return kube, server_version


def _check_docker() -> None:
    if not shutil.which("docker"):
        raise DeployError("docker est introuvable.")
    proc = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=30)
    if proc.returncode != 0:
        hint = " Le conteneur de l'agent doit monter /var/run/docker.sock (voir docker-compose.yml)." if IN_DOCKER else ""
        raise DeployError(f"Docker ne répond pas : {proc.stderr.strip()[-300:]}.{hint}")


# ---------------------------------------------------------------------------
# Sources et images
# ---------------------------------------------------------------------------

@dataclass
class Build:
    component: str
    dockerfile: str
    context: str
    origin: str
    image: str = ""
    status: str = "en attente"
    seconds: float = 0.0


def _extract(repo: Path, ref: str, dest: Path) -> None:
    if dest.exists():
        _rmtree(dest)
    dest.mkdir(parents=True)
    proc = _run(["git", "-C", str(repo), "archive", "--format=tar", ref], binary=True, timeout=180)
    if proc.returncode != 0:
        raise DeployError(f"git archive {ref} : {proc.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tf:
        try:
            tf.extractall(dest, filter="data")
        except TypeError:          # Python sans filtre d'extraction
            tf.extractall(dest)


def _component_from_ref(ref: str) -> str | None:
    """« $REGISTRY/frontend:$SHA » -> « frontend » ; « postgres:17 » -> « postgres » ; variables seules -> None."""
    ref = ref.split("@", 1)[0]
    last = ref.rstrip("/").split("/")[-1]
    if ":" in last:
        last = last.rsplit(":", 1)[0]
    last = re.sub(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", "", last)
    last = re.sub(r"[^A-Za-z0-9_.-]", "", last).lower()
    return last or None


def _component_from_dockerfile(df: str) -> str | None:
    p = Path(df)
    if p.name.lower().startswith("dockerfile.") and len(p.name) > len("dockerfile."):
        return p.name.split(".", 1)[1].lower()
    if str(p.parent) not in ("", "."):
        return p.parent.name.lower()
    return None


def _inside(src: Path, rel: str) -> Path | None:
    target = (src / rel).resolve()
    try:
        target.relative_to(src.resolve())
    except ValueError:
        return None
    return target


def _clean_path(p: str) -> str | None:
    p = WORKSPACE_VAR.sub("", p.strip().strip("\"'")) or "."
    return None if "$" in p else p


def detect_builds(src: Path, project: str) -> list[Build]:
    """Les images du projet : d'abord les commandes du pipeline CI, sinon les conventions de nommage."""
    found: list[Build] = []

    def add(dockerfile: str, context: str, tags: list[str], origin: str) -> None:
        dockerfile, context = _clean_path(dockerfile), _clean_path(context)
        if not dockerfile or not context:
            return
        if not Path(dockerfile).is_absolute() and _inside(src, context) and not (src / dockerfile).is_file():
            nested = str(Path(context) / dockerfile)          # -f relatif au contexte (kaniko)
            dockerfile = nested if (src / nested).is_file() else dockerfile
        df, ctx = _inside(src, dockerfile), _inside(src, context)
        if not df or not ctx or not df.is_file() or not ctx.is_dir():
            return
        rel_df, rel_ctx = df.relative_to(src.resolve()).as_posix(), ctx.relative_to(src.resolve()).as_posix() or "."
        if any(b.dockerfile == rel_df and b.context == rel_ctx for b in found):
            return
        comp = next((c for c in (_component_from_ref(t) for t in tags) if c), None) \
            or _component_from_dockerfile(rel_df) or _slug(project, 20)
        while any(b.component == comp for b in found):
            comp += "-2"
        found.append(Build(component=comp, dockerfile=rel_df, context=rel_ctx, origin=origin))

    for pattern in CI_FILES:
        for ci in sorted(src.glob(pattern)):
            text = re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", ci.read_text(encoding="utf-8", errors="replace"))
            for line in text.splitlines():
                m = re.search(r"\bdocker\s+(?:buildx\s+)?build\b(.*)", line)
                k = re.search(r"kaniko/executor\b(.*)", line)
                if not (m or k):
                    continue
                try:
                    tokens = shlex.split((m or k).group(1))
                except ValueError:
                    continue
                dockerfile, context, tags, positional = "Dockerfile", ".", [], []
                i = 0
                while i < len(tokens):
                    t = tokens[i]
                    if t in ("&&", "||", ";", "|"):
                        break
                    val = tokens[i + 1] if i + 1 < len(tokens) else ""
                    if t in ("-f", "--file", "--dockerfile"):
                        dockerfile, i = val, i + 2
                        continue
                    if t in ("-t", "--tag", "--destination"):
                        tags.append(val)
                        i += 2
                        continue
                    if t == "--context":
                        context, i = val, i + 2
                        continue
                    if t.startswith(("--file=", "--dockerfile=")):
                        dockerfile = t.split("=", 1)[1]
                    elif t.startswith(("--tag=", "--destination=")):
                        tags.append(t.split("=", 1)[1])
                    elif t.startswith("--context="):
                        context = t.split("=", 1)[1]
                    elif t in ("--build-arg", "--target", "--platform", "--label", "--cache-from", "--network",
                               "--secret", "--ssh", "--progress", "-o", "--output", "--cache-to"):
                        i += 2
                        continue
                    elif not t.startswith("-"):
                        positional.append(t)
                    i += 1
                if m and positional:
                    context = positional[-1]
                add(dockerfile, context, tags, f"pipeline ({ci.relative_to(src).as_posix()})")

    if not found:
        for df in sorted(src.glob("Dockerfile.*")):
            if not df.name.endswith((".dockerignore", ".md")):
                add(df.name, ".", [], "convention (Dockerfile.<nom>)")
        for df in sorted(src.glob("*/Dockerfile")):
            add(df.relative_to(src).as_posix(), df.parent.name, [], "convention (<nom>/Dockerfile)")
        if (src / "Dockerfile").is_file():
            add("Dockerfile", ".", [], "convention (Dockerfile)")
    return found


def _docker_build(src: Path, b: Build, say: ProgressCb) -> None:
    args = ["docker", "build", "--progress=plain", "-f", str(src / b.dockerfile), "-t", b.image, str(src / b.context)]
    t0, last = time.time(), 0.0
    tail: collections.deque[str] = collections.deque(maxlen=45)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", cwd=src)
    assert proc.stdout is not None
    for line in proc.stdout:
        tail.append(line.rstrip())
        m = re.match(r"#\d+ \[(?:[\w.-]+ )?(\d+/\d+)\] (.+)", line)
        if m and time.time() - last > 2:
            say("build", f"{b.component} · étape {m.group(1)} : {m.group(2).strip()[:90]}")
            last = time.time()
    rc = proc.wait(timeout=1800)
    b.seconds = round(time.time() - t0, 1)
    if rc != 0:
        b.status = "échec"
        raise _Failure("build", f"La construction de l'image « {b.component} » ({b.dockerfile}, contexte {b.context}) "
                                f"a échoué. Fin du journal docker build :\n" + "\n".join(tail), b)
    b.status = "construite"


LOADER = f"""apiVersion: v1
kind: Namespace
metadata: {{name: {SYSTEM_NS}, labels: {{app.kubernetes.io/managed-by: devops-agent}}}}
---
apiVersion: apps/v1
kind: DaemonSet
metadata: {{name: image-loader, namespace: {SYSTEM_NS}}}
spec:
  selector: {{matchLabels: {{app: devops-agent-image-loader}}}}
  template:
    metadata: {{labels: {{app: devops-agent-image-loader}}}}
    spec:
      tolerations: [{{operator: Exists}}]
      containers:
      - name: loader
        image: alpine:3.20
        command: ["sleep", "infinity"]
        securityContext: {{privileged: true}}
        volumeMounts: [{{name: host, mountPath: /host}}]
      volumes: [{{name: host, hostPath: {{path: /}}}}]
"""


def _load_images(kube: Kube, builds: list[Build], say: ProgressCb) -> None:
    """Importe les images dans le containerd de chaque nœud (Docker Desktop en mode kind)."""
    kube.run("apply", "-f", "-", input=LOADER, check=True)
    ro = kube.run("rollout", "status", "daemonset/image-loader", "-n", SYSTEM_NS, "--timeout=150s", timeout=170)
    if ro.returncode != 0:
        raise DeployError(f"Le chargeur d'images ne démarre pas : {ro.stderr.strip()[-300:]}")
    pods = [p["metadata"]["name"] for p in kube.json("get", "pods", "-n", SYSTEM_NS, "-l", "app=devops-agent-image-loader")["items"]
            if p.get("status", {}).get("phase") == "Running"]
    if not pods:
        raise DeployError("Aucun chargeur d'images en marche dans le cluster.")
    for b in builds:
        for pod in pods:
            say("load", f"import de {b.image} dans le nœud ({pod})")
            save = subprocess.Popen(["docker", "save", b.image], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            imp = subprocess.run(kube.base + ["exec", "-i", "-n", SYSTEM_NS, pod, "--", "chroot", "/host",
                                              "ctr", "-n", "k8s.io", "images", "import", "-"],
                                 stdin=save.stdout, capture_output=True, timeout=900)
            save.wait(timeout=60)
            if save.returncode != 0 or imp.returncode != 0:
                err = (imp.stderr or b"").decode("utf-8", "replace") + (save.stderr.read() if save.stderr else b"").decode("utf-8", "replace")
                raise DeployError(f"Import de {b.image} dans le nœud impossible : {err.strip()[-400:]}")
        b.status = "chargée dans le cluster"


# ---------------------------------------------------------------------------
# Manifests
# ---------------------------------------------------------------------------

def find_manifests(src: Path) -> tuple[Path | None, bool]:
    """(dossier, est_une_kustomization). Préfère une overlay locale/dev, puis la plus proche de la racine."""
    ks = [p for p in src.rglob("kustomization.y*ml") if len(p.relative_to(src).parts) <= 4]
    if ks:
        def rank(p: Path):
            s = p.relative_to(src).as_posix().lower()
            return (0 if "local" in s else 1 if "dev" in s else 2, len(p.parts), s)
        return sorted(ks, key=rank)[0].parent, True
    for name in ("k8s", "kubernetes", "manifests", "deploy", ".k8s", "kube"):
        d = src / name
        if d.is_dir() and any(d.rglob("*.y*ml")):
            if any(d.rglob("Chart.yaml")):
                raise DeployError(f"{name}/ contient un chart Helm : le déploiement local ne gère que les manifests et Kustomize.")
            return d, False
    return None, False


def _render(kube: Kube, mdir: Path, is_kustomize: bool) -> list[dict]:
    import yaml
    if is_kustomize:
        proc = kube.run("kustomize", str(mdir), "--load-restrictor", "LoadRestrictionsNone", timeout=120)
        if proc.returncode != 0:
            errs = "\n".join(l for l in proc.stderr.splitlines() if "warning" not in l.lower())
            raise _Failure("manifests", f"`kubectl kustomize {mdir.name}` a échoué :\n{errs.strip()[-2500:]}")
        text = proc.stdout
    else:
        text = "\n---\n".join(p.read_text(encoding="utf-8", errors="replace") for p in sorted(mdir.rglob("*.y*ml")))
    try:
        return [d for d in yaml.safe_load_all(text) if isinstance(d, dict) and d.get("kind")]
    except yaml.YAMLError as e:
        raise _Failure("manifests", f"YAML invalide dans les manifests : {e}") from e


def _pod_specs(d: dict) -> list[dict]:
    kind, spec = d.get("kind"), d.get("spec") or {}
    if kind == "Pod":
        return [spec]
    if kind == "CronJob":
        return [((spec.get("jobTemplate") or {}).get("spec") or {}).get("template", {}).get("spec") or {}]
    if kind in WORKLOADS + ("ReplicaSet", "Job"):
        return [(spec.get("template") or {}).get("spec") or {}]
    return []


def _containers(ps: dict) -> list[dict]:
    return list(ps.get("containers") or []) + list(ps.get("initContainers") or [])


def _gen_secret() -> str:
    return "local-" + secrets.token_urlsafe(18)


def adapt(docs: list[dict], builds: list[Build], slug: str, port: int, saved_secrets: dict,
          ingress_class: str | None = None) -> tuple[list[dict], dict]:
    """Rend les manifests déployables en local. Retourne (documents, informations)."""
    comp = {b.component: b.image for b in builds}
    ns_obj = next((d["metadata"]["name"] for d in docs if d["kind"] == "Namespace" and d.get("metadata", {}).get("name")), None)
    ns_seen = collections.Counter(d.get("metadata", {}).get("namespace") for d in docs if d.get("metadata", {}).get("namespace"))
    namespace = ns_obj or (ns_seen.most_common(1)[0][0] if ns_seen else slug)
    info: dict = {"namespace": namespace, "unresolved_images": [], "credentials": {}, "warnings": [], "entry": None}

    for d in docs:
        md = d.setdefault("metadata", {})
        md.setdefault("labels", {})[LABEL] = slug
        if d["kind"] not in CLUSTER_SCOPED:
            md.setdefault("namespace", namespace)
        for ps in _pod_specs(d):
            for c in _containers(ps):
                img = str(c.get("image", ""))
                name = _component_from_ref(img)
                if name in comp:
                    c["image"], c["imagePullPolicy"] = comp[name], "Never"
                elif "$" in img or not img:
                    info["unresolved_images"].append(f"{d['kind']}/{md.get('name')} : {img or '(vide)'}")
        if d["kind"] == "PersistentVolumeClaim":
            (d.get("spec") or {}).pop("storageClassName", None)       # StorageClass par défaut du cluster
        if d["kind"] == "Service" and (d.get("spec") or {}).get("type") == "LoadBalancer":
            d["spec"]["type"] = "ClusterIP"                           # une seule entrée sur localhost : la nôtre
            for p in d["spec"].get("ports") or []:
                p.pop("nodePort", None)
        if d["kind"] == "Ingress":
            # En CI, l'hôte et le certificat sont injectés (sed, variables) ; en local : <namespace>.localhost, sans TLS.
            spec = d.setdefault("spec", {})
            local_host = f"{namespace}.localhost"
            for rule in spec.get("rules") or []:
                host = str(rule.get("host") or "")
                if ingress_class or not host or "$" in host or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", host):
                    rule["host"] = local_host
            if spec.pop("tls", None) is not None:
                info["warnings"].append(f"Ingress « {md.get('name')} » : TLS retiré en local (pas de certificat).")
            if ingress_class:
                spec["ingressClassName"] = ingress_class
                ann = md.get("annotations") or {}
                for k in [k for k in ann if k.startswith(("kubernetes.io/ingress.class", "nginx.ingress.kubernetes.io/", "cert-manager.io/"))]:
                    ann.pop(k)
            if spec.get("rules"):
                info["ingress_host"] = local_host

    # Secrets : valeurs d'exemple -> valeurs générées, conservées d'un déploiement à l'autre.
    secrets_by_name = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Secret"}
    for sname, sd in secrets_by_name.items():
        keep = saved_secrets.setdefault(sname, {})
        for key, val in list((sd.get("stringData") or {}).items()):
            if PLACEHOLDER.search(str(val or "")):
                sd["stringData"][key] = keep.setdefault(key, _gen_secret())
                info["credentials"].setdefault(sname, {})[key] = sd["stringData"][key]
        for key, val in list((sd.get("data") or {}).items()):
            try:
                plain = base64.b64decode(str(val or "")).decode("utf-8", "replace")
            except (ValueError, TypeError):
                plain = ""
            if PLACEHOLDER.search(plain):
                new = keep.setdefault(key, _gen_secret())
                sd["data"][key] = base64.b64encode(new.encode()).decode()
                info["credentials"].setdefault(sname, {})[key] = new

    # Secrets référencés par les pods mais absents des manifests (créés à la main en CI) : on les génère.
    needed: dict[str, set] = collections.defaultdict(set)
    for d in docs:
        for ps in _pod_specs(d):
            for c in _containers(ps):
                for env in c.get("env") or []:
                    ref = ((env.get("valueFrom") or {}).get("secretKeyRef") or {})
                    if ref.get("name") and ref.get("key") and not ref.get("optional"):
                        needed[ref["name"]].add(ref["key"])
                for ef in c.get("envFrom") or []:
                    ref = ef.get("secretRef") or {}
                    if ref.get("name") and not ref.get("optional"):
                        needed.setdefault(ref["name"], set())
            for vol in ps.get("volumes") or []:
                sref = vol.get("secret") or {}
                if sref.get("secretName") and not sref.get("optional"):
                    needed.setdefault(sref["secretName"], set()).update(i.get("key") for i in sref.get("items") or [] if i.get("key"))
    for sname, keys in needed.items():
        keep = saved_secrets.setdefault(sname, {})
        sd = secrets_by_name.get(sname)
        if sd is None:
            sd = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                  "metadata": {"name": sname, "namespace": namespace, "labels": {LABEL: slug}}, "stringData": {}}
            docs.append(sd)
            secrets_by_name[sname] = sd
            info["warnings"].append(f"Secret « {sname} » absent des manifests : généré pour l'environnement local.")
        present = set((sd.get("stringData") or {}).keys()) | set((sd.get("data") or {}).keys())
        for key in sorted(keys - present):
            sd.setdefault("stringData", {})[key] = keep.setdefault(key, _gen_secret())
            info["credentials"].setdefault(sname, {})[key] = sd["stringData"][key]

    # Entrée : le Service visé par l'Ingress (chemin « / » en priorité), sinon un nom évocateur.
    services = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Service"}
    target: tuple[str, object] | None = None
    for ing in (d for d in docs if d["kind"] == "Ingress"):
        paths = []
        for rule in (ing.get("spec") or {}).get("rules") or []:
            for p in ((rule.get("http") or {}).get("paths") or []):
                paths.append(p)
        paths.sort(key=lambda p: 0 if p.get("path") in ("/", None, "") else 1)
        for p in paths:
            svc = ((p.get("backend") or {}).get("service") or {})
            if svc.get("name") in services:
                bport = svc.get("port") or {}      # ne pas écraser `port` : c'est le port local choisi
                target = (svc["name"], bport.get("number") or bport.get("name"))
                break
        if target:
            break
    if not target:
        for name in sorted(services, key=lambda n: (0 if re.search(r"front|web|ui|nginx|gateway|proxy", n) else 1, n)):
            target = (name, None)
            break
    if target:
        svc = services[target[0]]
        ports = (svc.get("spec") or {}).get("ports") or []
        chosen = next((p for p in ports if target[1] in (p.get("port"), p.get("name"))), None) \
            or next((p for p in ports if p.get("port") in (80, 8080, 3000, 8000) or p.get("name") == "http"), None) \
            or (ports[0] if ports else None)
        selector = (svc.get("spec") or {}).get("selector")
        if chosen and selector:
            docs.append({"apiVersion": "v1", "kind": "Service",
                         "metadata": {"name": f"{target[0]}-local"[:63], "namespace": svc["metadata"].get("namespace", namespace),
                                      "labels": {LABEL: slug, "app.kubernetes.io/managed-by": "devops-agent"}},
                         "spec": {"type": "LoadBalancer", "selector": selector,
                                  "ports": [{"name": "http", "protocol": "TCP", "port": port,
                                             "targetPort": chosen.get("targetPort", chosen.get("port"))}]}})
            info["entry"] = {"service": target[0], "port": chosen.get("port"), "local_port": port}
    if not info["entry"]:
        info["warnings"].append("Aucun Service d'entrée identifié : l'application n'est pas publiée sur localhost.")
    return docs, info


def _choose_port(kube: Kube, previous: int | None, kind: str = "desktop") -> int:
    candidates = clusters.K3S_APP_PORTS if kind == "k3s" else clusters.DESKTOP_PORTS
    used = set()
    try:
        for s in kube.json("get", "svc", "-A")["items"]:
            if (s.get("spec") or {}).get("type") == "LoadBalancer" and s["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != "devops-agent":
                used.update(p.get("port") for p in s["spec"].get("ports") or [])
    except DeployError:
        pass
    host = "host.docker.internal" if IN_DOCKER else "127.0.0.1"

    def free(port: int) -> bool:
        with socket.socket() as s:
            s.settimeout(0.4)
            return s.connect_ex((host, port)) != 0

    if previous and previous in candidates and previous not in used:
        return previous                      # même port qu'au déploiement précédent (le nôtre l'occupe peut-être)
    for port in candidates:
        # k3s : l'équilibreur de k3d écoute déjà sur tous ses ports, seul le cluster sait lesquels sont pris
        if port not in used and (kind == "k3s" or free(port)):
            return port
    raise DeployError(f"Aucun port libre entre {candidates[0]} et {candidates[-1]} pour publier l'application.")


# ---------------------------------------------------------------------------
# Déploiement, surveillance, diagnostic
# ---------------------------------------------------------------------------

def _workload_ready(d: dict) -> bool:
    st, spec, md = d.get("status") or {}, d.get("spec") or {}, d.get("metadata") or {}
    if d["kind"] == "DaemonSet":
        want = st.get("desiredNumberScheduled", 0)
        return want > 0 and st.get("numberReady", 0) >= want and st.get("updatedNumberScheduled", 0) >= want
    want = spec.get("replicas", 1)
    if want == 0:
        return True
    return (st.get("observedGeneration", 0) >= md.get("generation", 0)
            and st.get("readyReplicas", 0) >= want and st.get("updatedReplicas", 0) >= want)


def _current_pods(kube: Kube, ns: str, workloads: list[dict]) -> list[dict]:
    """Pods de la révision courante : les pods d'une version précédente qui plantent ne comptent pas."""
    pods = [p for p in kube.json("get", "pods", "-n", ns)["items"] if not p["metadata"].get("deletionTimestamp")]
    dep_rev = {w["metadata"]["name"]: (w["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision")
               for w in workloads if w["kind"] == "Deployment"}
    sts_rev = {w["metadata"]["name"]: (w.get("status") or {}).get("updateRevision") for w in workloads if w["kind"] == "StatefulSet"}
    current_rs = set()
    if dep_rev:
        for rs in kube.json("get", "rs", "-n", ns)["items"]:
            owner = next((o["name"] for o in rs["metadata"].get("ownerReferences") or [] if o["kind"] == "Deployment"), None)
            if owner in dep_rev and (rs["metadata"].get("annotations") or {}).get("deployment.kubernetes.io/revision") == dep_rev[owner]:
                current_rs.add(rs["metadata"]["name"])
    out = []
    for p in pods:
        owner = next(iter(p["metadata"].get("ownerReferences") or []), {})
        if owner.get("kind") == "ReplicaSet" and dep_rev and owner.get("name") not in current_rs:
            continue
        if owner.get("kind") == "StatefulSet" and sts_rev.get(owner.get("name")) and \
                (p["metadata"].get("labels") or {}).get("controller-revision-hash") != sts_rev[owner["name"]]:
            continue
        out.append(p)
    return out


def _pod_summary(p: dict) -> dict:
    st = p.get("status") or {}
    cs = list(st.get("initContainerStatuses") or []) + list(st.get("containerStatuses") or [])
    main = st.get("containerStatuses") or []
    reason, detail = "", ""
    for c in cs:
        w = (c.get("state") or {}).get("waiting") or {}
        t = (c.get("state") or {}).get("terminated") or {}
        lt = (c.get("lastState") or {}).get("terminated") or {}
        if w.get("reason") and w["reason"] not in ("ContainerCreating", "PodInitializing"):
            reason, detail = w["reason"], (w.get("message") or "")[:300]
            if lt:
                detail += f" (dernier arrêt : {lt.get('reason', '?')}, code {lt.get('exitCode', '?')})"
            break
        if t and t.get("exitCode", 0) != 0:
            reason, detail = t.get("reason", "Terminated"), f"code {t.get('exitCode')}"
            break
    if not reason:
        for cond in st.get("conditions") or []:
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                reason, detail = cond.get("reason", "Unschedulable"), (cond.get("message") or "")[:300]
    return {"name": p["metadata"]["name"], "phase": st.get("phase", "?"),
            "ready": f"{sum(1 for c in main if c.get('ready'))}/{len(main) or len((p.get('spec') or {}).get('containers') or [])}",
            "restarts": sum(c.get("restartCount", 0) for c in cs), "reason": reason, "detail": detail}


def _wait_rollout(kube: Kube, ns: str, names: list[tuple[str, str]], say: ProgressCb,
                  on_state: Callable[[list, list], None] | None = None) -> tuple[bool, list[dict], list[dict]]:
    if not names:
        return True, [], []
    started = time.time()
    deadline, last_msg, strikes = started + ROLLOUT_TIMEOUT, "", 0
    kinds = ",".join(sorted({k.lower() for k, _ in names}))
    while True:
        items = [w for w in kube.json("get", kinds, "-n", ns)["items"] if (w["kind"], w["metadata"]["name"]) in names]
        states = [{"kind": w["kind"], "name": w["metadata"]["name"], "ready": _workload_ready(w),
                   "replicas": f"{(w.get('status') or {}).get('readyReplicas', (w.get('status') or {}).get('numberReady', 0)) or 0}/"
                               f"{(w.get('spec') or {}).get('replicas', (w.get('status') or {}).get('desiredNumberScheduled', 1))}"}
                  for w in items]
        pods = [_pod_summary(p) for p in _current_pods(kube, ns, items)]
        bad = [p for p in pods if p["reason"] in FAIL_REASONS and (p["reason"] != "CrashLoopBackOff" or p["restarts"] >= 2)]
        if not bad and time.time() - started > 45:
            # Un pod bloqué en ContainerCreating sur un volume impossible à monter n'a pas de « raison » d'échec :
            # on lit les événements FailedMount récents (postérieurs au début de cette tentative).
            try:
                for ev in kube.json("get", "events", "-n", ns, "--field-selector", "reason=FailedMount")["items"]:
                    ts = ev.get("lastTimestamp") or ev.get("eventTime") or ""
                    when = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else 0
                    if when >= started and _MISSING_REF.search(ev.get("message") or ""):
                        bad = [{"name": (ev.get("involvedObject") or {}).get("name", "?"), "reason": "FailedMount",
                                "restarts": 0, "detail": ev.get("message", "")[:200]}]
                        break
            except (DeployError, ValueError):
                pass
        n_ok = sum(1 for s in states if s["ready"])
        if on_state:
            on_state(states, pods)
        msg = f"{n_ok}/{len(names)} charge(s) prête(s)" + (" · " + ", ".join(f"{p['name']} : {p['reason']}" for p in bad[:3]) if bad else "")
        if msg != last_msg:
            say("rollout", msg)
            last_msg = msg
        if n_ok == len(names):
            return True, states, pods
        strikes = strikes + 1 if bad else 0
        if strikes >= 2 or time.time() > deadline:
            return False, states, pods
        time.sleep(5)


def _apply_monitoring(kube: Kube, src: Path, ns: str, slug: str) -> tuple[bool | None, str]:
    """Observabilité de l'appli (k8s/monitoring/ : PrometheusRule, ServiceMonitor), si Prometheus Operator est là.

    Retourne (None, "") s'il n'y a rien à appliquer, (True, message) si c'est fait, (False, message) sinon.
    """
    import yaml
    dirs = sorted(p.parent for p in src.rglob("kustomization.y*ml") if "monitoring" in p.parent.name.lower())
    if not dirs:
        return None, ""
    if kube.run("get", "crd", "servicemonitors.monitoring.coreos.com").returncode != 0:
        return False, "observabilité de l'appli présente dans la branche, mais Prometheus Operator absent du cluster : non appliquée"
    proc = kube.run("kustomize", str(dirs[0]), "--load-restrictor", "LoadRestrictionsNone")
    if proc.returncode != 0:
        return False, f"{dirs[0].relative_to(src).as_posix()} ne se rend pas : {proc.stderr.strip()[-300:]}"
    docs = [d for d in yaml.safe_load_all(proc.stdout) if isinstance(d, dict) and d.get("kind")]
    for d in docs:
        md = d.setdefault("metadata", {})
        md.setdefault("labels", {})[LABEL] = slug
        if d["kind"] not in CLUSTER_SCOPED:
            md["namespace"] = ns
    ap = kube.run("apply", "-f", "-", input=yaml.safe_dump_all(docs, sort_keys=False))
    if ap.returncode != 0:
        return False, f"observabilité de l'appli refusée par le cluster : {ap.stderr.strip()[-300:]}"
    kinds = collections.Counter(d["kind"] for d in docs)
    return True, "observabilité de l'appli appliquée : " + ", ".join(f"{n} {k}" for k, n in kinds.items())


def _diagnose(kube: Kube, ns: str, states: list[dict], pods: list[dict]) -> str:
    out = ["## Charges de travail"] + [f"- {s['kind']}/{s['name']} : {s['replicas']} prêtes" + ("" if s["ready"] else " — PAS PRÊTE") for s in states]
    out.append("\n## Pods")
    out += [f"- {p['name']} : {p['phase']}, {p['ready']} prêts, {p['restarts']} redémarrage(s)"
            + (f" — {p['reason']} {p['detail']}" if p["reason"] else "") for p in pods] or ["(aucun pod)"]
    ev = kube.run("get", "events", "-n", ns, "--field-selector", "type=Warning", "--sort-by=.lastTimestamp",
                  "-o", "custom-columns=OBJET:.involvedObject.name,RAISON:.reason,MESSAGE:.message", "--no-headers")
    lines = [l for l in ev.stdout.splitlines() if l.strip()][-20:]
    out.append("\n## Événements (avertissements)")
    out += lines or ["(aucun)"]
    out.append("\n## Journaux")
    for p in [p for p in pods if p["reason"] or p["restarts"]][:4]:
        for previous in ((True, False) if p["restarts"] else (False,)):
            args = ["logs", p["name"], "-n", ns, "--all-containers", "--tail=40"] + (["--previous"] if previous else [])
            log = kube.run(*args, timeout=30)
            text = (log.stdout or log.stderr).strip()
            if text:
                out.append(f"### {p['name']}{' (exécution précédente)' if previous else ''}\n{text[-3000:]}")
                break
    return "\n".join(out)[:16000]


# ---------------------------------------------------------------------------
# Réparation par le modèle
# ---------------------------------------------------------------------------

DEPLOY_REPAIR = """## Ta tâche maintenant : RÉPARER LE DÉPLOIEMENT LOCAL
Le dépôt « {project} » (branche corrigée) est déployé sur un cluster Kubernetes local (Docker Desktop), et {stage}.
Voici le diagnostic réel, puis les fichiers concernés. Corrige les fichiers fautifs (Dockerfile, configuration
nginx, manifests Kubernetes, kustomization…) pour que le déploiement réussisse. Ne change que le strict nécessaire
et renvoie chaque fichier modifié en ENTIER.

Ce que l'agent fait lui-même pour le local (ne le « corrige » pas) :
- les images du projet sont construites localement et remplacées à la volée ; elles sont reconnues par le dernier
  segment de leur nom ({components}) : garde ces noms dans les champs image ;
- les secrets marqués CHANGE_ME et les secrets absents sont générés ;
- l'Ingress n'est pas utilisé ; l'accès se fait par un Service LoadBalancer ajouté par l'agent ;
- `kubectl kustomize` est lancé avec --load-restrictor LoadRestrictionsNone (les chemins en ../ sont permis).

## Diagnostic
```
{diagnostic}
```

## Images construites
{builds}

## Fichiers actuels
{files}

Réponds UNIQUEMENT avec cet objet JSON :
{{"explanation": "la cause identifiée et le correctif, en 2-3 phrases", "files": [{{"path": "chemin/relatif", "content": "contenu complet"}}]}}"""


_NOT_FOUND = re.compile(r'"/?([^"]+)": not found')


def _norm(p: str) -> str:
    while p.startswith("./"):
        p = p[2:]
    return p.lstrip("/")


def known_build_fix(src: Path, b: Build, log: str) -> dict[str, str] | None:
    """Correctifs déterministes des erreurs de build les plus courantes, sans appel au modèle.

    Aujourd'hui : un COPY/ADD d'un fichier absent du contexte (« "/backend/package-lock.json": not found »).
    Le fichier est retiré de l'instruction (la ligne disparaît si elle n'a plus de source) ; si c'était un
    lockfile, `npm ci` (qui l'exige) devient `npm install`.
    """
    missing = {_norm(m.group(1)) for m in _NOT_FOUND.finditer(log)}
    df = src / b.dockerfile
    if not missing or not df.is_file():
        return None
    out, removed = [], []
    for line in df.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^(\s*(?:COPY|ADD)\s+)((?:--\S+\s+)*)(.+)$", line, re.I)
        if m and "--from" not in m.group(2):
            try:
                parts = shlex.split(m.group(3))
            except ValueError:
                parts = []
            if len(parts) >= 2:
                sources, dest = parts[:-1], parts[-1]
                keep = [s for s in sources if _norm(s) not in missing]
                if len(keep) != len(sources):
                    removed += [s for s in sources if s not in keep]
                    if not keep:
                        continue
                    line = m.group(1) + m.group(2) + " ".join(keep + [dest])
        out.append(line)
    if not removed:
        return None
    text = "\n".join(out) + "\n"
    if any("lock" in r.lower() for r in removed):
        text = re.sub(r"\bnpm ci\b", "npm install", text)
    return {b.dockerfile: text}


def known_manifest_fix(src: Path, mdir: Path | None) -> tuple[dict[str, str], list[str]] | None:
    """Correctifs déterministes des kustomizations, pour les erreurs fréquentes des modèles.

    - un fichier de configMapGenerator/secretGenerator introuvable depuis le dossier de la kustomization mais
      présent ailleurs dans le dépôt : chemin corrigé (database/init.sql -> ../database/init.sql) ;
    - `disableNameSuffixHash` posé sur un générateur au lieu de ses `options` : déplacé.
    """
    import yaml
    if not mdir:
        return None
    kf = next((mdir / n for n in ("kustomization.yaml", "kustomization.yml") if (mdir / n).is_file()), None)
    if not kf:
        return None
    try:
        doc = yaml.safe_load(kf.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    notes: list[str] = []
    for gen_key in ("configMapGenerator", "secretGenerator"):
        for item in doc.get(gen_key) or []:
            if not isinstance(item, dict):
                continue
            if "disableNameSuffixHash" in item:
                item.setdefault("options", {})["disableNameSuffixHash"] = item.pop("disableNameSuffixHash")
                notes.append(f"{gen_key} « {item.get('name')} » : disableNameSuffixHash déplacé sous options")
            for field_name in ("files", "envs"):
                entries = item.get(field_name) or []
                for i, entry in enumerate(entries):
                    entry = str(entry)
                    key, path = entry.split("=", 1) if "=" in entry else ("", entry)
                    if (mdir / path).exists():
                        continue
                    cand = src / _norm(path)
                    if not cand.is_file():
                        matches = [p for p in src.rglob(Path(path).name) if p.is_file() and ".git" not in p.parts]
                        cand = matches[0] if len(matches) == 1 else None
                    if cand:
                        rel = os.path.relpath(cand, mdir).replace("\\", "/")
                        entries[i] = f"{key}={rel}" if key else rel
                        notes.append(f"{gen_key} « {item.get('name')} » : {path} → {rel}")
    if not notes:
        return None
    return {kf.relative_to(src).as_posix(): yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)}, notes


_RO_PATH = re.compile(r'(?:mkdir|open)\(\) "(/[^"]+)" failed \((?:13: Permission denied|30: Read-only file system)\)'
                      r'|can\'t create (/[^\s:]+): (?:Permission denied|Read-only file system)'
                      r'|(?:EACCES|EROFS)[^\n]*?[\'"](/[^\'"]+)[\'"]')


def known_rollout_fix(src: Path, mdir: Path | None, diagnostic: str) -> tuple[dict[str, str], list[str]] | None:
    """Correctif déterministe d'un démarrage raté : dossiers non inscriptibles.

    Un conteneur non-root, souvent avec `readOnlyRootFilesystem: true` (bonne pratique ajoutée par la
    remédiation), ne peut pas écrire là où l'image l'attend (nginx : /var/cache/nginx, /var/run…).
    Correctif : un volume `emptyDir` monté sur chaque dossier concerné, dans le Deployment du pod qui plante.
    """
    import yaml
    if not mdir:
        return None
    # Associe chaque section de journaux (« ### <pod> ») aux chemins non inscriptibles qu'elle mentionne.
    wanted: dict[str, set[str]] = collections.defaultdict(set)
    for section in diagnostic.split("\n### ")[1:]:
        pod = section.split(None, 1)[0]
        workload = re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-[0-9]+$|-[a-z0-9]{5}$", "", pod)
        for m in _RO_PATH.finditer(section):
            path = next(g for g in m.groups() if g)
            wanted[workload].add(posixpath.dirname(path.rstrip("/")) or path)
            if path.startswith("/var/cache/nginx"):
                # nginx non-root a besoin des trois : on les donne d'un coup plutôt qu'une réparation par erreur.
                wanted[workload].update({"/var/cache/nginx", "/var/run", "/tmp"})
    if not wanted:
        return None
    files, notes = {}, []
    for f in sorted(mdir.rglob("*.y*ml")):
        try:
            docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
        except yaml.YAMLError:
            continue
        changed = False
        for d in docs:
            if not isinstance(d, dict) or d.get("kind") not in WORKLOADS or d.get("metadata", {}).get("name") not in wanted:
                continue
            ps = _pod_specs(d)[0]
            containers = ps.get("containers") or []
            if not containers:
                continue
            c = next((x for x in containers if x.get("name") == d["metadata"]["name"]), containers[0])
            mounts, vols = c.setdefault("volumeMounts", []), ps.setdefault("volumes", [])
            for dirpath in sorted(wanted[d["metadata"]["name"]]):
                if any(m.get("mountPath") == dirpath for m in mounts):
                    continue
                vname = ("writable-" + re.sub(r"[^a-z0-9-]+", "-", dirpath.strip("/").lower()))[:60].rstrip("-")
                mounts.append({"name": vname, "mountPath": dirpath})
                if not any(v.get("name") == vname for v in vols):
                    vols.append({"name": vname, "emptyDir": {}})
                notes.append(f"{d['kind']}/{d['metadata']['name']} : dossier inscriptible {dirpath} (emptyDir)")
                changed = True
        if changed:
            files[f.relative_to(src).as_posix()] = yaml.safe_dump_all([d for d in docs if d is not None], sort_keys=False, allow_unicode=True)
    return (files, notes) if files else None


_MISSING_REF = re.compile(r'(configmap|secret)s? "([a-z0-9][a-z0-9.-]*)" not found', re.I)


def _ingress_backends(d: dict) -> list[dict]:
    """Les références `service` d'un Ingress (defaultBackend + chaque chemin)."""
    spec = d.get("spec") or {}
    out = [spec["defaultBackend"]] if spec.get("defaultBackend") else []
    for r in spec.get("rules") or []:
        out += [p["backend"] for p in ((r.get("http") or {}).get("paths") or []) if p.get("backend")]
    return [b["service"] for b in out if isinstance(b.get("service"), dict)]


def known_ingress_fix(src: Path, mdir: Path | None) -> tuple[dict[str, str], list[str]] | None:
    """Correctif déterministe : Ingress qui vise un service inexistant (« quiz-frontend » au lieu de « frontend »).

    Rapprochement uniquement s'il est sans ambiguïté (préfixe ou suffixe « -nom », un seul candidat) ;
    le port est aligné sur celui du service s'il n'existe pas.
    """
    import yaml
    if not mdir:
        return None
    parsed, services = [], {}
    for f in sorted(mdir.rglob("*.y*ml")):
        try:
            docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
        except yaml.YAMLError:
            continue
        parsed.append((f, docs))
        for d in docs:
            if isinstance(d, dict) and d.get("kind") == "Service" and (d.get("metadata") or {}).get("name"):
                services[d["metadata"]["name"]] = [p.get("port") for p in (d.get("spec") or {}).get("ports") or []]
    if not services:
        return None

    def match(name: str) -> str | None:
        cands = [s for s in services if name.endswith("-" + s) or name.startswith(s + "-")
                 or s.endswith("-" + name) or s.startswith(name + "-")]
        return cands[0] if len(cands) == 1 else None

    files, notes = {}, []
    for f, docs in parsed:
        changed = False
        for d in docs:
            if not isinstance(d, dict) or d.get("kind") != "Ingress":
                continue
            for svc in _ingress_backends(d):
                name = svc.get("name")
                if not name or name in services:
                    continue
                new = match(name)
                if not new:
                    continue
                svc["name"] = new
                port = svc.get("port") or {}
                if "number" in port and services[new] and port["number"] not in services[new]:
                    port["number"] = services[new][0]
                notes.append(f"Ingress/{d['metadata']['name']} : le service « {name} » n'existe pas, remplacé par « {new} »")
                changed = True
        if changed:
            files[f.relative_to(src).as_posix()] = yaml.safe_dump_all([d for d in docs if d is not None], sort_keys=False, allow_unicode=True)
    return (files, notes) if files else None


_PROBE_429 = re.compile(r"probe failed with statuscode: 429", re.I)
_POD_OF = re.compile(r"(?:pod/)?([a-z0-9][a-z0-9.-]*?)-[a-z0-9]{8,10}-[a-z0-9]{5}\b")


def known_probe_fix(src: Path, mdir: Path | None, diagnostic: str) -> tuple[dict[str, str], list[str]] | None:
    """Correctif déterministe : sondes HTTP refusées par la limitation de débit de l'appli (HTTP 429).

    La sonde frappe une route protégée par un rate limiter : les pods deviennent « pas prêts » puis sont
    redémarrés en boucle. Correctif : sonde TCP sur le même port, qui vérifie que le serveur écoute sans
    passer par le rate limiter. L'équipe pourra exclure /health de la limitation et revenir à httpGet.
    """
    import yaml
    if not mdir:
        return None
    workloads = {w for line in diagnostic.splitlines() if _PROBE_429.search(line) for w in _POD_OF.findall(line)}
    if not workloads:
        return None
    files, notes = {}, []
    for f in sorted(mdir.rglob("*.y*ml")):
        try:
            docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
        except yaml.YAMLError:
            continue
        changed = False
        for d in docs:
            if not isinstance(d, dict) or d.get("kind") not in WORKLOADS or (d.get("metadata") or {}).get("name") not in workloads:
                continue
            for c in _pod_specs(d)[0].get("containers") or []:
                for kind in ("readinessProbe", "livenessProbe", "startupProbe"):
                    probe = c.get(kind)
                    if isinstance(probe, dict) and isinstance(probe.get("httpGet"), dict):
                        port = probe.pop("httpGet").get("port")
                        probe["tcpSocket"] = {"port": port}
                        notes.append(f"{d['kind']}/{d['metadata']['name']} : {kind} en TCP sur le port {port} "
                                     "(la route HTTP de santé répond 429, limitation de débit)")
                        changed = True
        if changed:
            files[f.relative_to(src).as_posix()] = yaml.safe_dump_all([d for d in docs if d is not None], sort_keys=False, allow_unicode=True)
    return (files, notes) if files else None


def _probe_rejections(kube: "Kube", ns: str, pods: list[dict], since: datetime, wait: int = 20) -> list[str]:
    """Après un démarrage réussi, on observe ~20 s : des sondes refusées en 429 rendent l'appli instable."""
    names = {p["name"] for p in pods}
    deadline = time.time() + wait
    found: dict[str, str] = {}
    while True:
        try:
            events = kube.json("get", "events", "-n", ns, "--field-selector", "reason=Unhealthy").get("items", [])
        except DeployError:
            events = []
        for ev in events:
            obj = (ev.get("involvedObject") or {}).get("name", "")
            msg = ev.get("message", "")
            # Tout refus sur un pod ACTUEL compte, même antérieur au démarrage (`since` n'est qu'indicatif) :
            # les 429 arrivent par rafales (fenêtre du rate limiter) et un pod déjà en place a pu en subir avant.
            if obj in names and _PROBE_429.search(msg):
                found[obj] = f"{obj}    {msg}"
        if found or time.time() >= deadline:
            return sorted(found.values())
        time.sleep(5)


def known_reference_fix(src: Path, mdir: Path | None, diagnostic: str) -> tuple[dict[str, str], list[str]] | None:
    """Correctif déterministe : ConfigMap/Secret référencé sous un nom qui n'existe pas, alors qu'un voisin
    au nom proche est défini (générateur kustomize « db-init-sql » monté sous « db-init »). La référence
    est alignée sur le nom défini ; kustomize y ajoutera lui-même son suffixe d'empreinte."""
    import yaml
    if not mdir:
        return None
    missing = {(k.lower(), n) for k, n in _MISSING_REF.findall(diagnostic)}
    if not missing:
        return None
    defined: dict[str, set[str]] = {"configmap": set(), "secret": set()}
    kf = next((mdir / n for n in ("kustomization.yaml", "kustomization.yml") if (mdir / n).is_file()), None)
    if kf:
        try:
            kdoc = yaml.safe_load(kf.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            kdoc = {}
        defined["configmap"].update(i.get("name") for i in kdoc.get("configMapGenerator") or [] if isinstance(i, dict) and i.get("name"))
        defined["secret"].update(i.get("name") for i in kdoc.get("secretGenerator") or [] if isinstance(i, dict) and i.get("name"))
    parsed: dict[Path, list] = {}
    for f in sorted(mdir.rglob("*.y*ml")):
        try:
            parsed[f] = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
        except yaml.YAMLError:
            continue
        for d in parsed[f]:
            if isinstance(d, dict) and d.get("kind") in ("ConfigMap", "Secret") and d.get("metadata", {}).get("name"):
                defined[d["kind"].lower()].add(d["metadata"]["name"])
    mapping: dict[tuple[str, str], str] = {}
    for kind, name in missing:
        cands = [g for g in defined[kind] if g != name and (g.startswith(name) or name.startswith(g))]
        if len(cands) == 1:
            mapping[(kind, name)] = cands[0]
    if not mapping:
        return None
    files, notes = {}, []

    def swap(obj: dict | None, key: str, kind: str, where: str) -> bool:
        if isinstance(obj, dict) and (kind, obj.get(key)) in mapping:
            new = mapping[(kind, obj[key])]
            notes.append(f"{where} : {kind} « {obj[key]} » → « {new} »")
            obj[key] = new
            return True
        return False

    for f, docs in parsed.items():
        changed = False
        for d in docs:
            if not isinstance(d, dict):
                continue
            where = f"{d.get('kind')}/{d.get('metadata', {}).get('name')}"
            for ps in _pod_specs(d):
                for v in ps.get("volumes") or []:
                    changed |= swap(v.get("configMap"), "name", "configmap", where)
                    changed |= swap(v.get("secret"), "secretName", "secret", where)
                for c in _containers(ps):
                    for e in c.get("env") or []:
                        vf = e.get("valueFrom") or {}
                        changed |= swap(vf.get("configMapKeyRef"), "name", "configmap", where)
                        changed |= swap(vf.get("secretKeyRef"), "name", "secret", where)
                    for ef in c.get("envFrom") or []:
                        changed |= swap(ef.get("configMapRef"), "name", "configmap", where)
                        changed |= swap(ef.get("secretRef"), "name", "secret", where)
        if changed:
            files[f.relative_to(src).as_posix()] = yaml.safe_dump_all([d for d in docs if d is not None], sort_keys=False, allow_unicode=True)
    return (files, notes) if files else None


_PG_PERM = re.compile(r"initdb: error: could not (?:change permissions of|create) directory|chmod: /var/(?:lib|run)/postgresql", re.I)


def known_postgres_fix(src: Path, mdir: Path | None, diagnostic: str) -> tuple[dict[str, str], list[str]] | None:
    """Correctif déterministe : PostgreSQL non-root qui ne peut pas initialiser son volume.

    Deux causes cumulées, fréquentes dans les manifests générés : l'uid imposé n'est pas celui de l'image
    (70 pour postgres:*-alpine, 999 pour Debian) et un volume local n'applique pas fsGroup, donc initdb ne peut
    pas s'approprier la racine du volume. Correctif : PGDATA dans un sous-dossier, uid de l'image,
    /var/run/postgresql inscriptible.
    """
    import yaml
    if not mdir:
        return None
    workloads = set()
    for section in diagnostic.split("\n### ")[1:]:
        if _PG_PERM.search(section):
            workloads.add(re.sub(r"-[a-z0-9]{8,10}-[a-z0-9]{5}$|-[0-9]+$", "", section.split(None, 1)[0]))
    if not workloads:
        return None
    files, notes = {}, []
    for f in sorted(mdir.rglob("*.y*ml")):
        try:
            docs = list(yaml.safe_load_all(f.read_text(encoding="utf-8")))
        except yaml.YAMLError:
            continue
        changed = False
        for d in docs:
            if not isinstance(d, dict) or d.get("kind") not in WORKLOADS or d.get("metadata", {}).get("name") not in workloads:
                continue
            ps = _pod_specs(d)[0]
            containers = ps.get("containers") or []
            c = next((x for x in containers if "postgres" in str(x.get("image", ""))), containers[0] if containers else None)
            if not c:
                continue
            img = str(c.get("image", ""))
            uid = 70 if "alpine" in img else 999
            where = f"{d['kind']}/{d['metadata']['name']}"
            mounts = c.setdefault("volumeMounts", [])
            data = next((m for m in mounts if str(m.get("mountPath", "")).startswith("/var/lib/postgresql")), None)
            if data:
                # Nom neuf : un ancien dossier « pgdata » a pu être créé par le kubelet (subPath) et appartenir à root.
                pgdata = str(data["mountPath"]).rstrip("/") + f"/pgdata-{uid}"
                if data.pop("subPath", None):
                    notes.append(f"{where} : subPath retiré du volume de données (dossier créé par le kubelet, non inscriptible)")
                env = c.setdefault("env", [])
                cur = next((e for e in env if e.get("name") == "PGDATA"), None)
                if cur:
                    cur["value"] = pgdata
                    cur.pop("valueFrom", None)
                else:
                    env.append({"name": "PGDATA", "value": pgdata})
                notes.append(f"{where} : PGDATA={pgdata} (la racine du volume n'appartient pas à postgres)")
            for sc, keys in ((ps.setdefault("securityContext", {}), ("runAsUser", "runAsGroup", "fsGroup")),
                             (c.get("securityContext") or {}, ("runAsUser", "runAsGroup"))):
                for k in keys:
                    if k in sc and sc[k] != uid:
                        sc[k] = uid
            notes.append(f"{where} : uid/gid {uid}, celui de l'image {img}")
            if not any(m.get("mountPath") == "/var/run/postgresql" for m in mounts):
                mounts.append({"name": "writable-var-run-postgresql", "mountPath": "/var/run/postgresql"})
                ps.setdefault("volumes", []).append({"name": "writable-var-run-postgresql", "emptyDir": {}})
                notes.append(f"{where} : /var/run/postgresql inscriptible (emptyDir)")
            changed = True
        if changed:
            files[f.relative_to(src).as_posix()] = yaml.safe_dump_all([d for d in docs if d is not None], sort_keys=False, allow_unicode=True)
    return (files, notes) if files else None


def _tree(src: Path, limit: int = 160) -> str:
    skip = {".git", "node_modules", "images", "dist", "build", "__pycache__"}
    lines = []
    for p in sorted(src.rglob("*")):
        rel = p.relative_to(src)
        if any(part in skip for part in rel.parts) or not p.is_file():
            continue
        lines.append(rel.as_posix())
        if len(lines) >= limit:
            lines.append("…")
            break
    return "\n".join(lines)


def _repair_context(stage: str, src: Path, builds: list[Build], mdir: Path | None, failure: "_Failure",
                    budget: int = 30_000) -> dict[str, str]:
    """Juste ce qu'il faut au modèle : un contexte trop gros rend les modèles gratuits très lents."""
    paths: list[str] = []
    if stage == "build":
        target = failure.build or (builds[0] if builds else None)
        if target:
            paths.append(target.dockerfile)
            if (src / target.context / ".dockerignore").is_file():
                paths.append((Path(target.context) / ".dockerignore").as_posix())
    else:
        if mdir:
            paths += [p.relative_to(src).as_posix() for p in sorted(mdir.rglob("*"))
                      if p.is_file() and p.suffix in (".yaml", ".yml", ".conf", ".json")]
        if stage == "rollout":
            paths += [b.dockerfile for b in builds]
            paths += [p.relative_to(src).as_posix() for p in sorted(src.rglob("*.conf")) if len(p.relative_to(src).parts) <= 3]
    for word in set(re.findall(r"[\w./-]+\.(?:js|ts|py|conf|json|sql|yaml|yml|toml|ini)", failure.diagnostic)):
        cand = src / _norm(word)
        if cand.is_file() and cand.stat().st_size < 20_000:
            paths.append(cand.relative_to(src).as_posix())
    files, used = {}, 0
    for rel in dict.fromkeys(paths):
        p = src / rel
        if not p.is_file() or used > budget:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")[:8000]
        files[rel] = text
        used += len(text)
    return files


def _ask_repair(llm, project: str, failure: _Failure, src: Path, builds: list[Build], mdir: Path | None) -> dict | None:
    stage = {"build": "la construction d'une image Docker échoue",
             "manifests": "les manifests Kubernetes ne se rendent pas ou sont refusés par le cluster",
             "rollout": "l'application ne démarre pas correctement"}[failure.stage]
    files = _repair_context(failure.stage, src, builds, mdir, failure)
    listing = f"\n## Fichiers présents dans le dépôt\n```\n{_tree(src)}\n```\n" if failure.stage == "build" else ""
    user = DEPLOY_REPAIR.format(
        project=project, stage=stage, diagnostic=failure.diagnostic[-6000:],
        components=", ".join(b.component for b in builds) or "aucune",
        builds="\n".join(f"- {b.component} : {b.dockerfile} (contexte {b.context})" for b in builds) or "(aucune)",
        files="\n".join(f"\n### {p}\n```\n{c}\n```" for p, c in files.items()) + listing)
    if hasattr(llm, "timeout"):
        llm.timeout = REPAIR_TIMEOUT
    try:
        res = llm.complete(FIX_SYSTEM, user, expected_keys=("files",))
    except LLMError as e:
        if not getattr(e, "raw", None):
            raise
        # Le modèle a répondu, mais pas dans le format : une seule relance, avec le format rappelé.
        res = llm.complete(FIX_SYSTEM, user + "\n\nRAPPEL : ta réponse précédente n'était pas exploitable. Réponds "
                           "UNIQUEMENT par l'objet JSON demandé, sans texte autour, contenus de fichiers complets.",
                           expected_keys=("files",))
    out = {}
    for f in res.data.get("files") or []:
        rel = _safe_rel_path(str(f.get("path", "")))
        if rel and isinstance(f.get("content"), str) and f["content"].strip():
            out[rel] = f["content"] if f["content"].endswith("\n") else f["content"] + "\n"
    if not out:
        return None
    return {"explanation": str(res.data.get("explanation", "")), "files": out, "model": res.model}


def _commit_to_branch(repo: Path, branch: str, files: dict[str, str], message: str) -> str | None:
    _git(repo, "checkout", "-q", "--", ".", check=False)
    _git(repo, "clean", "-qfd", check=False)
    _git(repo, "checkout", "-q", branch)
    for rel, content in files.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    if _git(repo, "commit", "-q", "-m", message, check=False).returncode != 0:
        return None
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# État et résultat
# ---------------------------------------------------------------------------

@dataclass
class DeployResult:
    name: str
    ok: bool
    namespace: str | None = None
    url: str | None = None
    port: int | None = None
    http_status: int | None = None
    urls: dict = field(default_factory=dict)
    branch: str | None = None
    commit: str | None = None
    cluster: str = ""
    images: list = field(default_factory=list)
    workloads: list = field(default_factory=list)
    pods: list = field(default_factory=list)
    repairs: list = field(default_factory=list)
    credentials: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    failed_stage: str | None = None
    diagnostic: str = ""
    elapsed: float = 0.0
    files: dict = field(default_factory=dict)
    cluster_kind: str = "desktop"
    observability: dict = field(default_factory=dict)


def deploy_state(out_dir: Path) -> dict | None:
    f = out_dir / "deploy" / "state.json"
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_state(out_dir: Path, state: dict) -> None:
    d = out_dir / "deploy"
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# La boucle
# ---------------------------------------------------------------------------

def run_deploy(name: str, *, out_root: Path = Path("out"), backend: str | None = None, model: str | None = None,
               max_repairs: int = MAX_REPAIRS, branch: str | None = None, progress: ProgressCb | None = None,
               snapshot: Callable[[dict], None] | None = None, cluster: str = "desktop",
               observability: bool = True) -> DeployResult:
    import yaml

    def say(step: str, msg: str) -> None:
        if progress:
            progress(step, msg)

    t_start = time.time()
    out_dir = out_root / name
    meta_file, changes_file = out_dir / "meta.json", out_dir / "remediation" / "changes.json"
    if not changes_file.is_file():
        raise DeployError("Aucune remédiation pour ce projet : lancez d'abord « Corriger les risques ».")
    meta = json.loads(meta_file.read_text(encoding="utf-8")) if meta_file.is_file() else {}
    changes = json.loads(changes_file.read_text(encoding="utf-8"))
    branch = branch or changes.get("branch")
    if not meta.get("target") or not branch:
        raise DeployError("La remédiation ne mémorise pas sa branche : relancez-la.")
    slug = _slug(name)
    project = (json.loads((out_dir / "analysis.json").read_text(encoding="utf-8")).get("project", {}).get("name")
               if (out_dir / "analysis.json").is_file() else name)
    dep_dir = out_dir / "deploy"
    dep_dir.mkdir(parents=True, exist_ok=True)
    prev = deploy_state(out_dir) or {}
    secrets_file = dep_dir / "secrets.local.json"
    try:
        saved_secrets = json.loads(secrets_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        saved_secrets = {}

    result = DeployResult(name=name, ok=False, branch=branch, cluster_kind=cluster)
    builds: list[Build] = []
    live = {"stage": "prereq", "attempt": 0, "ok": None}

    def snap(**kw) -> None:
        """Instantané structuré pour le HUD : ce qui existe, ce qui est prêt, ce qui échoue."""
        if not snapshot:
            return
        live.update(kw)
        snapshot({**live, "branch": result.branch, "commit": result.commit, "cluster": result.cluster,
                  "namespace": result.namespace, "images": [asdict(b) for b in builds], "workloads": result.workloads,
                  "pods": result.pods, "repairs": result.repairs, "url": result.url, "urls": result.urls,
                  "failed_stage": result.failed_stage, "cluster_kind": cluster, "observability": result.observability})

    with tempfile.TemporaryDirectory() as tmp:
        # 1. PRÉREQUIS ---------------------------------------------------------
        say("prereq", f"vérification de Docker et du cluster {clusters.KINDS.get(cluster, {}).get('label', cluster)} …")
        _check_docker()
        if cluster == "k3s":
            try:
                clusters.ensure_k3s(say)
            except clusters.ClusterError as e:
                raise DeployError(str(e)) from e
        kube, server_version = _setup_kube(Path(tmp), cluster)
        node = kube.json("get", "nodes")["items"][0]
        runtime = node["status"]["nodeInfo"]["containerRuntimeVersion"]
        need_load = not runtime.startswith("docker://")
        result.cluster = f"{node['metadata']['name']} · Kubernetes {server_version} · {runtime}"
        say("prereq", f"cluster : {result.cluster}" + (" · les images seront importées dans le nœud" if need_load else ""))
        snap(stage="prereq")

        try:
            repo = resolve(meta["target"], refresh=False, log=lambda m: say("prereq", m.strip())).resolve()
        except RuntimeError as e:
            raise DeployError(str(e)) from e
        if _git(repo, "rev-parse", "--verify", "-q", branch, check=False).returncode != 0:
            raise DeployError(f"La branche « {branch} » n'existe plus dans le clone (dépôts nettoyés ?) : relancez la remédiation.")

        # OBSERVABILITÉ : installée au premier déploiement sur ce cluster, puis simplement vérifiée
        pre_warnings: list[str] = []
        if observability:
            snap(stage="platform")
            from .platform import ensure_platform
            try:
                result.observability = ensure_platform(
                    cluster, progress=say, snapshot=lambda s: snap(stage="platform", platform=s.get("components")))
            except DeployError as e:
                pre_warnings.append(f"Observabilité non installée : {str(e).splitlines()[0][:300]}")
                say("platform", pre_warnings[-1])
            snap(stage="platform")

        same_cluster = prev.get("cluster_kind", "desktop") == cluster
        port = _choose_port(kube, prev.get("port") if same_cluster else None, cluster)
        ns_created = prev.get("namespace_created") if same_cluster else None   # None : on le saura à l'apply
        ingress_host = None
        attempt, builds, mdir, src = 0, [], None, dep_dir / "src"
        while True:
            try:
                # 2. SOURCES -----------------------------------------------------
                commit = _git(repo, "rev-parse", branch).stdout.strip()
                result.commit = commit
                _extract(repo, branch, src)
                say("build", f"sources de {branch} ({commit[:8]}) extraites")

                # 3. IMAGES ------------------------------------------------------
                builds = detect_builds(src, project)
                for b in builds:
                    b.image = f"devops-agent/{slug}-{_slug(b.component, 20)}:{commit[:12]}"
                say("build", f"{len(builds)} image(s) à construire : " + (", ".join(f"{b.component} ← {b.dockerfile}" for b in builds) or "aucune"))
                snap(stage="build")
                for b in builds:
                    say("build", f"construction de {b.component} …")
                    b.status = "construction"
                    snap(stage="build")
                    _docker_build(src, b, say)
                    say("build", f"{b.component} construite en {b.seconds:.0f} s")
                    snap(stage="build")
                if cluster == "k3s" and builds:
                    snap(stage="load")
                    try:
                        clusters.import_images([b.image for b in builds], say)
                    except clusters.ClusterError as e:
                        raise DeployError(str(e)) from e
                    for b in builds:
                        b.status = "chargée dans le cluster"
                    snap(stage="load")
                elif need_load and builds:
                    snap(stage="load")
                    _load_images(kube, builds, say)
                    snap(stage="load")

                # 4. MANIFESTS ---------------------------------------------------
                mdir, is_k = find_manifests(src)
                if not mdir:
                    raise DeployError("Aucun manifest Kubernetes dans la branche : relancez la remédiation avec la cible Kubernetes.")
                docs = _render(kube, mdir, is_k)
                svc_names = {d["metadata"]["name"] for d in docs if d.get("kind") == "Service"}
                dangling = sorted({f"Ingress/{d['metadata']['name']} → service « {b.get('name')} »"
                                   for d in docs if d.get("kind") == "Ingress" for b in _ingress_backends(d)
                                   if b.get("name") not in svc_names})
                if dangling:
                    raise _Failure("manifests", "Ingress vers des services absents des manifests :\n- " + "\n- ".join(dangling)
                                   + f"\nServices rendus : {', '.join(sorted(svc_names)) or 'aucun'}.")
                # Plateforme installée (Traefik) : l'appli est aussi publiée sur http://<namespace>.localhost.
                ingress_class = "traefik" if kube.run("get", "ingressclass", "traefik").returncode == 0 else None
                docs, info = adapt(docs, builds, slug, port, saved_secrets, ingress_class)
                ingress_host = info.get("ingress_host") if ingress_class else None
                if info["unresolved_images"]:
                    raise _Failure("manifests", "Images non résolues (ni construites par l'agent, ni publiques) :\n- "
                                   + "\n- ".join(info["unresolved_images"])
                                   + f"\nImages construites : {', '.join(b.component for b in builds) or 'aucune'}.")
                secrets_file.write_text(json.dumps(saved_secrets, ensure_ascii=False, indent=2), encoding="utf-8")
                ns = info["namespace"]
                result.namespace, result.credentials, result.warnings = ns, info["credentials"], pre_warnings + info["warnings"]
                manifest_path = dep_dir / "manifests.yaml"
                docs.sort(key=lambda d: 0 if d["kind"] == "Namespace" else 1)
                manifest_path.write_text(yaml.safe_dump_all(docs, sort_keys=False, allow_unicode=True), encoding="utf-8")
                say("manifests", f"{len(docs)} objets rendus depuis {mdir.relative_to(src).as_posix()} · namespace {ns}"
                                 + (f" · entrée {info['entry']['service']}:{info['entry']['port']} → localhost:{port}" if info["entry"] else ""))

                snap(stage="manifests")

                # 5. DÉPLOIEMENT -------------------------------------------------
                if ns_created is None:
                    ns_created = kube.run("get", "namespace", ns).returncode != 0
                ap = kube.run("apply", "-f", str(manifest_path), timeout=180)
                if ap.returncode != 0 and "field is immutable" in ap.stderr:
                    say("apply", "champ immuable modifié : recréation des charges de travail")
                    kube.run("delete", "deployment,statefulset,daemonset", "-n", ns, "-l", f"{LABEL}={slug}", "--wait=true", timeout=180)
                    ap = kube.run("apply", "-f", str(manifest_path), timeout=180)
                if ap.returncode != 0 and "panic:" in ap.stderr:
                    # Bug connu du patch stratégique côté client (kubectl panique sur certaines listes) :
                    # l'application côté serveur ne calcule pas ce patch.
                    say("apply", "kubectl a planté en calculant le patch : application côté serveur")
                    ap = kube.run("apply", "--server-side", "--force-conflicts", "--field-manager=devops-agent",
                                  "-f", str(manifest_path), timeout=180)
                if ap.returncode != 0:
                    raise _Failure("manifests", f"`kubectl apply` a refusé les manifests :\n{ap.stderr.strip()[-2500:]}")
                say("apply", f"{len([l for l in ap.stdout.splitlines() if l.strip()])} objet(s) appliqué(s) dans {ns}")
                names = [(d["kind"], d["metadata"]["name"]) for d in docs if d["kind"] in WORKLOADS]
                def _on_state(states: list, pods: list) -> None:
                    result.workloads, result.pods = states, pods
                    snap(stage="rollout")

                t_roll = datetime.now(timezone.utc)
                ok, states, pods = _wait_rollout(kube, ns, names, say, on_state=_on_state)
                result.workloads, result.pods = states, pods
                if not ok:
                    raise _Failure("rollout", _diagnose(kube, ns, states, pods))
                flaky = _probe_rejections(kube, ns, pods, t_roll)
                if flaky:
                    say("rollout", f"{len(flaky)} pod(s) refusent leurs sondes de santé (HTTP 429) : l'appli n'est pas stable")
                    raise _Failure("rollout", "Sondes HTTP refusées par l'application (HTTP 429, limitation de débit) :\n"
                                   + "\n".join(flaky))
                mon_ok, mon_msg = _apply_monitoring(kube, src, ns, slug)
                if mon_msg:
                    say("apply", mon_msg)
                    if not mon_ok:
                        result.warnings.append(mon_msg)
                result.ok, result.failed_stage, result.diagnostic = True, None, ""
                break
            except _Failure as f:
                result.failed_stage, result.diagnostic = f.stage, f.diagnostic
                say("repair" if attempt < max_repairs else f.stage, f"échec ({f.stage}) : {str(f).splitlines()[0][:160]}")
                if attempt >= max_repairs:
                    break
                attempt += 1
                fix = None
                if f.stage == "build" and f.build is not None:
                    files = known_build_fix(src, f.build, f.diagnostic)
                    if files:
                        fix = {"explanation": "Règle connue : fichier(s) absent(s) du dépôt retiré(s) des instructions COPY "
                                              "(et `npm ci`, qui exige un lockfile, remplacé par `npm install`).",
                               "files": files, "model": "règle déterministe"}
                        say("repair", f"réparation {attempt}/{max_repairs} : erreur connue (COPY d'un fichier absent), correctif appliqué sans appel au modèle")
                if fix is None and f.stage == "rollout":
                    # Les règles de démarrage se cumulent : chacune part des fichiers déjà corrigés par la précédente.
                    rule_files, rule_notes = {}, []
                    for rule in (known_rollout_fix, known_reference_fix, known_postgres_fix, known_probe_fix):
                        got = rule(src, mdir, f.diagnostic)
                        if got:
                            for rel, content in got[0].items():
                                (src / rel).write_text(content, encoding="utf-8")
                            rule_files.update(got[0])
                            rule_notes += got[1]
                    if rule_files:
                        fix = {"explanation": "Règles connues : " + " ; ".join(rule_notes), "files": rule_files, "model": "règle déterministe"}
                        say("repair", f"réparation {attempt}/{max_repairs} : {len(rule_notes)} erreur(s) connue(s) au démarrage, correctif appliqué sans appel au modèle")
                if fix is None and f.stage == "manifests":
                    known = known_manifest_fix(src, mdir) or known_ingress_fix(src, mdir)
                    if known:
                        fix = {"explanation": "Règle connue : " + " ; ".join(known[1]), "files": known[0], "model": "règle déterministe"}
                        say("repair", f"réparation {attempt}/{max_repairs} : erreur connue dans les manifests, correctif appliqué sans appel au modèle")
                if fix is None:
                    try:
                        llm = make_backend(backend, model)
                        say("repair", f"réparation {attempt}/{max_repairs} : le modèle {llm.model} analyse le diagnostic …")
                        fix = _ask_repair(llm, project, f, src, builds, mdir)
                    except LLMError as e:
                        say("repair", f"réparation impossible : {str(e).splitlines()[0][:160]}")
                        break
                if not fix:
                    say("repair", "le modèle n'a proposé aucun correctif exploitable")
                    break
                sha = _commit_to_branch(repo, branch, fix["files"],
                                        f"DevOps agent : correctif de déploiement ({f.stage})\n\n{fix['explanation']}")
                if not sha and fix["model"] == "règle déterministe":
                    # Les règles sont épuisées sur cette erreur : le modèle prend le relais au lieu d'abandonner.
                    try:
                        llm = make_backend(backend, model)
                        say("repair", f"les règles connues ne changent plus rien : le modèle {llm.model} prend le relais …")
                        fix = _ask_repair(llm, project, f, src, builds, mdir)
                    except LLMError as e:
                        say("repair", f"réparation impossible : {str(e).splitlines()[0][:160]}")
                        break
                    if not fix:
                        say("repair", "le modèle n'a proposé aucun correctif exploitable")
                        break
                    sha = _commit_to_branch(repo, branch, fix["files"],
                                            f"DevOps agent : correctif de déploiement ({f.stage})\n\n{fix['explanation']}")
                result.repairs.append({"attempt": attempt, "stage": f.stage, "explanation": fix["explanation"],
                                       "files": sorted(fix["files"]), "commit": sha, "model": fix["model"]})
                if not sha:
                    say("repair", "le correctif proposé ne change rien : arrêt")
                    break
                say("repair", f"correctif commité ({sha[:8]}) : " + ", ".join(sorted(fix["files"])) + " — nouveau déploiement")
                snap(stage="repair", attempt=attempt)

        result.images = [asdict(b) for b in builds]

        # 6. ACCÈS -----------------------------------------------------------------
        if result.ok and result.namespace:
            lb = f"{next((d['metadata']['name'] for d in yaml.safe_load_all((dep_dir / 'manifests.yaml').read_text(encoding='utf-8')) if d and d.get('kind') == 'Service' and d['metadata']['name'].endswith('-local')), '')}"
            if lb:
                for _ in range(20):
                    svc = kube.json("get", "svc", lb, "-n", result.namespace)
                    if (svc.get("status") or {}).get("loadBalancer", {}).get("ingress"):
                        break
                    time.sleep(3)
                result.port, result.url = port, f"http://localhost:{port}"
                host = "host.docker.internal" if IN_DOCKER else "127.0.0.1"
                for _ in range(10):
                    try:
                        with urllib.request.urlopen(f"http://{host}:{port}/", timeout=5) as r:
                            result.http_status = r.status
                        break
                    except urllib.error.HTTPError as e:
                        result.http_status = e.code
                        break
                    except (urllib.error.URLError, OSError):
                        time.sleep(3)
                say("access", f"application publiée sur {result.url}" + (f" · HTTP {result.http_status}" if result.http_status else " · ne répond pas encore"))
                result.urls["localhost"] = result.url
            if ingress_host:
                from .platform import web_port as _web_port
                web_port = _web_port(cluster)
                pretty = f"http://{ingress_host}" + ("" if web_port == 80 else f":{web_port}")
                target = "host.docker.internal" if IN_DOCKER else "127.0.0.1"
                code = None
                for _ in range(10):
                    try:
                        req = urllib.request.Request(f"http://{target}:{web_port}/", headers={"Host": ingress_host})
                        with urllib.request.urlopen(req, timeout=5) as r:
                            code = r.status
                        break
                    except urllib.error.HTTPError as e:
                        code = e.code
                        if e.code != 404:          # 404 : Traefik n'a pas encore pris la route
                            break
                        time.sleep(3)
                    except (urllib.error.URLError, OSError):
                        time.sleep(3)
                result.urls["ingress"] = pretty
                if code and code < 500 and code != 404:
                    result.url, result.http_status = pretty, code
                say("access", f"Ingress Traefik : {pretty}" + (f" · HTTP {code}" if code else " · ne répond pas encore"))

    # 7. RAPPORT -------------------------------------------------------------------
    result.elapsed = round(time.time() - t_start, 1)
    state = {"namespace": result.namespace, "namespace_created": bool(ns_created),
             "port": result.port or prev.get("port") or port, "url": result.url, "ok": result.ok, "deployed": bool(result.namespace),
             "branch": branch, "commit": result.commit, "images": [b.image for b in builds], "slug": slug,
             "deployed_at": datetime.now().isoformat(timespec="seconds"), "http_status": result.http_status,
             "cluster_kind": cluster}
    _save_state(out_dir, state)
    (dep_dir / "DEPLOYMENT.md").write_text(_render_md(result, project), encoding="utf-8")
    result.files = {n: dep_dir / n for n in ("DEPLOYMENT.md", "manifests.yaml") if (dep_dir / n).is_file()}
    snap(stage="done", ok=result.ok)
    say("report", "déploiement réussi" if result.ok else f"déploiement en échec ({result.failed_stage}) : voir le diagnostic")
    return result


def live_view(name: str, out_root: Path = Path("out")) -> dict:
    """État en direct d'une appli déployée (charges, pods) — ce que le HUD rafraîchit toutes les 5 s."""
    state = deploy_state(out_root / name) or {}
    out = {"name": name, "deployed": bool(state.get("deployed")), "namespace": state.get("namespace"), "url": state.get("url"),
           "ok": state.get("ok"), "images": state.get("images", []), "commit": state.get("commit"), "branch": state.get("branch"),
           "workloads": [], "pods": [], "cluster": None}
    if not out["deployed"] or not out["namespace"]:
        return out
    kind = state.get("cluster_kind", "desktop")
    out["cluster_kind"] = kind
    try:
        from .platform import observability_info
        out["observability"] = observability_info(kind)
    except Exception:  # noqa: BLE001 — l'état de la plateforme est un bonus, pas une condition
        out["observability"] = {}
    with tempfile.TemporaryDirectory() as tmp:
        try:
            kube, version = _setup_kube(Path(tmp), kind)
        except DeployError as e:
            out["error"] = str(e)
            return out
        out["cluster"] = f"{clusters.KINDS[kind]['label']} · Kubernetes {version}"
        ns = out["namespace"]
        try:
            items = kube.json("get", "deployment,statefulset,daemonset", "-n", ns, timeout=30)["items"]
            out["workloads"] = [{"kind": w["kind"], "name": w["metadata"]["name"], "ready": _workload_ready(w),
                                 "replicas": f"{(w.get('status') or {}).get('readyReplicas', (w.get('status') or {}).get('numberReady', 0)) or 0}/"
                                             f"{(w.get('spec') or {}).get('replicas', (w.get('status') or {}).get('desiredNumberScheduled', 1))}"}
                                for w in items]
            out["pods"] = [_pod_summary(p) for p in _current_pods(kube, ns, items)]
        except DeployError as e:
            out["error"] = str(e)
    return out


def undeploy(name: str, *, out_root: Path = Path("out"), purge_images: bool = False) -> dict:
    """Retire du cluster ce que l'agent a déployé pour ce projet."""
    out_dir = out_root / name
    state = deploy_state(out_dir)
    if not state or not state.get("namespace"):
        raise DeployError("Aucun déploiement enregistré pour ce projet.")
    ns, slug = state["namespace"], state.get("slug") or _slug(name)
    with tempfile.TemporaryDirectory() as tmp:
        kube, _ = _setup_kube(Path(tmp), state.get("cluster_kind", "desktop"))
        if state.get("namespace_created"):
            proc = kube.run("delete", "namespace", ns, "--wait=false", "--ignore-not-found")
        else:
            proc = kube.run("delete", "all,configmap,secret,pvc,ingress", "-n", ns, "-l", f"{LABEL}={slug}", "--wait=false")
        if proc.returncode != 0:
            raise DeployError(f"Suppression impossible : {proc.stderr.strip()[-300:]}")
    if purge_images and shutil.which("docker"):
        for img in state.get("images") or []:
            _run(["docker", "image", "rm", "-f", img], timeout=60)
    state.update(deployed=False, url=None, undeployed_at=datetime.now().isoformat(timespec="seconds"))
    _save_state(out_dir, state)
    return {"namespace": ns, "removed": True}


def remove_loader() -> None:
    """Retire le chargeur d'images du cluster (nettoyage complet)."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            kube, _ = _setup_kube(Path(tmp))
        except DeployError:
            return
        kube.run("delete", "namespace", SYSTEM_NS, "--wait=false", "--ignore-not-found")


# Jamais supprimés par le nettoyage, même s'ils portaient le label par erreur.
PROTECTED_NS = {"default", "kube-system", "kube-public", "kube-node-lease", "local-path-storage",
                "monitoring", "traefik", "logging"}


def sweep_cluster(*, purge_images: bool = True) -> dict:
    """Filet de sécurité du « Tout nettoyer » : retire de chaque cluster tout ce qui porte le label de l'agent,
    même quand l'état sur disque a disparu ou ne correspond plus (déploiement interrompu, état effacé…)."""
    out: dict = {"namespaces": [], "images": 0, "error": None}
    errors = []
    for kind, meta in clusters.KINDS.items():
        if not clusters.kubeconfig_path(kind):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            try:
                kube, _ = _setup_kube(Path(tmp), kind)
            except DeployError as e:
                if kind == "desktop":
                    errors.append(f"{meta['label']} injoignable, rien n'y a été retiré : {str(e).splitlines()[0][:200]}")
                continue
            try:
                items = kube.json("get", "namespace", "-l", LABEL).get("items", [])
            except DeployError as e:
                items = []
                errors.append(f"{meta['label']} : liste des namespaces impossible : {e}")
            for ns in items:
                name = ns["metadata"]["name"]
                if name in PROTECTED_NS:
                    continue
                if kube.run("delete", "namespace", name, "--wait=false", "--ignore-not-found").returncode == 0:
                    out["namespaces"].append(name)
            # objets de l'agent posés dans des namespaces qu'il n'a pas créés
            kube.run("delete", "deployment,statefulset,daemonset,service,configmap,secret,pvc,ingress", "-A",
                     "-l", LABEL, "--wait=false")
            kube.run("delete", "namespace", SYSTEM_NS, "--wait=false", "--ignore-not-found")
    out["error"] = " · ".join(errors) or None
    if purge_images and shutil.which("docker"):
        try:
            refs = _run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}", "--filter", "reference=devops-agent/*"]).stdout.split()
            for ref in refs:
                if _run(["docker", "image", "rm", "-f", ref], timeout=60).returncode == 0:
                    out["images"] += 1
        except DeployError:
            pass
    return out


def _render_md(r: DeployResult, project: str) -> str:
    md = [f"# Déploiement local — {project}\n",
          f"_{datetime.now():%d/%m/%Y %H:%M} · branche `{r.branch}` · commit `{(r.commit or '')[:8]}` · {r.cluster}_\n",
          f"**Statut : {'✅ en marche' if r.ok else '❌ en échec (' + str(r.failed_stage) + ')'}**" + (f" — {r.url}" if r.url else ""), ""]
    if r.images:
        md += ["## Images\n", "| Composant | Dockerfile | Image locale | Statut |", "|---|---|---|---|"]
        md += [f"| {b['component']} | `{b['dockerfile']}` | `{b['image']}` | {b['status']} |" for b in r.images]
    if r.workloads:
        md += ["\n## Charges de travail\n"] + [f"- {'✅' if w['ready'] else '❌'} {w['kind']}/{w['name']} — {w['replicas']}" for w in r.workloads]
    if r.repairs:
        md.append("\n## Réparations automatiques\n")
        md += [f"{x['attempt']}. ({x['stage']}) {x['explanation']} — {', '.join('`' + f + '`' for f in x['files'])} · commit `{(x['commit'] or '—')[:8]}`" for x in r.repairs]
    if r.warnings:
        md += ["\n## Remarques\n"] + [f"- {w}" for w in r.warnings]
    if r.credentials:
        md.append("\n## Identifiants générés pour l'environnement local\n")
        for s, kv in r.credentials.items():
            md += [f"- `{s}` : " + ", ".join(f"`{k}`" for k in kv)]
        md.append("\nValeurs dans `secrets.local.json` (à ne jamais versionner).")
    if r.diagnostic:
        md += ["\n## Diagnostic\n", "```", r.diagnostic, "```"]
    if r.namespace:
        md += ["\n## Commandes utiles\n", "```bash", f"kubectl get pods -n {r.namespace}",
               f"kubectl logs -n {r.namespace} deploy/<nom> --tail=50", f"kubectl delete namespace {r.namespace}   # retirer", "```"]
    return "\n".join(md) + "\n"
