# TP — Construire votre premier agent IA

Un agent Python qui lit un dépôt (README + arborescence + fichiers de configuration), demande à un
modèle d'IA d'agir comme un **ingénieur DevOps senior**, et restitue un résumé structuré de
l'architecture ainsi qu'une liste de risques — chaque affirmation avec sa preuve (le fichier d'où elle vient).

Puis il va plus loin, en quatre étapes enchaînées :

| Étape | Ce que fait l'agent | Où ça se voit |
|---|---|---|
| 1. Analyse | lit le dépôt, raisonne, liste l'architecture et les risques | étape **1** du fil |
| 2. Usine | corrige les risques sur une branche Git : Dockerfiles, manifests Kubernetes, pipeline CI/CD complet, observabilité | étape **2** du fil |
| 3. Cluster | construit les images de la branche corrigée et la déploie sur Docker Desktop ou k3s, avec l'observabilité, en réparant seul les erreurs | étape **3** du fil |
| 4. Publication | crée le dépôt sur **GitHub ou GitLab**, pousse la branche et ouvre la demande de fusion : le pipeline tourne chez la forge | étape **4** du fil |

Trois façons de s'en servir :

```bash
python agent.py https://gitlab.com/groupe/projet      # ligne de commande : rapport console + HTML/MD/JSON
python server.py                                     # interface web sur http://localhost:8080
docker compose up -d                                 # la même interface, sans rien installer
```

## Interface web — le fil en direct

```bash
python server.py            # http://127.0.0.1:8080
python server.py --port 9000 --host 0.0.0.0
```

Une seule colonne, sobre. On colle l'URL d'un dépôt, on choisit le modèle, **Analyser**. Chaque étape
s'ajoute ensuite sous la précédente, comme un fil qu'on lit de haut en bas :

1. **Analyse** : les dernières actions de l'agent défilent en direct, les fichiers lus apparaissent, puis
   les risques un par un. À la fin, un résumé : le projet, les chiffres clés, les risques (un clic en
   ouvre la preuve et la recommandation) et le bouton **Corriger les risques**.
2. **Usine logicielle** : on coche les risques et la plateforme, puis **Construire l'usine**. Les fichiers
   s'écrivent sous vos yeux, rangés par famille (images, pipeline, Kubernetes, observabilité, config),
   chacun passant de prévu à écrit puis validé.
3. **Déploiement** : les images se construisent et se chargent dans le cluster, les charges de travail
   démarrent, les réparations de l'agent s'affichent. À la fin, le lien vers l'appli et une vue qui
   reste en direct.
4. **Publication** : le dépôt, la pull request ou merge request, les pipelines.

Chaque étape en cours affiche son chronomètre et un bouton **Arrêter** : l'analyse, l'usine ou le
déploiement s'interrompt aussitôt, l'appel au modèle en cours compris.

Pendant qu'une tâche tourne, la page glisse d'elle-même vers ce qui s'écrit. Remonter la page met le
suivi en pause ; le bouton **Suivre en direct** le relance. Une étape terminée se replie en un résumé,
et son journal complet reste disponible d'un clic.

La barre du haut ouvre trois panneaux : **Missions** (les analyses précédentes), **Plateforme**
(observabilité du cluster) et **Réglages** (fournisseurs d'IA, jetons GitHub / GitLab, nettoyage).

Le serveur n'utilise que la bibliothèque standard (`http.server`). Chaque tâche tourne dans un thread ;
le navigateur interroge `GET /api/jobs/<id>` chaque seconde et reçoit, en plus du journal, un
**instantané structuré** (`snapshot`) de ce qui existe à cet instant : c'est lui que la page affiche.
Deux tâches peuvent tourner en parallèle (`AGENT_MAX_PARALLEL`).

| Route | Rôle |
|---|---|
| `POST /api/analyze` `{"target", "backend"?, "model"?, "refresh"?}` | lance une analyse, renvoie `job_id` |
| `GET /api/jobs/<id>` | état, journal, instantané, résultat |
| `GET /api/models` | backends, modèles et cibles de remédiation |
| `GET /api/reports` | missions : analyses, remédiations, déploiements, publications |
| `GET /reports/<nom>/analysis.html` | un rapport (aussi `.md`, `.json`, `prompt.txt`, `remediation/…`, `deploy/…`) |
| `POST /api/fix` `{"name", "risks"?, "target_kind"?}` | construit l'usine (étape 2) |
| `POST /api/deploy` `{"name", "cluster"?, "observability"?}` · `POST /api/undeploy` `{"name"}` | déploie / retire la branche corrigée (étape 3) ; `cluster` : `desktop` ou `k3s` |
| `GET /api/clusters` · `POST /api/clusters/delete` | clusters disponibles et leur état ; suppression du cluster k3s |
| `POST /api/jobs/<id>/cancel` | arrête une tâche en cours |
| `GET /api/cluster?name=<nom>` | vue en direct d'une appli déployée (charges, pods) |
| `GET` / `POST /api/platform` `{"action": "install"｜"uninstall", "cluster"?}` | observabilité d'un cluster |
| `GET` / `POST /api/git`, `POST /api/git/test` | jetons GitHub / GitLab (masqués) |
| `POST /api/publish` `{"name", "platform", "repo"?, "private"?}` | publie l'usine (étape 4) |
| `GET` / `POST /api/providers`, `POST /api/providers/test`, `POST /api/providers/refresh` | fournisseurs d'IA et leurs modèles réels |
| `GET /api/cache`, `POST /api/cache/clear` | dépôts clonés, analyses : liste et nettoyage |

## Docker

Première fois (construit l'image et crée le conteneur `devops-agent`) :

```bash
docker compose up -d --build
```

Ensuite, au quotidien : **ouvrir Docker Desktop, puis http://localhost:8080**. Le conteneur est en
`restart: unless-stopped` : il redémarre de lui-même avec Docker Desktop. S'il a été arrêté à la main,
onglet *Containers* → `devops-agent` → ▶ (ou `docker start devops-agent`).

Après une modification du code, la même commande reconstruit l'image et remplace le conteneur :

```bash
docker compose up -d --build
```

L'image (`python:3.12-slim`) embarque git, le binaire `opencode` (modèles gratuits, aucune clé à
fournir), `kubectl`, `helm`, `k3d` et le client Docker. Les rapports sont écrits dans `./out` (monté en volume) ;
les dépôts clonés et les réglages restent dans les volumes nommés `agent-cache` et `agent-data`.
Pour les étapes 3 et 4, le `docker-compose.yml` monte aussi :

- le socket Docker de la machine, pour construire les images de l'appli ;
- `~/.kube` en lecture seule, pour joindre le cluster de Docker Desktop (le serveur `127.0.0.1` est
  réécrit en `host.docker.internal` à l'intérieur du conteneur).

Les clés d'API peuvent venir de l'environnement (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
`MISTRAL_API_KEY`, `GITHUB_TOKEN`, `GITLAB_TOKEN`), d'un fichier `.env`, ou des réglages de l'interface.

## Fournisseurs d'IA

opencode (modèles gratuits, sans clé) fonctionne d'emblée. Les autres fournisseurs se configurent dans
le panneau **Réglages** : la clé API, puis **Enregistrer**. L'agent interroge alors le fournisseur et
n'affiche que **les modèles réellement disponibles** avec cette clé, du plus récent au plus ancien : aucune
liste écrite à l'avance, les nouveautés apparaissent d'elles-mêmes. La liste est gardée 15 minutes et
**Actualiser la liste** la redemande. On y choisit aussi le modèle par défaut ; **Tester** fait un appel
minimal. Une clé refusée ou expirée est signalée à la place des modèles.

| Fournisseur | Comment l'agent lui parle | Variable d'environnement |
|---|---|---|
| opencode | CLI `opencode run` | — |
| Anthropic (Claude) | SDK officiel `anthropic`, sortie structurée par schéma JSON | `ANTHROPIC_API_KEY` |
| OpenAI | HTTP `/chat/completions`, `response_format: json_object` | `OPENAI_API_KEY` |
| Google Gemini | HTTP `generateContent`, `responseMimeType: application/json` | `GEMINI_API_KEY` |
| Mistral | HTTP compatible OpenAI | `MISTRAL_API_KEY` |
| Compatible OpenAI | URL de base modifiable : Ollama (`http://localhost:11434/v1`), OpenRouter, Groq, DeepSeek… | `OPENAI_COMPAT_API_KEY` |

Les clés saisies dans l'interface sont stockées côté serveur dans `data/settings.json` (ignoré par Git et
par le build Docker, persisté dans le volume `agent-data`) et ne sont **jamais renvoyées en clair** au
navigateur, seulement masquées (`sk-t…7890`). Une clé enregistrée dans l'interface prend le pas sur la
variable d'environnement. Sans choix explicite, le fournisseur par défaut est le premier configuré dans
l'ordre Anthropic → OpenAI → Gemini → Mistral → compatible → opencode.

Aucune dépendance supplémentaire : OpenAI, Gemini, Mistral et les compatibles passent par la bibliothèque
standard (`urllib`). En ligne de commande : `python agent.py <cible> --backend gemini --model gemini-2.5-pro`.

> Depuis le conteneur Docker, un Ollama qui tourne sur votre machine se joint par
> `http://host.docker.internal:11434/v1`, pas `localhost`.

## Étape 2 — L'agent construit l'usine logicielle

Sous le résumé de l'analyse, **Corriger les risques** affiche les risques trouvés (cochés par défaut) et la cible, puis
**Construire l'usine**. En ligne de commande : `python agent.py <cible> --fix [--fix-target k8s-gitlab|k8s-github|compose]`.

La cible décide de la forge : GitLab CI et son registre, ou GitHub Actions et GHCR. Elle est proposée
d'après l'URL du dépôt analysé, et on peut en changer.

| Cible | Ce que l'agent produit |
|---|---|
| `k8s-gitlab` | Dockerfiles de production, manifests Kubernetes (`k8s/`), `.gitlab-ci.yml`, observabilité (`k8s/monitoring/`) |
| `k8s-github` | la même chose avec `.github/workflows/` et GHCR |
| `compose` | Docker Compose durci : secrets externalisés, healthchecks, limites, images de production |

Ce que contient l'usine Kubernetes :

- **Images** : Dockerfiles multi-étapes, utilisateur non root, `.dockerignore`, uniquement des fichiers
  qui existent dans le dépôt (vérifié).
- **Manifests** : namespace, Deployment + Service par composant avec labels `app.kubernetes.io/*`, probes,
  ressources, `securityContext`, volumes `emptyDir` pour les dossiers inscriptibles, ConfigMap, gabarit de
  Secret, Ingress, kustomization.
- **Pipeline** : lint → tests → détection de secrets (gitleaks) → analyse statique (Semgrep) → build des
  images → scan de vulnérabilités (Trivy) → SBOM (Syft) → push au registre → déploiement `kubectl apply -k`
  → test de fumée. Le déploiement ne s'exécute que si le secret `KUBE_CONFIG` est défini.
- **Observabilité** : `ServiceMonitor` et règles d'alerte Prometheus dans `k8s/monitoring/`, avec leur
  propre kustomization, appliqués seulement si le cluster a Prometheus Operator.

### Comment ça marche — une vraie boucle agentique

Un seul appel ne suffit pas : les modèles coupent les longues sorties, et un plan écrit avant le code
donne de meilleurs fichiers. La remédiation ([fixer.py](devops_agent/fixer.py)) enchaîne donc :

1. **Plan** (1 appel) — quelles modifications, dans quels fichiers, pour quels risques ; ce qui ne sera
   pas corrigé et pourquoi ; les étapes manuelles qui resteront à l'équipe.
2. **Fichiers** (n appels, par lots de 3) — le contenu complet de chaque fichier ; pour une modification,
   le contenu actuel est fourni au modèle, qui renvoie le fichier entier.
3. **Application** — sur une **branche Git dédiée** du clone (`remediation/…`), jamais sur la branche
   courante, avec un commit. Rien n'est poussé sans votre clic (étape 4).
4. **Validation** — JSON, YAML, Dockerfile (instruction de tête, fichiers copiés présents) ;
   `kubectl kustomize` sur les dossiers qui ont une kustomization (rendu pur, sans cluster) et
   `kubectl apply --dry-run=client` sur les manifests simples — si aucun cluster n'est joignable, le
   résultat est « non vérifiable », pas un échec.
5. **Réparation** — chaque validation en échec est renvoyée au modèle avec le message d'erreur exact,
   trois fichiers maximum, puis revalidation. Le rapport indique « réparé » ou « toujours en échec ».
6. **Rapport** — `out/<projet>/remediation/` : `REMEDIATION.md`, `patch.diff`, `changes.json`.

Garde-fous : le modèle ne touche qu'à la configuration et au déploiement (pas au code applicatif),
les chemins sont vérifiés (pas de `..`, pas de `.git/`), 25 fichiers maximum, aucun secret en clair
(variables de CI, Secrets Kubernetes en gabarit, `.env.example`).

Ordre de grandeur avec un modèle gratuit : **5 à 15 minutes** pour une vingtaine de fichiers. Chaque
remédiation repart de la **branche par défaut** du dépôt, jamais d'une remédiation précédente.

## Étape 3 — Déploiement sur le cluster local

Sous le résumé de l'usine, **Déployer sur le cluster** propose deux cibles :

| Cluster | Ce que c'est | Adresses de l'appli |
|---|---|---|
| Docker Desktop | le Kubernetes intégré, à activer dans *Settings → Kubernetes* | `http://localhost:18080`, `http://<namespace>.localhost` |
| k3s | un cluster léger créé par l'agent avec k3d, dans des conteneurs Docker ; environ une minute la première fois | `http://localhost:18200`, `http://<namespace>.localhost:18280` |

La case **Observabilité**, cochée par défaut, ajoute Prometheus, Grafana et Loki au cluster lors du premier
déploiement (voir plus bas). L'agent ([deployer.py](devops_agent/deployer.py)) :

1. vérifie Docker et le cluster ; pour k3s, crée ou redémarre le cluster `devops-agent` ;
2. installe l'observabilité si elle manque sur ce cluster, sinon vérifie seulement qu'elle est en place ;
3. extrait la branche corrigée (`git archive`, le clone n'est pas touché) ;
4. repère les images à construire dans le pipeline (`docker build`, kaniko) ou par convention, et les
   construit avec le Docker de la machine ;
5. les importe dans le cluster, qui ne voit pas les images locales : `ctr images import` via un petit
   DaemonSet `image-loader` pour Docker Desktop, `k3d image import` pour k3s ;
6. rend les manifests (`kubectl kustomize`) et les adapte au local : images locales avec
   `imagePullPolicy: Never`, classe de stockage par défaut, secrets de gabarit remplacés par des valeurs
   générées (conservées d'un déploiement à l'autre), Ingress sur `http://<namespace>.localhost` ;
7. applique, suit le démarrage des pods et publie l'appli sur le premier port libre du cluster.

Quand un démarrage échoue, l'agent lit le diagnostic du cluster (événements, journaux des pods), corrige
les manifests **dans la branche**, commite, reconstruit et redéploie, trois fois au plus. Les erreurs
connues sont corrigées par des **règles déterministes**, sans appel au modèle :

| Erreur | Correctif |
|---|---|
| `COPY` d'un fichier absent, `npm ci` sans lockfile | instruction retirée, `npm install` |
| fichier de `configMapGenerator` hors du dossier, option kustomize inexistante | chemin et options corrigés |
| conteneur non root qui écrit dans `/var/cache/nginx`, `/tmp`… | volumes `emptyDir` |
| ConfigMap ou Secret référencé sous un autre nom | noms alignés |
| PostgreSQL qui ne peut pas initialiser son volume | `PGDATA` dans un sous-dossier, uid de l'image (70 alpine, 999 Debian) |
| Ingress qui vise un service inexistant | service corrigé |
| sondes de santé refusées en HTTP 429 (limitation de débit) | sondes TCP sur le même port |

Après un démarrage réussi, l'agent observe encore une vingtaine de secondes : des sondes refusées rendent l'appli instable, c'est traité comme un échec.
Le reste part au modèle, avec le seul contexte utile à l'étape en échec. Rapport :
`out/<projet>/deploy/DEPLOYMENT.md` et `manifests.yaml`. **Retirer** supprime le namespace ; la branche
corrigée reste.

## Observabilité

Elle s'installe **au fil du premier déploiement** sur un cluster : l'étape *Observabilité* du fil montre
chaque brique arriver, environ 5 minutes et 2 Go de mémoire la première fois. Les déploiements suivants
vérifient seulement qu'elle est en place. Les charts officiels sont installés par Helm, en versions épinglées.

| Brique | Chart | Rôle |
|---|---|---|
| Métriques et alertes | `kube-prometheus-stack` | Prometheus, Alertmanager, Grafana, métriques du cluster |
| Entrée | `traefik` | Ingress sur Docker Desktop ; k3s embarque déjà le sien |
| Logs | `loki` | stockage des journaux |
| Collecte | `alloy` | envoie à Loki les journaux de tous les pods |

Les accès apparaissent dans le résultat du déploiement : Grafana (utilisateur `admin`, mot de passe
généré), Prometheus et Alertmanager, sur `http://grafana.localhost` pour Docker Desktop et
`http://grafana.localhost:18280` pour k3s. Grafana contient les sources Prometheus, Loki et Alertmanager,
et un tableau de bord « Applications déployées par l'agent ». Les applis sont observées d'office : leurs
`ServiceMonitor` et alertes sont appliqués après le déploiement.

Panneau **Réglages**, section *Clusters* : l'état de chaque cluster, **Retirer l'observabilité**, et
**Supprimer** le cluster k3s, recréé au prochain déploiement sur k3s.

> Les adresses `*.localhost` fonctionnent dans les navigateurs. Sur Docker Desktop, si le port 80 est déjà
> pris, Traefik passe sur le port 18000 et les adresses deviennent `http://grafana.localhost:18000`.

## Étape 4 — Publier sur GitHub ou GitLab

Sous le résumé de l'usine, **Publier sur GitHub ou GitLab** ouvre un formulaire : la forge, le nom du dépôt et sa
visibilité. Un jeton personnel est nécessaire, à saisir dans les réglages (**Réglages** → *GitHub et GitLab*) :

| Forge | Droits du jeton |
|---|---|
| GitHub | jeton classique : `repo` + `workflow` ; jeton fin : Administration, Contents, Pull requests, Workflows en écriture |
| GitLab (gitlab.com ou votre instance) | portée `api` |

Au clic, et après confirmation, l'agent ([publisher.py](devops_agent/publisher.py)) :

1. vérifie le jeton et identifie le compte ;
2. crée le dépôt (privé par défaut) ou réutilise celui du même nom ;
3. pousse la branche de base puis la branche corrigée ;
4. ouvre la pull request (GitHub) ou la merge request (GitLab) : le pipeline se lance chez la forge.

Rien n'est fusionné automatiquement. Pour activer le job de déploiement du pipeline, ajoutez le secret
`KUBE_CONFIG` (le kubeconfig du cluster cible) : le lien *Secrets CI* y mène directement. Si l'usine a
été générée pour l'autre forge, l'agent prévient que le pipeline ne se lancera pas.

Le jeton passe à `git` par l'environnement du processus : il n'apparaît ni dans une URL, ni dans la
ligne de commande, ni dans les journaux.

## Sécurité

L'agent est un outil local, pour votre poste. Ce qu'il faut savoir :

- **Le socket Docker** monté dans le conteneur donne un accès équivalent à root sur Docker Desktop.
  C'est ce qui permet de construire les images de l'appli.
- **Le kubeconfig** est monté en lecture seule, mais il donne les droits d'administrateur du cluster local.
- **Le cluster k3s** tourne dans des conteneurs Docker créés par k3d (`k3d-devops-agent-*`). Il publie
  sur la machine les ports 6550 (API), 18200 à 18209 (applis) et 18280 (Ingress).
- **Le DaemonSet `image-loader`** est privilégié : il écrit dans le containerd du nœud. Il est supprimé
  par le nettoyage.
- **Les clés et jetons** restent dans `data/settings.json`, masqués dans l'interface.
- N'exposez pas le port 8080 hors de votre machine.

## Nettoyage

Panneau **Réglages**, bouton **Tout nettoyer** : retire des clusters, Docker Desktop et k3s, les applis déployées par l'agent, puis vide
`.cache/repos/` et `out/`. Clones, rapports, remédiations et branches non publiées disparaissent.
Les réglages (`data/`, dont les clés) et la plateforme d'observabilité sont conservés. Refusé tant
qu'une tâche tourne ; l'interface demande confirmation. En filet de sécurité, il retire aussi du cluster tout
ce qui porte le label `devops-agent/deploy` et les images locales `devops-agent/*`, même si leur trace sur
disque a disparu. Les namespaces de la plateforme et du système ne sont jamais touchés.

Pour une seule mission : bouton **Supprimer** en face d'elle, sur la page d'accueil ou dans le panneau
**Missions**. Son analyse, son usine, son clone local et son appli déployée sont supprimés définitivement.

## Installation (ligne de commande)

Prérequis : Python 3.10+ et **un** des deux backends LLM.

| Backend | Quand | Installation |
|---|---|---|
| `opencode` (par défaut si pas de clé) | Gratuit, modèles « opencode zen » | `npm install -g opencode-ai` ou l'installeur Windows |
| `anthropic` | Vous avez une clé API | `pip install anthropic` puis `set ANTHROPIC_API_KEY=sk-ant-…` |

Aucune autre dépendance : le reste est la bibliothèque standard.

> **Piège Windows.** Si Python vient du *Python install manager* (`AppData\Local\Python\…`), il tourne
> dans un conteneur qui **ne voit pas** `%APPDATA%\npm`, là où npm met la commande `opencode`.
> L'agent le contourne en cherchant aussi `%LOCALAPPDATA%\Programs\opencode\opencode.exe` : copiez-y le
> binaire autonome (`%APPDATA%\npm\node_modules\opencode-ai\node_modules\opencode-windows-x64\bin\opencode.exe`),
> ou pointez la variable `OPENCODE_BIN` vers lui.

## Utilisation

```bash
# Analyse complète (backend choisi automatiquement) — dossier local ou URL Git
python agent.py ../mon-projet
python agent.py https://gitlab.com/groupe/projet          # cloné en profondeur 1 dans .cache/repos/
python agent.py https://gitlab.com/groupe/projet --refresh --open   # recloner, puis ouvrir le rapport HTML

# Choisir le modèle
python agent.py ../mon-projet --backend opencode --model opencode/nemotron-3-ultra-free
python agent.py ../mon-projet --backend anthropic --model claude-opus-5

# Voir le prompt sans appeler le modèle (utile pour comprendre — et pour la restitution du TP)
python agent.py ../mon-projet --dry-run

# Régler ce que l'agent lit
python agent.py ../mon-projet --depth 3 --budget 50000
```

Sortie : le rapport dans la console, plus dans `out/<nom-du-projet>/` :

- `analysis.html` — le rapport mis en page, autonome, à ouvrir dans un navigateur ;
- `analysis.md` — le même en Markdown, prêt à coller dans une présentation ;
- `analysis.json` — la réponse structurée du modèle, telle quelle ;
- `prompt.txt` — le prompt exact envoyé au modèle.

## Comment l'agent est construit

Le mot « agent » désigne ici une boucle **percevoir → raisonner → agir → restituer**, chaque étape dans son module :

```
agent.py                 la ligne de commande
server.py                l'interface web (mêmes étapes, exposées en API + page HTML dans web/)
devops_agent/
  source.py              CIBLE        dossier local, ou URL Git clonée en profondeur 1
  explorer.py            PERCEPTION   lit le README, dessine l'arborescence, sélectionne les fichiers signaux
  prompt.py              RAISONNEMENT le prompt « DevOps senior » + la forme JSON exigée en retour
  llm.py                 ACTION       appelle le modèle (SDK Anthropic ou CLI opencode) et extrait le JSON
  report.py              RESTITUTION  valide, affiche, exporte (console, HTML, Markdown, JSON)
  pipeline.py            enchaîne les étapes ; partagé par la ligne de commande et le serveur
```

### Robustesse face aux modèles gratuits

Les modèles gratuits sont capricieux, et l'agent le sait :

- réponse enveloppée dans des fences Markdown ou noyée dans du bavardage → l'extracteur énumère tous les
  objets JSON et garde celui qui porte les bonnes clés ;
- sortie **coupée** avant la fin → le JSON est réparé (refermé au dernier élément complet), avec un
  avertissement dans le rapport ;
- liste de risques vide ou JSON inexploitable → **une relance automatique** avec un rappel explicite,
  et la réponse brute est conservée dans `out/<projet>/raw_response_N.txt` pour comprendre ce qui s'est passé.

Ce sont trois défaillances observées en vrai pendant la construction du TP, pas des hypothèses.

### 1. Perception — `explorer.py`

Un DevOps qui découvre un dépôt n'ouvre pas tout le code. Il ouvre le README, regarde l'arborescence,
puis les fichiers qui disent *comment* l'application est construite et déployée. L'agent fait pareil :

- **README** (jusqu'à 20 000 caractères) ;
- **arborescence** limitée en profondeur, en ignorant `node_modules`, `.git`, `venv`, `dist`… ;
- **fichiers signaux** : `Dockerfile*`, `docker-compose*`, `.gitlab-ci.yml`, workflows GitHub, `*.tf`,
  `package.json`, `requirements.txt`, manifests k8s, `prometheus.yml`, `.env.example`…

Deux garde-fous évitent de saturer le modèle (et la facture) :

- un **budget global** de caractères (80 000 par défaut), distribué en **tourniquet** entre les catégories —
  le 1er fichier de chaque catégorie, puis le 2e de chaque… — pour qu'un projet à dix Dockerfiles ne fasse
  pas disparaître son `package.json` ;
- un **dédoublonnage** par empreinte : dix Dockerfiles identiques deviennent un extrait plus neuf mentions
  « identique à … », ce qui est en soi une information pour le modèle.

### 2. Raisonnement — `prompt.py`

Le *system prompt* fixe la posture (DevOps senior qui arrive dans l'entreprise) et six règles, dont les
trois qui font la différence entre une analyse utile et un texte générique :

1. **raisonner uniquement à partir des extraits**, sinon marquer l'hypothèse ;
2. **citer la preuve** (fichier + élément précis) pour chaque affirmation ;
3. distinguer un composant **déclaré** d'un composant **utilisé**, et signaler quand la documentation
   contredit la configuration.

La sortie est contrainte par un **schéma JSON** (`OUTPUT_SCHEMA`) : projet, architecture (composants,
données, services externes, communication), déploiement (conteneurs, orchestration, CI/CD, IaC, cloud,
monitoring), risques (titre, sévérité, catégorie, description, preuve, recommandation), questions ouvertes,
confiance. Sans cette contrainte, le rapport ne serait pas exploitable par du code.

### 3. Action — `llm.py`

Même interface pour deux backends :

- **Anthropic** : SDK officiel, sortie structurée native (`output_config` + schéma JSON) — le modèle ne
  *peut pas* répondre hors format. Streaming pour supporter les entrées longues.
- **opencode** : le CLI en sous-processus, prompt sur **stdin** (pas de limite de taille d'argument),
  réponse lue dans le flux d'événements `--format json`. Le schéma est rappelé sous forme de **gabarit à
  remplir** — un modèle remplit bien mieux un exemple qu'il ne lit un JSON Schema, qu'il a tendance à recopier.
  Un `opencode.json` désactive les outils du CLI : c'est *notre* agent qui a fait la perception, on ne veut
  qu'un appel de modèle.

L'extracteur JSON tolère les fences Markdown et le bavardage : il énumère tous les objets JSON de la réponse
et garde celui qui porte les clés attendues.

### 4. Restitution — `report.py`

Validation minimale (clés présentes, sévérités normalisées, risques triés du critique au faible), affichage
console, export JSON et Markdown.

## Ce qu'il faut retenir pour la restitution

- **Une réponse de l'IA n'est pas une preuve.** Le schéma exige un champ `evidence` partout ; à vous de
  l'ouvrir. Le rapport `analysis.md` est un point de départ, pas une conclusion.
- **Le contexte est la matière première.** Changez `--depth` ou `--budget`, relancez, comparez : la qualité
  de l'analyse suit directement ce que l'agent a *vu*.
- **Comparez les modèles.** `--model opencode/…` avec chacun des modèles gratuits sur le même dépôt :
  lesquels respectent le format, citent de vraies preuves, distinguent déclaré et utilisé ?
- **Lisez le prompt.** `--dry-run` montre exactement ce que reçoit le modèle. C'est la pièce à montrer
  quand on vous demande « quelle question avez-vous posée à l'IA, et pourquoi ».

## Pistes d'amélioration

- Une seconde passe qui **vérifie chaque preuve** citée (le fichier existe-t-il ? contient-il l'élément ?)
  et dégrade la confiance sinon.
- Donner au modèle un vrai **outil** `read_file` (tool use) pour qu'il demande lui-même les fichiers qui
  lui manquent, au lieu de tout recevoir d'avance.
- Un mode **diff** : ne réanalyser que ce qui a changé depuis le dernier rapport.
