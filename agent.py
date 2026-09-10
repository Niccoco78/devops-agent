#!/usr/bin/env python3
"""Agent DevOps : analyse un dépôt et restitue architecture + risques.

Usage :
    python agent.py <dossier-ou-URL-git> [--backend anthropic|opencode] [--model ...] [--out dossier]
    python agent.py <dossier-ou-URL-git> --dry-run      # affiche le prompt, n'appelle pas le modèle
    python agent.py https://gitlab.com/groupe/projet --open   # clone, analyse, ouvre le rapport HTML

Le cycle : explorer (perception) -> prompt (raisonnement) -> llm (action) -> report (restitution).
Le même cycle est exposé en interface web par `python server.py`.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from devops_agent.explorer import explore
from devops_agent.pipeline import PipelineError, build_prompt_for, default_backend_name, run_analysis
from devops_agent.report import render_console
from devops_agent.source import resolve


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Agent IA DevOps : lit un dépôt, résume l'architecture, liste les risques.")
    p.add_argument("path", help="Dossier racine du projet à analyser, ou URL d'un dépôt Git (https://…, git@…)")
    p.add_argument("--backend", choices=["opencode", "anthropic", "openai", "gemini", "mistral", "compatible"], default=None,
                   help="Fournisseur d'IA (défaut : le premier configuré — réglages de l'interface ou variables "
                        "ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY / MISTRAL_API_KEY — sinon opencode)")
    p.add_argument("--model", default=None, help="Identifiant du modèle (ex. claude-opus-5, opencode/big-pickle)")
    p.add_argument("--out", default=None, help="Dossier de sortie (défaut : ./out/<nom-du-projet>)")
    p.add_argument("--depth", type=int, default=4, help="Profondeur max de l'arborescence et de la recherche de fichiers (défaut 4)")
    p.add_argument("--budget", type=int, default=80_000, help="Budget total de caractères envoyés au modèle (défaut 80000)")
    p.add_argument("--refresh", action="store_true", help="Recloner le dépôt distant même s'il est déjà en cache")
    p.add_argument("--open", action="store_true", help="Ouvrir le rapport HTML dans le navigateur à la fin")
    p.add_argument("--fix", action="store_true",
                   help="Étape 2 : après l'analyse, corriger les risques trouvés (branche Git + rapport de remédiation)")
    p.add_argument("--fix-target", choices=["k8s-gitlab", "k8s-github", "compose"], default="k8s-gitlab",
                   help="Cible de la remédiation (défaut : Kubernetes + GitLab CI)")
    p.add_argument("--dry-run", action="store_true", help="Construit et affiche le prompt sans appeler le modèle")
    p.add_argument("--quiet", action="store_true", help="N'affiche que le rapport final")
    return p.parse_args(argv)


STEP_LABEL = {"source": "0/4", "explore": "1/4", "prompt": "2/4", "llm": "3/4", "report": "4/4", "fix": "fix"}


def main(argv: list[str] | None = None) -> int:
    # Les consoles Windows ne sont pas toujours en UTF-8 ; on force pour les accents et les « • ».
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)

    def progress(step: str, msg: str) -> None:
        if not args.quiet:
            print(f"[{STEP_LABEL.get(step, ' ')}] {msg}", file=sys.stderr, flush=True)

    if args.dry_run:
        try:
            root = resolve(args.path, refresh=args.refresh, log=lambda m: progress("source", m.strip()))
            ctx = explore(root, max_depth=args.depth, total_budget=args.budget)
        except (RuntimeError, FileNotFoundError) as e:
            print(f"Erreur : {e}", file=sys.stderr)
            return 2
        system, user = build_prompt_for(ctx, default_backend_name(args.backend))
        print(system + "\n\n---\n\n" + user)
        return 0

    try:
        res = run_analysis(
            args.path, backend=args.backend, model=args.model, depth=args.depth, budget=args.budget,
            refresh=args.refresh, out_root=Path(args.out).parent if args.out else Path("out"), progress=progress,
        )
    except PipelineError as e:
        print(f"Erreur : {e}", file=sys.stderr)
        return 3

    print(render_console(res.data))
    html = res.files.get("analysis.html")
    if html:
        progress("report", f"rapport HTML : {html.resolve()}")
        if args.open:
            webbrowser.open(html.resolve().as_uri())
    if not res.data["risks"]:
        return 4

    if args.fix:
        from devops_agent.fixer import RemediationError, run_remediation
        print("\n" + "=" * 78 + "\n  ÉTAPE 2 — REMÉDIATION\n" + "=" * 78)
        try:
            fix = run_remediation(
                res.out_dir.name, out_root=res.out_dir.parent, target_kind=args.fix_target,
                backend=args.backend, model=args.model, progress=lambda s, m: progress("fix", f"{s} · {m}"),
            )
        except RemediationError as e:
            print(f"Erreur de remédiation : {e}", file=sys.stderr)
            return 5
        print((fix.out_dir / "REMEDIATION.md").read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
