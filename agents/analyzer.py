"""
Analyse de compatibilité entre le profil (profile.yml) et une offre, actu startup,
ou une entreprise ciblée. Utilise l'API Groq (Llama 3) pour noter la pertinence
et identifier le besoin primaire.
"""

import json
import logging
import os
from typing import Any

import yaml
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger("analyzer")

PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "profile.yml")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


def load_profile() -> dict[str, Any]:
    with open(PROFILE_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _groq_analyze(system_prompt: str, user_text: str) -> dict[str, Any]:
    if not GROQ_API_KEY:
        log.error("GROQ_API_KEY manquante — mets-la dans .env")
        return {"score": 0, "raison": "Clé API Groq manquante.", "besoin_primaire_identifie": ""}
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ],
        "temperature": 0.2,
        "max_tokens": 512,
    }
    import requests
    try:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
    except json.JSONDecodeError:
        log.warning("Réponse Groq non-JSON : %s", content)
        return {"score": 0, "raison": "Erreur de parsing de la réponse IA.", "besoin_primaire_identifie": ""}
    except Exception as exc:
        log.error("Erreur API Groq : %s", exc)
        return {"score": 0, "raison": f"Erreur API : {exc}", "besoin_primaire_identifie": ""}
    result.setdefault("score", 0)
    result.setdefault("raison", "")
    result.setdefault("besoin_primaire_identifie", "")
    return result


def build_system_prompt(profile: dict[str, Any]) -> str:
    return f"""Tu es un expert en recrutement et matching de profils tech. Tu reçois un profil candidat et une offre (ou actu startup). Tu dois évaluer objectivement la compatibilité.

## Profil candidat
- Niveau : {profile["target"]["academic_level"]}
- Postes visés : {", ".join(profile["target"]["roles"])}
- Compétences fortes : {", ".join(profile["skills"]["strong"])}
- Compétences intermédiaires : {", ".join(profile["skills"]["intermediate"])}
- En cours d'apprentissage : {", ".join(profile["skills"]["learning"])}
- Projets clés : {"; ".join(profile["key_projects_to_highlight"])}

## Règles d'exclusion (applique-les strictement)
1. Si l'offre exige un Master/Bac+5 pour commencer → score = 1/10
2. Si l'offre exige >2 ans d'expérience en entreprise → score = 2/10 max
3. Si la stack est 100% Java/C#/PHP sans lien Data/IA → score = 1/10

## Barème de score (0-10)
- 0-3 : inadéquat (exclusion déclenchée ou stack totalement hors cible)
- 4-5 : partiellement compatible (stack proche mais niveau ou expérience trop élevé)
- 6-7 : bon match (cherche un junior/alternant Python/Data, profil aligné)
- 8-10 : excellent match (recherche exactement ce que le candidat apporte, ou startup IA qui lève des fonds)

Réponds UNIQUEMENT avec un objet JSON valide (pas de markdown, pas de texte autour) :
{{
  "score": <int 0-10>,
  "raison": "<explication concise en français, 2-3 phrases max>",
  "besoin_primaire_identifie": "<le besoin principal que l'offre ou actu cherche à combler, ex: 'Développement RAG', 'Pipeline data', 'Automatisation LLM', 'Renfort équipe Data', 'Financement scale-up'>"
}}"""


def analyze(offer_text: str) -> dict[str, Any]:
    profile = load_profile()
    system_prompt = build_system_prompt(profile)
    return _groq_analyze(system_prompt, f"Voici l'offre ou actu à analyser :\n\n{offer_text}")


# ── Analyse entreprise (annuaire / incubateur) ────────────────────────────

def build_company_prompt(profile: dict[str, Any]) -> str:
    return f"""Tu es un analyste B2B impitoyable. Je te donne le nom, le secteur et la description d'une entreprise lyonnaise.

RÈGLE DE REJET ABSOLU : Si le texte fourni est un article de blog générique, un tutoriel, un fait divers (ex: moteurs défectueux) ou s'il ne permet pas d'identifier CLAIREMENT une véritable entreprise B2B ou B2C, le score DOIT être 1/10. Ne tente jamais d'inférer un besoin pour un article de presse générique.

INTERDICTION D'HALLUCINER : Si tu ne connais pas leurs concurrents ou leur philosophie, dis 'Non identifiable'. Ne les invente pas.

DÉDUCTION DE LA DOULEUR PRIMAIRE : Basé EXCLUSIVEMENT sur leur secteur d'activité (ex: Juridique, SaaS RH, Industrie), identifie leur goulot d'étranglement data/opérationnel le plus probable (ex: traitement documentaire lent, qualification de leads manuelle).

MATCH COMPÉTENCES : Mon profil sait faire [Python, RAG, CI/CD, Prompts]. Mes compétences peuvent-elles résoudre cette douleur précise à bas coût ?
Si OUI, retourne un score de 8/10 et le besoin. Si NON, retourne 2/10.

## Profil candidat
- Niveau : {profile["target"]["academic_level"]}
- Compétences fortes : {", ".join(profile["skills"]["strong"])}
- Projets clés : {"; ".join(profile["key_projects_to_highlight"])}

Réponds UNIQUEMENT avec un objet JSON valide (pas de markdown, pas de texte autour) :
{{
  "score": <8 ou 2>,
  "raison": "<explication concise en français, 2-3 phrases max>",
  "besoin_primaire_identifie": "<le besoin principal que l'entreprise pourrait avoir, ex: 'Automatisation traitement documentaire', 'Pipeline data qualification leads', 'Outil RAG interne'>",
  "douleur_identifiee": "<le goulot d'étranglement déduit du secteur>",
  "secteur_deduit": "<secteur d'activité déduit de la description>"
}}"""


def analyze_company(company: dict[str, Any]) -> dict[str, Any]:
    profile = load_profile()
    system_prompt = build_company_prompt(profile)
    text = (
        f"Nom : {company.get('company', '')}\n"
        f"Secteurs : {', '.join(s if isinstance(s, str) else json.dumps(s, ensure_ascii=False) for s in (company.get('sectors') or []))}\n"
        f"Taille : {company.get('size', '')} ({company.get('nb_employees', '?')} employés)\n"
        f"Description : {company.get('description', '')[:1500]}"
    )
    return _groq_analyze(system_prompt, text)


def run(offer_text: str) -> dict[str, Any]:
    return analyze(offer_text)


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read()
    if not text.strip():
        print("Usage : python analyzer.py \"<texte de l'offre>\"")
        sys.exit(1)
    result = run(text)
    print(json.dumps(result, indent=2, ensure_ascii=False))
