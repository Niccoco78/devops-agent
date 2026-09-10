"""Restitution : valider le JSON du modèle, l'afficher, l'exporter.

La validation est volontairement simple (présence des clés, types de base) : elle attrape
les réponses mal formées sans dépendre d'une bibliothèque de validation de schéma.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
SEVERITY_LABEL = {"critical": "CRITIQUE", "high": "ÉLEVÉ", "medium": "MOYEN", "low": "FAIBLE"}
CONFIDENCE_LABEL = {"low": "faible", "medium": "moyenne", "high": "élevée"}


class InvalidReport(ValueError):
    pass


def validate(data: dict, *, allow_empty_risks: bool = False) -> dict:
    """Vérifie la structure minimale, normalise les sévérités et trie les risques."""
    # Variante aplatie : {name, purpose, main_technologies, architecture, …} -> on reconstruit `project`.
    if "project" not in data and ("name" in data or "purpose" in data):
        data["project"] = {"name": data.pop("name", "?"), "purpose": data.pop("purpose", ""),
                           "main_technologies": data.pop("main_technologies", [])}
    data.setdefault("deployment", {})
    for key in ("project", "architecture", "deployment", "risks"):
        if key not in data:
            raise InvalidReport(f"Clé manquante dans la réponse du modèle : « {key} »")
    if not isinstance(data["risks"], list):
        data["risks"] = []
    if not data["risks"] and not allow_empty_risks:
        raise InvalidReport("La liste des risques est vide.")
    for r in data["risks"]:
        r["severity"] = str(r.get("severity", "medium")).lower()
        if r["severity"] not in SEVERITY_ORDER:
            r["severity"] = "medium"
        for k in ("title", "description", "evidence", "recommendation"):
            r.setdefault(k, "")
    data["risks"].sort(key=lambda r: SEVERITY_ORDER[r["severity"]])
    data.setdefault("open_questions", [])
    data.setdefault("confidence", "medium")
    return data


# ---------------------------------------------------------------------------
# Affichage console
# ---------------------------------------------------------------------------

def _rule(title: str, width: int = 78) -> str:
    return f"\n{'─' * 3} {title} {'─' * max(0, width - len(title) - 5)}"


def _wrap(text: str, indent: int = 4, width: int = 78) -> str:
    import textwrap
    return textwrap.fill(str(text), width=width, initial_indent=" " * indent, subsequent_indent=" " * indent)


def render_console(data: dict) -> str:
    p, a, d = data["project"], data["architecture"], data["deployment"]
    out: list[str] = []

    out.append("=" * 78)
    out.append(f"  {p.get('name', '?')}")
    out.append("=" * 78)
    out.append(_wrap(p.get("purpose", ""), indent=2))
    if p.get("main_technologies"):
        out.append(_wrap("Technologies : " + ", ".join(p["main_technologies"]), indent=2))
    out.append(f"  Confiance de l'analyse : {CONFIDENCE_LABEL.get(data['confidence'], data['confidence'])}")

    out.append(_rule("ARCHITECTURE"))
    out.append(_wrap("Style : " + a.get("style", ""), indent=2))
    out.append(_wrap("Communication : " + a.get("communication", ""), indent=2))
    if a.get("components"):
        out.append("\n  Composants")
        for c in a["components"]:
            out.append(f"  • {c.get('name')} — {c.get('technology')}")
            out.append(_wrap(c.get("role", ""), indent=6))
            out.append(_wrap("preuve : " + c.get("evidence", ""), indent=6))
    if a.get("data_stores"):
        out.append("\n  Données")
        for s in a["data_stores"]:
            used = ", ".join(s.get("used_by", [])) or "?"
            out.append(f"  • {s.get('name')} ({s.get('technology')}) — utilisé par : {used}")
            out.append(_wrap("preuve : " + s.get("evidence", ""), indent=6))
    if a.get("external_services"):
        out.append("\n  Services externes")
        for s in a["external_services"]:
            out.append(f"  • {s.get('name')} — {s.get('purpose')}")
            out.append(_wrap("preuve : " + s.get("evidence", ""), indent=6))

    out.append(_rule("EXÉCUTION ET DÉPLOIEMENT"))
    labels = [
        ("containerization", "Conteneurisation"), ("orchestration", "Orchestration"), ("ci_cd", "CI/CD"),
        ("infrastructure_as_code", "IaC"), ("cloud", "Cloud"), ("monitoring", "Monitoring"),
    ]
    for key, label in labels:
        out.append(_wrap(f"{label} : {d.get(key, 'non renseigné')}", indent=2))

    out.append(_rule(f"RISQUES ({len(data['risks'])})"))
    for i, r in enumerate(data["risks"], 1):
        out.append(f"\n  {i}. [{SEVERITY_LABEL[r['severity']]}] {r['title']}   ({r.get('category', '')})")
        out.append(_wrap(r["description"], indent=6))
        out.append(_wrap("preuve : " + r["evidence"], indent=6))
        out.append(_wrap("→ " + r["recommendation"], indent=6))

    if data.get("open_questions"):
        out.append(_rule("QUESTIONS À POSER À L'ÉQUIPE"))
        for i, q in enumerate(data["open_questions"], 1):
            out.append(_wrap(f"{i}. {q}", indent=2))

    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Export Markdown
# ---------------------------------------------------------------------------

def render_markdown(data: dict, *, model: str, source: str) -> str:
    p, a, d = data["project"], data["architecture"], data["deployment"]
    md: list[str] = []
    md.append(f"# {p.get('name', 'Analyse')}\n")
    md.append(f"_Analyse générée le {datetime.now():%Y-%m-%d %H:%M} par `{model}` sur `{source}` — "
              f"confiance {CONFIDENCE_LABEL.get(data['confidence'], data['confidence'])}._\n")
    md.append(p.get("purpose", "") + "\n")
    if p.get("main_technologies"):
        md.append("**Technologies :** " + ", ".join(p["main_technologies"]) + "\n")

    md.append("## Architecture\n")
    md.append(f"**Style :** {a.get('style', '')}\n\n**Communication :** {a.get('communication', '')}\n")
    if a.get("components"):
        md.append("| Composant | Rôle | Technologie | Preuve |\n|---|---|---|---|")
        for c in a["components"]:
            md.append(f"| {c.get('name')} | {c.get('role')} | {c.get('technology')} | `{c.get('evidence')}` |")
        md.append("")
    if a.get("data_stores"):
        md.append("| Données | Technologie | Utilisé par | Preuve |\n|---|---|---|---|")
        for s in a["data_stores"]:
            md.append(f"| {s.get('name')} | {s.get('technology')} | {', '.join(s.get('used_by', []))} | `{s.get('evidence')}` |")
        md.append("")
    if a.get("external_services"):
        md.append("| Service externe | Usage | Preuve |\n|---|---|---|")
        for s in a["external_services"]:
            md.append(f"| {s.get('name')} | {s.get('purpose')} | `{s.get('evidence')}` |")
        md.append("")

    md.append("## Exécution et déploiement\n")
    for key, label in [("containerization", "Conteneurisation"), ("orchestration", "Orchestration"), ("ci_cd", "CI/CD"),
                       ("infrastructure_as_code", "Infrastructure as Code"), ("cloud", "Cloud"), ("monitoring", "Monitoring")]:
        md.append(f"- **{label} :** {d.get(key, 'non renseigné')}")
    md.append("")

    md.append("## Risques\n")
    for i, r in enumerate(data["risks"], 1):
        md.append(f"### {i}. {r['title']} — {SEVERITY_LABEL[r['severity']]} ({r.get('category', '')})\n")
        md.append(r["description"] + "\n")
        md.append(f"- **Preuve :** `{r['evidence']}`\n- **Recommandation :** {r['recommendation']}\n")

    if data.get("open_questions"):
        md.append("## Questions à poser à l'équipe\n")
        for q in data["open_questions"]:
            md.append(f"1. {q}")
        md.append("")
    return "\n".join(md)


# ---------------------------------------------------------------------------
# Export HTML — un fichier autonome, lisible dans n'importe quel navigateur
# ---------------------------------------------------------------------------

_HTML_CSS = """
:root{--ground:#f3f5f7;--surface:#fff;--ink:#15212c;--muted:#5d6c7a;--line:#d9e0e6;--accent:#0f6e8c;--accent-soft:#e2f0f5;
--crit:#b3261e;--crit-soft:#fbe9e7;--high:#b9500a;--high-soft:#fdeee2;--med:#8a5a00;--med-soft:#fff3d6;--low:#2b6f48;--low-soft:#e3f2e8}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--ink);font:15px/1.6 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:40px 24px 80px}
h1,h2,h3{font-family:"IBM Plex Sans Condensed","Arial Narrow",sans-serif;line-height:1.15;margin:0}
h1{font-size:40px;font-weight:700}h2{font-size:24px;font-weight:600;margin-top:48px;padding-top:16px;border-top:2px solid var(--ink)}
h3{font-size:17px;font-weight:600}
.lede{font-size:17px;color:var(--muted);max-width:70ch;margin:10px 0 0}
.meta{margin-top:14px;font-size:12.5px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;display:flex;flex-wrap:wrap;gap:6px 20px}
.meta b{color:var(--ink);font-weight:500;text-transform:none;letter-spacing:0;font-size:13px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:16px}.chip{background:var(--surface);border:1px solid var(--line);border-radius:999px;padding:2px 11px;font-size:13px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-top:24px}
.tiles div{background:var(--surface);padding:12px 14px}.tiles .k{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.tiles .v{font-family:"IBM Plex Sans Condensed",sans-serif;font-size:26px;font-weight:600;font-variant-numeric:tabular-nums}
.tiles .v.crit{color:var(--crit)}.tiles .v.high{color:var(--high)}.tiles .v.med{color:var(--med)}.tiles .v.low{color:var(--low)}
p{margin:10px 0;max-width:72ch}code,.f{font-family:"IBM Plex Mono",ui-monospace,Consolas,monospace;font-size:.88em}
.f{background:var(--accent-soft);color:var(--accent);padding:1px 7px;border-radius:4px;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%;font-size:14px;margin-top:12px}th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:500}.scroll{overflow-x:auto}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;margin:12px 0;font-size:14.5px}dt{color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.08em;padding-top:4px}dd{margin:0;max-width:72ch}
.risk{background:var(--surface);border:1px solid var(--line);border-left:6px solid var(--med);border-radius:8px;padding:14px 18px;margin-top:14px}
.risk.critical{border-left-color:var(--crit)}.risk.high{border-left-color:var(--high)}.risk.low{border-left-color:var(--low)}
.risk h3{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.risk p{margin:8px 0}
.tag{display:inline-block;font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;padding:2px 8px;border-radius:999px}
.tag.critical{background:var(--crit-soft);color:var(--crit)}.tag.high{background:var(--high-soft);color:var(--high)}.tag.medium{background:var(--med-soft);color:var(--med)}.tag.low{background:var(--low-soft);color:var(--low)}
.cat{font-size:12px;color:var(--muted);font-family:"IBM Plex Mono",monospace}
.ev{font-size:13px;color:var(--muted)}.fix{font-size:14px;border-top:1px dashed var(--line);padding-top:8px;margin-top:8px}
ol.q{padding-left:22px}ol.q li{margin:8px 0;max-width:72ch}
.warn{background:var(--med-soft);border-left:4px solid var(--med);padding:10px 14px;border-radius:0 6px 6px 0;margin-top:20px;font-size:14px}
footer{margin-top:56px;padding-top:14px;border-top:1px solid var(--line);font-size:12.5px;color:var(--muted)}
"""

SEVERITY_FR = {"critical": "Critique", "high": "Élevé", "medium": "Moyen", "low": "Faible"}


def render_html(data: dict, *, model: str, source: str) -> str:
    from html import escape as e

    p, a, d = data["project"], data["architecture"], data["deployment"]
    risks = data["risks"]
    counts = {s: sum(1 for r in risks if r["severity"] == s) for s in SEVERITY_ORDER}
    h: list[str] = []
    h.append("<!doctype html><html lang='fr'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>")
    h.append(f"<title>{e(p.get('name', 'Analyse'))} — analyse DevOps</title>")
    h.append("<link rel='stylesheet' href='https://fonts.googleapis.com/css2?family=IBM+Plex+Sans+Condensed:wght@600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono&display=swap'>")
    h.append(f"<style>{_HTML_CSS}</style></head><body><div class='wrap'>")

    # En-tête
    h.append(f"<h1>{e(p.get('name', 'Analyse'))}</h1>")
    h.append(f"<p class='lede'>{e(p.get('purpose', ''))}</p>")
    h.append("<div class='meta'>"
             f"<span>Source <b>{e(source)}</b></span>"
             f"<span>Modèle <b>{e(model)}</b></span>"
             f"<span>Généré le <b>{datetime.now():%d/%m/%Y %H:%M}</b></span>"
             f"<span>Confiance <b>{e(CONFIDENCE_LABEL.get(data['confidence'], data['confidence']))}</b></span></div>")
    if p.get("main_technologies"):
        h.append("<div class='chips'>" + "".join(f"<span class='chip'>{e(t)}</span>" for t in p["main_technologies"]) + "</div>")
    h.append("<div class='tiles'>"
             f"<div><div class='k'>Composants</div><div class='v'>{len(a.get('components', []))}</div></div>"
             f"<div><div class='k'>Risques</div><div class='v'>{len(risks)}</div></div>"
             f"<div><div class='k'>Critiques</div><div class='v crit'>{counts['critical']}</div></div>"
             f"<div><div class='k'>Élevés</div><div class='v high'>{counts['high']}</div></div>"
             f"<div><div class='k'>Moyens</div><div class='v med'>{counts['medium']}</div></div>"
             f"<div><div class='k'>Faibles</div><div class='v low'>{counts['low']}</div></div></div>")
    if not risks:
        h.append("<div class='warn'>Le modèle n'a renvoyé aucun risque. Relancez l'analyse ou essayez un autre modèle.</div>")

    # Architecture
    h.append("<h2>Architecture</h2>")
    h.append(f"<dl><dt>Style</dt><dd>{e(a.get('style', ''))}</dd><dt>Communication</dt><dd>{e(a.get('communication', ''))}</dd></dl>")
    if a.get("components"):
        h.append("<h3>Composants</h3><div class='scroll'><table><tr><th>Composant</th><th>Rôle</th><th>Technologie</th><th>Preuve</th></tr>")
        for c in a["components"]:
            h.append(f"<tr><td><b>{e(c.get('name', ''))}</b></td><td>{e(c.get('role', ''))}</td><td>{e(c.get('technology', ''))}</td><td><span class='f'>{e(c.get('evidence', ''))}</span></td></tr>")
        h.append("</table></div>")
    if a.get("data_stores"):
        h.append("<h3>Données</h3><div class='scroll'><table><tr><th>Stockage</th><th>Technologie</th><th>Utilisé par</th><th>Preuve</th></tr>")
        for s in a["data_stores"]:
            h.append(f"<tr><td><b>{e(s.get('name', ''))}</b></td><td>{e(s.get('technology', ''))}</td><td>{e(', '.join(s.get('used_by', [])))}</td><td><span class='f'>{e(s.get('evidence', ''))}</span></td></tr>")
        h.append("</table></div>")
    if a.get("external_services"):
        h.append("<h3>Services externes</h3><div class='scroll'><table><tr><th>Service</th><th>Usage</th><th>Preuve</th></tr>")
        for s in a["external_services"]:
            h.append(f"<tr><td><b>{e(s.get('name', ''))}</b></td><td>{e(s.get('purpose', ''))}</td><td><span class='f'>{e(s.get('evidence', ''))}</span></td></tr>")
        h.append("</table></div>")

    # Déploiement
    h.append("<h2>Exécution et déploiement</h2><dl>")
    for key, label in [("containerization", "Conteneurisation"), ("orchestration", "Orchestration"), ("ci_cd", "CI/CD"),
                       ("infrastructure_as_code", "IaC"), ("cloud", "Cloud"), ("monitoring", "Monitoring")]:
        h.append(f"<dt>{label}</dt><dd>{e(d.get(key, 'non renseigné'))}</dd>")
    h.append("</dl>")

    # Risques
    h.append(f"<h2>Risques ({len(risks)})</h2>")
    for i, r in enumerate(risks, 1):
        sev = r["severity"]
        h.append(f"<div class='risk {sev}'><h3><span class='tag {sev}'>{SEVERITY_FR[sev]}</span>{i}. {e(r['title'])} <span class='cat'>{e(r.get('category', ''))}</span></h3>")
        h.append(f"<p>{e(r['description'])}</p><p class='ev'>Preuve : <span class='f'>{e(r['evidence'])}</span></p>")
        h.append(f"<p class='fix'>→ {e(r['recommendation'])}</p></div>")

    # Questions
    if data.get("open_questions"):
        h.append("<h2>Questions à poser à l'équipe</h2><ol class='q'>")
        h.extend(f"<li>{e(q)}</li>" for q in data["open_questions"])
        h.append("</ol>")

    h.append("<footer>Rapport généré par un agent IA à partir du README, de l'arborescence et des fichiers de configuration du dépôt. "
             "Une réponse de l'IA n'est pas une preuve : chaque affirmation cite un fichier, à vous de l'ouvrir.</footer>")
    h.append("</div></body></html>")
    return "\n".join(h)


def save(data: dict, out_dir: Path, *, model: str, source: str, prompt: str | None = None) -> list[Path]:
    """Écrit analysis.json, analysis.md, analysis.html et, si fourni, prompt.txt. Retourne les chemins créés."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    (out_dir / "analysis.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    written.append(out_dir / "analysis.json")
    (out_dir / "analysis.md").write_text(render_markdown(data, model=model, source=source), encoding="utf-8")
    written.append(out_dir / "analysis.md")
    (out_dir / "analysis.html").write_text(render_html(data, model=model, source=source), encoding="utf-8")
    written.append(out_dir / "analysis.html")
    if prompt is not None:
        (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        written.append(out_dir / "prompt.txt")
    return written
