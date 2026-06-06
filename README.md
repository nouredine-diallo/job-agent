# JobAgent — Pipeline autonome de candidature alternance

Pipelines automatisés qui scrapent, filtrent, analysent et rédigent des candidatures pour des offres d'alternance Data/IA sur Lyon.

## Problème

Postuler à une alternance demande de :
- Surveiller 5+ plateformes (WTTJ, JobTeaser, RSS, HN, LinkedIn)
- Filtrer manuellement les offres pertinentes
- Trouver le bon contact RH/CTO
- Rédiger un message personnalisé pour chaque cible

**JobAgent remplace 80% de ce travail manuel par des agents autonomes.**

## Stack

`Python 3.14` · `Groq (Llama 3.1 8B + 70B)` · `Serper.dev` · `Google Sheets` · `Telegram API` · `Algolia (WTTJ)` · `DNS`

## Architecture (4 agents)

```
Scraper → Analyzer → Finder → Emailer → Telegram
   ↑                        ↑
   scraping (WTTJ,          recherche LinkedIn
   JobTeaser, RSS, HN)      + validation email
```

## Installation

```bash
git clone https://github.com/ton-compte/job-agent
cd job-agent
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env   # remplir les clés
python main.py --audit # mode test sans envoi
```

### Clés nécessaires

| Variable | Usage | Gratuit ? |
|---|---|---|
| `GROQ_API_KEY` | Scoring + rédaction (Llama) | Oui (≈ 30k req/j) |
| `SERPER_API_KEY` | Recherche LinkedIn X-Ray | 100 req/mois gratuits |
| `TELEGRAM_BOT_TOKEN` | Réception des drafts | Oui |
| `GSPREAD_CREDENTIALS` | Mémoire des offres traitées | Oui (Google Sheet) |

## Ce que j'ai construit (vs généré)

**Architecture agentique modulaire.** Chaque agent (`scraper.py`, `analyzer.py`, `finder.py`, `emailer.py`) est indépendant, testable seul, avec une interface `run()` standard.

**Stratégie de scoring en 3 états :** < 7 → skip, 7 → offre standard, ≥ 8 → draft personnalisé, avec 3 règles d'exclusion (Bac+5, expérience > 2 ans, stack hors cible) et une règle de rejet absolu pour les articles blog.

**Preuve par l'audit :** Un mode `--audit` exporte un JSON complet de chaque décision. Testé sur 10 items réels (offres WTTJ, signaux RSS/HN, cold d'annuaire). Résultat : 3 drafts générés, 7 skips — dont un faux positif identifié et corrigé (article HN "Accelerators" scoré 8/10 alors que ce n'est pas une entreprise).

**Bugs réels corrigés :**
- `sectors` pouvait contenir des dictionnaires au lieu de strings (crash Algolia) → `isinstance` guard
- Emojis et caractères Unicode plantaient sur terminal Windows → migration ASCII
- Requête Serper confondait "Lyon" (ville) avec "Lyons" (nom) → ajout du filtre `"France"` dans la query X-Ray

## Utilisation

```bash
python main.py          # mode production (envoie les drafts sur Telegram)
python main.py --audit  # mode test (exporte un rapport JSON, pas d'envoi)
```

Le rapport d'audit est envoyé sur Telegram et sauvegardé en `audit_log_*.json`.

## Résultats d'un audit réel (10 items)

| Entreprise | Type | Score | Décision |
|---|---|---|---|
| Framatome (×5) | Offres | 1-6 | SKIP |
| Artelia | Offre | 1 | SKIP |
| **Groupe SII** | **Offre** | **8/10** | **DRAFT ✓** |
| PureTech | Signal RSS | 1 | SKIP |
| Accelerators* | Signal HN | 8 → corrigé | DRAFT (faux positif) |
| **Lya Protect** | **Cold** | **8/10** | **DRAFT ✓** |

\* Faux positif détecté pendant l'audit, corrigé par l'ajout d'un pré-filtre regex et d'une règle de rejet absolu.

## Limites (assumées)

- **Le scoring est 100% LLM** — pas de métriques de conversion réelles, pas de boucle de rétroaction
- **Les douleurs B2B (cold) sont inférées** par le LLM depuis le secteur d'activité, sans validation terrain
- **Projet unique** — le prompt cite toujours l'Agent RAG car c'est le projet le + parlant parmi les 3 listés
- **Pas de SMTP réel** pour l'instant (les drafts partent sur Telegram uniquement)

## Liens

- [Rapport d'audit complet](audit_log_20260605_114308.json) — 10 items, décisions, drafts
- [Profil candidat](data/profile.yml) — compétences, projets, règles d'exclusion

---

*Construit avec Groq, Serper et Python. Pas de boîte noire — chaque décision est tracée dans le JSON d'audit.*
