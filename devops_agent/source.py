"""Résolution de la cible : un dossier local, ou une URL Git à cloner.

    python agent.py ../mon-projet
    python agent.py https://gitlab.com/groupe/projet
    python agent.py git@github.com:org/repo.git

Les dépôts distants sont clonés en profondeur 1 (pas d'historique) dans `.cache/repos/`,
et réutilisés d'un run à l'autre sauf `--refresh`.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

# Cache à côté du code, quel que soit le dossier d'où l'on lance l'agent ou le serveur.
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "repos"
_URL_RE = re.compile(r"^(https?://|git@|ssh://|git://)")


def is_remote(target: str) -> bool:
    return bool(_URL_RE.match(target.strip()))


def _slug(url: str) -> str:
    """https://gitlab.com/ftutorials-projets/devops-ia-generer -> ftutorials-projets__devops-ia-generer"""
    path = re.sub(r"^(https?://[^/]+/|git@[^:]+:|ssh://[^/]+/|git://[^/]+/)", "", url.strip())
    path = re.sub(r"\.git/?$", "", path).strip("/")
    return re.sub(r"[^A-Za-z0-9._-]+", "__", path) or "repo"


def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def list_clones() -> list[dict]:
    """Les dépôts clonés dans le cache : nom, taille, date."""
    if not CACHE_DIR.is_dir():
        return []
    items = []
    for d in sorted(CACHE_DIR.iterdir()):
        if d.is_dir():
            items.append({"name": d.name, "bytes": _dir_size(d), "mtime": d.stat().st_mtime})
    return items


def _force_remove(func, path, _exc):
    """Sous Windows, les objets Git sont en lecture seule : on lève l'attribut puis on réessaie."""
    import os, stat
    os.chmod(path, stat.S_IWRITE)
    func(path)


def dir_size(path: Path) -> int:
    return _dir_size(path) if path.is_dir() else 0


def _rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path, onexc=_force_remove)        # Python ≥ 3.12
    except TypeError:
        shutil.rmtree(path, onerror=_force_remove)      # Python 3.10 / 3.11


def empty_dir(root: Path) -> dict:
    """Vide un dossier sans le supprimer lui-même (il peut être un volume Docker monté).
    Retourne {removed, freed_bytes}."""
    removed, freed = 0, 0
    for child in list(root.iterdir()) if root.is_dir() else []:
        if child.is_dir():
            freed += _dir_size(child)
            _rmtree(child)
        else:
            freed += child.stat().st_size
            child.unlink()
        removed += 1
    return {"removed": removed, "freed_bytes": freed}


def clear_cache() -> dict:
    """Supprime tous les clones. Retourne {removed, freed_bytes}."""
    return empty_dir(CACHE_DIR)


def resolve(target: str, *, refresh: bool = False, log=print) -> Path:
    """Retourne un dossier local à analyser, en clonant si `target` est une URL."""
    if not is_remote(target):
        return Path(target)

    if not shutil.which("git"):
        raise RuntimeError("`git` est introuvable : nécessaire pour cloner un dépôt distant.")

    dest = CACHE_DIR / _slug(target)
    if dest.exists() and refresh:
        shutil.rmtree(dest)
    if dest.exists():
        log(f"      dépôt déjà cloné dans {dest} (utilisez --refresh pour le recloner)")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    log(f"      clonage de {target} …")
    proc = subprocess.run(
        ["git", "clone", "--depth", "1", "--quiet", target, str(dest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Échec du clonage :\n{proc.stderr.strip()[-800:]}")
    return dest
