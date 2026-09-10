"""Le cycle complet de l'agent, réutilisable par la ligne de commande et par le serveur web.

    run_analysis(cible) -> AnalysisResult

`progress` reçoit des événements (étape, message) pour afficher l'avancement.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .explorer import RepoContext, explore
from .llm import LLMError, make_backend
from .prompt import OUTPUT_SCHEMA, SYSTEM_PROMPT, build_user_prompt
from .report import InvalidReport, save, validate
from .source import resolve

ProgressCb = Callable[[str, str], None]   # (étape, message)

STEPS = ("source", "explore", "prompt", "llm", "report")

REMINDER = (
    "\n\nRAPPEL IMPORTANT : ta réponse précédente était incomplète ou mal formée. Réponds avec UN SEUL objet JSON "
    "complet et fermé, descriptions courtes (2 phrases maximum), toutes les clés présentes, et AU MOINS TROIS "
    "risques concrets dans `risks`, chacun avec sa preuve."
)


class PipelineError(RuntimeError):
    pass


@dataclass
class AnalysisResult:
    data: dict
    out_dir: Path
    files: dict[str, Path]
    model: str
    backend: str
    source: str
    root: Path
    elapsed_llm: float
    attempts: int
    truncated: bool
    context: dict = field(default_factory=dict)


def default_backend_name(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    from .providers import default_provider
    return default_provider()


def build_prompt_for(ctx: RepoContext, backend_name: str) -> tuple[str, str]:
    """(system, user) — Anthropic impose le schéma via l'API ; opencode le lit dans le prompt."""
    user = build_user_prompt(ctx, include_schema=(backend_name != "anthropic"))
    return SYSTEM_PROMPT, user


def run_analysis(
    target: str,
    *,
    backend: str | None = None,
    model: str | None = None,
    depth: int = 4,
    budget: int = 80_000,
    refresh: bool = False,
    out_root: Path = Path("out"),
    progress: ProgressCb | None = None,
    snapshot: Callable[[dict], None] | None = None,
) -> AnalysisResult:
    def say(step: str, msg: str) -> None:
        if progress:
            progress(step, msg)

    # 0. Cible ---------------------------------------------------------------
    try:
        root = resolve(target, refresh=refresh, log=lambda m: say("source", m.strip()))
    except RuntimeError as e:
        raise PipelineError(str(e)) from e
    say("source", f"cible : {root.resolve()}")

    # 1. Perception ----------------------------------------------------------
    try:
        ctx = explore(root, max_depth=depth, total_budget=budget)
    except FileNotFoundError as e:
        raise PipelineError(str(e)) from e
    stats = {
        "readme": ctx.readme_path or "absent",
        "files": ctx.file_count,
        "key_files": len(ctx.key_files),
        "skipped": len(ctx.skipped_key_files),
        "chars": ctx.total_chars,
    }
    say("explore", f"README : {stats['readme']} · fichiers : {stats['files']} · configs retenues : {stats['key_files']} "
                   f"· ignorées (budget) : {stats['skipped']} · contexte : {stats['chars']:,} caractères")
    live = {"stage": "explore", "stats": stats, "files": [f.path for f in ctx.key_files], "readme": ctx.readme_path}
    if snapshot:
        snapshot(live)

    # 2. Raisonnement --------------------------------------------------------
    backend_name = default_backend_name(backend)
    system, user = build_prompt_for(ctx, backend_name)
    full_prompt = system + "\n\n---\n\n" + user
    say("prompt", f"prompt construit : {len(full_prompt):,} caractères (backend {backend_name})")

    # 3. Action --------------------------------------------------------------
    try:
        llm = make_backend(backend_name, model, OUTPUT_SCHEMA)
    except LLMError as e:
        raise PipelineError(str(e)) from e
    out_dir = out_root / ctx.name
    say("llm", f"appel du modèle {llm.model} …")
    if snapshot:
        snapshot({**live, "stage": "llm", "model": llm.model, "prompt_chars": len(full_prompt)})

    data = None
    result = None
    truncated = False
    prompt_to_send = user
    t_llm = 0.0
    attempts = 0
    for attempt in (1, 2):
        attempts = attempt
        t0 = time.time()
        try:
            result = llm.complete(system, prompt_to_send)
        except LLMError as e:
            if e.raw:
                out_dir.mkdir(parents=True, exist_ok=True)
                (out_dir / f"raw_response_{attempt}.txt").write_text(e.raw, encoding="utf-8")
            if e.raw and attempt == 1:
                say("llm", f"réponse inexploitable ({str(e).splitlines()[0]}) — nouvelle tentative avec rappel")
                prompt_to_send = user + REMINDER
                continue
            raise PipelineError(str(e)) from e
        t_llm += time.time() - t0
        tokens = ""
        if result.input_tokens is not None:
            tokens = f" · tokens entrée {result.input_tokens:,} / sortie {result.output_tokens or 0:,}"
        say("llm", f"réponse en {time.time() - t0:.0f}s{tokens}")
        if result.data.pop("_truncated", False):
            truncated = True
            say("llm", "sortie coupée par le modèle : JSON réparé, les derniers éléments peuvent manquer")
        try:
            data = validate(result.data)
            break
        except InvalidReport as e:
            if attempt == 1:
                say("llm", f"réponse incomplète ({e}) — nouvelle tentative avec rappel")
                prompt_to_send = user + REMINDER
            else:
                say("llm", f"toujours incomplète ({e}) — rapport partiel")
                data = validate(result.data, allow_empty_risks=True)
    assert data is not None and result is not None

    # 4. Restitution ---------------------------------------------------------
    if snapshot:
        a = data.get("architecture", {})
        snapshot({**live, "stage": "report", "model": result.model,
                  "components": [c.get("name") for c in a.get("components", [])],
                  "data_stores": [s.get("name") for s in a.get("data_stores", [])],
                  "risks": [{"title": r.get("title"), "severity": r.get("severity")} for r in data.get("risks", [])]})
    written = save(data, out_dir, model=result.model, source=target, prompt=full_prompt)
    files = {p.name: p for p in written}
    # meta.json : ce qu'il faut pour revenir sur cette analyse plus tard (étape 2 : remédiation).
    (out_dir / "meta.json").write_text(json.dumps({
        "target": target, "root": str(root.resolve()), "model": result.model, "backend": backend_name,
        "date": datetime.now().isoformat(timespec="seconds"), "context": stats,
        "key_files": [f.path for f in ctx.key_files],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    say("report", f"rapport écrit dans {out_dir}")
    return AnalysisResult(
        data=data, out_dir=out_dir, files=files, model=result.model, backend=backend_name,
        source=target, root=root, elapsed_llm=t_llm, attempts=attempts, truncated=truncated, context=stats,
    )
