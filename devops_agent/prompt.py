"""Raisonnement : le contrat entre l'agent et le modèle.

Deux pièces :
  - SYSTEM_PROMPT : la posture demandée au modèle (ingénieur DevOps senior, preuves obligatoires) ;
  - OUTPUT_SCHEMA : la forme exacte de la réponse, pour qu'elle soit exploitable par du code.
"""

from __future__ import annotations

import json

from .explorer import RepoContext

SYSTEM_PROMPT = """Tu es un ingénieur DevOps senior. Tu viens de rejoindre une entreprise et on te donne
accès à un dépôt que tu ne connais pas. Ta mission : expliquer à l'équipe, de façon fiable,
ce que fait cette application, comment elle est construite et déployée, et quels risques tu vois.

Règles de travail, non négociables :
1. Tu raisonnes UNIQUEMENT à partir des extraits fournis (README, arborescence, fichiers de configuration).
   Tu ne devines pas ce qui n'y figure pas.
2. Chaque affirmation importante cite sa preuve : le chemin du fichier (et si possible la ligne ou la clé)
   d'où elle vient. Sans preuve dans les extraits, tu la présentes comme une hypothèse.
3. Quand la documentation et la configuration se contredisent, la configuration gagne, et tu signales
   la contradiction : c'est un risque en soi.
4. Un composant DÉCLARÉ (dans un compose, un package.json, un Terraform) n'est pas forcément UTILISÉ.
   Distingue les deux quand les extraits le permettent.
5. Les risques sont concrets et actionnables : un titre, la preuve, l'impact, une recommandation.
   Pas de généralités valables pour n'importe quel projet.
6. Tu réponds en français, dans le format JSON demandé, et rien d'autre."""

# Schéma JSON de la réponse. Sert deux fois : imposé au modèle (structured output côté Anthropic,
# recopié dans le prompt côté opencode) et utilisé par report.py pour valider ce qui revient.
OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["project", "architecture", "deployment", "risks", "open_questions", "confidence"],
    "properties": {
        "project": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "purpose", "main_technologies"],
            "properties": {
                "name": {"type": "string"},
                "purpose": {"type": "string", "description": "Ce que fait l'application, en 1 à 3 phrases."},
                "main_technologies": {"type": "array", "items": {"type": "string"}},
            },
        },
        "architecture": {
            "type": "object",
            "additionalProperties": False,
            "required": ["style", "components", "data_stores", "external_services", "communication"],
            "properties": {
                "style": {"type": "string", "description": "Monolithe, microservices, serverless, etc. — justifié."},
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "role", "technology", "evidence"],
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "technology": {"type": "string"},
                            "evidence": {"type": "string", "description": "Fichier(s) qui prouvent l'existence et le rôle du composant."},
                        },
                    },
                },
                "data_stores": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "technology", "used_by", "evidence"],
                        "properties": {
                            "name": {"type": "string"},
                            "technology": {"type": "string"},
                            "used_by": {"type": "array", "items": {"type": "string"}},
                            "evidence": {"type": "string"},
                        },
                    },
                },
                "external_services": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "purpose", "evidence"],
                        "properties": {
                            "name": {"type": "string"},
                            "purpose": {"type": "string"},
                            "evidence": {"type": "string"},
                        },
                    },
                },
                "communication": {"type": "string", "description": "Comment les composants se parlent : REST, gRPC, file de messages, WebSocket…"},
            },
        },
        "deployment": {
            "type": "object",
            "additionalProperties": False,
            "required": ["containerization", "orchestration", "ci_cd", "infrastructure_as_code", "cloud", "monitoring"],
            "properties": {
                "containerization": {"type": "string"},
                "orchestration": {"type": "string"},
                "ci_cd": {"type": "string"},
                "infrastructure_as_code": {"type": "string"},
                "cloud": {"type": "string"},
                "monitoring": {"type": "string"},
            },
            "description": "Pour chaque champ : ce qui est en place (avec la preuve) ou « non trouvé dans les extraits ».",
        },
        "risks": {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "severity", "category", "description", "evidence", "recommendation"],
                "properties": {
                    "title": {"type": "string"},
                    "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                    "category": {
                        "type": "string",
                        "enum": ["security", "secrets", "availability", "data", "network", "ci_cd", "dependencies", "observability", "scalability", "operations", "documentation"],
                    },
                    "description": {"type": "string"},
                    "evidence": {"type": "string", "description": "Chemin de fichier + élément précis (ligne, clé, valeur)."},
                    "recommendation": {"type": "string"},
                },
            },
        },
        "open_questions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Questions à poser à l'équipe sur ce que les extraits ne permettent pas de trancher.",
        },
        "confidence": {
            "type": "string",
            "enum": ["low", "medium", "high"],
            "description": "Confiance globale, selon la quantité et la cohérence des preuves disponibles.",
        },
    },
}


# Gabarit d'exemple, dérivé du schéma. Pour les backends sans structured output, un modèle
# remplit bien mieux un exemple qu'il ne lit un schéma JSON Schema (qu'il a tendance à recopier).
OUTPUT_TEMPLATE: dict = {
    "project": {
        "name": "nom du projet",
        "purpose": "ce que fait l'application, 1 à 3 phrases",
        "main_technologies": ["techno 1", "techno 2"],
    },
    "architecture": {
        "style": "monolithe | microservices | … — justifié en une phrase",
        "components": [
            {"name": "composant", "role": "ce qu'il fait", "technology": "langage/framework", "evidence": "chemin/du/fichier"}
        ],
        "data_stores": [
            {"name": "base ou cache", "technology": "PostgreSQL 16, Redis 7…", "used_by": ["composant"], "evidence": "chemin/du/fichier"}
        ],
        "external_services": [
            {"name": "service tiers", "purpose": "à quoi il sert", "evidence": "chemin/du/fichier"}
        ],
        "communication": "REST, WebSocket, file de messages… avec preuves",
    },
    "deployment": {
        "containerization": "… ou « non trouvé dans les extraits »",
        "orchestration": "…",
        "ci_cd": "…",
        "infrastructure_as_code": "…",
        "cloud": "…",
        "monitoring": "…",
    },
    "risks": [
        {
            "title": "titre court",
            "severity": "critical | high | medium | low",
            "category": "security | secrets | availability | data | network | ci_cd | dependencies | observability | scalability | operations | documentation",
            "description": "le problème et son impact",
            "evidence": "chemin/du/fichier : élément précis (ligne, clé, valeur)",
            "recommendation": "action concrète",
        }
    ],
    "open_questions": ["question à poser à l'équipe"],
    "confidence": "low | medium | high",
}


def build_user_prompt(ctx: RepoContext, *, include_schema: bool) -> str:
    """Assemble le message utilisateur : contexte du dépôt + consigne.

    `include_schema` : True pour les backends sans structured output natif (opencode),
    où la forme attendue doit être rappelée en clair dans le prompt.
    """
    parts: list[str] = []
    parts.append(f"# Dépôt à analyser : {ctx.name}\n")
    parts.append(
        "Tout le contexte nécessaire est dans ce message. N'utilise aucun outil, ne cherche aucun "
        "fichier : analyse les extraits ci-dessous et réponds directement."
    )
    parts.append(f"Fichiers visibles dans l'arborescence : {ctx.file_count}.")
    if ctx.skipped_key_files:
        parts.append(
            "Fichiers de configuration détectés mais NON inclus (budget atteint) — "
            "leur simple existence est une information : " + ", ".join(ctx.skipped_key_files)
        )

    parts.append("\n## 1. README" + (f" ({ctx.readme_path})" if ctx.readme_path else ""))
    parts.append(ctx.readme.strip() if ctx.readme.strip() else "(aucun README trouvé)")

    parts.append("\n## 2. Arborescence (profondeur limitée, dossiers de dépendances exclus)")
    parts.append("```\n" + ctx.tree + "\n```")

    parts.append("\n## 3. Fichiers de configuration et de déploiement")
    if not ctx.key_files:
        parts.append("(aucun fichier signal trouvé : Dockerfile, compose, CI, Terraform, manifests…)")
    for f in ctx.key_files:
        parts.append(f"\n### {f.path}" + (" (tronqué)" if f.truncated else ""))
        parts.append("```\n" + f.content.rstrip() + "\n```")

    parts.append(
        "\n## 4. Ta mission\n"
        "Produis un résumé structuré de l'architecture, du mode de déploiement, et une liste d'au moins "
        "trois risques classés par sévérité, chacun avec sa preuve (fichier + élément précis) et une "
        "recommandation. Termine par les questions que tu poserais à l'équipe."
    )
    if include_schema:
        parts.append(
            "\nRéponds UNIQUEMENT avec un objet JSON valide qui REMPLIT ce gabarit avec les informations du dépôt "
            "(mêmes clés, autant d'éléments que nécessaire dans les listes, au moins 3 risques). "
            "Sois dense : 1 à 3 phrases par champ texte, pas de répétition entre champs. "
            "Ne recopie pas le gabarit, ne mets aucun texte avant ni après, pas de bloc de code Markdown, "
            "et termine bien l'objet JSON :\n"
            + json.dumps(OUTPUT_TEMPLATE, ensure_ascii=False, indent=1)
        )
    return "\n".join(parts)
