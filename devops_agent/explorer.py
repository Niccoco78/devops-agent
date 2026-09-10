"""Perception : collecte ce que l'agent va « voir » du dépôt.

On ne lit pas tout le code — on lit ce qu'un DevOps senior ouvrirait en premier :
le README, l'arborescence, puis les fichiers qui décrivent comment l'application
est construite, exécutée et déployée.
"""

from __future__ import annotations

import fnmatch
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

# Dossiers qu'on ne descend jamais : bruit, dépendances, artefacts de build.
IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".terraform", ".next", ".cache", "coverage", "target", ".idea", ".vscode",
}

# Fichiers « signaux » : leur simple présence dit quelque chose sur l'infra.
# L'ordre compte : c'est aussi l'ordre de priorité si le budget de caractères est atteint.
KEY_FILE_PATTERNS = [
    "docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml",
    "Dockerfile", "Dockerfile.*",
    ".gitlab-ci.yml", ".github/workflows/*.yml", ".github/workflows/*.yaml", "Jenkinsfile", ".circleci/config.yml",
    "main.tf", "providers.tf", "variables.tf",
    "package.json", "pyproject.toml", "requirements.txt", "go.mod", "pom.xml", "build.gradle", "Cargo.toml",
    ".env.example", ".env.sample",
    "Makefile", "Procfile",
    "k8s/*.yml", "k8s/*.yaml", "kubernetes/*.yml", "kubernetes/*.yaml", "helm/*/values.yaml",
    "prometheus.yml", "alert-rules.yml", "nginx.conf",
]

README_NAMES = ("README.md", "README.MD", "readme.md", "README.rst", "README.txt", "README")


@dataclass
class FileExcerpt:
    path: str          # chemin relatif à la racine du dépôt, avec des « / »
    content: str       # contenu (éventuellement tronqué)
    truncated: bool


@dataclass
class RepoContext:
    root: Path
    name: str
    readme_path: str | None
    readme: str
    readme_truncated: bool
    tree: str                       # arborescence texte, style `tree`
    file_count: int
    key_files: list[FileExcerpt] = field(default_factory=list)
    skipped_key_files: list[str] = field(default_factory=list)   # trouvés mais hors budget

    @property
    def total_chars(self) -> int:
        return len(self.readme) + len(self.tree) + sum(len(f.content) for f in self.key_files)


def _read_text(path: Path, limit: int) -> tuple[str, bool]:
    """Lit un fichier texte en tolérant les encodages douteux ; tronque à `limit` caractères."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", False
    if len(raw) > limit:
        return raw[:limit] + f"\n… [tronqué : {len(raw) - limit} caractères omis]", True
    return raw, False


def _is_ignored(name: str) -> bool:
    return name in IGNORED_DIRS


def build_tree(root: Path, max_depth: int, max_entries: int) -> tuple[str, int]:
    """Arborescence textuelle limitée en profondeur et en nombre d'entrées.

    Retourne (texte, nombre total de fichiers rencontrés).
    """
    lines: list[str] = [root.name + "/"]
    count = 0
    budget = [max_entries]  # liste pour muter dans la fonction imbriquée

    def walk(dir_path: Path, prefix: str, depth: int) -> None:
        nonlocal count
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (PermissionError, OSError):
            return
        entries = [e for e in entries if not _is_ignored(e.name)]
        for i, entry in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            if budget[0] <= 0:
                lines.append(prefix + "└── … (limite d'affichage atteinte)")
                return
            budget[0] -= 1
            if entry.is_dir():
                lines.append(f"{prefix}{branch}{entry.name}/")
                if depth < max_depth:
                    walk(entry, prefix + ("    " if last else "│   "), depth + 1)
                else:
                    lines.append(prefix + ("    " if last else "│   ") + "└── …")
            else:
                count += 1
                lines.append(f"{prefix}{branch}{entry.name}")

    walk(root, "", 1)
    return "\n".join(lines), count


def find_key_files(root: Path, max_depth: int) -> list[Path]:
    """Cherche les fichiers « signaux » jusqu'à `max_depth` niveaux, sans entrer dans les dossiers ignorés."""
    found: dict[str, Path] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(_is_ignored(part) for part in rel.parts[:-1]):
            continue
        if len(rel.parts) > max_depth:
            continue
        rel_posix = rel.as_posix()
        for pattern in KEY_FILE_PATTERNS:
            # On matche soit le nom seul, soit le chemin relatif (pour `.github/workflows/*.yml`).
            if fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(rel_posix, pattern) or rel_posix.endswith("/" + pattern):
                found.setdefault(rel_posix, path)
                break
    # Regroupe par pattern, chaque groupe trié par profondeur (près de la racine d'abord).
    def pattern_index(rel_posix: str, p: Path) -> int:
        for idx, pattern in enumerate(KEY_FILE_PATTERNS):
            if fnmatch.fnmatch(p.name, pattern) or fnmatch.fnmatch(rel_posix, pattern) or rel_posix.endswith("/" + pattern):
                return idx
        return len(KEY_FILE_PATTERNS)

    groups: dict[int, list[Path]] = {}
    for rel_posix, p in found.items():
        groups.setdefault(pattern_index(rel_posix, p), []).append(p)
    for paths in groups.values():
        paths.sort(key=lambda p: (len(p.relative_to(root).parts), p.as_posix()))

    # Tourniquet : le 1er fichier de chaque catégorie, puis le 2e de chaque, etc.
    # Ainsi un projet à 10 Dockerfiles ne fait pas disparaître le package.json ou le Terraform.
    ordered: list[Path] = []
    round_idx = 0
    while any(len(paths) > round_idx for paths in groups.values()):
        for idx in sorted(groups):
            if len(groups[idx]) > round_idx:
                ordered.append(groups[idx][round_idx])
        round_idx += 1
    return ordered


def explore(
    root: Path,
    *,
    max_depth: int = 4,
    max_tree_entries: int = 400,
    readme_limit: int = 20_000,
    file_limit: int = 6_000,
    total_budget: int = 80_000,
) -> RepoContext:
    """Construit le contexte que l'agent enverra au modèle.

    `total_budget` borne la taille totale (README + arbre + extraits) pour ne pas
    saturer la fenêtre de contexte du modèle ni la facture.
    """
    root = root.resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Dossier introuvable : {root}")

    readme_path, readme, readme_trunc = None, "", False
    for name in README_NAMES:
        candidate = root / name
        if candidate.is_file():
            readme, readme_trunc = _read_text(candidate, readme_limit)
            readme_path = name
            break

    tree, file_count = build_tree(root, max_depth, max_tree_entries)

    ctx = RepoContext(
        root=root, name=root.name, readme_path=readme_path, readme=readme,
        readme_truncated=readme_trunc, tree=tree, file_count=file_count,
    )

    remaining = total_budget - len(readme) - len(tree)
    seen_hashes: dict[str, str] = {}   # empreinte du contenu -> premier chemin qui l'a
    for path in find_key_files(root, max_depth):
        rel = path.relative_to(root).as_posix()
        if remaining <= 0:
            ctx.skipped_key_files.append(rel)
            continue
        content, truncated = _read_text(path, min(file_limit, remaining))
        if not content.strip():
            continue
        # Dédoublonnage : 10 Dockerfiles identiques n'apportent rien de plus que le premier,
        # mais savoir qu'ils sont identiques est une information utile pour le modèle.
        digest = hashlib.sha1(content.encode("utf-8", "replace")).hexdigest()
        if digest in seen_hashes:
            content = f"(contenu strictement identique à {seen_hashes[digest]})"
            truncated = False
        else:
            seen_hashes[digest] = rel
        ctx.key_files.append(FileExcerpt(path=rel, content=content, truncated=truncated))
        remaining -= len(content)

    return ctx
