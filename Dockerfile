# Agent DevOps — interface web + CLI, avec opencode (modèles gratuits) préinstallé.
#
#   docker build -t devops-agent .
#   docker run --rm -p 8080:8080 -v "$PWD/out:/app/out" devops-agent
#   -> http://localhost:8080
#
# Pour le backend Anthropic : docker run -e ANTHROPIC_API_KEY=sk-ant-... (et pip install anthropic, voir ci-dessous)

# --- Étape 1 : récupérer le binaire opencode via npm (version épinglée, téléchargement fiable) ---
FROM node:22-slim AS opencode
ARG OPENCODE_VERSION=1.18.30
RUN npm install -g opencode-ai@${OPENCODE_VERSION} \
 && cp /usr/local/lib/node_modules/opencode-ai/node_modules/opencode-linux-x64/bin/opencode /opencode \
 && /opencode --version

# --- Étape 2 : kubectl et Helm (binaires officiels, versions épinglées), CLI Docker : déploiement local ---
FROM alpine:3.20 AS k8stools
ARG KUBECTL_VERSION=v1.34.1
ARG HELM_VERSION=v4.3.0
ARG K3D_VERSION=v5.8.3
ARG TARGETARCH
RUN apk add --no-cache curl \
 && curl -fsSLo /kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${TARGETARCH:-amd64}/kubectl" \
 && chmod +x /kubectl && /kubectl version --client \
 && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-${TARGETARCH:-amd64}.tar.gz" | tar -xz -C /tmp \
 && mv /tmp/linux-*/helm /helm && /helm version --short \
 && curl -fsSLo /k3d "https://github.com/k3d-io/k3d/releases/download/${K3D_VERSION}/k3d-linux-${TARGETARCH:-amd64}" \
 && chmod +x /k3d && /k3d version

FROM docker:29-cli AS dockercli

# --- Étape 3 : l'image finale, Python + git + opencode + kubectl + CLI Docker ---
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENT_HOST=0.0.0.0 \
    AGENT_PORT=8080

# git : pour cloner les dépôts à analyser.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# opencode (CLI) — fournit les modèles gratuits « opencode zen », sans clé.
COPY --from=opencode /opencode /usr/local/bin/opencode
RUN opencode --version

# Déploiement local : kubectl pilote le cluster de Docker Desktop, la CLI Docker (et buildx) construit les
# images de l'application via le socket Docker de la machine, monté par docker-compose.yml.
COPY --from=k8stools /kubectl /usr/local/bin/kubectl
COPY --from=k8stools /helm /usr/local/bin/helm
# k3d : crée à la demande un cluster k3s léger (k3s dans des conteneurs Docker), cible alternative du déploiement.
COPY --from=k8stools /k3d /usr/local/bin/k3d
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
ENV AGENT_IN_DOCKER=1

WORKDIR /app

# SDK Anthropic (backend Claude). OpenAI, Gemini, Mistral et compatibles passent par la bibliothèque standard.
RUN pip install --no-cache-dir anthropic pyyaml

COPY devops_agent/ devops_agent/
COPY web/ web/
COPY agent.py server.py ./

# Rapports, clones et réglages (clés API) sont des données : montez-les en volume pour les garder.
VOLUME ["/app/out", "/app/.cache", "/app/data"]

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/models', timeout=4).status == 200 else 1)"

CMD ["python", "server.py"]
