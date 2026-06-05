"""
Détermine le bon interlocuteur pour une candidature alternance via recherche X-Ray
Serper.dev. N'hallucine aucun nom — 'Équipe Tech' par défaut.
"""

import json
import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import requests
import yaml
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
log = logging.getLogger("finder")

SERPER_API_KEY = os.getenv("SERPER_API_KEY")
SERPER_URL = "https://google.serper.dev/search"
SERPER_DAILY_LIMIT = 70
_serper_calls_today = 0
GROQ_API_KEY = os.getenv("GROQ_API_KEY")


def load_profile() -> dict[str, Any]:
    path = os.path.join(os.path.dirname(__file__), "..", "data", "profile.yml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ── Serper.dev helpers ─────────────────────────────────────────────────────

def _serper_search(query: str, num: int = 5) -> dict | None:
    global _serper_calls_today
    if not SERPER_API_KEY:
        log.warning("SERPER_API_KEY manquante — recherche X-Ray impossible")
        return None
    if _serper_calls_today >= SERPER_DAILY_LIMIT:
        log.warning("Limite Serper quotidienne atteinte (%d)", SERPER_DAILY_LIMIT)
        return None
    try:
        r = requests.post(
            SERPER_URL,
            json={"q": query, "num": num},
            headers={"X-API-KEY": SERPER_API_KEY, "Content-Type": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        _serper_calls_today += 1
        return r.json()
    except Exception as exc:
        log.warning("Erreur Serper: %s", exc)
        return None


def _extract_name_from_title(title: str) -> tuple[str, str] | None:
    parts = title.split(" - ")
    if not parts:
        return None
    name_part = parts[0].strip()
    space = name_part.find(" ")
    if space == -1:
        return None
    return (name_part[:space], name_part[space + 1:])


def _validate_person(company_name: str, snippet: str, full_name: str) -> bool:
    if not GROQ_API_KEY:
        return True
    system = "Tu valides si une personne travaille dans une entreprise donnée. Réponds UNIQUEMENT par 'OUI' ou 'NON'."
    user = f"Personne : {full_name}\nEntreprise : {company_name}\nExtrait Google : {snippet}\n\n{full_name} travaille-t-il/elle chez {company_name} ?"
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.1-8b-instant",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0,
                "max_tokens": 10,
            },
            timeout=15,
        )
        r.raise_for_status()
        answer = r.json()["choices"][0]["message"]["content"].strip().upper()
        return answer.startswith("OUI")
    except Exception:
        return True


def _xray_find_person(company_name: str, location: str, search_roles: list[str]) -> dict | None:
    role_query = " OR ".join(f'"{r}"' for r in search_roles)
    query = f'site:linkedin.com/in/ "{company_name}" ("{location}" OR "Rhône-Alpes") "France" ({role_query})'
    data = _serper_search(query)
    if not data:
        return None
    for result in data.get("organic", []):
        title = result.get("title", "")
        link = result.get("link", "")
        snippet = result.get("snippet", "")
        name = _extract_name_from_title(title)
        if not name:
            continue
        if not _validate_person(company_name, snippet, f"{name[0]} {name[1]}"):
            continue
        return {"first_name": name[0], "last_name": name[1], "full_name": f"{name[0]} {name[1]}", "linkedin_url": link, "snippet": snippet}
    return None


def _find_company_domain(company_name: str) -> str | None:
    data = _serper_search(f"{company_name} site web", num=3)
    if not data:
        return None
    for result in data.get("organic", []):
        link = result.get("link", "")
        parsed = urlparse(link)
        domain = (parsed.netloc or parsed.path).lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain and "google" not in domain and "linkedin" not in domain:
            return domain
    return None


def _search_email(first_name: str, last_name: str, domain: str) -> str | None:
    queries = [
        f'"{first_name} {last_name}" email',
        f'"{first_name} {last_name}" contact',
        f'"{first_name} {last_name}" @{domain}',
    ]
    email_pattern = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
    for query in queries:
        data = _serper_search(query, num=3)
        if not data:
            continue
        for result in data.get("organic", []):
            snippet = result.get("snippet", "")
            link = result.get("link", "")
            for text in [snippet, link]:
                found = email_pattern.findall(text)
                for e in found:
                    if domain and domain in e:
                        return e
    return None


def _build_email(first_name: str, last_name: str, domain: str) -> str:
    import unicodedata
    def strip_accents(s: str) -> str:
        return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    fn = strip_accents(first_name.lower())
    ln = strip_accents(last_name.lower())
    return f"{fn}.{ln}@{domain}"


def search_company_news(company_name: str) -> str:
    """Cherche une actualité récente sur l'entreprise via Serper (1 requête)."""
    data = _serper_search(f'"{company_name}" actualité 2025', num=3)
    if not data:
        return ""
    snippets = [r.get("snippet", "") for r in data.get("organic", []) if r.get("snippet")]
    return " ".join(snippets[:2])


def _verify_mx(domain: str) -> bool:
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX")
        return len(answers) > 0
    except Exception:
        return False


# ── Contact resolution ────────────────────────────────────────────────────

def find_contact(company_name: str, company_size: int | None = None, company_domain: str | None = None) -> dict[str, Any]:
    profile = load_profile()
    is_startup = company_size is not None and company_size < 50
    location = "Lyon"

    contact_name = "Équipe Tech"
    emails: list[str] = []
    linkedin_url = ""
    person_found = False

    search_roles = ["CTO", "Lead", "Senior", "Tech"] if is_startup else ["Talent Acquisition", "RH", "HR", "Recruitment"]

    person = _xray_find_person(company_name, location, search_roles)

    if person:
        contact_name = person["full_name"]
        linkedin_url = person["linkedin_url"]
        person_found = True

        domain = company_domain or _find_company_domain(company_name)
        if domain:
            found_email = _search_email(person["first_name"], person["last_name"], domain)
            if found_email:
                emails = [found_email]
            else:
                emails = [_build_email(person["first_name"], person["last_name"], domain)]

            if emails and not _verify_mx(domain):
                log.info("Aucun MX pour %s — emails ignorés", domain)
                emails = []

    linkedin_search_url = linkedin_url or (
        f"https://www.linkedin.com/search/results/people/?keywords={'CTO' if is_startup else 'Talent%20Acquisition'}%20{company_name.replace(' ', '%20')}"
    )

    if is_startup:
        role = "CTO"
        fallback_roles = ["Lead Tech", "Directeur Technique", "Tech Lead"]
        justification = (
            f"{company_name} a < 50 employés → cibler le CTO/Lead Tech. "
            f"Ils comprennent directement les enjeux techniques (RAG, CI/CD, LLM) "
            f"et décident souvent seuls du recrutement en alternance."
        )
        pitch = (
            f"Bonjour,\n\n"
            f"Je suis étudiant en L2 Informatique et je cherche une alternance "
            f"dans le domaine Data/IA. J'ai déjà déployé un agent RAG en production "
            f"sur HuggingFace avec une pipeline CI/CD complète, et un wrapper LLM "
            f"qui génère des scripts viraux.\n\n"
            f"Je pense pouvoir apporter une vraie valeur ajoutée à {company_name} "
            f"dès le début de l'alternance. Auriez-vous 10 minutes pour en discuter ?\n\n"
            f"Merci d'avance !"
        )
    else:
        role = "Talent Acquisition Manager"
        fallback_roles = ["RH Alternance", "HR Manager", "Recruitment Lead"]
        justification = (
            f"{company_name} a ≥ 50 employés ou taille inconnue → cibler le service RH / Talent Acquisition. "
            f"Ce sont eux qui filtrent les candidatures alternance avant de les transmettre aux équipes techniques."
        )
        pitch = (
            f"Bonjour,\n\n"
            f"Je suis étudiant en L2 Informatique à la recherche d'une alternance "
            f"dans le domaine Data/IA. J'ai une expérience concrète en Python, LangChain, "
            f"RAG et CI/CD, avec des projets déployés et utilisés.\n\n"
            f"Mon profil correspond aux offres alternance de {company_name}. "
            f"Pourriez-vous me dire si des postes sont ouverts, ou vers qui je peux me tourner ?\n\n"
            f"Merci pour votre retour !"
        )

    return {
        "company": company_name,
        "size_estimate": company_size,
        "contact_role": role,
        "fallback_roles": fallback_roles,
        "contact_name": contact_name,
        "emails": emails,
        "linkedin": linkedin_search_url,
        "linkedin_search_url": linkedin_search_url,
        "justification": justification,
        "pitch": pitch,
        "person_found": person_found,
        "candidate_profile": {
            "academic_level": profile["target"]["academic_level"],
            "strong_skills": profile["skills"]["strong"],
            "projects": profile["key_projects_to_highlight"],
        },
    }


def run(company_name: str, company_size: int | None = None, company_domain: str | None = None) -> dict[str, Any]:
    return find_contact(company_name, company_size, company_domain)


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args:
        name = input("Nom de l'entreprise : ").strip()
    else:
        name = args[0]
    size = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    result = run(name, size)
    print(json.dumps(result, indent=2, ensure_ascii=False))
