"""Agent DevOps : lit un dépôt, interroge un LLM, restitue architecture et risques.

Le cycle de l'agent tient en trois étapes, chacune dans son module :

    explorer.py  ->  perception : README + arborescence + fichiers de config
    prompt.py    ->  raisonnement : le prompt "ingénieur DevOps senior" + le schéma JSON attendu
    llm.py       ->  action : appel du modèle (Anthropic SDK ou opencode)
    report.py    ->  restitution : affichage structuré + export JSON / Markdown
"""

__version__ = "1.0.0"
